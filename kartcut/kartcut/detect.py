"""AI models: kart/person/trophy detection, pose (celebrations), appearance embeddings, number OCR.

Every model is optional. If one fails to load the rest of the pipeline still runs
and the UI shows which parts are missing.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config
from .util import log


@dataclass
class Det:
    cls: str            # kart | person | trophy
    box: np.ndarray     # x1, y1, x2, y2 in the input image's pixels
    conf: float

    @property
    def w(self) -> float:
        return float(self.box[2] - self.box[0])

    @property
    def h(self) -> float:
        return float(self.box[3] - self.box[1])

    @property
    def cx(self) -> float:
        return float(self.box[0] + self.box[2]) / 2

    @property
    def cy(self) -> float:
        return float(self.box[1] + self.box[3]) / 2


def iou(a: np.ndarray, b: np.ndarray) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return float(inter / ua) if ua > 0 else 0.0


def overlap_frac(a: np.ndarray, b: np.ndarray) -> float:
    """Fraction of box a covered by box b."""
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return float(inter / area)


def nms(dets: list[Det], thr: float = 0.6) -> list[Det]:
    dets = sorted(dets, key=lambda d: -d.conf)
    keep: list[Det] = []
    for d in dets:
        if all(k.cls != d.cls or iou(k.box, d.box) < thr for k in keep):
            keep.append(d)
    return keep


class BaseDetector:
    name = "none"
    available = False

    def detect(self, img_bgr: np.ndarray) -> list[Det]:
        return []


class NullDetector(BaseDetector):
    name = "none"


def _weights(name: str) -> str:
    """Local path for a weights file; ultralytics downloads it there on first use."""
    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return str(config.MODELS_DIR / name)


class WorldDetector(BaseDetector):
    """YOLO-World open-vocabulary detector prompted with 'go-kart'."""

    PROMPTS = ["go-kart", "person", "trophy"]
    MAP = {"go-kart": "kart", "person": "person", "trophy": "trophy"}
    MIN_CONF = {"kart": 0.18, "person": 0.30, "trophy": 0.30}

    def __init__(self, device: str):
        from ultralytics import YOLO

        size = "m" if device.startswith("cuda") else "s"
        cache = config.MODELS_DIR / f"kartcut-world-{size}.pt"
        if cache.exists():
            self.model = YOLO(str(cache))
        else:
            self.model = YOLO(_weights(f"yolov8{size}-worldv2.pt"))
            self.model.set_classes(self.PROMPTS)
            try:
                self.model.save(str(cache))
            except Exception as e:  # cache is an optimisation only
                log.warning("Could not cache custom vocabulary model: %s", e)
        self.device = device
        self.name = f"YOLO-World ({size})"
        self.available = True

    def detect(self, img_bgr: np.ndarray) -> list[Det]:
        r = self.model.predict(img_bgr, imgsz=640, conf=0.12, iou=0.5, verbose=False,
                               device=self.device, max_det=40)[0]
        names = r.names
        out = []
        if r.boxes is None or len(r.boxes) == 0:
            return out
        xyxy = r.boxes.xyxy.cpu().numpy()
        conf = r.boxes.conf.cpu().numpy()
        cls = r.boxes.cls.cpu().numpy().astype(int)
        for b, c, k in zip(xyxy, conf, cls):
            lab = self.MAP.get(str(names.get(int(k), "")), None)
            if lab and c >= self.MIN_CONF[lab]:
                out.append(Det(lab, b.astype(np.float32), float(c)))
        return nms(out)


class CocoDetector(BaseDetector):
    """Fallback: standard COCO YOLO. Karts show up as car/motorcycle or as a seated person."""

    def __init__(self, device: str):
        from ultralytics import YOLO

        size = "m" if device.startswith("cuda") else "s"
        self.model = YOLO(_weights(f"yolo11{size}.pt"))
        self.device = device
        self.name = f"YOLO11 COCO ({size}, fallback)"
        self.available = True

    def detect(self, img_bgr: np.ndarray) -> list[Det]:
        r = self.model.predict(img_bgr, imgsz=640, conf=0.2, iou=0.5, verbose=False, device=self.device,
                               classes=[0, 2, 3], max_det=40)[0]
        if r.boxes is None or len(r.boxes) == 0:
            return []
        xyxy = r.boxes.xyxy.cpu().numpy()
        conf = r.boxes.conf.cpu().numpy()
        cls = r.boxes.cls.cpu().numpy().astype(int)
        karts = [Det("kart", b.astype(np.float32), float(c)) for b, c, k in zip(xyxy, conf, cls) if k in (2, 3)]
        out = list(karts)
        H, W = img_bgr.shape[:2]
        for b, c, k in zip(xyxy, conf, cls):
            if k != 0:
                continue
            d = Det("person", b.astype(np.float32), float(c))
            if any(overlap_frac(d.box, kd.box) > 0.4 for kd in karts):
                continue
            if d.w / max(d.h, 1) > 0.75 and d.h < 0.5 * H:  # seated driver, no kart box found
                x1, y1, x2, y2 = d.box
                eb = np.array([x1 - 0.35 * d.w, y1, x2 + 0.35 * d.w, y2 + 0.45 * d.h], np.float32)
                eb[[0, 2]] = np.clip(eb[[0, 2]], 0, W)
                eb[[1, 3]] = np.clip(eb[[1, 3]], 0, H)
                out.append(Det("kart", eb, float(c) * 0.8))
            else:
                out.append(d)
        return nms(out)


class PoseModel:
    """Arms-raised detection for celebrations, podiums and chequered-flag waves."""

    def __init__(self, device: str):
        from ultralytics import YOLO

        self.model = YOLO(_weights("yolo11s-pose.pt"))
        self.device = device
        self.available = True

    def keypoints(self, img_bgr: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
        r = self.model.predict(img_bgr, imgsz=640, conf=0.3, verbose=False, device=self.device,
                               max_det=20)[0]
        if r.keypoints is None or r.boxes is None or len(r.boxes) == 0:
            return []
        boxes = r.boxes.xyxy.cpu().numpy()
        xy = r.keypoints.xy.cpu().numpy()
        kc = r.keypoints.conf.cpu().numpy() if r.keypoints.conf is not None else np.ones(xy.shape[:2])
        return [(boxes[i], np.concatenate([xy[i], kc[i][:, None]], axis=1)) for i in range(len(boxes))]


def arms_raised(kp: np.ndarray, box: np.ndarray) -> float:
    """0 = no, 0.6 = one arm above the head, 1.0 = both. COCO keypoints (17 x [x, y, conf])."""
    h = max(1.0, float(box[3] - box[1]))
    nose = kp[0]
    head_y = nose[1] if nose[2] > 0.3 else None
    if head_y is None:
        eyes = [kp[i] for i in (1, 2) if kp[i][2] > 0.3]
        if eyes:
            head_y = float(np.mean([e[1] for e in eyes]))
        else:
            sh = [kp[i] for i in (5, 6) if kp[i][2] > 0.3]
            if not sh:
                return 0.0
            head_y = float(np.mean([s[1] for s in sh])) - 0.12 * h
    up = 0
    for wi in (9, 10):
        w = kp[wi]
        if w[2] > 0.35 and w[1] < head_y - 0.05 * h:
            up += 1
    return {0: 0.0, 1: 0.6, 2: 1.0}[up]


class Embedder:
    """CLIP image embeddings: a strong 'does this kart/helmet look like that one' signal."""

    def __init__(self, device: str):
        import clip  # installed alongside YOLO-World
        import torch

        try:
            from ultralytics.utils import WEIGHTS_DIR

            root = str(Path(WEIGHTS_DIR) / "clip")
        except Exception:
            root = str(config.MODELS_DIR / "clip")
        dev = device if device.startswith("cuda") else "cpu"
        self.model, self.pre = clip.load("ViT-B/32", device=dev, download_root=root)
        self.model.eval()
        self.torch = torch
        self.device = dev
        self.available = True

    def embed(self, crops_bgr: list[np.ndarray]) -> np.ndarray:
        from PIL import Image

        if not crops_bgr:
            return np.zeros((0, 512), np.float32)
        ims = [self.pre(Image.fromarray(c[:, :, ::-1].copy())) for c in crops_bgr]
        with self.torch.no_grad():
            x = self.torch.stack(ims).to(self.device)
            f = self.model.encode_image(x).float()
            f = f / f.norm(dim=-1, keepdim=True).clamp_min(1e-6)
        return f.cpu().numpy().astype(np.float32)


_OCR_FIX = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "I": "1", "l": "1", "|": "1", "i": "1",
                          "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2", "G": "6", "b": "6", "q": "9", "g": "9"})


class NumberReader:
    """Reads kart race numbers from crops with RapidOCR (models ship inside the pip package)."""

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR

        self.engine = RapidOCR()
        self.available = True
        self.lock = threading.Lock()

    def read(self, crop_bgr: np.ndarray) -> list[tuple[str, float]]:
        import cv2

        from .metrics import normalise_brightness

        h, w = crop_bgr.shape[:2]
        if h < 24 or w < 24:
            return []
        crop_bgr = normalise_brightness(crop_bgr, 235.0)
        scale = 1.0
        if h < 200:
            scale = 200.0 / h
        img = cv2.resize(crop_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC) \
            if scale != 1.0 else crop_bgr
        with self.lock:
            try:
                res, _ = self.engine(img)
            except Exception as e:
                log.debug("OCR failed: %s", e)
                return []
        out = []
        for item in res or []:
            text, score = str(item[1]), float(item[2])
            box = np.array(item[0], dtype=np.float32)
            th = float(box[:, 1].max() - box[:, 1].min()) / max(1.0, img.shape[0])
            cleaned = parse_number(text)
            if cleaned:
                # Bigger characters relative to the kart = more trustworthy read.
                out.append((cleaned, score * min(1.0, 0.4 + th * 3.0)))
        if not out:
            out = self._plates(crop_bgr)
        return out

    def _plates(self, crop_bgr: np.ndarray) -> list[tuple[str, float]]:
        """Fallback for short numbers the text detector skips (single digits especially):
        find white/yellow number plates, crop tight around the digits, recognise directly."""
        import cv2

        rec = getattr(self.engine, "text_recognizer", None)
        if rec is None:
            return []
        h, w = crop_bgr.shape[:2]
        hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)
        white = (hsv[..., 1] < 60) & (hsv[..., 2] > 165)
        yellow = (hsv[..., 0] >= 18) & (hsv[..., 0] <= 35) & (hsv[..., 1] > 90) & (hsv[..., 2] > 140)
        out: list[tuple[str, float]] = []
        for m in (white, yellow):
            mm = cv2.morphologyEx(m.astype(np.uint8) * 255, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
            cnts, _ = cv2.findContours(mm, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for c in cnts:
                x, y, pw, ph = cv2.boundingRect(c)
                if pw * ph < 0.012 * h * w or ph < 12 or not (0.5 <= pw / max(1, ph) <= 4.0):
                    continue
                panel = crop_bgr[y:y + ph, x:x + pw]
                g = cv2.cvtColor(panel, cv2.COLOR_BGR2GRAY)
                _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                ink = bw == 0 if (bw == 0).mean() < 0.5 else bw == 255
                frac = ink.mean()
                if not (0.03 <= frac <= 0.5):
                    continue
                ys, xs = np.nonzero(ink[1:-1, 1:-1])
                if len(xs) < 10:
                    continue
                gx1, gx2, gy1, gy2 = xs.min() + 1, xs.max() + 1, ys.min() + 1, ys.max() + 1
                gw, gh = gx2 - gx1 + 1, gy2 - gy1 + 1
                if gh < 0.35 * ph:
                    continue
                padx, pady = int(gw * 0.3) + 2, int(gh * 0.25) + 2
                canvas = np.full((ph + 2 * pady, pw + 2 * padx), 255, np.uint8)
                canvas[pady:pady + ph, padx:padx + pw] = np.where(ink, 0, 255).astype(np.uint8)
                tight = canvas[gy1:gy2 + 2 * pady + 1, gx1:gx2 + 2 * padx + 1]
                tight = cv2.resize(tight, (max(8, int(tight.shape[1] * 48 / tight.shape[0])), 48),
                                   interpolation=cv2.INTER_CUBIC)
                with self.lock:
                    try:
                        res, _ = rec([cv2.cvtColor(tight, cv2.COLOR_GRAY2BGR)])
                    except Exception:
                        continue
                for text, score in res or []:
                    num = parse_number(str(text))
                    if num and float(score) > 0.25:
                        out.append((num, float(score) * 0.85))
        return out


def parse_number(text: str) -> str | None:
    import unicodedata

    t = unicodedata.normalize("NFKC", text).strip().replace(" ", "")  # full-width "７" -> "7"
    if not t:
        return None
    digits_like = sum(ch.isdigit() for ch in t)
    if digits_like == 0 or digits_like < len(t) / 2:
        return None
    t = t.translate(_OCR_FIX)
    t = re.sub(r"\D", "", t)
    if not (1 <= len(t) <= 3):
        return None
    return str(int(t))  # strips leading zeros: "07" -> "7"


class Models:
    """Lazy holder so models load once per app run, on first analysis."""

    def __init__(self, settings: dict):
        self.settings = settings
        self.device = config.torch_device()
        self._lock = threading.Lock()
        self._detector: BaseDetector | None = None
        self._pose = None
        self._embedder = None
        self._ocr = None
        self.status: dict[str, str] = {}

    @property
    def detector(self) -> BaseDetector:
        with self._lock:
            if self._detector is None:
                self._detector = self._load_detector()
            return self._detector

    def _load_detector(self) -> BaseDetector:
        choice = self.settings.get("detector", "auto")
        order = {"auto": ["world", "coco"], "world": ["world"], "coco": ["coco"], "none": []}.get(choice, ["world", "coco"])
        for kind in order:
            try:
                det = WorldDetector(self.device) if kind == "world" else CocoDetector(self.device)
                self.status["detector"] = det.name
                log.info("Detector: %s on %s", det.name, self.device)
                return det
            except Exception as e:
                self.status[f"detector_{kind}_error"] = f"{type(e).__name__}: {e}"
                log.warning("Detector %s unavailable: %s", kind, e)
        self.status["detector"] = "none"
        return NullDetector()

    @property
    def pose(self):
        with self._lock:
            if self._pose is None:
                try:
                    self._pose = PoseModel(self.device)
                    self.status["pose"] = "YOLO11 pose"
                except Exception as e:
                    self._pose = False
                    self.status["pose"] = f"unavailable: {e}"
            return self._pose or None

    @property
    def embedder(self):
        with self._lock:
            if self._embedder is None:
                if not self.settings.get("appearance_embeddings", True):
                    self._embedder = False
                else:
                    try:
                        self._embedder = Embedder(self.device)
                        self.status["embedder"] = "CLIP ViT-B/32"
                    except Exception as e:
                        self._embedder = False
                        self.status["embedder"] = f"unavailable: {e}"
            return self._embedder or None

    @property
    def ocr(self):
        with self._lock:
            if self._ocr is None:
                if not self.settings.get("ocr", True):
                    self._ocr = False
                else:
                    try:
                        self._ocr = NumberReader()
                        self.status["ocr"] = "RapidOCR"
                    except Exception as e:
                        self._ocr = False
                        self.status["ocr"] = f"unavailable: {e}"
            return self._ocr or None
