"""Output dimensions match the source aspect ratio (portrait stays portrait), capped to the
configured box, and rotation metadata is read so encoded != display dims are handled."""

from yapper.ffmpeg.probe import fit_output_dims, video_rotation
from yapper.schemas import ProbeManifest


def test_fit_preserves_aspect_and_orientation():
    assert fit_output_dims(1080, 1920, long_cap=1920, short_cap=1080) == (1080, 1920)   # portrait kept
    assert fit_output_dims(1920, 1080, long_cap=1920, short_cap=1080) == (1920, 1080)   # landscape kept
    assert fit_output_dims(1080, 1080, long_cap=1920, short_cap=1080) == (1080, 1080)   # square kept


def test_fit_caps_4k_to_1080p_box():
    assert fit_output_dims(3840, 2160, long_cap=1920, short_cap=1080) == (1920, 1080)
    assert fit_output_dims(2160, 3840, long_cap=1920, short_cap=1080) == (1080, 1920)


def test_fit_never_upscales_and_is_even():
    # small source isn't blown up; odd inputs round to even (yuv420p)
    assert fit_output_dims(640, 480, long_cap=1920, short_cap=1080) == (640, 480)
    w, h = fit_output_dims(1079, 1921, long_cap=1920, short_cap=1080)
    assert w % 2 == 0 and h % 2 == 0


def test_fit_unknown_source_falls_back_to_box():
    assert fit_output_dims(0, 0, long_cap=1920, short_cap=1080) == (1920, 1080)


def test_video_rotation_parses_tag_and_side_data():
    assert video_rotation({"tags": {"rotate": "90"}}) == 90
    assert video_rotation({"side_data_list": [{"rotation": -90}]}) == 270   # normalized to 0..359
    assert video_rotation({"side_data_list": [{"rotation": 180}]}) == 180
    assert video_rotation({}) == 0


def test_probe_display_dims_swap_when_rotated():
    # a phone clip encoded 1920x1080 but flagged 90° displays as 1080x1920
    pm = ProbeManifest(source_path="x", container="mov", duration_sec=5.0,
                       width=1920, height=1080, rotation=90, fps=30.0, is_vfr=False, video_codec="h264")
    assert (pm.display_width, pm.display_height) == (1080, 1920)
    pm0 = pm.model_copy(update={"rotation": 0})
    assert (pm0.display_width, pm0.display_height) == (1920, 1080)
