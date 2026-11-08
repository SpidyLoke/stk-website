"""Start KartCut: python run.py  (opens http://127.0.0.1:8765 in your browser)."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
# Model downloads (YOLO weights, CLIP) go under the app folder, on the same drive as KartCut.
os.chdir(APP)
os.environ.setdefault("YOLO_CONFIG_DIR", str(APP / "models" / "ultralytics"))

from kartcut import config  # noqa: E402
from kartcut.util import setup_logging  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="KartCut")
    ap.add_argument("--port", type=int, default=int(os.environ.get("KARTCUT_PORT", 8765)))
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--project", help="open this project folder on start")
    ap.add_argument("--prepare", action="store_true", help="download and test every AI model, then exit")
    args = ap.parse_args()

    setup_logging(config.HOME_DIR / "logs" / "kartcut.log")
    if args.prepare:
        prepare()
        return
    import uvicorn

    from kartcut import server

    if args.project:
        server.open_project(Path(args.project))
    else:
        server.startup_open_last()
    config.log_tools()
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()
    print(f"\n  KartCut is running at {url}\n  Close this window to stop it.\n")
    uvicorn.run(server.app, host="127.0.0.1", port=args.port, log_level="warning")


def prepare() -> None:
    """Fetch all models now, so the first analysis at the track works without internet."""
    import numpy as np

    from kartcut.detect import Models

    print(f"Device: {config.torch_device()} {config.gpu_name()}")
    m = Models(config.default_settings())
    img = np.zeros((360, 640, 3), np.uint8)
    det = m.detector
    det.detect(img)
    print("Detector:", m.status.get("detector"), m.status.get("detector_world_error", ""))
    if m.pose is not None:
        m.pose.keypoints(img)
    print("Pose:", m.status.get("pose"))
    if m.embedder is not None:
        m.embedder.embed([img[:64, :64]])
    print("Appearance:", m.status.get("embedder"))
    print("OCR:", "ready" if m.ocr is not None else m.status.get("ocr"))
    print("FFmpeg:", config.system_status().get("ffmpeg"), "| NVENC:", config.nvenc_available())


if __name__ == "__main__":
    main()
