"""Per-frame measurements: exposure, white balance, waveform, sharpness, camera motion, colour fingerprints."""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np

from . import colour as C

STAT_W = 320  # exposure stats are computed on a 320 px wide copy; plenty for histograms


class ExposureAccumulator:
    """Collects exposure + white balance stats for one clip, in the camera's own encoding."""

    def __init__(self, profile: C.Profile):
        self.p = profile
        self.wave = np.zeros((100, 256), np.float64)
        self.hist = np.zeros((3, 64), np.float64)
        self.rows: list[dict] = []
        m = profile.m_to_xyz
        self._y_bgr = np.array([m[1, 2], m[1, 1], m[1, 0]], np.float32)  # luminance weights, BGR order
        self._w_cache: dict[tuple[int, int], np.ndarray] = {}

    def _centre_weights(self, h: int, w: int) -> np.ndarray:
        k = (h, w)
        if k not in self._w_cache:
            yy, xx = np.mgrid[0:h, 0:w]
            g = np.exp(-(((xx - w / 2) / (0.45 * w)) ** 2 + ((yy - h / 2) / (0.45 * h)) ** 2))
            self._w_cache[k] = (g / g.sum()).astype(np.float32)
        return self._w_cache[k]

    def add(self, frame_bgr: np.ndarray, t: float, subject_boxes: list[np.ndarray]) -> dict:
        H, W = frame_bgr.shape[:2]
        sw = STAT_W
        sh = max(2, int(round(H * sw / W)))
        small = cv2.resize(frame_bgr, (sw, sh), interpolation=cv2.INTER_AREA)
        x = small.astype(np.float32) * (1.0 / 255.0)
        maxc = x.max(axis=2)
        clipm = maxc >= self.p.clip_ire
        lin = np.maximum(self.p.decode_lut[small], 0.0)  # below-black codes decode negative
        Y = np.maximum(lin @ self._y_bgr, 1e-5)
        wts = self._centre_weights(sh, sw)
        key = float(np.exp(np.sum(wts * np.log(Y + 1e-4))))
        # Waveform + histograms use code values, the way you'd read them on a monitor.
        luma = 0.0722 * x[..., 0] + 0.7152 * x[..., 1] + 0.2126 * x[..., 2]
        cols = (np.arange(sw) * 256 // sw)[None, :].repeat(sh, 0)
        rows = np.clip((luma * 99.0).astype(np.int32), 0, 99)
        np.add.at(self.wave, (rows.ravel(), cols.ravel()), 1.0)
        for ci in range(3):
            self.hist[ci] += np.bincount(np.clip((x[..., 2 - ci] * 63.999).astype(np.int32), 0, 63).ravel(), minlength=64)
        # White balance from the dominant near-neutral surface (asphalt, concrete, tyres).
        # Find the densest cluster in log-chroma space first, so a strong cast (cloud,
        # wrong camera WB) shifts the cluster instead of pushing every pixel out of a
        # fixed "neutral" window.
        eps = 1e-4
        rg = np.log((lin[..., 2] + eps) / (lin[..., 1] + eps))
        bg = np.log((lin[..., 0] + eps) / (lin[..., 1] + eps))
        valid = (Y > 0.01) & (Y < 0.6) & (maxc < self.p.clip_ire - 0.02)
        rg0 = bg0 = 0.0
        win = valid & (np.abs(rg) < 0.6) & (np.abs(bg) < 0.6)
        if win.sum() > 0.02 * win.size:
            rows_w = np.linspace(0.6, 1.4, sh, dtype=np.float32)[:, None].repeat(sw, 1)  # track is low in frame
            h2, xe, ye = np.histogram2d(rg[win], bg[win], bins=24, range=[[-0.6, 0.6], [-0.6, 0.6]],
                                        weights=rows_w[win])
            h2 = cv2.GaussianBlur(h2.astype(np.float32), (3, 3), 0)
            i, j = np.unravel_index(int(np.argmax(h2)), h2.shape)
            rg0, bg0 = (xe[i] + xe[i + 1]) / 2, (ye[j] + ye[j + 1]) / 2
        grey = valid & (np.abs(rg - rg0) < 0.15) & (np.abs(bg - bg0) < 0.15)
        gfrac = float(grey.mean())
        if gfrac > 0.02:
            g = lin[grey]
            grey_rgb = (np.mean(g ** 4, axis=0) ** 0.25)[::-1]
            ref_lum = float(np.median(Y[grey]))
        else:
            ok = ~clipm
            g = lin[ok] if ok.any() else lin.reshape(-1, 3)
            grey_rgb = np.mean(g, axis=0)[::-1]
            ref_lum = float(np.median(Y))
        subj_clip = 0.0
        if subject_boxes:
            sx, sy = sw / W, sh / H
            tot = cnt = 0
            for b in subject_boxes:
                x1, y1 = int(max(0, b[0] * sx)), int(max(0, b[1] * sy))
                x2, y2 = int(min(sw, b[2] * sx + 1)), int(min(sh, b[3] * sy + 1))
                if x2 > x1 and y2 > y1:
                    reg = clipm[y1:y2, x1:x2]
                    tot += reg.size
                    cnt += int(reg.sum())
            subj_clip = cnt / tot if tot else 0.0
        row = {
            "t": round(t, 3),
            "key": key,
            "ref": ref_lum,
            "clip": float(clipm.mean()),
            "near": float((maxc >= self.p.clip_ire - 0.04).mean()),
            "shadow": float((Y < 0.18 * 2 ** -6).mean()),
            "grey": [float(v) for v in grey_rgb],
            "gfrac": gfrac,
            "subj_clip": subj_clip,
            "luma": float(luma.mean()),
        }
        self.rows.append(row)
        return row

    def waveform_png(self, path: Path) -> None:
        w = self.wave.copy()
        colsum = w.sum(axis=0, keepdims=True)
        w = w / np.maximum(colsum, 1)
        img = np.clip(np.log1p(w * 400) / np.log1p(400 * 0.08), 0, 1)
        img = img[::-1]  # 100 IRE at top
        out = np.zeros((100, 256, 3), np.uint8)
        out[..., 1] = (img * 235).astype(np.uint8)
        out[..., 0] = (img * 120).astype(np.uint8)
        out[..., 2] = (img * 120).astype(np.uint8)
        guides = [0.92, 0.61, 0.41, 0.035] if self.p.is_log else [1.0, 0.7, 0.42, 0.0]
        for ire in guides:
            yy = int(round((1 - ire) * 99))
            out[yy, ::2] = np.maximum(out[yy, ::2], (90, 90, 90))
        out = cv2.resize(out, (512, 200), interpolation=cv2.INTER_NEAREST)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), out)

    def summary(self) -> dict:
        if not self.rows:
            return {}
        rows = self.rows
        greys = np.array([r["grey"] for r in rows])
        gfr = np.array([r["gfrac"] for r in rows])
        wts = np.clip(gfr, 0.01, 1.0)
        grey = np.average(greys, axis=0, weights=wts)
        ccts = [wb_from_grey(g, self.p)["cct"] for g in greys]
        refs = np.array([r["ref"] for r in rows])
        keys = np.array([r["key"] for r in rows])
        mireds = np.array([C.mired(c) for c in ccts])
        wb = wb_from_grey(grey, self.p)
        hist = (self.hist / max(1.0, self.hist.sum(axis=1, keepdims=True).max())).round(5).tolist()
        return {
            "key": float(np.exp(np.mean(np.log(keys)))),
            "ref": float(np.median(refs)),
            "ref_conf": float(np.median(gfr)),
            "clip": float(np.mean([r["clip"] for r in rows])),
            "clip_max": float(np.max([r["clip"] for r in rows])),
            "near": float(np.mean([r["near"] for r in rows])),
            "shadow": float(np.mean([r["shadow"] for r in rows])),
            "subj_clip": float(np.percentile([r["subj_clip"] for r in rows], 90)),
            "drift_ev": float(np.log2(np.percentile(refs, 90) / max(1e-5, np.percentile(refs, 10)))),
            "drift_mired": float(np.percentile(mireds, 90) - np.percentile(mireds, 10)),
            "grey": [float(v) for v in grey],
            "cct": wb["cct"],
            "duv": wb["duv"],
            "cast_mired": wb["cast_mired"],
            "hist": hist,
            "series": {
                "t": [r["t"] for r in rows],
                "ev": [round(math.log2(max(r["ref"], 1e-5) / 0.11), 3) for r in rows],
                "cct": [round(c) for c in ccts],
            },
        }


