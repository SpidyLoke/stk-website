"""Driver identification: race-number OCR votes + helmet/suit/kart colour + CLIP appearance.

Each kart track gets a probability per driver plus an 'unknown' option, so a
weak match stays in Needs Review instead of landing in the wrong driver's bin.
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np

from .metrics import bhattacharyya

UNKNOWN_LOGIT = 0.8


def driver_models(drivers: dict, analyses: dict[str, dict], track_overrides: dict, learn: bool) -> dict:
    """Reference fingerprints per driver from chosen reference tracks (+ manual corrections)."""
    refs: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for did, d in drivers.items():
        for r in d.get("refs", []):
            refs[did].append((r["clip"], int(r["track"])))
    if learn:
        for cid, tmap in track_overrides.items():
            for tid, did in tmap.items():
                if did and did != "none" and did in drivers:
                    refs[did].append((cid, int(tid)))
    out = {}
    for did, lst in refs.items():
        hists, embs = [], []
        seen = set()
        for cid, tid in lst:
            if (cid, tid) in seen:
                continue
            seen.add((cid, tid))
            an = analyses.get(cid)
            if not an:
                continue
            tr = next((t for t in an.get("tracks", []) if int(t["id"]) == tid), None)
            if not tr:
                continue
            if tr.get("hist"):
                hists.append(np.asarray(tr["hist"], np.float32))
            if tr.get("emb"):
                embs.append(np.asarray(tr["emb"], np.float32))
        out[did] = {"hists": hists, "embs": embs}
    return out


def number_votes(track: dict) -> dict[str, float]:
    v: dict[str, float] = defaultdict(float)
    for num, conf in track.get("ocr", []) or []:
        v[str(num)] += float(conf)
    return dict(v)


def score_track(track: dict, drivers: dict, models: dict) -> list[tuple[str | None, float]]:
    if not drivers:
        return [(None, 1.0)]
    votes = number_votes(track)
    total = sum(votes.values())
    top_num, top_v = (max(votes.items(), key=lambda kv: kv[1]) if votes else (None, 0.0))
    top_share = top_v / total if total else 0.0
    reliab = min(1.0, total / 1.2)
    hist = np.asarray(track["hist"], np.float32) if track.get("hist") else None
    emb = np.asarray(track["emb"], np.float32) if track.get("emb") else None
    ids, logits = [], []
    for did, d in drivers.items():
        lg = 0.0
        num = str(d.get("number", "")).strip().lstrip("#")
        num = str(int(num)) if num.isdigit() else num
        if total > 0 and num:
            share = votes.get(num, 0.0) / total
            lg += 3.0 * reliab * share
            if top_num != num and top_share >= 0.6:
                lg -= 1.5 * reliab
        m = models.get(did)
        if m:
            if hist is not None and m["hists"]:
                s = max(bhattacharyya(hist, h) for h in m["hists"])
                lg += 6.0 * (s - 0.7)
            if emb is not None and m["embs"]:
                c = max(float(np.dot(emb, e)) for e in m["embs"])
                lg += float(np.clip(12.0 * (c - 0.82), -3.0, 3.0))
        ids.append(did)
        logits.append(lg)
    ids.append(None)
    logits.append(UNKNOWN_LOGIT)
    z = np.array(logits) - max(logits)
    p = np.exp(z)
    p /= p.sum()
    order = np.argsort(-p)
    return [(ids[i], float(p[i])) for i in order]


def track_share(an: dict) -> dict[int, float]:
    H = float(an.get("analysis_h") or 720)
    out = {}
    for tr in an.get("tracks", []):
        out[int(tr["id"])] = float(sum((f[4] - f[2]) / H for f in tr["frames"]))
    return out


def identify_clip(an: dict, drivers: dict, models: dict, overrides: dict, clip_override: list | None,
                  min_prob: float) -> dict:
    share = track_share(an)
    total = sum(share.values()) or 1.0
    tracks_out = {}
    for tr in an.get("tracks", []):
        tid = int(tr["id"])
        ov = overrides.get(str(tid))
        votes = number_votes(tr)
        top_num = max(votes, key=votes.get) if votes else None
        if ov:
            did = None if ov == "none" else ov
            tracks_out[tid] = {"driver": did, "prob": 1.0, "alts": [], "source": "manual", "number_read": top_num,
                               "share": share.get(tid, 0) / total}
            continue
        ranked = score_track(tr, drivers, models)
        best, pb = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        assigned = best if (best is not None and pb >= min_prob and pb - second >= 0.12) else None
        tracks_out[tid] = {"driver": assigned, "prob": round(pb, 3),
                           "alts": [[d, round(p, 3)] for d, p in ranked[:4] if d is not None],
                           "source": "auto", "number_read": top_num, "share": share.get(tid, 0) / total}
    per_driver: dict[str, dict] = {}
    for tid, info in tracks_out.items():
        d = info["driver"]
        if not d:
            continue
        e = per_driver.setdefault(d, {"id": d, "share": 0.0, "prob": 0.0})
        e["share"] += info["share"]
        e["prob"] = max(e["prob"], info["prob"])
    main = None
    needs_review = False
    if tracks_out:
        biggest = max(tracks_out, key=lambda t: tracks_out[t]["share"])
        main = tracks_out[biggest]["driver"]
        if main is None and tracks_out[biggest]["share"] >= 0.25:
            needs_review = True
    dlist = sorted([d for d in per_driver.values() if d["share"] >= 0.15 or d["id"] == main],
                   key=lambda d: -d["share"])
    if clip_override is not None:
        dlist = [{"id": d, "share": 1.0 / max(1, len(clip_override)), "prob": 1.0} for d in clip_override]
        main = clip_override[0] if clip_override else None
        needs_review = False
    return {"tracks": {str(k): v for k, v in tracks_out.items()}, "drivers": dlist, "main": main,
            "needs_review": needs_review}
