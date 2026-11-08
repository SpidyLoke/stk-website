"""Appearance fingerprints per kart track, computed from the saved kart crop.

Runs after analysis (and on demand via "Re-match karts"), so better matching
never needs a re-decode of the footage.

fp2: colour histogram with the surroundings (asphalt, grass, kerbs, sky) removed,
     split into the driver zone (helmet + suit) and the kart zone.
emb / emb_top: CLIP embeddings of the whole crop and of the helmet + suit area.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .metrics import normalise_brightness
from .util import log

FP_VERSION = 2
_W, _H = 80, 64


def _codes(hsv: np.ndarray) -> np.ndarray:
    """54 colour codes: 16 hues x 3 saturations for coloured pixels, 6 levels for black/grey/white."""
    hh, ss, vv = hsv[..., 0].astype(np.int32), hsv[..., 1].astype(np.int32), hsv[..., 2].astype(np.int32)
    chrom = (ss > 60) & (vv > 40)
    hb = np.clip(hh * 16 // 180, 0, 15)
    sb = np.clip((ss - 60) * 3 // 196, 0, 2)
    vb = np.clip(vv * 6 // 256, 0, 5)
    return np.where(chrom, hb * 3 + sb, 48 + vb)


_ELL = None


def _ellipse() -> np.ndarray:
    global _ELL
    if _ELL is None:
        yy, xx = np.mgrid[0:_H, 0:_W]
        _ELL = (((xx - _W / 2 + 0.5) / (0.5 * _W)) ** 2 + ((yy - _H / 2 + 0.5) / (0.5 * _H)) ** 2) <= 1.0
    return _ELL


def fp2_from_crop(crop_bgr: np.ndarray) -> np.ndarray | None:
    if crop_bgr is None or crop_bgr.size == 0:
        return None
    img = normalise_brightness(cv2.resize(crop_bgr, (_W, _H), interpolation=cv2.INTER_AREA))
    codes = _codes(cv2.cvtColor(img, cv2.COLOR_BGR2HSV))
    inside = _ellipse()
    # The box corners are almost always track, grass or sky: use them as the background model.
    bg = np.bincount(codes[~inside], minlength=54).astype(np.float64)
    fg = np.bincount(codes[inside], minlength=54).astype(np.float64)
    bg /= max(1.0, bg.sum())
    fg /= max(1.0, fg.sum())
    w_code = np.clip(1.0 - bg / (fg + 1e-6), 0.0, 1.0)
    pw = w_code[codes] * inside
    out = []
    for y0, y1 in ((0, int(_H * 0.55)), (int(_H * 0.45), _H)):
        z_codes = codes[y0:y1].ravel()
        z_w = pw[y0:y1].ravel()
        if z_w.sum() < 0.05 * inside[y0:y1].sum():  # everything looked like background: don't trust weights
            z_w = inside[y0:y1].ravel().astype(np.float64)
        hz = np.bincount(z_codes, weights=z_w, minlength=54)
        out.append(hz / max(1e-6, hz.sum()))
    return (np.concatenate(out) / 2.0).astype(np.float32)


def refresh(project, models=None, progress=None, force: bool = False) -> int:
    """Add fp2 (+ CLIP embeddings when available) to every track that lacks them."""
    embedder = None
    if models is not None:
        try:
            embedder = models.embedder
        except Exception as e:  # CLIP is optional
            log.warning("Appearance model unavailable: %s", e)
    ans = project.all_analyses()
    updated = 0
    for i, (cid, an) in enumerate(ans.items()):
        if progress:
            progress(i / max(1, len(ans)), f"Fingerprinting karts ({i + 1}/{len(ans)})")
        changed = False
        todo = []
        for tr in an.get("tracks", []):
            if not tr.get("crop"):
                continue
            need_fp = force or tr.get("fp_v") != FP_VERSION or not tr.get("fp2")
            need_emb = embedder is not None and (force or not tr.get("emb") or not tr.get("emb_top"))
            if not (need_fp or need_emb):
                continue
            img = cv2.imread(str(Path(project.root) / tr["crop"]))
            if img is None:
                continue
            if need_fp:
                fp = fp2_from_crop(img)
                if fp is not None:
                    tr["fp2"] = fp.round(5).tolist()
                    tr["fp_v"] = FP_VERSION
                    changed = True
            if need_emb:
                todo.append((tr, img))
        if todo:
            try:
                full = embedder.embed([im for _, im in todo])
                tops = embedder.embed([im[: max(8, int(im.shape[0] * 0.55))] for _, im in todo])
                for (tr, _), e, t in zip(todo, full, tops):
                    if force or not tr.get("emb"):
                        tr["emb"] = e.round(4).tolist()
                    tr["emb_top"] = t.round(4).tolist()
                changed = True
            except Exception as ex:
                log.warning("CLIP embedding failed: %s", ex)
        if changed:
            project.save_analysis(cid, an)
            updated += 1
    return updated
