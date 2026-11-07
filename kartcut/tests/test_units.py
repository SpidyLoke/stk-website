import subprocess

import numpy as np
import pytest

from kartcut import colour as C
from kartcut.autocut import make_slots
from kartcut.detect import parse_number
from kartcut.render import _crop_expr
from kartcut.tracker import Tracker


def test_slog3_reference_points():
    # Sony: 18% grey at CV 420 (~41 IRE), 90% white at CV 598 (~61 IRE), black at CV 95 (3.5 IRE)
    assert C.linear_to_slog3(0.18) == pytest.approx((420 - 64) / 876, abs=1e-3)
    assert C.linear_to_slog3(0.90) == pytest.approx((598 - 64) / 876, abs=2e-3)
    assert C.linear_to_slog3(0.0) == pytest.approx((95 - 64) / 876, abs=1e-3)
    x = np.linspace(0.04, 0.94, 50)
    assert np.allclose(C.linear_to_slog3(C.slog3_to_linear(x)), x, atol=1e-6)


def test_display_transform_grey_and_white():
    g = C.to_display(np.full(3, C.linear_to_slog3(0.18)), C.get_profile("slog3-sgamut3cine"))
    assert 0.40 < g[1] < 0.48
    w = C.to_display(np.full(3, C.linear_to_slog3(0.90)), C.get_profile("slog3-sgamut3cine"))
    assert 0.78 < w[1] < 0.88


def test_fast_display_matches_reference():
    prof = C.get_profile("slog3-sgamut3cine")
    img = np.random.default_rng(1).integers(20, 240, (8, 8, 3)).astype(np.uint8)
    fast = C.display_bgr_u8(img, prof).astype(np.float32) / 255
    ref = C.to_display(img[..., ::-1].astype(np.float64) / 255, prof)[..., ::-1]
    assert np.abs(fast - ref).max() < 0.03


def test_cube_roundtrip_and_correction(tmp_path):
    prof = C.get_profile("slog3-sgamut3cine")
    g = C.lut_grid(17)
    fn = C.correction_fn(prof, 1.0, [1.0, 1.0, 1.0])
    p = tmp_path / "x.cube"
    C.write_cube(p, fn(g), 17, "t")
    cube = C.read_cube(p)
    grey = np.full(3, C.linear_to_slog3(0.18))
    out = cube.apply(grey)
    assert C.slog3_to_linear(out[1]) == pytest.approx(0.36, rel=0.03)  # +1 stop doubles linear


def test_parse_number():
    assert parse_number("07") == "7"
    assert parse_number("７") == "7"   # full-width digit from the OCR model
    assert parse_number("23") == "23"
    assert parse_number("B8") == "88"
    assert parse_number("KART") is None
    assert parse_number("12345") is None


def test_tracker_keeps_ids_at_low_fps():
    tr = Tracker()
    ids = []
    for i in range(8):
        t = i * 0.125
        a = np.array([100 + 300 * t, 400, 300 + 300 * t, 500], np.float32)
        b = np.array([900 - 250 * t, 300, 1100 - 250 * t, 400], np.float32)
        ids.append(tr.update(t, [a, b]))
    assert all(x == ids[0] for x in ids)


def test_slots_follow_beats_and_sections():
    beats = list(np.arange(0, 60, 0.5))
    music = {"beats": beats, "tempo": 120.0, "downbeat_phase": 0, "drop": 14.0, "duration": 60.0}
    slots = make_slots(music, 30.0)
    assert slots[0][0] == 0.0
    assert abs(slots[-1][1] - 30.0) <= 2.0
    for t0, t1, _ in slots:
        assert t1 > t0
        assert min(abs(t0 - b) for b in beats) < 1e-6
    order = ["intro", "build", "climax", "outro"]
    secs = [s for _, _, s in slots]
    assert [order.index(s) for s in secs] == sorted(order.index(s) for s in secs)
    climax = [t1 - t0 for t0, t1, s in slots if s == "climax"]
    intro = [t1 - t0 for t0, t1, s in slots if s == "intro"]
    assert np.mean(climax) < np.mean(intro)  # cutting faster on the drop
    assert any(abs(t0 - 14.0) < 1e-6 for t0, _, s in slots if s == "climax")


def test_crop_expression_is_valid_ffmpeg(tmp_path):
    expr = _crop_expr([(0.0, 0.1), (0.5, 0.3), (1.0, 0.6)])
    out = tmp_path / "c.mp4"
    cp = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                         "testsrc2=s=640x360:d=1.2:r=25", "-filter_complex",
                         f"[0:v]setpts=PTS-STARTPTS,crop=w=trunc(ih*9/16/2)*2:h=ih:x={expr}:y=0[v]",
                         "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", str(out)],
                        capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr
    w = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
    assert w == "202,360"
