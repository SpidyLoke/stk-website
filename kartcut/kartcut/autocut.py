"""Montage planning: beat grid -> sections -> shot slots -> best segment per slot.

The plan is a plain Timeline object. The renderer and the Resolve/Premiere
exporters both read the same plan, so what you render is what you get in the NLE.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import asdict, dataclass, field

import numpy as np

from . import events

AFFINITY = {
    "intro":  {"grid": 1.4, "broll": 1.2, "pan": 1.2, "action": 0.9, "battle": 0.8, "overtake": 0.6,
               "celebration": 0.5, "podium": 0.3},
    "build":  {"action": 1.2, "pan": 1.2, "battle": 1.1, "overtake": 0.9, "grid": 0.6, "broll": 0.5,
               "celebration": 0.5, "podium": 0.2},
    "climax": {"overtake": 1.6, "battle": 1.4, "action": 1.1, "pan": 0.9, "celebration": 0.5, "grid": 0.2,
               "broll": 0.2, "podium": 0.1},
    "outro":  {"podium": 1.6, "celebration": 1.5, "pan": 1.0, "overtake": 0.8, "action": 0.7, "battle": 0.7,
               "broll": 0.7, "grid": 0.5},
}
SHOT_LEN = {"intro": 2.4, "build": 1.5, "climax": 0.8, "outro": 2.2}  # target seconds per cut


@dataclass
class TLClip:
    clip_id: str
    path: str
    name: str
    src_in: float
    src_dur: float
    speed: float
    start_f: int
    frames: int
    section: str
    seg_type: str
    label: str
    score: float
    orientation: str
    transpose: int
    profile: str
    clip_fps: float
    drivers: list = field(default_factory=list)
    crop: list | None = None      # [(t_rel_source, x_left_fraction)] for 9:16 reframing
    incident: bool = False


@dataclass
class Timeline:
    name: str
    kind: str                     # event | driver
    driver_id: str | None
    aspect: str                   # 16:9 | 9:16
    fps: float
    frames: int
    clips: list[TLClip]
    music: dict | None
    markers: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.frames / self.fps

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = self.duration
        if d.get("music"):
            d["music"] = {k: v for k, v in d["music"].items() if k not in ("energy", "energy_t")}
        return d


def output_fps(project) -> float:
    v = float(project.settings.get("output_fps") or 0)
    if v > 0:
        return v
    fpss = [round(c.get("fps") or 0, 2) for c in project.data["clips"].values() if c.get("status") == "analysed"]
    common = Counter(f for f in fpss if f > 0)
    if not common:
        return 25.0
    f = common.most_common(1)[0][0]
    if f > 60:  # high-frame-rate clips are slow-mo sources, not delivery rates
        f = 50.0 if abs(f % 25) < 1 else 59.94 if abs(f - 119.88) < 1 else 60.0
    return float(f)


def beat_grid(music: dict | None, length: float) -> tuple[list[float], float, int]:
    if music and music.get("beats"):
        beats = sorted(b for b in music["beats"] if b <= music["duration"] - 0.2)
        period = 60.0 / max(40.0, float(music.get("tempo") or 120.0))
        return fill_beats(beats, period), period, int(music.get("downbeat_phase", 0))
    period = 0.5
    return list(np.arange(0.0, length + 4.0, period)), period, 0


def fill_beats(beats: list[float], period: float) -> list[float]:
    """Beat trackers skip quiet passages and the first bars of an excerpt. Fill any gap
    longer than 1.5 beats at the song's tempo, so no shot is stretched over the gap."""
    if not beats:
        return beats
    out = []
    t = beats[0] - period
    lead = []
    while t > 0.05:
        lead.append(t)
        t -= period
    out.extend(reversed(lead))
    out.append(beats[0])
    for b in beats[1:]:
        gap = b - out[-1]
        if gap > 1.5 * period:
            n = max(2, int(round(gap / period)))
            out.extend(out[-1] + gap * k / n for k in range(1, n))
        out.append(b)
    return out


