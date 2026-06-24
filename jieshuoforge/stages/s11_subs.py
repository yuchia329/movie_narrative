"""Stage 11 — subtitle generation (styled .ass on the voiceover timeline).

For the MVP, subtitles are line-level: each narration line gets one cue spanning
its EDL segment's screen time (which equals its measured voiceover duration). This
keeps the subtitle timeline aligned to the voiceover without a separate forced
aligner — word-level karaoke is a later polish. Generating the .ass needs no
libass; only burning it in (s12) does.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..schemas import Edl

log = logging.getLogger("jieshuoforge.s11")


def _ts(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_header(width: int, height: int, font_name: str, font_size: int, margin_v: int) -> str:
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # BorderStyle=3 → opaque box behind text; Alignment=2 → bottom-center
        f"Style: Default,{font_name},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,3,2,0,2,40,40,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _dialogue(start: float, end: float, text: str) -> str:
    return f"Dialogue: 0,{_ts(start)},{_ts(end)},Default,,0,0,0,,{text.replace(chr(10), chr(92) + 'N').strip()}\n"


def build_ass(
    edl: Edl,
    out_path: str | Path,
    *,
    width: int,
    height: int,
    font_name: str = "Noto Sans CJK SC",
    font_size: int = 48,
    margin_v: int = 60,
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [_ass_header(width, height, font_name, font_size, margin_v)]
    t = 0.0
    for seg in edl.segments:
        end = t + seg.screen_duration
        lines.append(_dialogue(t, end, seg.subtitle_text))
        t = end

    out_path.write_text("".join(lines), encoding="utf-8")
    log.info("[subs] %d cues -> %s", len(edl.segments), out_path.name)
    return out_path


def build_segment_ass(
    text: str,
    duration: float,
    out_path: str | Path,
    *,
    width: int,
    height: int,
    font_name: str = "Noto Sans CJK SC",
    font_size: int = 48,
    margin_v: int = 60,
) -> Path | None:
    """Write a one-cue .ass spanning ``[0, duration]`` in the SEGMENT's local timeline, for
    burning into that segment during its encode (the segment video is reset to PTS 0).

    Because each EDL segment carries exactly one cue (see :func:`build_ass`), per-segment
    burn-in needs no timeline splitting. Returns ``None`` for empty text (nothing to burn)."""
    if not text or not text.strip():
        return None
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = _ass_header(width, height, font_name, font_size, margin_v) + _dialogue(0.0, duration, text)
    out_path.write_text(body, encoding="utf-8")
    return out_path
