"""App-level paths, tool discovery and default settings."""

from __future__ import annotations

import copy
import functools
import os
import shutil
from pathlib import Path

from .util import IS_WINDOWS, log, run

APP_DIR = Path(__file__).resolve().parent.parent  # .../kartcut
MODELS_DIR = Path(os.environ.get("KARTCUT_MODELS", APP_DIR / "models"))
# Projects, logs and recent list live next to the app, so they sit on whatever drive KartCut is on.
HOME_DIR = Path(os.environ.get("KARTCUT_HOME", APP_DIR / "projects"))

DEFAULT_SETTINGS: dict = {
    # Footage
    "footage_profile": "slog3-sgamut3cine",  # used when a clip has no readable metadata
    "analysis_long_side": 1280,               # px, long side of frames used for analysis
    "analysis_fps": 0,                        # 0 = auto (8 on GPU, 4 on CPU)
    "motion_fps_cap": 50,                     # shake analysis never decodes faster than this
    "make_proxies": True,
    "proxy_height": 540,
    "detect_orientation_from_content": True,
    # AI
    "detector": "auto",                       # auto | world | coco | none
    "ocr": True,
    "appearance_embeddings": True,
    "learn_from_corrections": True,
    # Driver ID thresholds
    "id_min_prob": 0.55,
    # Junk filter
    "junk_min_duration": 1.0,
    # Colour
    "match_exposure_strength": 1.0,
    "match_wb_strength": 1.0,
    # Montage
    "event_length": 60.0,
    "reel_length": 30.0,
    "aspects": ["16:9"],
    "include_incidents": False,
    "slow_motion": True,
    "engine_mix": 0.25,
    "music_path": "",
    "music_start": 0.0,
    "output_fps": 0,                          # 0 = most common clip frame rate
    "look": "builtin",                        # builtin | custom | log
    "custom_lut": "",                         # .cube, S-Log3 in, Rec.709 out
    "encoder": "auto",                        # auto | nvenc | x264
    "seed": 1,
}


def default_settings() -> dict:
    return copy.deepcopy(DEFAULT_SETTINGS)


def _bin_candidates(name: str) -> list[Path]:
    exe = name + (".exe" if IS_WINDOWS else "")
    return [
        APP_DIR / "bin" / "ffmpeg" / "bin" / exe,
        APP_DIR / "bin" / exe,
    ]


@functools.lru_cache(maxsize=None)
def tool_path(name: str) -> str:
    env = os.environ.get(f"KARTCUT_{name.upper()}")
    if env and Path(env).exists():
        return env
    for c in _bin_candidates(name):
        if c.exists():
            return str(c)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(
        f"{name} not found. Run setup_windows.bat again, or install FFmpeg and add it to PATH."
    )


def ffmpeg() -> str:
    return tool_path("ffmpeg")


def ffprobe() -> str:
    return tool_path("ffprobe")


@functools.lru_cache(maxsize=None)
def nvenc_available() -> bool:
    try:
        cp = run(
            [ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"],
            timeout=30, check=False,
        )
        return cp.returncode == 0
    except Exception:
        return False


_CUDA_DECODE_BROKEN = False


def mark_cuda_decode_broken() -> None:
    """ffmpeg lists cuda even on machines where it can't decode; stop trying after one failure."""
    global _CUDA_DECODE_BROKEN
    _CUDA_DECODE_BROKEN = True


def cuda_decode_available() -> bool:
    return (not _CUDA_DECODE_BROKEN) and _cuda_hwaccel_listed()


@functools.lru_cache(maxsize=None)
def _cuda_hwaccel_listed() -> bool:
    try:
        cp = run([ffmpeg(), "-hide_banner", "-hwaccels"], timeout=15, check=False)
        return b"cuda" in cp.stdout
    except Exception:
        return False


@functools.lru_cache(maxsize=None)
def torch_device() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda:0"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


@functools.lru_cache(maxsize=None)
def gpu_name() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except Exception:
        pass
    return ""


def system_status() -> dict:
    status = {"ffmpeg": None, "nvenc": False, "cuda_decode": False, "device": "cpu", "gpu": ""}
    try:
        status["ffmpeg"] = ffmpeg()
        status["nvenc"] = nvenc_available()
        status["cuda_decode"] = cuda_decode_available()
    except FileNotFoundError as e:
        status["ffmpeg_error"] = str(e)
    status["device"] = torch_device()
    status["gpu"] = gpu_name()
    try:
        import ultralytics  # noqa: F401

        status["ultralytics"] = True
    except Exception:
        status["ultralytics"] = False
    try:
        import rapidocr_onnxruntime  # noqa: F401

        status["ocr"] = True
    except Exception:
        status["ocr"] = False
    return status


def resolved_analysis_fps(settings: dict) -> float:
    v = float(settings.get("analysis_fps") or 0)
    if v > 0:
        return v
    return 8.0 if torch_device().startswith("cuda") else 4.0


def log_tools() -> None:
    s = system_status()
    log.info("System: %s", s)