def make_slots(music: dict | None, length: float) -> list[tuple[float, float, str]]:
    beats, period, phase = beat_grid(music, length)
    if not beats:
        return []
    if beats[0] > 0.05:
        beats = [0.0] + beats
        phase = (phase + 1) % 4
    max_len = beats[-1]
    if music:
        max_len = min(max_len, music["duration"] - 0.3)
    L = min(length, max_len)
    downs = [b for i, b in enumerate(beats) if (i - phase) % 4 == 0 and b >= 0.8 * L]
    if downs:
        L = min(downs, key=lambda b: abs(b - L))
    drop = music.get("drop") if music else None
    s1 = 0.18 * L
    s2 = drop if (drop and 0.25 * L < drop < 0.75 * L) else 0.45 * L
    s3 = 0.82 * L

    def section(t):
        return "intro" if t < s1 else "build" if t < s2 else "climax" if t < s3 else "outro"

    slots = []
    i = 0
    beats = [b for b in beats if b <= L + 1e-6]
    if beats[-1] < L - 1e-6:
        beats.append(L)
    while i < len(beats) - 1:
        t0 = beats[i]
        sec = section(t0)
        n = max(1, int(round(SHOT_LEN[sec] / period)))
        n = 1 << int(round(math.log2(n)))           # 1, 2, 4, 8 beats: cuts land on phrase boundaries
        j = min(len(beats) - 1, i + n)
        if sec != section(beats[j] - 1e-6) and j > i + 1:  # don't let a shot straddle a section change
            k = i + 1
            while k < j and section(beats[k]) == sec:
                k += 1
            j = k
        while j > i + 1 and beats[j] - t0 > 2.0 * SHOT_LEN[sec] + period:  # belt and braces on odd grids
            j -= 1
        t1 = beats[j]
        if t1 - t0 < 0.45 and slots:  # too short to read: fold into the previous slot
            p0, _, ps = slots[-1]
            slots[-1] = (p0, t1, ps)
        else:
            slots.append((t0, t1, sec))
        i = j
    return slots


