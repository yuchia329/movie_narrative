"""TTS fan-out: s09 shards independent narration lines across a pool of synthesizer
instances, in parallel, while preserving script (timeline) order. ffmpeg (loudnorm +
duration probe) is monkeypatched out so the test needs no GPU/ffmpeg.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from yapper.schemas import Script, ScriptLine  # noqa: E402
from yapper.stages import s09_tts  # noqa: E402


class FakeSynth:
    def __init__(self, name: str):
        self.name = name
        self.calls: list[str] = []

    def synthesize(self, text, out_path, *, seed=42, **kw):
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RIFF")  # dummy wav
        time.sleep(0.02)        # encourage real overlap so the fan-out is observable
        self.calls.append(text)
        return p


def _script(n: int) -> Script:
    return Script(lines=[
        ScriptLine(line_id=f"l{i}", text=f"line {i}", clip_refs=[], est_spoken_seconds=1.0)
        for i in range(n)
    ])


@pytest.fixture(autouse=True)
def _no_ffmpeg(monkeypatch):
    # loudnorm just copies; duration is a constant — keep ffmpeg/ffprobe out of the test.
    monkeypatch.setattr(s09_tts, "_loudnorm",
                        lambda src, dst, **k: Path(dst).write_bytes(b"RIFF"))
    monkeypatch.setattr(s09_tts, "duration_sec", lambda p: 1.0)


def test_pool_preserves_order_and_synthesizes_every_line(tmp_path):
    pool = [FakeSynth("a"), FakeSynth("b"), FakeSynth("c")]
    vo = s09_tts.run_stage(_script(6), pool[0], tmp_path, voice_id="v", synthesizers=pool)

    # order matches script order regardless of which instance finished first
    assert [ln.line_id for ln in vo.lines] == [f"l{i}" for i in range(6)]
    # every line was synthesized exactly once across the pool
    assert sorted(c for s in pool for c in s.calls) == [f"line {i}" for i in range(6)]


def test_pool_actually_fans_out_across_instances(tmp_path):
    pool = [FakeSynth("a"), FakeSynth("b"), FakeSynth("c")]
    s09_tts.run_stage(_script(6), pool[0], tmp_path, voice_id="v", synthesizers=pool)
    used = [s for s in pool if s.calls]
    assert len(used) >= 2, "lines should be distributed across multiple instances, not serialized on one"


def test_each_instance_serves_one_line_at_a_time(tmp_path):
    # A synth that asserts it's never entered concurrently (the queue must hand each
    # instance to one worker at a time) — guards the per-instance serialization invariant.
    class ExclusiveSynth(FakeSynth):
        def __init__(self, name):
            super().__init__(name)
            self._in = False
            self._lock = threading.Lock()

        def synthesize(self, text, out_path, *, seed=42, **kw):
            with self._lock:
                assert not self._in, f"{self.name} entered concurrently"
                self._in = True
            try:
                return super().synthesize(out_path=out_path, text=text, seed=seed, **kw)
            finally:
                with self._lock:
                    self._in = False

    pool = [ExclusiveSynth("a"), ExclusiveSynth("b")]
    vo = s09_tts.run_stage(_script(5), pool[0], tmp_path, voice_id="v", synthesizers=pool)
    assert len(vo.lines) == 5


def test_single_synthesizer_legacy_path_still_works(tmp_path):
    syn = FakeSynth("solo")
    vo = s09_tts.run_stage(_script(3), syn, tmp_path, voice_id="v")
    assert [ln.line_id for ln in vo.lines] == ["l0", "l1", "l2"]
    assert len(syn.calls) == 3


def test_empty_pool_raises_immediately(tmp_path):
    with pytest.raises(ValueError):
        s09_tts.run_stage(_script(2), None, tmp_path, voice_id="v", synthesizers=[])


def test_tts_client_closes_only_owned_channel():
    grpc = pytest.importorskip("grpc")
    from yapper.server_clients.tts_client import TTSClient

    # owned channel (created internally): closed on exit, close() idempotent
    with TTSClient("127.0.0.1:59999") as c:
        assert c._owns_channel is True and c._channel is not None
    assert c._channel is None
    c.close()  # idempotent — no raise

    # injected channel: not owned, left open for the caller to manage
    ch = grpc.insecure_channel("127.0.0.1:59999")
    inj = TTSClient("127.0.0.1:59999", channel=ch)
    assert inj._owns_channel is False
    inj.close()
    assert inj._channel is ch  # untouched
    ch.close()
