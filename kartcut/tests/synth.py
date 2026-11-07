"""Synthetic karting shoot for end-to-end tests.

Renders S-Log3-encoded clips with numbered karts (real OCR runs on them), plus a
detector that reads ground truth from a tiny barcode in each frame's corner, so the
whole pipeline can be exercised without downloading AI models.
"""

from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import cv2
import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kartcut import colour as C  # noqa: E402
from kartcut.detect import BaseDetector, Det  # noqa: E402

W, H, FPS = 1280, 720, 25
BAR_BITS = 20
BAR_SQ = 16

KARTS = {
    "23": {"body": (40, 40, 200), "suit": (200, 60, 20), "helmet": (230, 230, 30)},   # red kart, blue suit, cyan helmet
    "7": {"body": (20, 200, 240), "suit": (30, 30, 30), "helmet": (250, 250, 250)},    # yellow kart, black suit, white helmet
    "12": {"body": (60, 170, 30), "suit": (240, 240, 240), "helmet": (20, 20, 220)},   # green kart, white suit, red helmet
}


def background(width: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    bg = np.zeros((H, width, 3), np.uint8)
    sky_h, grass_h = int(H * 0.30), int(H * 0.45)
    for y in range(sky_h):
        bg[y] = (235 - y // 3, 200 - y // 4, 150 - y // 6)
    bg[sky_h:grass_h] = (40, 150, 60)
    bg[grass_h:] = (100, 100, 102)
    noise = rng.normal(0, 14, (H, width, 1)).astype(np.int16)
    bg = np.clip(bg.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    for x in range(0, width, 60):  # kerb
        col = (40, 40, 220) if (x // 60) % 2 else (240, 240, 240)
        cv2.rectangle(bg, (x, grass_h - 6), (x + 60, grass_h + 14), col, -1)
    for _ in range(width // 6):  # blobs for optical-flow features
        x, y = int(rng.integers(0, width)), int(rng.integers(0, H))
        r = int(rng.integers(3, 12))
        c = tuple(int(v) for v in rng.integers(20, 240, 3))
        cv2.circle(bg, (x, y), r, c, -1)
    return bg


def draw_kart(img: np.ndarray, num: str, cx: float, cy: float, s: float, facing: int = 1) -> list[float]:
    k = KARTS[num]
    bw, bh = 220 * s, 64 * s
    x1, y1 = int(cx - bw / 2), int(cy - bh / 2)
    x2, y2 = int(cx + bw / 2), int(cy + bh / 2)
    for wx in (x1 + int(18 * s), x2 - int(58 * s)):
        cv2.rectangle(img, (wx, y2 - int(14 * s)), (wx + int(40 * s), y2 + int(14 * s)), (15, 15, 15), -1)
    cv2.rectangle(img, (x1, y1), (x2, y2), k["body"], -1)
    tx1 = int(cx - 30 * s * facing) - int(28 * s)
    cv2.rectangle(img, (tx1, y1 - int(70 * s)), (tx1 + int(56 * s), y1 + int(6 * s)), k["suit"], -1)
    cv2.circle(img, (tx1 + int(28 * s), y1 - int(92 * s)), int(30 * s), k["helmet"], -1)
    px1 = int(cx + 25 * s * facing) - int(45 * s)
    cv2.rectangle(img, (px1, y1 + int(6 * s)), (px1 + int(90 * s), y2 - int(6 * s)), (250, 250, 250), -1)
    fs = 1.5 * s
    (tw, th), _ = cv2.getTextSize(num, cv2.FONT_HERSHEY_DUPLEX, fs, max(2, int(4 * s)))
    cv2.putText(img, num, (px1 + int(45 * s) - tw // 2, int(cy) + th // 2), cv2.FONT_HERSHEY_DUPLEX, fs,
                (10, 10, 10), max(2, int(4 * s)), cv2.LINE_AA)
    return [float(x1), float(y1 - 125 * s), float(x2), float(y2 + 14 * s)]


def stamp(img: np.ndarray, clip_no: int, frame: int) -> None:
    code = (clip_no << 12) | (frame & 0xFFF)
    for b in range(BAR_BITS):
        v = 255 if (code >> (BAR_BITS - 1 - b)) & 1 else 0
        img[0:BAR_SQ, b * BAR_SQ:(b + 1) * BAR_SQ] = v
    img[BAR_SQ:BAR_SQ + 4, 0:BAR_BITS * BAR_SQ] = 128


def read_stamp(img: np.ndarray) -> tuple[int, int] | None:
    h, w = img.shape[:2]
    sq = BAR_SQ * w / W
    if h < sq * 2:
        return None
    code = 0
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    for b in range(BAR_BITS):
        x = int((b + 0.5) * sq)
        y = int(0.5 * sq)
        code = (code << 1) | (1 if g[y, x] > 110 else 0)
    return code >> 12, code & 0xFFF


def encode(display_bgr: np.ndarray, profile: str, ev: float = 0.0, gains=(1.0, 1.0, 1.0)) -> np.ndarray:
    lin = (display_bgr.astype(np.float32) / 255.0) ** 2.2 * (0.18 / 0.2) * (2.0 ** ev)
    lin = lin * np.array(gains[::-1], np.float32)  # gains given RGB, image is BGR
    if profile == "rec709":
        out = C.linear_to_rec709(np.clip(lin, 0, 1))
    else:
        out = C.linear_to_slog3(lin)
    return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)


class Writer:
    def __init__(self, path: Path, fps: int = FPS, size=(W, H), rotate: int | None = None, audio: bool = True):
        self.path = Path(path)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-s", f"{size[0]}x{size[1]}", "-r", str(fps), "-i", "pipe:0"]
        if audio:
            cmd += ["-f", "lavfi", "-i", "anoisesrc=color=brown:amplitude=0.2:sample_rate=48000", "-shortest",
                    "-c:a", "aac", "-b:a", "96k"]
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-pix_fmt", "yuv420p",
                "-color_range", "tv", str(self.path if rotate is None else self.path.with_suffix(".tmp.mp4"))]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        self.rotate = rotate

    def write(self, img: np.ndarray) -> None:
        self.proc.stdin.write(np.ascontiguousarray(img).tobytes())

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait()
        if self.rotate is not None:
            tmp = self.path.with_suffix(".tmp.mp4")
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-display_rotation",
                            str(self.rotate), "-i", str(tmp), "-c", "copy", str(self.path)], check=True)
            tmp.unlink()


def make_shoot(out: Path) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    gt: dict[str, dict[str, list]] = {}
    bg_static = background(W, 1)
    bg_wide = background(W * 4, 2)

    def clip(no: int, name: str, seconds: float, fn, profile="slog3", ev=0.0, gains=(1, 1, 1), rotate=None,
             blur=0, shake=0.0, fps=FPS):
        path = out / name
        wr = Writer(path, fps=fps, rotate=rotate)
        n = int(seconds * fps)
        rng = np.random.default_rng(no)
        g = gt.setdefault(str(no), {})
        for f in range(n):
            t = f / fps
            img, boxes = fn(t)
            if shake:
                dx, dy = rng.normal(0, shake, 2)
                M = np.float32([[1, 0, dx], [0, 1, dy]])
                img = cv2.warpAffine(img, M, (W, H), borderMode=cv2.BORDER_REFLECT)
                boxes = [(c, [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]) for c, b in boxes]
            if blur:
                img = cv2.GaussianBlur(img, (0, 0), blur)
            stamp(img, no, f)
            g[str(f)] = [{"cls": c, "box": b} for c, b in boxes]
            wr.write(encode(img, profile, ev, gains))
        wr.close()
        return path

    def battle(t):  # 23 passes 7 on the straight
        img = bg_static.copy()
        x7 = 150 + 190 * t
        x23 = 20 + 260 * t
        b7 = draw_kart(img, "7", x7, 470, 0.95)
        b23 = draw_kart(img, "23", x23, 600, 1.0)
        return img, [("kart", b7), ("kart", b23)]

    def pan23(t):
        off = int(700 * t)
        bgc = bg_wide[:, off:off + W].copy()
        bgc = cv2.blur(bgc, (31, 1))
        b = draw_kart(bgc, "23", W / 2 + 10 * np.sin(t * 3), 560, 1.25)
        return bgc, [("kart", b)]

    def solo7(t):
        img = bg_static.copy()
        b = draw_kart(img, "7", 100 + 230 * t, 560, 1.1)
        return img, [("kart", b)]

    def solo12(t):
        img = bg_static.copy()
        b = draw_kart(img, "12", 1150 - 240 * t, 560, 1.1, facing=-1)
        return img, [("kart", b)]

    def solo23(t):
        img = bg_static.copy()
        b = draw_kart(img, "23", 150 + 220 * t, 560, 1.15)
        return img, [("kart", b)]

    def black(t):
        return np.zeros((H, W, 3), np.uint8), []

    def empty(t):
        return bg_static.copy(), []

    clip(1, "C0001.MP4", 6.0, battle)
    clip(2, "C0002.MP4", 5.0, pan23)
    clip(3, "C0003.MP4", 5.0, solo7, ev=-2.0)
    clip(4, "C0004.MP4", 4.0, solo12, shake=6.0, gains=(1.25, 1.0, 0.8))
    clip(5, "C0005.MP4", 3.0, black)
    clip(6, "C0006.MP4", 0.6, solo7)
    clip(7, "C0007.MP4", 5.0, solo7, ev=-2.0)                      # duplicate of C0003
    clip(8, "C0008.MP4", 4.0, solo23, rotate=-90)                  # vertical via rotation metadata
    clip(9, "PHONE_0009.MP4", 4.0, solo23, profile="rec709")       # standard profile, no Sony XML
    clip(10, "C0010.MP4", 5.0, empty)                              # B-roll, no karts
    clip(11, "C0011.MP4", 5.0, solo23, blur=4)                     # soft focus
    clip(12, "C0012.MP4", 4.0, solo23, fps=50)                     # high frame rate, slow-mo candidate
    for no in (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12):
        nm = f"C{no:04d}"
        cap = "50p" if no == 12 else "25p"
        (out / f"{nm}M01.XML").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<NonRealTimeMeta xmlns="urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.20">'
            f'<CreationDate value="2026-11-07T10:{no:02d}:00+08:00"/><VideoFormat><VideoFrame videoCodec="AVC_3840_2160_HP@L51" '
            f'captureFps="{cap}" formatFps="{cap}"/></VideoFormat><Device manufacturer="Sony" modelName="ILME-FX3"/>'
            '<AcquisitionRecord><Group name="CameraUnitMetadataSet"><Item name="CaptureGammaEquation" value="s-log3-cine"/>'
            '<Item name="CaptureColorPrimaries" value="S-Gamut3Cine"/></Group></AcquisitionRecord></NonRealTimeMeta>',
            encoding="utf-8")
    (out / "ground_truth.json").write_text(json.dumps(gt), encoding="utf-8")
    return gt


def make_music(path: Path, seconds: float = 40.0, bpm: float = 120.0, drop: float = 12.0) -> Path:
    sr = 44100
    n = int(seconds * sr)
    y = np.zeros(n, np.float32)
    period = 60.0 / bpm
    click = np.sin(2 * np.pi * 900 * np.arange(int(0.03 * sr)) / sr) * np.exp(-np.arange(int(0.03 * sr)) / 300)
    kick = np.sin(2 * np.pi * 60 * np.arange(int(0.12 * sr)) / sr) * np.exp(-np.arange(int(0.12 * sr)) / 2500)
    for i, t in enumerate(np.arange(0, seconds, period)):
        s = int(t * sr)
        amp = 0.25 if t < drop else 0.9
        snd = kick if i % 4 == 0 else click
        e = min(n, s + len(snd))
        y[s:e] += amp * snd[:e - s]
    y += (np.random.default_rng(0).normal(0, 0.01, n) * np.where(np.arange(n) / sr > drop, 3.0, 1.0)).astype(np.float32)
    y = np.clip(y, -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((y * 32767).astype(np.int16).tobytes())
    return path


class SynthDetector(BaseDetector):
    """Reads the corner barcode and returns ground-truth boxes, scaled to the input image."""

    name = "synthetic ground truth"
    available = True

    def __init__(self, gt: dict):
        self.gt = gt

    def detect(self, img_bgr: np.ndarray) -> list[Det]:
        st = read_stamp(img_bgr)
        if st is None:
            return []
        clip_no, frame = st
        items = self.gt.get(str(clip_no), {}).get(str(frame))
        if not items:
            return []
        h, w = img_bgr.shape[:2]
        s = w / W
        if abs(h / w - H / W) > 0.05:
            return []
        return [Det(i["cls"], np.array(i["box"], np.float32) * s, 0.9) for i in items]
