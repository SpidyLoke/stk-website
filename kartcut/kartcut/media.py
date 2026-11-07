"""ffprobe/ffmpeg wrappers: probing, Sony metadata, raw frame decoding, orientation."""

from __future__ import annotations

import json
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import config
from .util import log, popen_flags, run


# --------------------------------------------------------------------------- probe

def _rate(s: str | None) -> float:
    if not s or s in ("0/0", "N/A"):
        return 0.0
    if "/" in s:
        a, b = s.split("/", 1)
        try:
            return float(a) / float(b) if float(b) else 0.0
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


@dataclass
class Probe:
    duration: float = 0.0
    fps: float = 0.0
    width: int = 0
    height: int = 0
    rotation: int = 0          # degrees, as ffprobe reports display matrix rotation
    codec: str = ""
    pix_fmt: str = ""
    bit_depth: int = 8
    has_audio: bool = False
    audio_rate: int = 0
    color_transfer: str = ""
    creation_time: str = ""
    timecode: str = ""
    nb_frames: int = 0
    sar: float = 1.0
    sony: dict = field(default_factory=dict)

    @property
    def display_size(self) -> tuple[int, int]:
        w = int(round(self.width * self.sar))
        h = self.height
        if abs(self.rotation) % 180 == 90:
            return h, w
        return w, h

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["display_w"], d["display_h"] = self.display_size
        return d


