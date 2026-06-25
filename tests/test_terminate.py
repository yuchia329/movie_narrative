"""Terminate / regenerate for an IN-FLIGHT back-half run.

Two user actions on a running (or stuck) run, both pinned here:
  - POST /api/runs/{id}/terminate        -> revoke the chain, mark the run errored ("Stopped by
                                            user"). 409 if the run isn't active, 404 if not theirs.
  - POST /api/runs  {force:true} while running -> stop the in-flight chain first, then re-queue +
                                            clear artifacts + start a fresh chain (regenerate).

Same harness as test_resume_restart: an in-memory SQLite sessionmaker injected into yapper_web.db,
the session-cookie dependency overridden, and the Celery launchers / revoke stubbed so nothing
touches S3/Redis/GPU.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import yapper_web.db as dbmod  # noqa: E402
import yapper_web.tasks as tasks  # noqa: E402
from yapper_web import api  # noqa: E402
from yapper_web.db import (  # noqa: E402
    Base,
    Movie,
    Run,
    RunStage,
    RunStatus,
    Session_,
    _now,
)

SID = "test-session"


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dbmod, "_engine", engine, raising=False)
    monkeypatch.setattr(
        dbmod, "_SessionLocal",
        sessionmaker(bind=engine, expire_on_commit=False, future=True), raising=False,
    )
    # stub the launchers + artifact wipe + revoke so the endpoints touch no infra
    calls: dict[str, list] = {"back": [], "clear": [], "revoke": []}
    monkeypatch.setattr(tasks, "start_back_half", lambda rid: calls["back"].append(rid))
    monkeypatch.setattr(tasks, "clear_back_half_artifacts", lambda *a: calls["clear"].append(a))
    monkeypatch.setattr(tasks, "revoke_run", lambda rid: calls["revoke"].append(rid))
    api.app.dependency_overrides[api.get_session] = lambda: SID
    c = TestClient(api.app)
    c.calls = calls  # type: ignore[attr-defined]
    yield c
    api.app.dependency_overrides.clear()


def _seed_movie(mid: str, *, status=None):
    """Add a ready movie. Returns nothing; runs are added separately."""
    m = Movie(
        id=mid, session_id=SID, original_filename=f"{mid}.mp4", slug=mid,
        s3_prefix=f"{SID}/{mid}/{mid}", source_key=f"sources/{SID}/{mid}/{mid}.mp4",
        status=status or dbmod.MovieStatus.ready, default_lang="zh",
    )
    return m


def _seed(*, run_status=RunStatus.running, fillers=0, running_stage_age=None):
    """Seed a ready movie m1 with one run r1 (run_status), plus `fillers` extra running runs on
    their own movies (to exercise the per-session concurrency cap). When `running_stage_age` (sec)
    is given, r1 also gets a completed 'script' stage and an in-flight 'tts' stage that started
    that many seconds ago (so terminate has a live stage to close out)."""
    with dbmod.session_scope() as db:
        db.add(Session_(id=SID))
        db.add(_seed_movie("m1"))
        db.add(Run(id="r1", movie_id="m1", session_id=SID, lang="zh", status=run_status))
        if running_stage_age is not None:
            db.add(RunStage(run_id="r1", stage="script", status="ran", seconds=54.0))
            db.add(RunStage(run_id="r1", stage="tts", status="running", seconds=0.0,
                            recorded_at=_now() - timedelta(seconds=running_stage_age)))
        for i in range(fillers):
            db.add(_seed_movie(f"f{i}"))
            db.add(Run(id=f"fr{i}", movie_id=f"f{i}", session_id=SID, lang="zh",
                       status=RunStatus.running))


def test_terminate_running_run_revokes_and_marks_stopped(client):
    _seed(run_status=RunStatus.running)
    r = client.post("/api/runs/r1/terminate")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "error"
    assert client.calls["revoke"] == ["r1"]      # the chain was revoked
    with dbmod.session_scope() as db:
        run = db.get(Run, "r1")
        assert run.status == RunStatus.error
        assert run.error == "Stopped by user"


def test_terminate_closes_out_the_inflight_stage(client):
    # the stage mid-flight when we stop must not stay "running" (the UI would keep ticking it);
    # it's frozen to "error" with its elapsed-so-far recorded.
    _seed(run_status=RunStatus.running, running_stage_age=100)
    r = client.post("/api/runs/r1/terminate")
    assert r.status_code == 200, r.text
    with dbmod.session_scope() as db:
        stages = {s.stage: s for s in db.get(Run, "r1").stages}
        assert stages["tts"].status == "error"          # no longer a live "running" pill
        assert stages["tts"].seconds >= 100              # elapsed-so-far frozen in
        assert stages["script"].status == "ran"          # a finished stage is left untouched


def test_terminate_queued_run_is_allowed(client):
    _seed(run_status=RunStatus.queued)
    r = client.post("/api/runs/r1/terminate")
    assert r.status_code == 200, r.text
    assert client.calls["revoke"] == ["r1"]


def test_terminate_finished_run_is_409_and_does_not_revoke(client):
    _seed(run_status=RunStatus.done)
    r = client.post("/api/runs/r1/terminate")
    assert r.status_code == 409
    assert client.calls["revoke"] == []          # nothing revoked for an inactive run


def test_terminate_unknown_run_is_404(client):
    _seed()
    r = client.post("/api/runs/nope/terminate")
    assert r.status_code == 404


def test_regenerate_running_run_stops_old_chain_then_restarts(client):
    _seed(run_status=RunStatus.running)
    r = client.post("/api/runs", json={"movie_id": "m1", "lang": "zh", "force": True})
    assert r.status_code == 200, r.text
    # old in-flight chain revoked BEFORE the fresh one is dispatched + artifacts cleared
    assert client.calls["revoke"] == ["r1"]
    assert client.calls["clear"] and client.calls["back"] == ["r1"]
    with dbmod.session_scope() as db:
        run = db.get(Run, "r1")
        assert run.status == RunStatus.queued and run.error is None


def test_regenerate_clears_old_stage_rows_so_no_stale_ticking_pill(client):
    # the in-flight run had a finished 'script' and a live 'tts'; a full regenerate wipes both so
    # the fresh chain shows progress from step 1 (not a tts pill still ticking from the old start).
    _seed(run_status=RunStatus.running, running_stage_age=100)
    r = client.post("/api/runs", json={"movie_id": "m1", "lang": "zh", "force": True})
    assert r.status_code == 200, r.text
    with dbmod.session_scope() as db:
        assert db.query(RunStage).filter(RunStage.run_id == "r1").count() == 0


def test_regenerate_at_concurrency_cap_excludes_the_regenerated_run(client):
    # fill the session to the cap with OTHER running runs; the run we regenerate must not count
    # against the cap (else this would 429 instead of restarting).
    cap = api.S.max_concurrent_runs_per_session
    _seed(run_status=RunStatus.running, fillers=cap - 1)
    r = client.post("/api/runs", json={"movie_id": "m1", "lang": "zh", "force": True})
    assert r.status_code == 200, r.text
    assert client.calls["revoke"] == ["r1"] and client.calls["back"] == ["r1"]


# --- _fail_run guard: a task killed by a regenerate must not clobber the fresh chain -----------
class _FakeRedis:
    """Minimal stand-in: .get() returns a preset value, or raises if it's an Exception."""

    def __init__(self, value):
        self._value = value

    def get(self, _key):
        if isinstance(self._value, BaseException):
            raise self._value
        return self._value


