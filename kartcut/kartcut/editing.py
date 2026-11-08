"""Shot-level edits to a planned Timeline: slip, length, speed, framing, order, replace, insert, delete.

Every change ripples: later shots move up or down so there are never gaps, and each
shot is kept inside its source clip.
"""

from __future__ import annotations

import math

from . import autocut
from .autocut import TLClip, Timeline

MIN_FRAMES = 3


def beat_frames(tl: Timeline) -> int:
    tempo = float((tl.music or {}).get("tempo") or 120.0)
    return max(1, int(round(60.0 / max(40.0, tempo) * tl.fps)))


def _clip_dur(project, cid: str) -> float:
    return float(project.data["clips"].get(cid, {}).get("duration") or 0.0)


def fit(project, tl: Timeline, c: TLClip) -> None:
    """Keep the shot inside its source clip."""
    dur = _clip_dur(project, c.clip_id)
    c.speed = float(min(1.0, max(0.25, c.speed)))
    max_frames = max(MIN_FRAMES, int(math.floor((dur - 0.1) / c.speed * tl.fps)))
    c.frames = int(min(max(MIN_FRAMES, c.frames), max_frames))
    c.src_dur = round(c.frames / tl.fps * c.speed, 4)
    c.src_in = round(float(min(max(0.05, c.src_in), max(0.05, dur - c.src_dur - 0.05))), 4)


def reflow(tl: Timeline) -> None:
    start = 0
    for c in tl.clips:
        c.start_f = start
        start += c.frames
    tl.frames = start
    markers = []
    m = tl.music or {}
    if m.get("drop") and m["drop"] * tl.fps < tl.frames:
        markers.append({"frame": int(round(m["drop"] * tl.fps)), "name": "Drop"})
    for c in tl.clips:
        if c.seg_type in ("overtake", "battle", "podium", "celebration"):
            markers.append({"frame": c.start_f, "name": c.label if c.label != c.seg_type else c.seg_type.title()})
    tl.markers = markers


def refresh_crop(project, tl: Timeline, c: TLClip) -> None:
    if tl.aspect != "9:16" or c.orientation == "vertical":
        c.crop = None
        return
    an = project.load_analysis(c.clip_id) or {}
    W = float(an.get("analysis_w") or 1280)
    H = float(an.get("analysis_h") or 720)
    cw = H * 9.0 / 16.0 / W
    if c.crop_fixed is not None:
        left = min(max(0.0, c.crop_fixed - cw / 2), max(0.0, 1.0 - cw))
        c.crop = [(0.0, round(left, 4))]
        return
    target = None
    if tl.kind == "driver" and tl.driver_id:
        tmap = (project.derived.get("clips", {}).get(c.clip_id, {}).get("identity", {}) or {}).get("tracks", {})
        target = {int(t) for t, inf in tmap.items() if inf.get("driver") == tl.driver_id}
    c.crop = autocut.crop_path(an, {"main_track": c.main_track}, c.src_in, c.src_dur, target)


def clip_from_segment(project, tl: Timeline, seg: dict, frames: int, section: str) -> TLClip:
    cl = project.data["clips"][seg["clip"]]
    an = project.load_analysis(seg["clip"]) or {}
    need = frames / tl.fps
    src_in = float(seg["peak"]) - 0.45 * need
    tc = TLClip(
        clip_id=seg["clip"], path=cl["path"], name=cl["name"], src_in=src_in, src_dur=need, speed=1.0,
        start_f=0, frames=frames, section=section, seg_type=seg["type"], label=seg.get("label") or seg["type"],
        score=round(float(seg["score"]), 4), orientation=seg.get("orientation", "horizontal"),
        transpose=int(an.get("transpose", 0) or 0), profile=an.get("profile", "slog3-sgamut3cine"),
        clip_fps=float(cl.get("fps") or tl.fps), drivers=list(seg.get("drivers") or []),
        incident=bool(seg.get("incident")), main_track=seg.get("main_track"),
        seg_a=float(seg["a"]), seg_b=float(seg["b"]), seg_peak=float(seg["peak"]))
    return tc