def wb_from_grey(grey_rgb, profile: C.Profile) -> dict:
    g = np.asarray(grey_rgb, np.float64)
    g = g / max(1e-6, g[1])
    xyz = profile.m_to_xyz.astype(np.float64) @ g
    x, y = C.xyz_to_xy(xyz)
    cct = C.cct_mccamy(x, y)
    return {"cct": round(cct), "duv": round(C.duv(x, y, cct), 4),
            "cast_mired": round(C.mired(cct) - C.D65_MIRED, 1)}


# --------------------------------------------------------------------------- sharpness

def _acuity(g: np.ndarray, mask: np.ndarray | None = None) -> float:
    """Edge acuity: steepest gradient relative to the local contrast step.

    Roughly 0.5 / edge width in pixels, so it doesn't change with kart size or
    contrast. ~0.45 tack sharp, ~0.2 soft, <0.1 badly blurred (defocus or motion).
    """
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3) * 0.125
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3) * 0.125
    mag = np.sqrt(gx * gx + gy * gy)
    k = np.ones((5, 5), np.uint8)
    rng = cv2.dilate(g, k) - cv2.erode(g, k)
    m = rng > 28.0
    if mask is not None:
        m &= mask
    if int(m.sum()) < 25:
        return 0.0
    return float(np.percentile(mag[m] / rng[m], 90))


