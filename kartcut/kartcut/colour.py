"""Colour science: camera log curves, display transform, LUTs, white balance.

Conventions
-----------
Pixel values arrive as full-range RGB that ffmpeg expanded from limited-range
video, so ``x = value / 255`` is the IRE fraction ((CV - 64) / 876 for 10-bit).
That is the same normalisation Resolve and ffmpeg's lut3d use for .cube files,
so LUTs written here drop straight into Resolve.

Scene-linear values are "reflectance" units: 18% grey card = 0.18.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Matrices verified against colour-science 0.4.x.
M_SG3C_TO_709 = np.array([
    [1.62694741, -0.54013854, -0.08680887],
    [-0.17851553, 1.41794093, -0.2394254],
    [-0.04443611, -0.19591997, 1.24035608],
], dtype=np.float32)
M_SG3C_TO_XYZ = np.array([
    [0.59908392, 0.24892552, 0.10244649],
    [0.21507582, 0.8850685, -0.10014432],
    [-0.03206585, -0.02765839, 1.14878199],
], dtype=np.float32)
M_SG3_TO_709 = np.array([
    [1.87791513, -0.79416876, -0.08374637],
    [-0.17680698, 1.35099962, -0.17419264],
    [-0.02620113, -0.14842226, 1.17462339],
], dtype=np.float32)
M_SG3_TO_XYZ = np.array([
    [0.70648271, 0.12880105, 0.11517216],
    [0.27097967, 0.78660641, -0.05758608],
    [-0.00967785, 0.00460004, 1.09413556],
], dtype=np.float32)
M_709_TO_XYZ = np.array([
    [0.4123908, 0.35758434, 0.18048079],
    [0.21263901, 0.71516868, 0.07219232],
    [0.01933082, 0.11919478, 0.95053215],
], dtype=np.float32)
I3 = np.eye(3, dtype=np.float32)


# --------------------------------------------------------------------------- curves

def ire_to_cv(x):
    return np.asarray(x, dtype=np.float64) * 876.0 + 64.0


def cv_to_ire(cv):
    return (np.asarray(cv, dtype=np.float64) - 64.0) / 876.0


def slog3_to_linear(x):
    """S-Log3 (IRE fraction) to scene reflectance. Sony technical summary formula."""
    code = ire_to_cv(x) / 1023.0
    hi = (10.0 ** ((code * 1023.0 - 420.0) / 261.5)) * 0.19 - 0.01
    lo = (code * 1023.0 - 95.0) * 0.01125 / (171.2102946929 - 95.0)
    return np.where(code >= 171.2102946929 / 1023.0, hi, lo)


def linear_to_slog3(r):
    r = np.asarray(r, dtype=np.float64)
    hi = (420.0 + np.log10((np.maximum(r, 0.01125) + 0.01) / 0.19) * 261.5) / 1023.0
    lo = (r * (171.2102946929 - 95.0) / 0.01125 + 95.0) / 1023.0
    code = np.where(r >= 0.01125, hi, lo)
    return cv_to_ire(code * 1023.0)


def rec709_to_linear(v):
    v = np.clip(np.asarray(v, dtype=np.float64), 0.0, 1.2)
    return np.where(v < 0.081, v / 4.5, ((v + 0.099) / 1.099) ** (1.0 / 0.45))


def linear_to_rec709(L):
    L = np.maximum(np.asarray(L, dtype=np.float64), 0.0)
    return np.where(L < 0.018, L * 4.5, 1.099 * L ** 0.45 - 0.099)


_HLG_A, _HLG_B, _HLG_C = 0.17883277, 0.28466892, 0.55991073
_HLG_SCALE = 0.18 / (0.38 ** 2 / 3.0)  # HLG 18% grey sits at ~38% signal


def hlg_to_linear(v):
    v = np.clip(np.asarray(v, dtype=np.float64), 0.0, 1.0)
    e = np.where(v <= 0.5, v * v / 3.0, (np.exp((v - _HLG_C) / _HLG_A) + _HLG_B) / 12.0)
    return e * _HLG_SCALE


def linear_to_hlg(L):
    e = np.clip(np.asarray(L, dtype=np.float64) / _HLG_SCALE, 0.0, 1.0)
    return np.where(e <= 1.0 / 12.0, np.sqrt(3.0 * e), _HLG_A * np.log(np.maximum(12.0 * e - _HLG_B, 1e-9)) + _HLG_C)


# --------------------------------------------------------------------------- profiles

@dataclass(frozen=True)
class Profile:
    key: str
    label: str
    is_log: bool
    to_linear: callable
    from_linear: callable
    m_to_709: np.ndarray
    m_to_xyz: np.ndarray
    clip_ire: float       # any channel at/above this is treated as clipped
    black_ire: float      # code value of zero reflectance

    @functools.cached_property
    def decode_lut(self) -> np.ndarray:
        """uint8 code -> scene linear float32, for fast per-pixel decoding."""
        return self.to_linear(np.arange(256) / 255.0).astype(np.float32)


PROFILES: dict[str, Profile] = {
    "slog3-sgamut3cine": Profile(
        "slog3-sgamut3cine", "S-Log3 / S-Gamut3.Cine", True,
        slog3_to_linear, linear_to_slog3, M_SG3C_TO_709, M_SG3C_TO_XYZ, 0.92, 0.0354),
    "slog3-sgamut3": Profile(
        "slog3-sgamut3", "S-Log3 / S-Gamut3", True,
        slog3_to_linear, linear_to_slog3, M_SG3_TO_709, M_SG3_TO_XYZ, 0.92, 0.0354),
    "rec709": Profile(
        "rec709", "Rec.709 / standard", False,
        rec709_to_linear, linear_to_rec709, I3, M_709_TO_XYZ, 0.985, 0.0),
    "hlg": Profile(
        "hlg", "HLG", False,
        hlg_to_linear, linear_to_hlg, I3, M_709_TO_XYZ, 0.985, 0.0),
}


def get_profile(key: str | None) -> Profile:
    return PROFILES.get(key or "", PROFILES["slog3-sgamut3cine"])


def profile_choices() -> list[dict]:
    return [{"key": p.key, "label": p.label} for p in PROFILES.values()]


# --------------------------------------------------------------------------- display

DISPLAY_EXPOSURE = 0.6  # puts 18% grey at ~44% code value after the tone curve


def tonemap(x):
    """Filmic shoulder (Narkowicz ACES fit), per channel. Scene linear -> display linear."""
    x = np.maximum(x, 0.0) * DISPLAY_EXPOSURE
    return np.clip((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0)


def display_from_linear709(lin709):
    return tonemap(lin709) ** (1.0 / 2.4)  # BT.1886 inverse


def to_display(x_rgb, profile: Profile):
    """Float RGB in profile encoding (0-1) -> Rec.709 display (0-1)."""
    if not profile.is_log:
        return np.clip(x_rgb, 0.0, 1.0)
    lin = profile.to_linear(x_rgb)
    lin709 = lin @ profile.m_to_709.T
    return display_from_linear709(lin709)


@functools.lru_cache(maxsize=8)
def _tone_lut(n: int = 4096, max_lin: float = 64.0) -> tuple[np.ndarray, float]:
    # Sampled on a sqrt axis so shadows get most of the entries.
    axis = (np.linspace(0, 1, n) ** 2) * max_lin
    return display_from_linear709(axis).astype(np.float32), max_lin


def display_bgr_u8(img_bgr: np.ndarray, profile: Profile) -> np.ndarray:
    """Fast uint8 BGR (profile encoded) -> uint8 BGR Rec.709 display. Used for thumbnails/detection."""
    if not profile.is_log:
        return img_bgr
    lin = profile.decode_lut[img_bgr]                     # H,W,3 (BGR order) float32
    m = profile.m_to_709[::-1, ::-1]                       # same matrix in BGR order
    lin709 = lin @ m.T
    lut, max_lin = _tone_lut()
    idx = np.sqrt(np.clip(lin709, 0.0, max_lin) / max_lin) * (len(lut) - 1)
    out = lut[idx.astype(np.int32)]
    return (out * 255.0 + 0.5).astype(np.uint8)


# --------------------------------------------------------------------------- LUT files

def lut_grid(size: int = 33) -> np.ndarray:
    """(size^3, 3) RGB grid in .cube order (red varies fastest)."""
    a = np.linspace(0.0, 1.0, size)
    b, g, r = np.meshgrid(a, a, a, indexing="ij")
    return np.stack([r.ravel(), g.ravel(), b.ravel()], axis=1)


def write_cube(path: Path, table: np.ndarray, size: int, title: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = np.clip(np.asarray(table, dtype=np.float64), 0.0, 1.0)
    with open(path, "w", encoding="ascii", newline="\n") as f:
        f.write(f'TITLE "{title}"\n')
        f.write(f"LUT_3D_SIZE {size}\n")
        f.write("DOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n")
        for r, g, b in table:
            f.write(f"{r:.6f} {g:.6f} {b:.6f}\n")


@dataclass
class Cube:
    size: int
    table: np.ndarray  # (size, size, size, 3) indexed [b, g, r]
    dmin: np.ndarray
    dmax: np.ndarray

    def apply(self, rgb: np.ndarray) -> np.ndarray:
        """Trilinear lookup. rgb (..., 3) in the LUT's domain."""
        shp = rgb.shape
        x = (np.asarray(rgb, dtype=np.float64).reshape(-1, 3) - self.dmin) / (self.dmax - self.dmin)
        x = np.clip(x, 0.0, 1.0) * (self.size - 1)
        i0 = np.minimum(np.floor(x).astype(np.int64), self.size - 2)
        f = x - i0
        r0, g0, b0 = i0[:, 0], i0[:, 1], i0[:, 2]
        fr, fg, fb = f[:, 0:1], f[:, 1:2], f[:, 2:3]
        t = self.table
        c000 = t[b0, g0, r0]
        c100 = t[b0, g0, r0 + 1]
        c010 = t[b0, g0 + 1, r0]
        c110 = t[b0, g0 + 1, r0 + 1]
        c001 = t[b0 + 1, g0, r0]
        c101 = t[b0 + 1, g0, r0 + 1]
        c011 = t[b0 + 1, g0 + 1, r0]
        c111 = t[b0 + 1, g0 + 1, r0 + 1]
        c00 = c000 * (1 - fr) + c100 * fr
        c10 = c010 * (1 - fr) + c110 * fr
        c01 = c001 * (1 - fr) + c101 * fr
        c11 = c011 * (1 - fr) + c111 * fr
        c0 = c00 * (1 - fg) + c10 * fg
        c1 = c01 * (1 - fg) + c11 * fg
        return (c0 * (1 - fb) + c1 * fb).reshape(shp)


