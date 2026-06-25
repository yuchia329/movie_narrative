"""Resume / Start-over recovery for a pipeline that stopped on a stage error.

POST /api/movies/{id}/retry has three behaviours we pin here:
  - back-half error + mode=resume   -> re-queue THAT run, drop only its error stage row,
                                       call start_back_half (no artifact clearing => resumes).
  - front-half error + mode=resume  -> movie back to 'uploaded', re-run the front chain.
  - mode=restart                    -> wipe artifacts, delete runs, re-run from ingest.

We inject an in-memory SQLite sessionmaker into yapper_web.db so the endpoint's
session_scope() uses it (no Postgres), override the session-cookie dependency, and stub
the Celery launchers so nothing touches S3/Redis/GPU.
"""

from __future__ import annotations

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
    MovieStage,
    MovieStatus,
    Run,
    RunStage,
    RunStatus,
    Session_,
)

SID = "test-session"


@pytest.fixture()
def client(monkeypatch):
    # in-memory SQLite shared across connections (StaticPool => one connection, so every
    # session_scope() sees the same DB), wired in as the db module's global session factory
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dbmod, "_engine", engine, raising=False)
    monkeypatch.setattr(
        dbmod, "_SessionLocal",
        sessionmaker(bind=engine, expire_on_commit=False, future=True), raising=False,
    )
    # stub the launchers + artifact wipe so the endpoint touches no infra
    calls: dict[str, list] = {"front": [], "back": [], "clear": []}
    monkeypatch.setattr(tasks, "start_front_half", lambda mid: calls["front"].append(mid))
    monkeypatch.setattr(tasks, "start_back_half", lambda rid: calls["back"].append(rid))
    monkeypatch.setattr(tasks, "clear_movie_artifacts", lambda *a: calls["clear"].append(a))
    api.app.dependency_overrides[api.get_session] = lambda: SID
    c = TestClient(api.app)
    c.calls = calls  # type: ignore[attr-defined]
    yield c
    api.app.dependency_overrides.clear()


def _seed(*, movie_status, run_status=None, run_lang="zh"):
    with dbmod.session_scope() as db:
        db.add(Session_(id=SID))
        m = Movie(
            id="m1", session_id=SID, original_filename="x.mp4", slug="x",
            s3_prefix=f"{SID}/m1/x", source_key=f"sources/{SID}/m1/x.mp4",
            status=movie_status, default_lang="zh",
        )
        db.add(m)
        db.add(MovieStage(movie_id="m1", stage="asr", status="error", seconds=0.0))
        db.add(MovieStage(movie_id="m1", stage="audio", status="ran", seconds=5.0))
        if run_status is not None:
            db.add(Run(id="r1", movie_id="m1", session_id=SID, lang=run_lang, status=run_status))
            db.add(RunStage(run_id="r1", stage="tts", status="error", seconds=0.0))
            db.add(RunStage(run_id="r1", stage="script", status="ran", seconds=9.0))


def test_resume_back_half_error_requeues_run_and_resumes(client):
    _seed(movie_status=MovieStatus.ready, run_status=RunStatus.error)
    r = client.post("/api/movies/m1/retry", json={"mode": "resume", "lang": "zh"})
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "back"
    # the failed run is re-queued, its error cleared, and the back half is resumed (no clearing)
    assert client.calls["back"] == ["r1"]
    assert client.calls["front"] == [] and client.calls["clear"] == []
    with dbmod.session_scope() as db:
        run = db.get(Run, "r1")
        assert run.status == RunStatus.queued and run.error is None
        stages = {s.stage: s.status for s in run.stages}
        assert "tts" not in stages          # the error stage row was dropped (re-runs fresh)
        assert stages["script"] == "ran"    # completed stage kept (skipped on resume)


def test_resume_front_half_error_reruns_front_chain(client):
    _seed(movie_status=MovieStatus.error)   # front half failed, no run yet
    r = client.post("/api/movies/m1/retry", json={"mode": "resume"})
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "front"
    assert client.calls["front"] == ["m1"] and client.calls["back"] == []
    with dbmod.session_scope() as db:
        m = db.get(Movie, "m1")
        assert m.status == MovieStatus.uploaded and m.error is None
        stages = {s.stage: s.status for s in m.stages}
        assert "asr" not in stages          # error front stage dropped
        assert stages["audio"] == "ran"     # cached front stage kept


def test_restart_wipes_everything_and_runs_from_ingest(client):
    _seed(movie_status=MovieStatus.error, run_status=RunStatus.error)
    r = client.post("/api/movies/m1/retry", json={"mode": "restart"})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "restart"
    assert client.calls["clear"] and client.calls["front"] == ["m1"]
    with dbmod.session_scope() as db:
        m = db.get(Movie, "m1")
        assert m.status == MovieStatus.uploaded and m.error is None
        assert db.query(Run).filter(Run.movie_id == "m1").count() == 0      # runs deleted
        assert db.query(MovieStage).filter(MovieStage.movie_id == "m1").count() == 0


def test_resume_with_nothing_failed_is_409(client):
    _seed(movie_status=MovieStatus.ready, run_status=RunStatus.done)
    r = client.post("/api/movies/m1/retry", json={"mode": "resume"})
    assert r.status_code == 409


def test_unknown_mode_is_400(client):
    _seed(movie_status=MovieStatus.error)
    r = client.post("/api/movies/m1/retry", json={"mode": "sideways"})
    assert r.status_code == 400