def plan(project, kind: str, driver_id: str | None, aspect: str, length: float, music: dict | None,
         seed: int = 1) -> Timeline:
    rng = random.Random(seed)
    s = project.settings
    fps = output_fps(project)
    dclips = project.derived.get("clips", {})
    drivers = project.data["drivers"]
    inc_global = bool(s.get("include_incidents", False))

    def incidents_ok(seg) -> bool:
        if not seg.get("incident"):
            return True
        ids = [driver_id] if kind == "driver" else (seg.get("drivers") or [])
        for d in ids:
            v = (drivers.get(d) or {}).get("include_incidents")
            if v is False:
                return False
            if v is True:
                return True
        return inc_global

    pool = []
    driver_clips = 0
    for seg in project.derived.get("segments", []):
        info = dclips.get(seg["clip"])
        if not info or info.get("junk", {}).get("is_junk"):
            continue
        if not incidents_ok(seg):
            continue
        if kind == "driver":
            clip_drivers = {d["id"] for d in (info.get("identity") or {}).get("drivers", [])}
            if driver_id in (seg.get("drivers") or []):
                pass
            elif driver_id in clip_drivers:
                seg = {**seg, "drivers": list(seg.get("drivers") or []) + [driver_id], "score": seg["score"] * 0.75}
            else:
                continue
        pool.append(seg)
    if kind == "driver":
        driver_clips = sum(1 for i in dclips.values() if not i.get("junk", {}).get("is_junk") and
                           driver_id in {d["id"] for d in (i.get("identity") or {}).get("drivers", [])})
    if not pool:
        if kind == "driver":
            raise RuntimeError(f"no usable shots ({driver_clips} clips in their bin; check they aren't junk "
                               "or incidents you've left out)")
        raise RuntimeError("No usable highlights for this montage yet. Analyse footage (and assign drivers) first.")

    slots = make_slots(music, length)
    used: dict[str, list[tuple[float, float]]] = {}
    shown_drivers: Counter = Counter()
    chosen: list[TLClip] = []
    prev_type = prev_driver = prev_clip = None
    notes = []
    jitter = {id(sg): 1.0 + 0.08 * (rng.random() - 0.5) for sg in pool}

    for si, (t0, t1, sec) in enumerate(slots):
        start_f = int(round(t0 * fps))
        frames = int(round(t1 * fps)) - start_f
        if frames < 2:
            continue
        dur = frames / fps
        last = si == len(slots) - 1
        best, best_v, best_fit = None, 0.0, None
        for sg in pool:
            an_clip = project.data["clips"].get(sg["clip"], {})
            clip_dur = float(an_clip.get("duration") or 0)
            clip_fps = float(an_clip.get("fps") or fps)
            speed = 1.0
            slowmo_ok = s.get("slow_motion", True) and clip_fps >= 2 * fps - 1
            if slowmo_ok and (sec in ("intro", "outro") or sg["type"] in ("pan", "celebration", "podium")) \
                    and sec != "climax":
                speed = 0.5
            need = dur * speed
            if need > clip_dur - 0.1:
                continue
            fit = _fit_in(sg, need, clip_dur, used.get(sg["clip"], []))
            if fit is None:
                continue
            v = sg["score"] * AFFINITY[sec].get(sg["type"], 0.5) * jitter[id(sg)]
            if sg["clip"] in used:
                v *= 0.25
            if sg["clip"] == prev_clip:
                v *= 0.1
            if sg["type"] == prev_type:
                v *= 0.85
            if aspect == "16:9" and sg["orientation"] == "vertical":
                v *= 0.5
            if aspect == "9:16" and sg["orientation"] == "vertical":
                v *= 1.3
            if kind == "driver":
                if sg.get("main_driver") == driver_id:
                    v *= 1.3
            else:
                md = sg.get("main_driver")
                if md and md == prev_driver:
                    v *= 0.7
                if md and shown_drivers[md] == 0:
                    v *= 1.25
            if last and sg["type"] in ("podium", "celebration"):
                v *= 1.5
            if fit[1]:
                v *= 0.6  # had to stretch past the segment's best range
            if v > best_v:
                best, best_v, best_fit = sg, v, (fit[0], speed, need)
        if best is None or best_v < 0.005:
            notes.append(f"Ran out of usable footage at {t0:.1f}s")
            break
        src_in, speed, need = best_fit
        an = project.load_analysis(best["clip"]) or {}
        c = project.data["clips"][best["clip"]]
        crop = None
        if aspect == "9:16" and best["orientation"] != "vertical":
            target = None
            if kind == "driver":
                tmap = (dclips.get(best["clip"], {}).get("identity", {}) or {}).get("tracks", {})
                target = {int(t) for t, inf in tmap.items() if inf.get("driver") == driver_id}
            crop = crop_path(an, best, src_in, need, target)
        chosen.append(TLClip(
            clip_id=best["clip"], path=c["path"], name=c["name"], src_in=round(src_in, 4), src_dur=round(need, 4),
            speed=speed, start_f=start_f, frames=frames, section=sec, seg_type=best["type"],
            label=best.get("label") or best["type"], score=round(best["score"], 4),
            orientation=best["orientation"], transpose=int(an.get("transpose", 0) or 0),
            profile=an.get("profile", "slog3-sgamut3cine"), clip_fps=float(c.get("fps") or fps),
            drivers=list(best.get("drivers") or []), crop=crop, incident=bool(best.get("incident"))))
        used.setdefault(best["clip"], []).append((src_in, src_in + need))
        prev_type, prev_clip = best["type"], best["clip"]
        prev_driver = best.get("main_driver")
        if prev_driver:
            shown_drivers[prev_driver] += 1

    if not chosen:
        raise RuntimeError("clips too short for the first cut. Try a shorter reel length or a different "
                           "start point in the song")
    total = chosen[-1].start_f + chosen[-1].frames
    title = "Event montage" if kind == "event" else f"Reel {project.driver_label(driver_id)}"
    markers = []
    if music and music.get("drop") and music["drop"] * fps < total:
        markers.append({"frame": int(round(music["drop"] * fps)), "name": "Drop"})
    for tc in chosen:
        if tc.seg_type in ("overtake", "battle", "podium", "celebration"):
            markers.append({"frame": tc.start_f, "name": tc.label if tc.label != tc.seg_type else tc.seg_type.title()})
    if kind == "event":
        missing = [project.driver_label(d) for d in drivers if shown_drivers[d] == 0 and
                   any(d in (sg.get("drivers") or []) for sg in pool)]
        if missing:
            notes.append("Not in the montage (ran out of slots): " + ", ".join(missing[:12]))
    return Timeline(name=f"{title} {aspect}", kind=kind, driver_id=driver_id, aspect=aspect, fps=fps,
                    frames=total, clips=chosen, music=music, markers=markers, notes=notes)


