"""Turn raw per-frame analysis into racing events and scored highlight segments.

Pure functions over the analysis dict, so tuning never needs a re-decode.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import maximum_filter1d, uniform_filter1d
from scipy.signal import find_peaks

from .detect import iou

TYPE_BONUS = {"overtake": 1.5, "battle": 1.15, "pan": 1.1, "celebration": 1.05, "podium": 1.0,
              "grid": 0.8, "action": 1.0, "broll": 0.5}


@dataclass
class TrackK:
    tid: int
    t: np.ndarray
    cx: np.ndarray
    cy: np.ndarray
    w: np.ndarray
    h: np.ndarray
    conf: np.ndarray
    sharp: np.ndarray
    grass: np.ndarray
    vx: np.ndarray
    vy: np.ndarray
    speed: np.ndarray      # compensated speed, box widths per second
    growth: np.ndarray     # d log(h) / dt
    pan: np.ndarray        # camera following this kart


def bg_velocity(an: dict):
    mot = an.get("motion") or {}
    dx = np.asarray(mot.get("dx") or [], np.float64)
    dy = np.asarray(mot.get("dy") or [], np.float64)
    fps = float(mot.get("fps") or an.get("motion_fps") or 25.0)
    if len(dx) < 2:
        return lambda ts: (np.zeros(len(ts)), np.zeros(len(ts)))
    k = max(1, int(round(fps / max(1.0, an.get("analysis_fps", 8)))))
    vx = uniform_filter1d(dx * fps, k)
    vy = uniform_filter1d(dy * fps, k)
    tm = np.arange(len(dx)) / fps
    return lambda ts: (np.interp(ts, tm, vx), np.interp(ts, tm, vy))


def track_kinematics(an: dict) -> dict[int, TrackK]:
    W = float(an.get("analysis_w") or 1280)
    bgv = bg_velocity(an)
    out = {}
    for tr in an.get("tracks", []):
        F = np.asarray(tr["frames"], np.float64)
        if len(F) < 2:
            continue
        t = F[:, 0]
        cx, cy = (F[:, 1] + F[:, 3]) / 2, (F[:, 2] + F[:, 4]) / 2
        w, h = np.maximum(1, F[:, 3] - F[:, 1]), np.maximum(1, F[:, 4] - F[:, 2])
        if len(np.unique(t)) < 2:
            continue
        vxi, vyi = np.gradient(cx, t), np.gradient(cy, t)
        bx, by = bgv(t)
        vx, vy = vxi - bx, vyi - by
        speed = np.hypot(vx, vy) / w
        growth = np.gradient(np.log(h), t)
        cam = np.hypot(bx, by) / W
        held = np.hypot(vxi, vyi) / W
        pan = (cam > 0.12) & (held < 0.5 * cam)
        out[int(tr["id"])] = TrackK(int(tr["id"]), t, cx, cy, w, h, F[:, 5], F[:, 6], F[:, 7], vx, vy,
                                    speed, growth, pan)
    return out


def _runs(mask: np.ndarray, gap: int = 1) -> list[tuple[int, int]]:
    """Index ranges [i0, i1] where mask is true, bridging gaps of up to `gap` frames."""
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return []
    runs = []
    s = p = idx[0]
    for i in idx[1:]:
        if i - p > gap + 1:
            runs.append((s, p))
            s = i
        p = i
    runs.append((s, p))
    return runs


def _per_frame(kin: dict[int, TrackK], times: np.ndarray) -> list[list[tuple[int, int]]]:
    pos = {round(float(t), 3): i for i, t in enumerate(times)}
    per: list[list[tuple[int, int]]] = [[] for _ in times]
    for tid, k in kin.items():
        for j, t in enumerate(k.t):
            i = pos.get(round(float(t), 3))
            if i is not None:
                per[i].append((tid, j))
    return per


def detect_events(an: dict) -> dict:
    frames = an.get("frames", [])
    if not frames:
        return {"events": [], "series": {}}
    H = float(an.get("analysis_h") or 720)
    fps = float(an.get("analysis_fps") or 8)
    T = np.array([f["t"] for f in frames], np.float64)
    n = len(T)
    kin = track_kinematics(an)
    per = _per_frame(kin, T)
    events: list[dict] = []

    # ---------------- battles
    battle = np.zeros(n)
    battle_pairs: list[set] = [set() for _ in range(n)]
    for i, ks in enumerate(per):
        big = [(tid, j) for tid, j in ks if kin[tid].h[j] >= 0.07 * H]
        for a in range(len(big)):
            for b in range(a + 1, len(big)):
                ka, ja = kin[big[a][0]], big[a][1]
                kb, jb = kin[big[b][0]], big[b][1]
                mw = (ka.w[ja] + kb.w[jb]) / 2
                d = math.hypot(ka.cx[ja] - kb.cx[jb], ka.cy[ja] - kb.cy[jb]) / mw
                ratio = max(ka.h[ja], kb.h[jb]) / min(ka.h[ja], kb.h[jb])
                if ratio < 1.8:
                    s = math.exp(-max(0.0, d - 0.8) / 1.2)
                    if s > battle[i]:
                        battle[i] = s
                    if s >= 0.35:
                        battle_pairs[i].update({big[a][0], big[b][0]})
    for i0, i1 in _runs(battle >= 0.35, gap=1):
        if T[i1] - T[i0] >= 0.6:
            trs = set().union(*battle_pairs[i0:i1 + 1])
            events.append({"type": "battle", "t0": T[i0], "t1": T[i1], "peak": float(T[i0 + np.argmax(battle[i0:i1 + 1])]),
                           "score": float(battle[i0:i1 + 1].mean()), "tracks": sorted(trs)})

    # ---------------- overtakes
    tids = sorted(kin)
    for ai in range(len(tids)):
        for bi in range(ai + 1, len(tids)):
            ev = _overtake(kin[tids[ai]], kin[tids[bi]], fps)
            if ev:
                events.append(ev)

    # ---------------- incidents
    for tid, k in kin.items():
        events.extend(_track_incidents(k, fps))
    events.extend(_contacts(kin, fps))
    events.extend(_stopped(kin, fps))

    # ---------------- celebrations, grid, podium
    cel = maximum_filter1d(np.array([max(f.get("cel", 0), f.get("dcel", 0)) for f in frames]), 3)
    dcel = maximum_filter1d(np.array([f.get("dcel", 0) for f in frames]), 3)
    for i0, i1 in _runs(cel >= 0.6, gap=2):
        events.append({"type": "celebration", "t0": T[i0], "t1": T[i1], "peak": float(T[i0 + np.argmax(cel[i0:i1 + 1])]),
                       "score": float(cel[i0:i1 + 1].max()), "in_kart": bool(dcel[i0:i1 + 1].max() >= 0.6)})
    moving = np.zeros(n)
    still = np.zeros(n, bool)
    for i, ks in enumerate(per):
        sp = [kin[tid].speed[j] for tid, j in ks]
        moving[i] = sum(1 for s in sp if s > 1.0)
        still[i] = len(sp) >= 3 and float(np.median(sp)) < 0.4
    for i0, i1 in _runs(still, gap=1):
        if T[i1] - T[i0] >= 1.0:
            events.append({"type": "grid", "t0": T[i0], "t1": T[i1], "peak": float((T[i0] + T[i1]) / 2), "score": 0.7})
    pod = np.array([(f.get("p", 0) >= 2 and f.get("ph", 0) >= 0.18 and (f.get("tro", 0) >= 1 or f.get("cel", 0) >= 0.6))
                    for f in frames]) & (moving == 0)
    for i0, i1 in _runs(pod, gap=2):
        if T[i1] - T[i0] >= 0.4:
            events.append({"type": "podium", "t0": T[i0], "t1": T[i1], "peak": float((T[i0] + T[i1]) / 2), "score": 0.9})

    for e in events:
        for key in ("t0", "t1", "peak"):
            if key in e:
                e[key] = round(float(e[key]), 3)
    return {"events": events, "series": {"battle": battle.round(3).tolist(), "celebrate": cel.round(3).tolist()}}


def _overtake(a: TrackK, b: TrackK, fps: float) -> dict | None:
    ta = {round(float(t), 3): i for i, t in enumerate(a.t)}
    common = [(ta[round(float(t), 3)], j) for j, t in enumerate(b.t) if round(float(t), 3) in ta]
    if len(common) < max(3, int(1.0 * fps)):
        return None
    ia = np.array([c[0] for c in common])
    ib = np.array([c[1] for c in common])
    t = a.t[ia]
    mw = (a.w[ia] + b.w[ib]) / 2
    dist = np.hypot(a.cx[ia] - b.cx[ib], a.cy[ia] - b.cy[ib]) / mw
    ratio = np.maximum(a.h[ia], b.h[ib]) / np.minimum(a.h[ia], b.h[ib])
    if dist.min() > 2.5 or np.median(ratio) > 1.7:
        return None
    ux = np.mean(a.vx[ia] + b.vx[ib]) / 2
    uy = np.mean(a.vy[ia] + b.vy[ib]) / 2
    un = math.hypot(ux, uy) / float(np.mean(mw))
    if un > 0.8:
        rel = ((a.cx[ia] - b.cx[ib]) * ux + (a.cy[ia] - b.cy[ib]) * uy) / math.hypot(ux, uy) / mw
    else:
        g = float(np.mean(a.growth[ia] + b.growth[ib]) / 2)
        if abs(g) < 0.15:
            return None
        rel = np.log(a.h[ia] / b.h[ib]) * np.sign(g) * 4.0
    rel = uniform_filter1d(rel, 3)
    win = max(2, int(1.5 * fps))
    sgn = np.sign(rel)
    for k in range(1, len(rel)):
        if sgn[k] == 0 or sgn[k] == sgn[k - 1] or sgn[k - 1] == 0:
            continue
        before = rel[max(0, k - win):k]
        after = rel[k:k + win]
        if len(before) < 2 or len(after) < 2:
            continue
        if sgn[k] > 0 and before.min() < -0.2 and after.max() > 0.2 or \
           sgn[k] < 0 and before.max() > 0.2 and after.min() < -0.2:
            if dist[max(0, k - win):k + win].min() > 2.2:
                continue
            by, on = (a.tid, b.tid) if sgn[k] > 0 else (b.tid, a.tid)
            mag = (np.abs(before).max() + np.abs(after).max())
            tc = float(t[k])
            return {"type": "overtake", "t0": tc - 1.5, "t1": tc + 1.0, "peak": tc, "by": by, "on": on,
                    "tracks": [a.tid, b.tid], "score": float(min(1.0, mag / 1.0))}
    return None


def _track_incidents(k: TrackK, fps: float) -> list[dict]:
    out = []
    n = len(k.t)
    if n < 4:
        return out
    sp = uniform_filter1d(k.speed, 3)
    w05 = max(2, int(round(0.5 * fps)))
    w12 = max(3, int(round(1.2 * fps)))
    for i in range(w05, n - 1):
        pre = sp[max(0, i - w12):i]
        post = sp[i:i + w05]
        if len(post) >= 2 and pre.max() >= 2.5 and post.max() <= 0.6:
            out.append({"type": "incident", "kind": "sudden stop / spin", "track": k.tid,
                        "peak": float(k.t[i]), "conf": 0.6})
            break
    for i in range(w05, n - w05):
        v1 = np.array([k.vx[i - w05:i].mean(), k.vy[i - w05:i].mean()])
        v2 = np.array([k.vx[i:i + w05].mean(), k.vy[i:i + w05].mean()])
        s1, s2 = np.linalg.norm(v1) / k.w[i], np.linalg.norm(v2) / k.w[i]
        if s1 >= 1.5 and s2 >= 1.5:
            ang = math.degrees(math.acos(float(np.clip(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)), -1, 1))))
            dt = k.t[min(n - 1, i + w05)] - k.t[i - w05]
            if ang > 120 and dt <= 1.3:
                out.append({"type": "incident", "kind": "spin", "track": k.tid, "peak": float(k.t[i]), "conf": 0.45})
                break
    ar = k.w / k.h
    w06 = max(2, int(round(0.6 * fps)))
    for i in range(0, n - w06):
        seg = ar[i:i + w06 + 1]
        disp = math.hypot(k.cx[i + w06] - k.cx[i], k.cy[i + w06] - k.cy[i]) / k.w[i]
        if seg.max() / max(1e-3, seg.min()) > 1.8 and disp < 1.5:
            out.append({"type": "incident", "kind": "spin (rotation)", "track": k.tid,
                        "peak": float(k.t[i + int(np.argmax(np.abs(np.diff(seg))))]), "conf": 0.5})
            break
    grass = k.grass >= 0.5
    for i0, i1 in _runs(grass & (sp > 0.5), gap=0):
        if i1 - i0 >= 1:
            out.append({"type": "incident", "kind": "off track", "track": k.tid,
                        "peak": float(k.t[(i0 + i1) // 2]), "conf": 0.5})
            break
    return out


def _contacts(kin: dict[int, TrackK], fps: float) -> list[dict]:
    out = []
    tids = sorted(kin)
    w = max(2, int(round(0.5 * fps)))
    for ai in range(len(tids)):
        a = kin[tids[ai]]
        ta = {round(float(t), 3): i for i, t in enumerate(a.t)}
        for bi in range(ai + 1, len(tids)):
            b = kin[tids[bi]]
            for j, t in enumerate(b.t):
                i = ta.get(round(float(t), 3))
                if i is None:
                    continue
                ba = np.array([a.cx[i] - a.w[i] / 2, a.cy[i] - a.h[i] / 2, a.cx[i] + a.w[i] / 2, a.cy[i] + a.h[i] / 2])
                bb = np.array([b.cx[j] - b.w[j] / 2, b.cy[j] - b.h[j] / 2, b.cx[j] + b.w[j] / 2, b.cy[j] + b.h[j] / 2])
                if iou(ba, bb) < 0.3 or max(a.h[i], b.h[j]) / min(a.h[i], b.h[j]) > 1.4:
                    continue
                jolt = 0.0
                for k, idx in ((a, i), (b, j)):
                    lo, hi = max(0, idx - w), min(len(k.t), idx + w + 1)
                    if hi - lo >= 3:
                        jolt = max(jolt, float(np.abs(np.diff(k.speed[lo:hi])).max()))
                if jolt > 2.5:
                    out.append({"type": "incident", "kind": "possible contact", "track": a.tid, "other": b.tid,
                                "peak": float(t), "conf": 0.5})
                    break
    return out


def _stopped(kin: dict[int, TrackK], fps: float) -> list[dict]:
    out = []
    for tid, k in kin.items():
        slow = uniform_filter1d(k.speed, 3) < 0.3
        for i0, i1 in _runs(slow, gap=1):
            if k.t[i1] - k.t[i0] < 1.5:
                continue
            others_fast = 0
            stopped_others = 0
            for oid, o in kin.items():
                if oid == tid:
                    continue
                m = (o.t >= k.t[i0]) & (o.t <= k.t[i1])
                if m.sum() >= 2:
                    if np.median(o.speed[m]) > 3.0:
                        others_fast += 1
                    elif np.median(o.speed[m]) < 0.4:
                        stopped_others += 1
            if others_fast and not stopped_others:
                out.append({"type": "incident", "kind": "stopped on track", "track": tid,
                            "peak": float((k.t[i0] + k.t[i1]) / 2), "conf": 0.4})
                break
    return out


# --------------------------------------------------------------------------- segments

def build_segments(an: dict, ev: dict, sharp_rank, exp_quality: float) -> list[dict]:
    frames = an.get("frames", [])
    dur = float(an.get("probe", {}).get("duration") or 0.0)
    if not frames or dur <= 0.3:
        return []
    H = float(an.get("analysis_h") or 720)
    fps = float(an.get("analysis_fps") or 8)
    T = np.array([f["t"] for f in frames], np.float64)
    n = len(T)
    kin = track_kinematics(an)
    per = _per_frame(kin, T)
    events = ev.get("events", [])
    battle = np.asarray(ev.get("series", {}).get("battle") or np.zeros(n))
    cel = np.asarray(ev.get("series", {}).get("celebrate") or np.zeros(n))

    action = np.zeros(n)
    pan_q = np.zeros(n)
    for i, ks in enumerate(per):
        for tid, j in ks:
            k = kin[tid]
            size = float(np.clip((k.h[j] / H) / 0.30, 0.1, 1.0))
            sp = max(float(k.speed[j]), abs(float(k.growth[j])) * 3.0)
            focus = float(sharp_rank(k.sharp[j]))
            v = size * (0.35 + 0.65 * (1 - math.exp(-sp / 2.5))) * (0.3 + 0.7 * focus)
            action[i] = max(action[i], v)
            if k.pan[j]:
                bgs = frames[i].get("bgs", 0.0)
                blur_bg = 1.0 if bgs < 0.7 * max(1e-6, k.sharp[j]) else 0.6
                pan_q[i] = max(pan_q[i], focus * blur_bg)

    stable = _stability(an, T)
    bumps = np.zeros(n)
    flat = np.zeros(n)
    for e in events:
        if e["type"] == "overtake":
            bumps += e["score"] * np.exp(-0.5 * ((T - e["peak"]) / 0.7) ** 2)
        elif e["type"] in ("podium", "grid"):
            m = (T >= e["t0"] - 0.2) & (T <= e["t1"] + 0.2)
            flat[m] = np.maximum(flat[m], 0.65 if e["type"] == "podium" else 0.45)
    interest = np.maximum.reduce([0.8 * action + 0.5 * battle, 0.9 * cel, flat]) + 0.8 * bumps
    score = uniform_filter1d(interest * stable * exp_quality, 3)

    segs: list[dict] = []
    has_subject = any(f.get("k", 0) or f.get("p", 0) for f in frames)
    if has_subject and score.max() > 0.05:
        peaks, _ = find_peaks(np.concatenate([[0], score, [0]]), distance=max(1, int(1.5 * fps)), height=0.06)
        peaks = [p - 1 for p in peaks if 0 < p <= n]
        for p in peaks:
            thr = 0.45 * score[p]
            lo = p
            while lo > 0 and score[lo - 1] >= thr and T[p] - T[lo - 1] <= 4.0:
                lo -= 1
            hi = p
            while hi < n - 1 and score[hi + 1] >= thr and T[hi + 1] - T[p] <= 4.0:
                hi += 1
            a, b = max(0.05, T[lo] - 0.3), min(dur - 0.05, T[hi] + 0.3)
            if b - a < 0.8:
                mid = (a + b) / 2
                a, b = max(0.05, mid - 0.4), min(dur - 0.05, mid + 0.4)
            if b - a < 0.5:
                continue
            segs.append(_make_segment(an, T, per, kin, events, battle, pan_q, a, b, float(T[p]), float(score[p]),
                                      float(stable[p])))
        segs = _merge_overlaps(segs)
    if not segs:
        segs = [_broll_segment(an, T, stable, exp_quality, dur)]
    return [s for s in segs if s]


def _stability(an: dict, T: np.ndarray) -> np.ndarray:
    mot = an.get("motion") or {}
    ss = mot.get("shake_series") or []
    step = float(mot.get("shake_step") or 0.5)
    if not ss:
        return np.ones(len(T))
    st = np.arange(len(ss)) * step + step
    shake = np.interp(T, st, ss)
    return np.clip(1.0 - (shake - 0.15) / 0.6, 0.15, 1.0)


def _make_segment(an, T, per, kin, events, battle, pan_q, a, b, peak, score, stab) -> dict:
    m = (T >= a) & (T <= b)
    idx = np.flatnonzero(m)
    share: dict[int, float] = {}
    H = float(an.get("analysis_h") or 720)
    for i in idx:
        for tid, j in per[i]:
            share[tid] = share.get(tid, 0.0) + float(kin[tid].h[j] / H)
    total = sum(share.values()) or 1.0
    tracks = sorted([tid for tid, s in share.items() if s / total >= 0.15], key=lambda t: -share[t])
    inside = [e for e in events if e.get("peak", -1) >= a - 0.3 and e.get("peak", -1) <= b + 0.3]
    types = {e["type"] for e in inside}
    stype = "action"
    label = ""
    if "overtake" in types:
        stype = "overtake"
        ov = max((e for e in inside if e["type"] == "overtake"), key=lambda e: e["score"])
        label = f"overtake:{ov['by']}:{ov['on']}"
    elif "podium" in types:
        stype = "podium"
    elif "celebration" in types:
        stype = "celebration"
    elif "grid" in types:
        stype = "grid"
    elif len(idx) and battle[idx].mean() >= 0.35:
        stype = "battle"
    elif len(idx) and (pan_q[idx] > 0.5).mean() >= 0.5:
        stype = "pan"
    elif not share and len(idx) and any(an["frames"][i].get("p", 0) for i in idx):
        stype = "celebration" if any(an["frames"][i].get("cel", 0) > 0.3 for i in idx) else "broll"
    inc = [e for e in inside if e["type"] == "incident"]
    return {
        "clip": an["clip_id"],
        "a": round(a, 3), "b": round(b, 3), "peak": round(peak, 3),
        "type": stype,
        "label": label,
        "score": round(score * TYPE_BONUS.get(stype, 1.0), 4),
        "stability": round(stab, 3),
        "tracks": tracks,
        "main_track": tracks[0] if tracks else None,
        "incident": bool(inc),
        "incident_kinds": sorted({e["kind"] for e in inc}),
        "orientation": an.get("orientation", "horizontal"),
    }


def _merge_overlaps(segs: list[dict]) -> list[dict]:
    segs = sorted(segs, key=lambda s: -s["score"])
    keep: list[dict] = []
    for s in segs:
        clash = False
        for k in keep:
            ov = min(s["b"], k["b"]) - max(s["a"], k["a"])
            if ov > 0.5 * min(s["b"] - s["a"], k["b"] - k["a"]):
                clash = True
                break
        if not clash:
            keep.append(s)
    return sorted(keep, key=lambda s: s["a"])


def _broll_segment(an, T, stable, exp_quality, dur) -> dict | None:
    if dur < 0.8:
        return None
    L = min(3.0, dur - 0.2)
    best, best_a = -1.0, 0.1
    for i, t in enumerate(T):
        if t + L > dur - 0.05:
            break
        m = (T >= t) & (T <= t + L)
        s = float(stable[m].mean()) if m.any() else 0.0
        if s > best:
            best, best_a = s, float(t)
    a = max(0.05, best_a)
    return {"clip": an["clip_id"], "a": round(a, 3), "b": round(min(dur - 0.05, a + L), 3),
            "peak": round(a + L / 2, 3), "type": "broll", "label": "",
            "score": round(0.25 * max(best, 0.2) * exp_quality * TYPE_BONUS["broll"], 4),
            "stability": round(max(best, 0.0), 3), "tracks": [], "main_track": None, "incident": False,
            "incident_kinds": [], "orientation": an.get("orientation", "horizontal")}