def read_cube(path: Path) -> Cube:
    size = None
    dmin = np.zeros(3)
    dmax = np.ones(3)
    rows: list[list[float]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            head = s.split()[0].upper()
            if head == "TITLE":
                continue
            if head == "LUT_3D_SIZE":
                size = int(s.split()[1])
            elif head == "LUT_1D_SIZE":
                raise ValueError("1D .cube LUTs are not supported, use a 3D LUT")
            elif head == "DOMAIN_MIN":
                dmin = np.array([float(v) for v in s.split()[1:4]])
            elif head == "DOMAIN_MAX":
                dmax = np.array([float(v) for v in s.split()[1:4]])
            elif head[0].isdigit() or head[0] in "-.":
                rows.append([float(v) for v in s.split()[:3]])
    if not size or len(rows) != size ** 3:
        raise ValueError(f"{Path(path).name}: not a valid 3D .cube LUT")
    table = np.array(rows, dtype=np.float64).reshape(size, size, size, 3)
    return Cube(size, table, dmin, dmax)


def builtin_display_cube(profile: Profile, out_dir: Path, size: int = 33) -> Path:
    """KartCut's own S-Log3 -> Rec.709 look (or identity for non-log)."""
    path = Path(out_dir) / f"kartcut_{profile.key}_to_709.cube"
    if not path.exists():
        g = lut_grid(size)
        write_cube(path, to_display(g, profile), size, f"KartCut {profile.label} to Rec.709")
    return path


def correction_fn(profile: Profile, ev: float, gains: np.ndarray):
    """Exposure (stops) + per-channel gains applied in scene linear, re-encoded to the same curve."""
    k = (2.0 ** ev) * np.asarray(gains, dtype=np.float64)

    def fn(x):
        lin = profile.to_linear(x) * k
        return profile.from_linear(lin)

    return fn


def make_render_cube(profile: Profile, ev: float, gains, look: str, custom: Cube | None,
                     path: Path, size: int = 33) -> Path:
    """Single LUT for rendering: match correction then the chosen look."""
    g = lut_grid(size)
    x = correction_fn(profile, ev, gains)(g)
    if look == "custom" and custom is not None:
        out = custom.apply(x)
    elif look == "log":
        out = x
    else:
        out = to_display(x, profile)
    write_cube(path, out, size, "KartCut render")
    return path


# --------------------------------------------------------------------------- white balance

def xyz_to_xy(xyz):
    s = float(np.sum(xyz))
    if s <= 1e-9:
        return 0.3127, 0.3290
    return float(xyz[0] / s), float(xyz[1] / s)


def cct_mccamy(x: float, y: float) -> float:
    n = (x - 0.3320) / (0.1858 - y)
    cct = 449.0 * n ** 3 + 3525.0 * n ** 2 + 6823.3 * n + 5520.33
    return float(np.clip(cct, 1500.0, 25000.0))


def _planck_uv(T: float) -> tuple[float, float]:
    # Krystek (1985) rational approximation of the Planckian locus in CIE 1960 uv.
    u = (0.860117757 + 1.54118254e-4 * T + 1.28641212e-7 * T * T) / (1 + 8.42420235e-4 * T + 7.08145163e-7 * T * T)
    v = (0.317398726 + 4.22806245e-5 * T + 4.20481691e-8 * T * T) / (1 - 2.89741816e-5 * T + 1.61456053e-7 * T * T)
    return u, v


def duv(x: float, y: float, cct: float) -> float:
    d = -2 * x + 12 * y + 3
    u, v = 4 * x / d, 6 * y / d
    pu, pv = _planck_uv(cct)
    dist = math.hypot(u - pu, v - pv)
    return dist if v > pv else -dist


def mired(cct: float) -> float:
    return 1e6 / max(cct, 1.0)


D65_MIRED = mired(6504.0)