def probe(path: Path) -> Probe:
    cp = run([config.ffprobe(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
              str(path)], timeout=60)
    info = json.loads(cp.stdout.decode("utf-8", "replace") or "{}")
    p = Probe()
    fmt = info.get("format", {})
    p.duration = float(fmt.get("duration") or 0.0)
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    p.creation_time = tags.get("creation_time", "")
    p.timecode = tags.get("timecode", "")
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if v:
        p.width = int(v.get("width") or 0)
        p.height = int(v.get("height") or 0)
        p.codec = v.get("codec_name", "")
        p.pix_fmt = v.get("pix_fmt", "")
        p.fps = _rate(v.get("avg_frame_rate")) or _rate(v.get("r_frame_rate"))
        p.nb_frames = int(v.get("nb_frames") or 0)
        p.color_transfer = v.get("color_transfer", "") or ""
        if not p.duration:
            p.duration = float(v.get("duration") or 0.0)
        bd = v.get("bits_per_raw_sample")
        if bd and str(bd).isdigit():
            p.bit_depth = int(bd)
        elif "10" in p.pix_fmt:
            p.bit_depth = 10
        sar = v.get("sample_aspect_ratio", "1:1")
        if sar and ":" in sar and sar != "0:1":
            n, d = sar.split(":")
            try:
                p.sar = float(n) / float(d) if float(d) else 1.0
            except ValueError:
                p.sar = 1.0
        vt = {k.lower(): val for k, val in (v.get("tags") or {}).items()}
        rot = vt.get("rotate")
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = sd["rotation"]
        try:
            p.rotation = int(round(float(rot))) if rot is not None else 0
        except ValueError:
            p.rotation = 0
        p.timecode = p.timecode or vt.get("timecode", "")
    if a:
        p.has_audio = True
        p.audio_rate = int(a.get("sample_rate") or 0)
    p.sony = sony_metadata(Path(path))
    return p


# --------------------------------------------------------------------------- Sony XML

_SONY_GAMMA = {
    "s-log3-cine": "slog3-sgamut3cine",
    "s-log3": "slog3",
    "rec709": "rec709",
    "rec709-xvycc": "rec709",
    "rec2100-hlg": "hlg",
    "hlg": "hlg",
}


def _sony_xml_text(path: Path) -> str | None:
    for cand in (path.with_name(path.stem + "M01.XML"), path.with_name(path.stem + "M01.xml"),
                 path.with_suffix(".XML"), path.with_suffix(".xml")):
        if cand.exists() and cand != path:
            try:
                return cand.read_text("utf-8", errors="replace")
            except OSError:
                pass
    # Sony also embeds the same NonRealTimeMeta XML inside the MP4.
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            chunks = [f.read(2_000_000)]
            if size > 4_000_000:
                f.seek(size - 2_000_000)
                chunks.append(f.read())
        for c in chunks:
            i = c.find(b"<NonRealTimeMeta")
            if i >= 0:
                j = c.find(b"</NonRealTimeMeta>", i)
                if j > i:
                    return c[i:j + 18].decode("utf-8", "replace")
    except OSError:
        pass
    return None


def sony_metadata(path: Path) -> dict:
    txt = _sony_xml_text(path)
    if not txt:
        return {}
    out: dict = {}

    def item(name: str) -> str | None:
        m = re.search(r'<Item\s+name="%s"\s+value="([^"]*)"' % re.escape(name), txt)
        return m.group(1) if m else None

    gamma = item("CaptureGammaEquation")
    prim = item("CaptureColorPrimaries")
    if gamma:
        out["gamma"] = gamma
        key = _SONY_GAMMA.get(gamma.lower())
        if key == "slog3":
            key = "slog3-sgamut3" if prim and prim.lower().replace(".", "") in ("s-gamut3", "sgamut3") else "slog3-sgamut3cine"
        if key:
            out["profile"] = key
    if prim:
        out["primaries"] = prim
    vf = re.search(r"<VideoFrame\b[^>]*>", txt)
    if vf:
        tag = vf.group(0)
        cap = re.search(r'captureFps="([^"]+)"', tag)
        fmt = re.search(r'formatFps="([^"]+)"', tag)
        if cap:
            out["capture_fps"] = _fps_token(cap.group(1))
        if fmt:
            out["format_fps"] = _fps_token(fmt.group(1))
    m = re.search(r'<CreationDate\s+value="([^"]+)"', txt)
    if m:
        out["created"] = m.group(1)
    m = re.search(r'<Device[^>]*modelName="([^"]+)"', txt)
    if m:
        out["camera"] = m.group(1)
    return out


def _fps_token(s: str) -> float:
    m = re.match(r"([\d.]+)", s or "")
    return float(m.group(1)) if m else 0.0


# --------------------------------------------------------------------------- decoding

class FrameReader:
    """Decode a clip to raw BGR frames through an ffmpeg pipe.

    Frames are scaled so the long side is ``long_side`` px, rotated per the clip's
    metadata (ffmpeg autorotate) plus an optional manual ``transpose`` fix.
    """

    def __init__(self, path: Path, probe_: Probe, long_side: int = 1280, fps: float | None = None,
                 transpose: int = 0, start: float | None = None, duration: float | None = None,
                 hwaccel: bool = True):
        self.path = Path(path)
        self.probe = probe_
        dw, dh = probe_.display_size
        if transpose in (90, -90):
            dw, dh = dh, dw
        scale = min(1.0, long_side / max(dw, dh, 1))
        self.w = max(2, int(round(dw * scale / 2)) * 2)
        self.h = max(2, int(round(dh * scale / 2)) * 2)
        self.fps = fps or probe_.fps or 25.0
        self.transpose = transpose
        self.start = start
        self.duration = duration
        self.hwaccel = hwaccel and config.cuda_decode_available()
        self.proc: subprocess.Popen | None = None
        self._stderr: list[bytes] = []

    def _cmd(self, hw: bool) -> list[str]:
        cmd = [config.ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin"]
        if hw:
            cmd += ["-hwaccel", "cuda"]
        if self.start:
            cmd += ["-ss", f"{self.start:.3f}"]
        cmd += ["-i", str(self.path)]
        if self.duration:
            cmd += ["-t", f"{self.duration:.3f}"]
        vf = []
        if self.transpose == 90:
            vf.append("transpose=1")
        elif self.transpose == -90:
            vf.append("transpose=2")
        if self.probe.fps and abs(self.fps - self.probe.fps) > 0.01:
            vf.append(f"fps={self.fps:.4f}")
        vf.append(f"scale={self.w}:{self.h}:flags=area")
        cmd += ["-vf", ",".join(vf), "-an", "-sn", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
        return cmd

    def _drain(self, stream) -> None:
        for line in iter(stream.readline, b""):
            self._stderr.append(line)
            if len(self._stderr) > 50:
                self._stderr.pop(0)

    def __iter__(self):
        frame_bytes = self.w * self.h * 3
        tried_sw = False
        hw = self.hwaccel
        while True:
            self._stderr = []
            self.proc = subprocess.Popen(self._cmd(hw), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         bufsize=frame_bytes * 2, **popen_flags())
            threading.Thread(target=self._drain, args=(self.proc.stderr,), daemon=True).start()
            n = 0
            try:
                while True:
                    buf = self.proc.stdout.read(frame_bytes)
                    if not buf or len(buf) < frame_bytes:
                        break
                    yield n, n / self.fps, np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)
                    n += 1
            finally:
                self.close()
            if n == 0 and hw and not tried_sw:
                log.info("GPU decode gave no frames for %s, retrying on CPU", self.path.name)
                if not self.start:
                    config.mark_cuda_decode_broken()
                hw = False
                tried_sw = True
                continue
            if n == 0:
                err = b"".join(self._stderr).decode("utf-8", "replace").strip()
                raise RuntimeError(f"Could not decode {self.path.name}: {err[-500:] or 'no frames'}")
            return

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass
        if self.proc:
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass


def grab_frames(path: Path, probe_: Probe, times: list[float], long_side: int = 640,
                transpose: int = 0) -> list[np.ndarray]:
    """Single frames at given times (fast seek). Used for orientation checks."""
    out = []
    for t in times:
        try:
            rd = FrameReader(path, probe_, long_side=long_side, transpose=transpose, start=max(0.0, t),
                             duration=0.5, hwaccel=False)
            for _, _, fr in rd:
                out.append(fr.copy())
                break
            rd.close()
        except RuntimeError:
            continue
    return out


# --------------------------------------------------------------------------- orientation

def orientation_from_metadata(p: Probe) -> tuple[str, str]:
    dw, dh = p.display_size
    if abs(p.rotation) % 180 == 90:
        return ("vertical" if dh > dw else "horizontal"), "metadata"
    return ("vertical" if dh > dw else "horizontal"), "dimensions"


def detect_rotation_from_content(frames: list[np.ndarray], detector) -> int:
    """Camera turned on its side but no rotation flag: try 0/90/-90 and keep the upright one.

    People and karts are detected far more confidently when upright. Returns the
    transpose needed (0, 90 or -90).
    """
    import cv2

    if not frames or detector is None or not getattr(detector, "available", False):
        return 0
    scores = {0: 0.0, 90: 0.0, -90: 0.0}
    for fr in frames:
        variants = {0: fr, 90: cv2.rotate(fr, cv2.ROTATE_90_CLOCKWISE),
                    -90: cv2.rotate(fr, cv2.ROTATE_90_COUNTERCLOCKWISE)}
        for k, img in variants.items():
            dets = detector.detect(img)
            scores[k] += sum(d.conf for d in dets if d.cls in ("person", "kart"))
    best = max(scores, key=scores.get)
    if best != 0 and scores[best] >= 0.8 and scores[best] > 2.0 * max(scores[0], 0.05):
        return best
    return 0
