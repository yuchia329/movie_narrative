"""s11 generates a valid ASS on the voiceover timeline. Captions are now split into timed
pieces (sentence/clause-aware, sized to the frame) so long lines fit a narrow/portrait frame and
appear roughly in step with the speech (estimated by text proportion, not true word sync)."""

from pathlib import Path

from yapper.schemas import Edl, EdlSegment
from yapper.stages import s11_subs


def _edl() -> Edl:
    return Edl(
        fps=30, width=1920, height=1080,
        segments=[
            EdlSegment(segment_id="seg_000", line_id="l0", src_in=0, src_out=4, vo_file="/v/0.wav", vo_duration=4.0, subtitle_text="第一句"),
            EdlSegment(segment_id="seg_001", line_id="l1", src_in=4, src_out=10, vo_file="/v/1.wav", vo_duration=6.0, subtitle_text="第二句\n换行"),
        ],
    )


def _cue_times(line: str) -> tuple[str, str]:
    # "Dialogue: 0,<start>,<end>,Default,,..." -> (start, end)
    parts = line.split(",")
    return parts[1], parts[2]


def test_ass_cues_are_sequential_and_cover_the_timeline(tmp_path: Path):
    out = s11_subs.build_ass(_edl(), tmp_path / "subs.ass", width=1920, height=1080, font_name="Noto Sans CJK SC")
    body = out.read_text(encoding="utf-8")
    dialogues = [ln for ln in body.splitlines() if ln.startswith("Dialogue:")]
    # seg_000 -> 1 piece; seg_001 ("第二句\n换行") -> 2 pieces (newline is a hard break)
    assert len(dialogues) == 3
    starts = [_cue_times(d)[0] for d in dialogues]
    assert starts[0] == "0:00:00.00"                         # timeline starts at 0
    assert _cue_times(dialogues[-1])[1] == "0:00:10.00"      # ...and covers the full duration
    assert any(s == "0:00:04.00" for s in starts)            # second segment starts at 4.0s
    assert starts == sorted(starts)                          # monotonic
    assert "第一句" in dialogues[0]
    assert "第二句" in body and "换行" in body                 # the explicit newline split into 2 pieces
    assert "Noto Sans CJK SC" in body and "BorderStyle" in body and "WrapStyle: 2" in body


def test_short_caption_is_one_local_cue(tmp_path: Path):
    out = s11_subs.build_segment_ass("第一句", 4.2, tmp_path / "seg.ass", width=1920, height=1080)
    assert out is not None
    dialogues = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln.startswith("Dialogue:")]
    assert len(dialogues) == 1
    assert _cue_times(dialogues[0]) == ("0:00:00.00", "0:00:04.20")   # spans the segment locally
    assert "第一句" in dialogues[0]


def test_long_caption_splits_into_proportional_timed_pieces(tmp_path: Path):
    text = "第一句话。第二句话。第三句话。"           # three sentences -> three timed pieces
    out = s11_subs.build_segment_ass(text, 9.0, tmp_path / "seg.ass", width=1920, height=1080)
    dialogues = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln.startswith("Dialogue:")]
    assert len(dialogues) == 3
    times = [_cue_times(d) for d in dialogues]
    assert times[0][0] == "0:00:00.00"                       # starts at 0
    assert times[-1][1] == "0:00:09.00"                      # ends at duration
    # cues abut with no gaps/overlap (each end == next start)
    assert all(times[i][1] == times[i + 1][0] for i in range(len(times) - 1))


def test_narrow_portrait_frame_makes_more_pieces():
    text = "这是一句比较长的旁白用来测试在窄屏竖屏画面下的换行与切分效果。" * 2
    wide = s11_subs._segment_cues(text, 10.0, s11_subs._max_chars(1920, 48))
    narrow = s11_subs._segment_cues(text, 10.0, s11_subs._max_chars(1080, 48))
    assert len(narrow) > len(wide)                           # narrower frame -> more, shorter pieces


def test_long_unpunctuated_line_is_hard_split():
    chunks = s11_subs._chunk_text("字" * 100, max_chars=20)   # no punctuation at all
    assert len(chunks) >= 5
    assert all(len(c) <= 20 for c in chunks)


def test_segment_ass_empty_text_returns_none(tmp_path: Path):
    # playback segments with no narration -> nothing to burn
    assert s11_subs.build_segment_ass("  ", 3.0, tmp_path / "x.ass", width=1920, height=1080) is None
    assert not (tmp_path / "x.ass").exists()