def _fit_in(sg: dict, need: float, clip_dur: float, used: list[tuple[float, float]]):
    """Place `need` seconds around the segment peak. Returns (src_in, stretched) or None."""
    a, b, peak = sg["a"], sg["b"], sg["peak"]
    lo_bound, hi_bound = 0.05, clip_dur - 0.05
    start = peak - 0.45 * need
    start = min(max(start, a), max(a, b - need))
    stretched = need > (b - a) + 0.3
    start = float(np.clip(start, lo_bound, max(lo_bound, hi_bound - need)))
    if start + need > hi_bound + 1e-6:
        return None
    for u0, u1 in used:
        if start < u1 and start + need > u0:
            return None
    return start, stretched


def crop_path(an: dict, seg: dict, src_in: float, src_dur: float, target_tracks: set | None) -> list | None:
    """Virtual camera operator for 9:16: follow the kart, leave lead room, never jerk."""
    W = float(an.get("analysis_w") or 1280)
    H = float(an.get("analysis_h") or 720)
    cw = H * 9.0 / 16.0
    if cw >= W - 2:
        return None
    kin = events.track_kinematics(an)
    T = np.array([f["t"] for f in an.get("frames", [])], np.float64)
    if len(T) == 0:
        return [(0.0, (W - cw) / 2 / W)]
    m = (T >= src_in - 0.6) & (T <= src_in + src_dur + 0.6)
    ts = T[m]
    if len(ts) == 0:
        return [(0.0, (W - cw) / 2 / W)]
    pref = list(target_tracks or [])
    if seg.get("main_track") is not None:
        pref.append(int(seg["main_track"]))
    xs = np.full(len(ts), np.nan)
    for i, t in enumerate(ts):
        best = None
        for tid in pref:
            k = kin.get(tid)
            if k is None:
                continue
            j = np.flatnonzero(np.abs(k.t - t) < 1e-3)
            if len(j):
                best = (k, j[0])
                break
        if best is None:
            cands = [(k, np.flatnonzero(np.abs(k.t - t) < 1e-3)) for k in kin.values()]
            cands = [(k, j[0]) for k, j in cands if len(j)]
            if cands:
                best = max(cands, key=lambda kj: kj[0].h[kj[1]])
        if best is not None:
            k, j = best
            lead = np.sign(k.vx[j]) * 0.12 * cw if abs(k.vx[j]) > 0.3 * k.w[j] else 0.0
            xs[i] = k.cx[j] + lead
    if np.isnan(xs).all():
        return [(0.0, (W - cw) / 2 / W)]
    idx = np.arange(len(xs))
    good = ~np.isnan(xs)
    xs = np.interp(idx, idx[good], xs[good])
    # zero-phase exponential smoothing (forward + backward): smooth like a fluid head, no lag
    alpha = 0.35
    f = xs.copy()
    for i in range(1, len(f)):
        f[i] = alpha * xs[i] + (1 - alpha) * f[i - 1]
    for i in range(len(f) - 2, -1, -1):
        f[i] = alpha * f[i] + (1 - alpha) * f[i + 1]
    left = np.clip(f - cw / 2, 0, W - cw)
    return [(round(float(t - src_in), 3), round(float(x / W), 4)) for t, x in zip(ts, left)]