class _Task:
    def __init__(self, tid):
        self.request = type("R", (), {"id": tid})()


def _capture_fail(monkeypatch):
    failed: list = []
    monkeypatch.setattr(tasks, "_fail", lambda _cls, rid, _exc: failed.append(rid))
    return failed


def test_fail_run_records_a_genuine_failure(monkeypatch):
    import json
    failed = _capture_fail(monkeypatch)
    monkeypatch.setattr(tasks, "_redis", _FakeRedis(json.dumps(["t1", "t2"])))
    tasks._fail_run(_Task("t1"), "r1", RuntimeError("boom"))   # this task is still current
    assert failed == ["r1"]


def test_fail_run_suppressed_when_superseded_by_regenerate(monkeypatch):
    import json
    failed = _capture_fail(monkeypatch)
    # the run's task-id set was rewritten by a regenerate -> the old task's id is gone
    monkeypatch.setattr(tasks, "_redis", _FakeRedis(json.dumps(["new1", "new2"])))
    tasks._fail_run(_Task("old1"), "r1", RuntimeError("boom"))
    assert failed == []


def test_fail_run_records_when_no_record_present(monkeypatch):
    # key cleared by a terminate (or never stored): don't swallow — record as normal
    failed = _capture_fail(monkeypatch)
    monkeypatch.setattr(tasks, "_redis", _FakeRedis(None))
    tasks._fail_run(_Task("t1"), "r1", RuntimeError("boom"))
    assert failed == ["r1"]


def test_fail_run_records_on_redis_error(monkeypatch):
    # a Redis hiccup must never hide a real failure
    failed = _capture_fail(monkeypatch)
    monkeypatch.setattr(tasks, "_redis", _FakeRedis(RuntimeError("redis down")))
    tasks._fail_run(_Task("t1"), "r1", RuntimeError("boom"))
    assert failed == ["r1"]