def alternatives(project, tl: Timeline, limit: int = 60) -> list[dict]:
    pool, _ = autocut.segment_pool(project, tl.kind, tl.driver_id)
    used = {(c.clip_id, round(c.seg_peak, 2)) for c in tl.clips}
    out = []
    for sg in sorted(pool, key=lambda s: -s["score"])[:limit]:
        an = project.load_analysis(sg["clip"]) or {}
        out.append({"clip": sg["clip"], "name": project.data["clips"][sg["clip"]]["name"], "a": sg["a"], "b": sg["b"],
                    "peak": sg["peak"], "type": sg["type"], "label": sg.get("label") or sg["type"],
                    "score": round(sg["score"], 3), "orientation": sg.get("orientation"),
                    "thumb": an.get("thumb"), "in_edit": (sg["clip"], round(sg["peak"], 2)) in used,
                    "drivers": [project.driver_label(d) for d in (sg.get("drivers") or [])]})
    return out


def _find_segment(project, tl: Timeline, clip: str, peak: float) -> dict | None:
    pool, _ = autocut.segment_pool(project, tl.kind, tl.driver_id)
    best = None
    for sg in pool:
        if sg["clip"] == clip and (best is None or abs(sg["peak"] - peak) < abs(best["peak"] - peak)):
            best = sg
    return best


def apply(project, tl: Timeline, op: str, a: dict) -> Timeline:
    n = len(tl.clips)
    i = int(a.get("shot", -1))
    c = tl.clips[i] if 0 <= i < n else None
    touched: list[TLClip] = []
    if op in ("slip", "frames", "seconds", "speed", "crop", "move", "delete", "replace", "nudge") and c is None:
        raise ValueError("No such shot")
    if op == "slip":
        c.src_in = float(a["src_in"])
        touched.append(c)
    elif op == "nudge":
        c.src_in += float(a["by"])
        touched.append(c)
    elif op == "frames":
        c.frames = int(a["frames"])
        touched.append(c)
    elif op == "seconds":
        c.frames = int(round(float(a["seconds"]) * tl.fps))
        touched.append(c)
    elif op == "beats":
        c.frames = c.frames + int(a["by"]) * beat_frames(tl)
        touched.append(c)
    elif op == "speed":
        old_src = c.src_dur
        c.speed = float(a["speed"])
        c.src_in += (old_src - c.frames / tl.fps * c.speed) / 2  # keep the moment centred
        touched.append(c)
    elif op == "crop":
        c.crop_fixed = None if a.get("x") is None else float(a["x"])
        touched.append(c)
    elif op == "move":
        j = int(min(max(0, int(a["to"])), n - 1))
        tl.clips.insert(j, tl.clips.pop(i))
    elif op == "delete":
        if n <= 1:
            raise ValueError("An edit needs at least one shot")
        tl.clips.pop(i)
    elif op in ("replace", "insert"):
        seg = _find_segment(project, tl, a["clip"], float(a["peak"]))
        if seg is None:
            raise ValueError("That highlight isn't available for this edit")
        if op == "replace":
            new = clip_from_segment(project, tl, seg, c.frames, c.section)
            tl.clips[i] = new
        else:
            after = int(a.get("after", n - 1))
            ref = tl.clips[min(max(0, after), n - 1)] if n else None
            frames = int(a.get("frames") or 2 * beat_frames(tl))
            new = clip_from_segment(project, tl, seg, frames, ref.section if ref else "build")
            tl.clips.insert(after + 1, new)
        touched.append(new)
    elif op == "fades":
        if "fade_in" in a:
            tl.fade_in = bool(a["fade_in"])
        if "fade_out" in a:
            tl.fade_out = bool(a["fade_out"])
    else:
        raise ValueError(f"Unknown edit: {op}")
    for t in touched:
        fit(project, tl, t)
        refresh_crop(project, tl, t)
    reflow(tl)
    return tl
