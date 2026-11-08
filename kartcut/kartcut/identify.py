"""Driver identification across the whole shoot.

1. Every kart track gets appearance features: background-free colour fingerprint
   (helmet/suit zone + kart zone) and, when available, CLIP embeddings of the whole
   kart and of the helmet/suit area.
2. Features are compared track-to-track and rank-normalised over the whole shoot,
   so no hand-tuned thresholds: a similarity of 0.97 means "more alike than 97% of
   all pairs of karts today".
3. The "how alike is too alike to be a different kart" threshold is learnt from the
   footage itself: two karts in the same frame are definitely different drivers.
4. Seeds (reference shots, your corrections, confidently read race numbers) label
   the rest by nearest-neighbour with a margin. Karts you mark as "other racer"
   form their own class, so lookalikes of other competitors stay out of your bins.
5. Whatever stays unknown is grouped by look, so a whole group can be named at once.
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np

OTHER = "_other"


def norm_number(n) -> str:
    s = str(n or "").strip().lstrip("#")
    return str(int(s)) if s.isdigit() else s


def number_votes(track: dict) -> dict[str, float]:
    v: dict[str, float] = defaultdict(float)
    for num, conf in track.get("ocr", []) or []:
        v[str(num)] += float(conf)
    return dict(v)


def confident_number(track: dict) -> str | None:
    v = number_votes(track)
    tot = sum(v.values())
    if tot < 0.9:
        return None
    n, c = max(v.items(), key=lambda kv: kv[1])
    return n if c / tot >= 0.65 else None


# ---------------------------------------------------------------------- features

def _rank(M: np.ndarray, pair_ok: np.ndarray) -> np.ndarray:
    """Map each similarity to its percentile among all valid pairs (upper triangle)."""
    iu = np.triu_indices(M.shape[0], 1)
    vals = M[iu][pair_ok[iu]]
    out = np.zeros_like(M, dtype=np.float32)
    if len(vals) < 3:
        return out
    srt = np.sort(vals)
    out = (np.searchsorted(srt, M, side="right") / len(srt)).astype(np.float32)
    out[~pair_ok] = 0.0
    return out


def similarity(tracks: list[dict]) -> np.ndarray:
    n = len(tracks)
    if n == 0:
        return np.zeros((0, 0), np.float32)
    feats: list[tuple[np.ndarray, np.ndarray, str]] = []
    have_fp2 = sum(1 for t in tracks if t.get("fp2"))
    ckey = "fp2" if have_fp2 >= 0.5 * n else "hist"
    for key, kind in ((ckey, "bhatt"), ("emb", "cos"), ("emb_top", "cos")):
        avail = np.array([bool(t.get(key)) for t in tracks])
        if avail.sum() < 2:
            continue
        dim = len(next(t[key] for t in tracks if t.get(key)))
        X = np.zeros((n, dim), np.float32)
        for i, t in enumerate(tracks):
            if t.get(key) and len(t[key]) == dim:
                X[i] = np.asarray(t[key], np.float32)
            else:
                avail[i] = False
        if kind == "bhatt":
            X = np.sqrt(np.maximum(X, 0))
        else:
            X = X / np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-6)
        feats.append((X @ X.T, np.outer(avail, avail), key))
    if not feats:
        return np.zeros((n, n), np.float32)
    acc = np.zeros((n, n), np.float32)
    cnt = np.zeros((n, n), np.float32)
    for M, ok, _ in feats:
        acc += _rank(M, ok)
        cnt += ok
    S = np.where(cnt > 0, acc / np.maximum(cnt, 1), 0.0).astype(np.float32)
    np.fill_diagonal(S, 1.0)
    return S


def _same_frame_pairs(tracks: list[dict], idx) -> list[tuple[int, int]]:
    by_clip: dict[str, list[int]] = defaultdict(list)
    for i in idx:
        by_clip[tracks[i]["_cid"]].append(i)
    out = []
    for lst in by_clip.values():
        for x in range(len(lst)):
            for y in range(x + 1, len(lst)):
                if _overlap(tracks[lst[x]], tracks[lst[y]]):
                    out.append((lst[x], lst[y]))
    return out


def _overlap(a: dict, b: dict) -> bool:
    if a["_cid"] != b["_cid"]:
        return False
    return a["_t0"] <= b["_t1"] and b["_t0"] <= a["_t1"]


# ---------------------------------------------------------------------- main

def identify_project(ans: dict[str, dict], drivers: dict, overrides: dict, settings: dict,
                     cache: dict | None = None) -> tuple[dict, list[dict], dict]:
    """Returns ({(cid, tid): info}, groups, stats)."""
    tracks: list[dict] = []
    for cid, an in ans.items():
        for tr in an.get("tracks", []):
            F = tr.get("frames") or [[0.0]]
            tracks.append({**tr, "_cid": cid, "_tid": int(tr["id"]), "_t0": F[0][0], "_t1": F[-1][0]})
    n = len(tracks)
    keyidx = {(t["_cid"], t["_tid"]): i for i, t in enumerate(tracks)}

    sig = (n, tuple((t["_cid"], t["_tid"], bool(t.get("fp2")), bool(t.get("emb")), bool(t.get("emb_top")))
                    for t in tracks), (cache or {}).get("rev"))
    if cache is not None and cache.get("sig") == sig:
        S = cache["S"]
    else:
        S = similarity(tracks)
        if cache is not None:
            cache["sig"], cache["S"] = sig, S

    num_to_driver: dict[str, list[str]] = defaultdict(list)
    for did, d in drivers.items():
        if norm_number(d.get("number")):
            num_to_driver[norm_number(d.get("number"))].append(did)
    all_numbered = bool(drivers) and all(norm_number(d.get("number")) for d in drivers.values())

    # ---- seeds
    seeds: dict[int, tuple[str, str]] = {}  # idx -> (class, source)
    for cid, tmap in overrides.items():
        for tid, did in tmap.items():
            i = keyidx.get((cid, int(tid)))
            if i is None:
                continue
            if did == "none":
                seeds[i] = (OTHER, "manual")
            elif did in drivers:
                seeds[i] = (did, "manual")
    for did, d in drivers.items():
        for r in d.get("refs", []):
            i = keyidx.get((r["clip"], int(r["track"])))
            if i is not None and i not in seeds:
                seeds[i] = (did, "manual")
    for i, t in enumerate(tracks):
        if i in seeds:
            continue
        num = confident_number(t)
        if num and len(num_to_driver.get(num, [])) == 1:
            seeds[i] = (num_to_driver[num][0], "number")
        elif num and all_numbered and num not in num_to_driver:
            seeds[i] = (OTHER, "number not on entry list")

    # ---- threshold learnt from definite negatives
    neg = []
    for a, b in _same_frame_pairs(tracks, range(n)):
        neg.append(S[a, b])
    seed_items = list(seeds.items())
    for x in range(len(seed_items)):
        for y in range(x + 1, len(seed_items)):
            (ia, (ca, _)), (ib, (cb, _)) = seed_items[x], seed_items[y]
            if ca != cb and OTHER not in (ca, cb):
                neg.append(S[ia, ib])
    if len(neg) >= 20:
        T = float(np.clip(np.percentile(neg, 95) + 0.01, 0.6, 0.985))
    elif len(neg) >= 2:          # few examples: stay clear of the most similar known-different pair
        T = float(np.clip(max(neg) + 0.05, 0.6, 0.985))
    else:                        # nothing to learn from yet: only very close lookalikes
        T = 0.9
    margin_need = 0.04

    # ---- classify
    by_class: dict[str, list[int]] = defaultdict(list)
    for i, (c, _) in seeds.items():
        by_class[c].append(i)
    results: dict[tuple[str, int], dict] = {}
    for i, t in enumerate(tracks):
        key = (t["_cid"], t["_tid"])
        num_read = max(number_votes(t).items(), key=lambda kv: kv[1])[0] if t.get("ocr") else None
        if i in seeds:
            c, src = seeds[i]
            results[key] = {"driver": None if c == OTHER else c, "prob": 1.0 if src == "manual" else 0.9,
                            "alts": [], "source": src, "number_read": num_read, "other": c == OTHER}
            continue
        scores = {}
        for c, idxs in by_class.items():
            sims = sorted((float(S[i, j]) for j in idxs if not _overlap(t, tracks[j])), reverse=True)
            if not sims:
                continue
            scores[c] = float(np.mean(sims[:2]))
        votes = number_votes(t)
        tot = sum(votes.values())
        for num, v in votes.items():  # a weak number read still nudges towards that driver
            for did in num_to_driver.get(num, []):
                if did in scores:
                    scores[did] += 0.03 * min(1.0, v / max(0.5, tot))
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        alts = [[c, round(min(0.99, s), 3)] for c, s in ranked if c != OTHER][:4]
        info = {"driver": None, "prob": 0.0, "alts": alts, "source": "auto", "number_read": num_read, "other": False}
        if ranked:
            best_c, best = ranked[0]
            second = ranked[1][1] if len(ranked) > 1 else T - 0.06
            margin = best - second
            if best >= T and margin >= margin_need:
                if best_c == OTHER:
                    info["other"] = True
                    info["source"] = "looks like an other racer"
                else:
                    info["driver"] = best_c
                info["prob"] = round(float(np.clip(0.55 + 3.0 * margin + 2.0 * (best - T), 0.55, 0.99)), 3)
            else:
                info["prob"] = round(float(max(0.0, best)), 3)
        results[key] = info

    groups = _groups(tracks, S, results, T, settings)
    stats = {"threshold": round(T, 3), "negatives": len(neg), "seeds": len(seeds), "tracks": n}
    return results, groups, stats


def _groups(tracks, S, results, T, settings) -> list[dict]:
    """Cluster the still-unknown karts by look so a whole group can be named at once."""
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import squareform

    idx = [i for i, t in enumerate(tracks)
           if not results[(t["_cid"], t["_tid"])]["driver"] and not results[(t["_cid"], t["_tid"])]["other"]
           and results[(t["_cid"], t["_tid"])]["source"] != "manual" and t.get("crop")]
    if len(idx) < 2:
        return []
    sub = S[np.ix_(idx, idx)].astype(np.float64)
    D = 1.0 - sub
    # karts in the same frame can never be the same kart
    pos = {k: j for j, k in enumerate(idx)}
    for a, b in _same_frame_pairs(tracks, idx):
        D[pos[a], pos[b]] = D[pos[b], pos[a]] = 1.0
    np.fill_diagonal(D, 0.0)
    strict = {"strict": 0.0, "normal": 0.02, "loose": 0.06}.get(settings.get("grouping", "normal"), 0.02)
    cut = max(0.01, 1.0 - T + strict)
    Z = linkage(squareform(np.clip(D, 0, 1), checks=False), method="average")
    labels = fcluster(Z, t=cut, criterion="distance")
    groups: dict[int, list[int]] = defaultdict(list)
    for k, lab in zip(idx, labels):
        groups[int(lab)].append(k)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        nums: dict[str, float] = defaultdict(float)
        for k in members:
            for num, v in number_votes(tracks[k]).items():
                nums[num] += v
        top = sorted(nums.items(), key=lambda kv: -kv[1])[:3]
        members.sort(key=lambda k: -len(tracks[k].get("frames", [])))
        out.append({
            "tracks": [[tracks[k]["_cid"], tracks[k]["_tid"]] for k in members],
            "crops": [tracks[k]["crop"] for k in members[:10]],
            "clips": len({tracks[k]["_cid"] for k in members}),
            "numbers": [[num, round(v, 2)] for num, v in top],
        })
    out.sort(key=lambda g: -len(g["tracks"]))
    for i, g in enumerate(out):
        g["id"] = i
    return out[:80]


def track_share(an: dict) -> dict[int, float]:
    H = float(an.get("analysis_h") or 720)
    return {int(tr["id"]): float(sum((f[4] - f[2]) / H for f in tr["frames"])) for tr in an.get("tracks", [])}


def identify_clip(an: dict, cid: str, results: dict, clip_override: list | None) -> dict:
    share = track_share(an)
    total = sum(share.values()) or 1.0
    tracks_out = {}
    for tr in an.get("tracks", []):
        tid = int(tr["id"])
        info = dict(results.get((cid, tid), {"driver": None, "prob": 0.0, "alts": [], "source": "auto",
                                              "number_read": None, "other": False}))
        info["share"] = share.get(tid, 0) / total
        tracks_out[str(tid)] = info
    per_driver: dict[str, dict] = {}
    for info in tracks_out.values():
        d = info["driver"]
        if not d:
            continue
        e = per_driver.setdefault(d, {"id": d, "share": 0.0, "prob": 0.0})
        e["share"] += info["share"]
        e["prob"] = max(e["prob"], info["prob"])
    main = None
    needs_review = False
    if tracks_out:
        big = max(tracks_out.values(), key=lambda x: x["share"])
        main = big["driver"]
        if main is None and big["share"] >= 0.25 and not big.get("other") and big.get("source") != "manual":
            needs_review = True
    dlist = sorted([d for d in per_driver.values() if d["share"] >= 0.15 or d["id"] == main], key=lambda d: -d["share"])
    if clip_override is not None:
        dlist = [{"id": d, "share": 1.0 / max(1, len(clip_override)), "prob": 1.0} for d in clip_override]
        main = clip_override[0] if clip_override else None
        needs_review = False
    return {"tracks": tracks_out, "drivers": dlist, "main": main, "needs_review": needs_review}
