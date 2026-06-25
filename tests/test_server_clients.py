"""gRPC server clients + s09 validated without a real GPU server: the gRPC contract is
exercised against an in-process server with fake servicers, and s09 runs end-to-end
with a synthesizer that emits real WAVs via ffmpeg.

Guarded on grpcio so the suite stays green where the web extra isn't installed; the
s09 test (duck-typed synthesizer) needs no gRPC and always runs."""

from pathlib import Path

import pytest

from yapper.ffmpeg.run import FFMPEG, run
from yapper.schemas import Script, ScriptLine
from yapper.stages import s09_tts


@pytest.fixture
def grpc_server():
    """Spin up an in-process gRPC server with fake ASR + TTS servicers on an ephemeral
    port; yields the target string. Returns a factory so each test sets the behavior."""
    grpc = pytest.importorskip("grpc")
    from concurrent import futures

    from yapper_rpc import asr_pb2, asr_pb2_grpc, tts_pb2, tts_pb2_grpc

    class FakeAsr(asr_pb2_grpc.AsrServicer):
        def Transcribe(self, request_iterator, context):
            cfg = asr_pb2.TranscribeConfig()
            nbytes = 0
            for req in request_iterator:
                kind = req.WhichOneof("payload")
                if kind == "config":
                    cfg = req.config
                elif kind == "audio_chunk":
                    nbytes += len(req.audio_chunk)
            assert nbytes > 0  # audio actually streamed
            word = asr_pb2.Word(text="Hello", start=1.0, end=1.5, score=0.9, has_score=True)
            seg = asr_pb2.Segment(
                start=1.0, end=4.0, text="Hello there.", speaker=cfg.language and "SPEAKER_00" or "",
                words=[word],
            )
            return asr_pb2.TranscriptReply(language=cfg.language or "und", segments=[seg])

    class FakeTts(tts_pb2_grpc.TtsServicer):
        def Synthesize(self, request, context):
            assert request.seed == 7  # request fields propagate
            data = b"RIFF" + b"\x00" * 600_000  # bigger than one chunk -> exercises streaming
            for i in range(0, len(data), 256 * 1024):
                yield tts_pb2.AudioChunk(data=data[i:i + 256 * 1024])

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    asr_pb2_grpc.add_AsrServicer_to_server(FakeAsr(), server)
    tts_pb2_grpc.add_TtsServicer_to_server(FakeTts(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    yield f"127.0.0.1:{port}"
    server.stop(0)


def test_asr_client_streams_wav_and_parses_reply(grpc_server, tmp_path):
    from yapper.server_clients.asr_client import ASRClient

    wav = tmp_path / "in.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 4096)  # any bytes; streamed as the upload body
    tr = ASRClient(grpc_server).transcribe(wav, language="en")
    assert tr.source == "asr" and tr.language == "en"
    assert tr.segments[0].text == "Hello there."
    assert tr.segments[0].speaker == "SPEAKER_00"
    assert tr.segments[0].words[0].text == "Hello"
    assert abs(tr.segments[0].words[0].score - 0.9) < 1e-6


def test_tts_client_reassembles_streamed_audio(grpc_server, tmp_path):
    from yapper.server_clients.tts_client import TTSClient

    out = TTSClient(grpc_server).synthesize("你好", tmp_path / "l0.wav", seed=7)
    data = out.read_bytes()
    assert data.startswith(b"RIFF")
    assert len(data) == 600_004  # all streamed chunks concatenated


class _SineSynth:
    """Stand-in TTS: emits a real WAV whose length scales with text length."""

    def synthesize(self, text, out_path, *, seed=42, speed=1.0):
        dur = max(0.5, len(text) * 0.15)
        run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", f"sine=frequency=300:duration={dur}:sample_rate=48000", "-ac", "1", str(out_path)])
        return Path(out_path)


def test_s09_builds_voiceover_manifest_with_measured_durations(tmp_path: Path):
    script = Script(lines=[
        ScriptLine(line_id="line_000", text="第一句旁白", clip_refs=["clip_0000"], est_spoken_seconds=2),
        ScriptLine(line_id="line_001", text="这是更长一些的第二句旁白内容", clip_refs=["clip_0001"], est_spoken_seconds=4),
    ])
    vo = s09_tts.run_stage(script, _SineSynth(), tmp_path / "vo", voice_id="narrator", seed=1)
    assert vo.voice_id == "narrator" and vo.seed == 1
    assert len(vo.lines) == 2
    assert all(Path(l.file).exists() and l.measured_duration_sec > 0 for l in vo.lines)
    # longer text -> longer measured voiceover
    assert vo.lines[1].measured_duration_sec > vo.lines[0].measured_duration_sec
