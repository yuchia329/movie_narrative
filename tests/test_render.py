"""s12 render orchestration — the per-segment subtitle gating (which segments get a burned
.ass, and the libass-missing / subs-disabled fallbacks). ffmpeg is mocked out: we capture
the seg_ass map handed to graph.render_segments rather than actually encoding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from yapper.schemas import Edl, EdlSegment  # noqa: E402
from yapper.stages import s12_render  # noqa: E402

RCFG = {"width": 1920, "height": 1080, "fps": 30, "vcodec": "libx264", "pix_fmt": "yuv420p", "audio_rate": 48000}


def _edl() -> Edl:
    return Edl(fps=30, width=1920, height=1080, segments=[
        EdlSegment(segment_id="seg_000", line_id="l0", src_in=0, src_out=4, vo_file="/v/0.wav",
                   vo_duration=4.0, subtitle_text="第一句"),
        EdlSegment(segment_id="seg_001", line_id="l1", src_in=4, src_out=8, vo_file="/v/1.wav",
                   vo_duration=4.0, subtitle_text="   "),                       # blank -> no cue
        EdlSegment(segment_id="seg_002", line_id="l2", kind="playback", src_in=8, src_out=12,
                   vo_duration=4.0, subtitle_text="引用台词"),                    # playback WITH a caption
    ])


@pytest.fixture
def captured(monkeypatch):
    """Stub ffmpeg-touching calls; record the seg_ass render_segments receives."""
    cap: dict = {}

    def fake_render_segments(movie_path, edl, scratch_dir, **kw):
        cap["seg_ass"] = kw.get("seg_ass")
        cap["fonts_dir"] = kw.get("fonts_dir")
        return [Path(scratch_dir) / f"{s.segment_id}.mp4" for s in edl.segments]

    monkeypatch.setattr(s12_render.graph, "render_segments", fake_render_segments)
    monkeypatch.setattr(s12_render.graph, "write_concat_list", lambda paths, lf: lf)
    monkeypatch.setattr(s12_render.graph, "concat_cmd", lambda lf, out, **kw: ["true"])
    monkeypatch.setattr(s12_render, "run", lambda cmd: None)
    return cap


def _run(tmp_path, subtitle_style):
    return s12_render.run_stage(
        "/movie.mkv", _edl(), scratch_dir=tmp_path, out_path=tmp_path / "out.mp4",
        render_cfg=RCFG, ducking_cfg={}, subtitle_style=subtitle_style,
        fonts_dir="/fonts",
    )


def test_subs_enabled_builds_ass_for_nonblank_segments_only(captured, tmp_path, monkeypatch):
    monkeypatch.setattr(s12_render, "has_filter", lambda f: True)
    _run(tmp_path, {"font_name": "Noto Sans CJK SC", "font_size": 48, "margin_v": 60})
    seg_ass = captured["seg_ass"]
    # blank seg_001 excluded; the playback segment WITH a caption IS included
    assert set(seg_ass) == {"seg_000", "seg_002"}
    for p in seg_ass.values():           # the per-segment .ass files were actually written
        assert Path(p).exists()
    assert str(captured["fonts_dir"]) == "/fonts"


def test_libass_missing_falls_back_to_no_subs(captured, tmp_path, monkeypatch):
    monkeypatch.setattr(s12_render, "has_filter", lambda f: False)
    _run(tmp_path, {"font_name": "Noto Sans CJK SC", "font_size": 48, "margin_v": 60})
    assert captured["seg_ass"] == {}     # graceful: render proceeds, no subtitles


def test_no_subtitle_style_means_no_subs(captured, tmp_path, monkeypatch):
    monkeypatch.setattr(s12_render, "has_filter", lambda f: True)
    _run(tmp_path, None)
    assert captured["seg_ass"] == {}
