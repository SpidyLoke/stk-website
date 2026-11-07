"""Small shared helpers: JSON on disk, ids, subprocess flags, logging."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("kartcut")

IS_WINDOWS = os.name == "nt"
VIDEO_EXTS = {".mp4", ".mov", ".mxf", ".mts", ".m2ts", ".m4v", ".avi", ".mkv", ".insv", ".lrv"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".aif", ".aiff"}
LUT_EXTS = {".cube"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
# Proxy/low-res sidecars that cameras write next to the real clip.
SKIP_VIDEO_EXTS = {".lrv"}

_json_lock = threading.Lock()


def setup_logging(log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        log.warning("Corrupt JSON at %s, ignoring", path)
        return default


def write_json(path: Path, data: Any) -> None:
    """Atomic write so a crash mid-save never leaves a half-written project."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _json_lock:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"), default=_json_default)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _json_default(o: Any) -> Any:
    try:
        import numpy as np

        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, np.bool_):
            return bool(o)
    except ImportError:
        pass
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"Not JSON serialisable: {type(o)}")


def clip_id_for(path: Path, size: int, mtime: float) -> str:
    h = hashlib.sha1(f"{Path(path).resolve()}|{size}|{int(mtime)}".encode("utf-8")).hexdigest()
    return h[:12]


def short_id(prefix: str = "") -> str:
    return prefix + hashlib.sha1(f"{time.time_ns()}{os.getpid()}{threading.get_ident()}".encode()).hexdigest()[:8]


def popen_flags() -> dict:
    """Hide the console window that Windows pops up for every ffmpeg call."""
    if IS_WINDOWS:
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def run(cmd: list[str], timeout: float | None = None, check: bool = True,
        cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    log.debug("run: %s", " ".join(map(str, cmd)))
    cp = subprocess.run(
        [str(c) for c in cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        **popen_flags(),
    )
    if check and cp.returncode != 0:
        tail = cp.stderr.decode("utf-8", "replace")[-2000:]
        raise RuntimeError(f"{Path(str(cmd[0])).name} failed ({cp.returncode}): {tail}")
    return cp


def fmt_dur(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(int(m), 60)
    if h:
        return f"{h}:{m:02d}:{s:04.1f}"
    return f"{m}:{s:04.1f}"


def safe_name(s: str, maxlen: int = 80) -> str:
    bad = '<>:"/\\|?*'
    out = "".join("_" if (c in bad or ord(c) < 32) else c for c in s).strip(" .")
    return (out or "untitled")[:maxlen]


def open_in_os(path: Path) -> None:
    path = Path(path)
    if IS_WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class Cancelled(Exception):
    """Raised inside a job when the user presses Cancel."""