def sharpness(gray: np.ndarray) -> float:
    if gray.size < 64:
        return 0.0
    return _acuity(gray.astype(np.float32))


def background_sharpness(gray: np.ndarray, boxes: list[np.ndarray]) -> float:
    mask = np.ones(gray.shape, bool)
    for b in boxes:
        x1, y1, x2, y2 = [int(v) for v in b]
        mask[max(0, y1):max(0, y2), max(0, x1):max(0, x2)] = False
    if mask.sum() < 100:
        return 0.0
    return _acuity(gray.astype(np.float32), mask)


# --------------------------------------------------------------------------- camera motion

class GlobalMotion:
    """Frame-to-frame camera motion from sparse optical flow (RANSAC rejects the karts)."""

    def __init__(self, width: int = 320):
        self.width = width
        self.prev = None
        self.dx: list[float] = []
        self.dy: list[float] = []
        self.da: list[float] = []
        self.scale = 1.0

    def update(self, frame_bgr: np.ndarray) -> None:
        H, W = frame_bgr.shape[:2]
        h = max(2, int(round(H * self.width / W)))
        self.scale = W / self.width
        g = cv2.cvtColor(cv2.resize(frame_bgr, (self.width, h), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        if self.prev is None:
            self.prev = g
            self.dx.append(0.0)
            self.dy.append(0.0)
            self.da.append(0.0)
            return
        dx = dy = da = float("nan")
        pts = cv2.goodFeaturesToTrack(self.prev, maxCorners=200, qualityLevel=0.01, minDistance=7, blockSize=7)
        if pts is not None and len(pts) >= 10:
            nxt, st, _ = cv2.calcOpticalFlowPyrLK(self.prev, g, pts, None, winSize=(21, 21), maxLevel=3)
            if nxt is not None:
                ok = st.reshape(-1) == 1
                if ok.sum() >= 8:
                    M, _ = cv2.estimateAffinePartial2D(pts[ok], nxt[ok], method=cv2.RANSAC,
                                                       ransacReprojThreshold=2.0, maxIters=500)
                    if M is not None:
                        dx, dy = float(M[0, 2]), float(M[1, 2])
                        da = float(math.atan2(M[1, 0], M[0, 0]))
        if math.isnan(dx):
            try:
                (sx, sy), resp = cv2.phaseCorrelate(np.float32(self.prev), np.float32(g))
                if resp > 0.05:
                    dx, dy, da = float(sx), float(sy), 0.0
            except cv2.error:
                pass
        self.dx.append(dx)
        self.dy.append(dy)
        self.da.append(da)
        self.prev = g

    def analyse(self, fps: float) -> dict:
        """Split camera movement into intentional (pan/tilt) and shake."""
        from scipy.signal import savgol_filter

        n = len(self.dx)
        if n < 5:
            return {"shake": 0.0, "pan_speed": [], "shake_series": [], "dx": [], "dy": []}
        dx = _fill_nan(np.array(self.dx))
        dy = _fill_nan(np.array(self.dy))
        da = _fill_nan(np.array(self.da))
        tx, ty, ta = np.cumsum(dx), np.cumsum(dy), np.cumsum(da)
        win = max(5, int(round(fps * 0.5)) | 1)
        if win >= n:
            win = n if n % 2 == 1 else n - 1
        if win < 5:
            sx = tx - tx.mean()
            sy = ty - ty.mean()
            sa = ta - ta.mean()
        else:
            sx = tx - savgol_filter(tx, win, 2)
            sy = ty - savgol_filter(ty, win, 2)
            sa = ta - savgol_filter(ta, win, 2)
        jitter = np.sqrt(sx ** 2 + sy ** 2) / self.width * 100.0 + np.abs(sa) * 50.0  # % of frame width
        speed = np.sqrt(dx ** 2 + dy ** 2) * fps / self.width  # frame widths per second
        sec = max(1, int(round(fps)))
        shake_series = [float(np.sqrt(np.mean(jitter[i:i + sec] ** 2))) for i in range(0, n, max(1, sec // 2))]
        return {
            "shake": float(np.sqrt(np.mean(jitter ** 2))),
            "shake_series": [round(v, 4) for v in shake_series],
            "shake_step": max(1, sec // 2) / fps,
            "pan_speed": [round(float(v), 4) for v in speed],
            "dx": [round(float(v) * self.scale, 3) for v in dx],   # analysis-frame pixels per motion frame
            "dy": [round(float(v) * self.scale, 3) for v in dy],
            "fps": fps,
        }


def _fill_nan(a: np.ndarray) -> np.ndarray:
    m = np.isnan(a)
    if m.all():
        return np.zeros_like(a)
    if m.any():
        idx = np.arange(len(a))
        a = a.copy()
        a[m] = np.interp(idx[m], idx[~m], a[~m])
    return a


# --------------------------------------------------------------------------- appearance

_HSV_MASKS: dict[tuple[int, int], np.ndarray] = {}


def _ellipse_mask(h: int, w: int) -> np.ndarray:
    k = (h, w)
    if k not in _HSV_MASKS:
        yy, xx = np.mgrid[0:h, 0:w]
        _HSV_MASKS[k] = (((xx - w / 2) / (0.48 * w)) ** 2 + ((yy - h / 2) / (0.5 * h)) ** 2) <= 1.0
    return _HSV_MASKS[k]


def colour_hist(crop_display_bgr: np.ndarray) -> np.ndarray:
    """108-d fingerprint: helmet/suit zone + kart zone, hue-saturation plus black/grey/white bins."""
    crop = normalise_brightness(cv2.resize(crop_display_bgr, (64, 64), interpolation=cv2.INTER_AREA))
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = _ellipse_mask(64, 64)
    out = []
    for y0, y1 in ((0, 34), (28, 64)):
        hz = hsv[y0:y1][mask[y0:y1]]
        hh, ss, vv = hz[:, 0].astype(int), hz[:, 1].astype(int), hz[:, 2].astype(int)
        chrom = (ss > 60) & (vv > 40)
        hb = np.clip(hh * 16 // 180, 0, 15)
        sb = np.clip((ss - 60) * 3 // 196, 0, 2)
        h1 = np.bincount((hb * 3 + sb)[chrom], minlength=48).astype(np.float32)
        vb = np.clip(vv * 6 // 256, 0, 5)
        h2 = np.bincount(vb[~chrom], minlength=6).astype(np.float32)
        z = np.concatenate([h1, h2])
        out.append(z / max(1.0, z.sum()))
    v = np.concatenate(out) / 2.0
    return v.astype(np.float32)


def normalise_brightness(img_bgr: np.ndarray, target: float = 225.0) -> np.ndarray:
    """One gain on all channels so the brightest 5% sit at `target`. Hue/saturation untouched,
    so an under- or over-exposed shot of the same kart fingerprints the same."""
    if img_bgr.size == 0:
        return img_bgr
    v = img_bgr.max(axis=2)
    p = float(np.percentile(v, 95))
    if p < 8:
        return img_bgr
    gain = float(np.clip(target / p, 0.6, 4.0))
    if abs(gain - 1.0) < 0.05:
        return img_bgr
    return np.clip(img_bgr.astype(np.float32) * gain, 0, 255).astype(np.uint8)


def bhattacharyya(a, b) -> float:
    return float(np.sum(np.sqrt(np.maximum(np.asarray(a), 0) * np.maximum(np.asarray(b), 0))))


def grass_under(display_bgr: np.ndarray, box: np.ndarray) -> float:
    H, W = display_bgr.shape[:2]
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    sx1, sx2 = int(max(0, x1 + 0.15 * w)), int(min(W, x2 - 0.15 * w))
    sy1, sy2 = int(max(0, y2 - 0.04 * h)), int(min(H, y2 + 0.12 * h))
    if sx2 - sx1 < 4 or sy2 - sy1 < 2:
        return 0.0
    hsv = cv2.cvtColor(display_bgr[sy1:sy2, sx1:sx2], cv2.COLOR_BGR2HSV)
    g = (hsv[..., 0] >= 33) & (hsv[..., 0] <= 88) & (hsv[..., 1] > 70) & (hsv[..., 2] > 35)
    return float(g.mean())


def dhash(display_bgr: np.ndarray) -> int:
    g = cv2.cvtColor(cv2.resize(display_bgr, (9, 8), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    bits = (g[:, 1:] > g[:, :-1]).ravel()
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return bin(int(a) ^ int(b)).count("1")


def crop(img: np.ndarray, box: np.ndarray, pad: float = 0.0) -> np.ndarray:
    H, W = img.shape[:2]
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    x1, x2 = int(max(0, x1 - pad * w)), int(min(W, x2 + pad * w))
    y1, y2 = int(max(0, y1 - pad * h)), int(min(H, y2 + pad * h))
    return img[y1:y2, x1:x2]
