"""Live per-stage progress tracking: the no-downgrade upsert that lets the real
duration of a stage survive a chain that re-enters the pipeline per resource.

The back half runs as bh_script -> bh_tts -> bh_render, each re-calling run_back_half
from the top; later tasks mark already-done stages as ``cached`` (0s). Without the
no-downgrade rule the headline-expensive steps (understand/script/tts) would end up
recorded as ``cached:0``. These tests pin the rule against an in-memory SQLite session
(record_stage_event/record_timings operate on the passed Session — no Postgres needed).
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from yapper_web.db import (  # noqa: E402
    Base,
    MovieStage,
    RunStage,
    record_stage_event,
    record_timings,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)   # creates run_stages + movie_stages (FKs off in sqlite)
    with Session(engine) as s:
        yield s


def _rows(db, run_id="r1"):
    return {r.stage: r for r in db.query(RunStage).filter(RunStage.run_id == run_id).all()}


def test_running_then_ran_records_real_duration(db):
    record_stage_event(db, "run", "r1", "understand", 0.0, "running")
    assert _rows(db)["understand"].status == "running"
    record_stage_event(db, "run", "r1", "understand", 42.0, "ran")
    row = _rows(db)["understand"]
    assert row.status == "ran" and row.seconds == 42.0


def test_cached_does_not_clobber_a_finished_stage(db):
    # bh_script ran understand for real...
    record_stage_event(db, "run", "r1", "understand", 0.0, "running")
    record_stage_event(db, "run", "r1", "understand", 42.0, "ran")
    # ...bh_tts and bh_render re-enter and mark it cached:0 — must be ignored.
    record_stage_event(db, "run", "r1", "understand", 0.0, "cached")
    record_stage_event(db, "run", "r1", "understand", 0.0, "cached")
    row = _rows(db)["understand"]
    assert row.status == "ran" and row.seconds == 42.0


def test_running_does_not_downgrade_a_finished_stage(db):
    record_stage_event(db, "run", "r1", "tts", 30.0, "ran")
    record_stage_event(db, "run", "r1", "tts", 0.0, "running")   # stray re-entry
    row = _rows(db)["tts"]
    assert row.status == "ran" and row.seconds == 30.0


def test_rerun_keeps_max_observed_duration(db):
    # budget/edl/subs have no cache guard and re-run every task; keep the largest time.
    record_stage_event(db, "run", "r1", "budget", 1.0, "ran")
    record_stage_event(db, "run", "r1", "budget", 3.0, "ran")
    record_stage_event(db, "run", "r1", "budget", 2.0, "ran")
    assert _rows(db)["budget"].seconds == 3.0


def test_fresh_cached_inserts(db):
    record_stage_event(db, "run", "r1", "script", 0.0, "cached")
    assert _rows(db)["script"].status == "cached"


def test_error_is_terminal(db):
    record_stage_event(db, "run", "r1", "render", 5.0, "error")
    record_stage_event(db, "run", "r1", "render", 0.0, "cached")
    record_stage_event(db, "run", "r1", "render", 0.0, "running")
    assert _rows(db)["render"].status == "error"


def test_movie_scope_uses_movie_stages(db):
    record_stage_event(db, "movie", "m1", "asr", 0.0, "running")
    record_stage_event(db, "movie", "m1", "asr", 120.0, "ran")
    rows = {r.stage: r for r in db.query(MovieStage).filter(MovieStage.movie_id == "m1").all()}
    assert rows["asr"].status == "ran" and rows["asr"].seconds == 120.0
    assert db.query(RunStage).count() == 0   # movie events don't leak into run_stages


def test_record_timings_preserves_live_rows(db):
    # live rows captured the truth during the run...
    record_stage_event(db, "run", "r1", "understand", 50.0, "ran")
    record_stage_event(db, "run", "r1", "tts", 20.0, "ran")
    # ...finalize reconciles from the final timings.json, where early stages are cached:0.
    record_timings(db, "r1", [
        {"stage": "understand", "seconds": 0.0, "status": "cached"},
        {"stage": "script", "seconds": 0.0, "status": "cached"},
        {"stage": "tts", "seconds": 0.0, "status": "cached"},
        {"stage": "edl", "seconds": 2.0, "status": "ran"},
        {"stage": "render", "seconds": 9.0, "status": "ran"},
    ])
    rows = _rows(db)
    assert rows["understand"].seconds == 50.0 and rows["understand"].status == "ran"
    assert rows["tts"].seconds == 20.0 and rows["tts"].status == "ran"
    assert rows["edl"].seconds == 2.0          # filled in from timings.json
    assert rows["render"].seconds == 9.0
    assert rows["script"].status == "cached"   # genuinely never ran
