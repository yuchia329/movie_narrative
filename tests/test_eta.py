"""Pipeline ETA: estimated_total/elapsed/remaining from historical per-stage averages (with a
static seed fallback when there's no history), tightening as stages complete."""

import pytest

pytest.importorskip("fastapi")

from yapper.pipeline import BACK_HALF  # noqa: E402
from yapper_web import api  # noqa: E402


class _Stage:
    def __init__(self, stage, status, seconds):
        self.stage, self.status, self.seconds = stage, status, seconds


def test_eta_falls_back_to_seeds_with_no_history(monkeypatch):
    monkeypatch.setattr(api, "_stage_avgs", lambda scope: {})     # no history
    eta = api._eta("run", BACK_HALF, None)
    assert eta["elapsed_sec"] == 0
    assert eta["estimated_remaining_sec"] == eta["estimated_total_sec"] > 0


def test_eta_uses_history_and_shrinks_as_stages_finish(monkeypatch):
    avgs = {s: 10.0 for s in BACK_HALF}
    monkeypatch.setattr(api, "_stage_avgs", lambda scope: avgs)
    full = api._eta("run", BACK_HALF, [])
    assert full["estimated_total_sec"] == 10 * len(BACK_HALF)
    assert full["estimated_remaining_sec"] == full["estimated_total_sec"]
    # two stages finished (15s each) -> elapsed counts real time, remaining only the rest
    done = [_Stage("understand", "ran", 15), _Stage("script", "cached", 15)]
    part = api._eta("run", BACK_HALF, done)
    assert part["elapsed_sec"] == 30
    assert part["estimated_remaining_sec"] == 10 * (len(BACK_HALF) - 2)
    assert part["estimated_total_sec"] == 30 + 10 * (len(BACK_HALF) - 2)


def test_eta_all_done_has_no_remaining(monkeypatch):
    monkeypatch.setattr(api, "_stage_avgs", lambda scope: {s: 10.0 for s in BACK_HALF})
    done = [_Stage(s, "ran", 12) for s in BACK_HALF]
    eta = api._eta("run", BACK_HALF, done)
    assert eta["estimated_remaining_sec"] == 0
    assert eta["estimated_total_sec"] == 12 * len(BACK_HALF)
