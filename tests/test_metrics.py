"""Resource-usage metrics: the per-stage LLM ledger, the DB-derived collector series
(stage timing / render RTF / running stages), and the gpud instance-state bucketing.

All run without a real Redis/Postgres: the StageUsageLedger takes an injected fake redis
(like Budget), the PlatformCollector is pointed at an in-memory SQLite via the db module
globals (StaticPool so the one connection is shared across sessions), and instance_state
is a pure function.
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("prometheus_client")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import yapper_web.db as dbmod  # noqa: E402
from yapper_web.db import Base, Run, RunStage, RunStatus  # noqa: E402


# --------------------------------------------------------------------------- #
# per-stage LLM ledger (Redis HINCRBY tallies, surfaced as yapper_llm_stage_*)
# --------------------------------------------------------------------------- #
class _FakeRedis:
    """Just the hash ops StageUsageLedger uses."""

    def __init__(self):
        self.h: dict[str, dict[str, int]] = {}

    def hincrby(self, key, field, n):
        d = self.h.setdefault(key, {})
        d[field] = d.get(field, 0) + int(n)
        return d[field]

    def hgetall(self, key):
        return {k.encode(): str(v).encode() for k, v in self.h.get(key, {}).items()}


class _Usage:
    def __init__(self, pin, pout):
        self.prompt_tokens = pin
        self.completion_tokens = pout


def _ledger(client):
    pytest.importorskip("redis")
    from yapper_web.budget import StageUsageLedger
    from yapper_web.settings import Settings

    s = Settings(llm_max_cost_usd=3.0, llm_price_in_per_1k=0.0003, llm_price_out_per_1k=0.0011)
    return StageUsageLedger(client=client, settings=s)


def test_stage_ledger_splits_by_stage_and_accumulates():
    r = _FakeRedis()
    led = _ledger(r)
    led.record("understand", _Usage(100_000, 2_000))  # vision MAP
    led.record("understand", _Usage(50_000, 1_000))   # a second understand call
    led.record("script", _Usage(8_000, 20_000))       # reasoning REDUCE

    snap = led.snapshot()
    assert set(snap) == {"understand", "script"}
    assert snap["understand"]["tokens_in"] == 150_000
    assert snap["understand"]["tokens_out"] == 3_000
    assert snap["script"]["tokens_in"] == 8_000
    assert snap["script"]["tokens_out"] == 20_000
    # cost = in/1k*price_in + out/1k*price_out, summed across the stage's calls
    exp_understand = 150_000 / 1000 * 0.0003 + 3_000 / 1000 * 0.0011
    assert snap["understand"]["cost_usd"] == pytest.approx(exp_understand, abs=1e-3)


def test_stage_ledger_defaults_blank_stage_to_unknown():
    r = _FakeRedis()
    led = _ledger(r)
    led.record("", _Usage(1_000, 1_000))
    assert "unknown" in led.snapshot()


# --------------------------------------------------------------------------- #
# gpud instance-state bucketing (pure)
# --------------------------------------------------------------------------- #
def test_instance_state_buckets():
    srv = pytest.importorskip("server.gpud") if False else None  # noqa: F841
    from server.gpud import Instance, instance_state

    leased = Instance(service="asr", gpu_index=0, port=1, state="ready", leased=True)
    idle = Instance(service="asr", gpu_index=0, port=2, state="ready", leased=False)
    starting = Instance(service="tts", gpu_index=0, port=3, state="starting", leased=False)
    assert instance_state(leased) == "leased"
    assert instance_state(idle) == "idle"
    assert instance_state(starting) == "starting"  # cold start, NOT counted as idle


# --------------------------------------------------------------------------- #
# DB-derived collector series
# --------------------------------------------------------------------------- #
@pytest.fixture()
def sqlite_db(monkeypatch):
    # StaticPool keeps the single in-memory connection alive so the collector's separate
    # session_scope() sees the rows we seed here.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(dbmod, "_engine", engine, raising=False)
    monkeypatch.setattr(dbmod, "_SessionLocal", SessionLocal, raising=False)
    return SessionLocal


def _families(monkeypatch):
    # Silence the Redis-backed blocks (no broker/ledger in this test) so collect() is quiet
    # and fast; the DB block is what we assert on.
    from yapper_web.metrics import PlatformCollector

    fams = {}
    for fam in PlatformCollector().collect():
        fams[fam.name] = fam
    return fams


def _sample(fam, labels):
    for s in fam.samples:
        if all(s.labels.get(k) == v for k, v in labels.items()):
            return s.value
    return None


def test_collector_emits_stage_timing_and_rtf(sqlite_db, monkeypatch):
    with sqlite_db() as db:
        db.add(Run(id="r1", movie_id="m1", session_id="s1", lang="zh",
                   status=RunStatus.done, output_duration_sec=120.0))
        # render: 60s wall, 120s output -> RTF 2.0
        db.add(RunStage(run_id="r1", stage="render", status="ran", seconds=60.0))
        db.add(RunStage(run_id="r1", stage="understand", status="ran", seconds=45.0))
        db.add(RunStage(run_id="r1", stage="tts", status="running", seconds=0.0))
        db.commit()

    fams = _families(monkeypatch)

    assert "yapper_stage_seconds_avg" in fams
    assert _sample(fams["yapper_stage_seconds_avg"],
                   {"scope": "run", "stage": "render", "status": "ran"}) == pytest.approx(60.0)
    assert _sample(fams["yapper_stage_seconds_avg"],
                   {"scope": "run", "stage": "understand", "status": "ran"}) == pytest.approx(45.0)
    # running stage surfaced
    assert _sample(fams["yapper_stage_running"], {"scope": "run", "stage": "tts"}) == 1
    # render efficiency
    assert _sample(fams["yapper_render_rtf"], {"lang": "zh"}) == pytest.approx(2.0)


def test_collector_rtf_skips_runs_without_render_or_duration(sqlite_db, monkeypatch):
    with sqlite_db() as db:
        # no output_duration_sec -> excluded from RTF
        db.add(Run(id="r2", movie_id="m2", session_id="s1", lang="en", status=RunStatus.done))
        db.add(RunStage(run_id="r2", stage="render", status="ran", seconds=30.0))
        db.commit()

    fams = _families(monkeypatch)
    assert _sample(fams["yapper_render_rtf"], {"lang": "en"}) is None
