"""Project state on disk, plus the 'finalise' pass that turns analyses into bins, scores and verdicts."""

from __future__ import annotations

import math
import threading
import time
from collections import Counter
from pathlib import Path

import numpy as np

from . import colour as C
from . import config, events, identify, media, metrics
from .util import (SKIP_VIDEO_EXTS, VIDEO_EXTS, clip_id_for, log, read_json, safe_name, short_id,
                   write_json)

PROJECT_FILE = "kartcut_project.json"
EXP_Q = {"good": 1.0, "warn": 0.85, "bad": 0.55}


class Project:
    def __init__(self, root: Path, name: str | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.file = self.root / PROJECT_FILE
        data = read_json(self.file)
        if not data:
            data = {
                "version": 1,
                "name": name or self.root.name,
                "created": time.strftime("%Y-%m-%d %H:%M"),
                "settings": config.default_settings(),
                "folders": [],
                "clips": {},
                "drivers": {},
                "track_overrides": {},
                "clip_overrides": {},
                "junk_overrides": {},
                "hero": None,
                "offloads": [],
                "outputs": [],
            }
        for k, v in config.default_settings().items():
            data["settings"].setdefault(k, v)
        for k in ("track_overrides", "clip_overrides", "junk_overrides"):
            data.setdefault(k, {})
        self.data = data
        self.analyses: dict[str, dict] = {}
        self.derived: dict = read_json(self.cache / "derived.json", {}) or {}
        self.save()

    # ------------------------------------------------------------------ basics
    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def settings(self) -> dict:
        return self.data["settings"]

    def save(self) -> None:
        with self.lock:
            write_json(self.file, self.data)

    def analysis_file(self, cid: str) -> Path:
        return self.cache / "analysis" / f"{cid}.json"

    def load_analysis(self, cid: str) -> dict | None:
        with self.lock:
            if cid not in self.analyses:
                an = read_json(self.analysis_file(cid))
                if an:
                    self.analyses[cid] = an
            return self.analyses.get(cid)

    def save_analysis(self, cid: str, an: dict) -> None:
        with self.lock:
            self.analyses[cid] = an
            write_json(self.analysis_file(cid), an)
            c = self.data["clips"].get(cid)
            if c:
                c["status"] = "analysed"
                c.pop("error", None)
                c["duration"] = an["probe"]["duration"]
                c["fps"] = an["probe"]["fps"]
                c["profile"] = an["profile"]
            self.save()

    def all_analyses(self) -> dict[str, dict]:
        out = {}
        for cid, c in self.data["clips"].items():
            if c.get("status") == "analysed":
                an = self.load_analysis(cid)
                if an:
                    out[cid] = an
        return out

    # ------------------------------------------------------------------ footage
    def add_folder(self, folder: Path, progress=None) -> int:
        folder = Path(folder).resolve()
        if not folder.is_dir():
            raise FileNotFoundError(f"Folder not found: {folder}")
        files = []
        for p in sorted(folder.rglob("*")):
            if not p.is_file() or p.name.startswith("._"):
                continue
            ext = p.suffix.lower()
            if ext in VIDEO_EXTS and ext not in SKIP_VIDEO_EXTS:
                if "kartcut" in {part.lower() for part in p.relative_to(folder).parts[:-1]}:
                    continue  # never re-import our own renders
                files.append(p)
        added = 0
        known = {c["path"] for c in self.data["clips"].values()}
        for i, p in enumerate(files):
            if progress:
                progress(i / max(1, len(files)), f"Scanning {p.name}")
            if str(p) in known:
                continue
            st = p.stat()
            cid = clip_id_for(p, st.st_size, st.st_mtime)
            try:
                pr = media.probe(p)
            except Exception as e:
                log.warning("Skipping %s: %s", p, e)
                continue
            if pr.width == 0 or pr.duration <= 0:
                continue
            created = pr.sony.get("created") or pr.creation_time or time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))
            with self.lock:
                self.data["clips"][cid] = {
                    "id": cid, "path": str(p), "name": p.name, "folder": str(folder),
                    "size": st.st_size, "mtime": st.st_mtime, "created": created,
                    "duration": pr.duration, "fps": pr.fps, "width": pr.width, "height": pr.height,
                    "codec": pr.codec, "camera": pr.sony.get("camera", ""), "status": "new",
                }
            added += 1
        with self.lock:
            if str(folder) not in self.data["folders"]:
                self.data["folders"].append(str(folder))
            self.save()
        return added

    def remove_folder(self, folder: str) -> None:
        with self.lock:
            self.data["folders"] = [f for f in self.data["folders"] if f != folder]
            for cid in [cid for cid, c in self.data["clips"].items() if c.get("folder") == folder]:
                self.data["clips"].pop(cid, None)
                self.analyses.pop(cid, None)
            self.save()

    # ------------------------------------------------------------------ drivers
    def upsert_driver(self, d: dict) -> str:
        with self.lock:
            did = d.get("id") or short_id("d")
            cur = self.data["drivers"].get(did, {"id": did, "refs": [], "include_incidents": None})
            for k in ("number", "name", "category", "team", "include_incidents"):
                if k in d:
                    cur[k] = d[k]
            cur["number"] = str(cur.get("number", "")).strip().lstrip("#")
            self.data["drivers"][did] = cur
            self.save()
            return did

    def import_entry_list(self, text: str) -> int:
        """Paste from Excel/Sheets/CSV: number, name, category [, team]. Header row optional."""
        n = 0
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            sep = "\t" if "\t" in line else ("," if "," in line else (";" if ";" in line else None))
            parts = [p.strip() for p in (line.split(sep) if sep else line.split(None, 1))]
            if not parts or not parts[0].lstrip("#").strip().isdigit():
                continue  # header or junk
            num = parts[0].lstrip("#").strip()
            name = parts[1] if len(parts) > 1 else f"#{num}"
            cat = parts[2] if len(parts) > 2 else ""
            team = parts[3] if len(parts) > 3 else ""
            existing = next((d for d in self.data["drivers"].values()
                             if d.get("number") == num and d.get("category", "") == cat), None)
            self.upsert_driver({"id": existing["id"] if existing else None, "number": num, "name": name,
                                "category": cat, "team": team})
            n += 1
        return n

    def delete_driver(self, did: str) -> None:
        with self.lock:
            self.data["drivers"].pop(did, None)
            for tmap in self.data["track_overrides"].values():
                for k in [k for k, v in tmap.items() if v == did]:
                    tmap.pop(k)
            for cid in list(self.data["clip_overrides"]):
                self.data["clip_overrides"][cid] = [d for d in self.data["clip_overrides"][cid] if d != did]
            self.save()

    def set_reference(self, did: str, cid: str, tid: int, add: bool = True) -> None:
        with self.lock:
            d = self.data["drivers"][did]
            refs = [r for r in d.get("refs", []) if not (r["clip"] == cid and int(r["track"]) == int(tid))]
            if add:
                refs.append({"clip": cid, "track": int(tid)})
            d["refs"] = refs
            self.save()

    def assign_track(self, cid: str, tid: int, did: str | None) -> None:
        with self.lock:
            tmap = self.data["track_overrides"].setdefault(cid, {})
            if did is None:
                tmap.pop(str(tid), None)
            else:
                tmap[str(tid)] = did
            self.save()

    def driver_label(self, did: str | None) -> str:
        d = self.data["drivers"].get(did or "")
        if not d:
            return "Unknown"
        return f"#{d.get('number', '?')} {d.get('name', '')}".strip()

    # ------------------------------------------------------------------ finalise
    def finalise(self) -> dict:
        """Recompute everything derived from analyses. No decoding, takes a second or two."""
        with self.lock:
            ans = self.all_analyses()
            s = self.settings
            # sharpness normalisation across the whole shoot
            vals = [f[6] for an in ans.values() for tr in an.get("tracks", []) for f in tr["frames"]]
            if len(vals) >= 10:
                qs = np.percentile(vals, np.arange(5, 100, 5))
            else:
                qs = np.linspace(0.05, 1.5, 19)
            levels = np.arange(5, 100, 5) / 100.0

            def sharp_rank(x, qs=qs):
                return float(min(1.0, np.interp(x, qs, levels) / 0.6))

            # exposure verdicts need the session median
            abs_ev = {cid: _abs_ev(an.get("exposure") or {}) for cid, an in ans.items()}
            casts = {cid: (an.get("exposure") or {}).get("cast_mired", 0.0) for cid, an in ans.items()}
            med_ev = float(np.median(list(abs_ev.values()))) if abs_ev else 0.0
            med_cast = float(np.median(list(casts.values()))) if casts else 0.0

            models = identify.driver_models(self.data["drivers"], ans, self.data["track_overrides"],
                                            bool(s.get("learn_from_corrections", True)))
            derived = {"clips": {}, "segments": [], "updated": time.time(),
                       "session": {"median_ev": med_ev, "median_cast": med_cast}}
            junk_base = {}
            for cid, an in ans.items():
                exp = an.get("exposure") or {}
                prof = C.get_profile(an.get("profile"))
                verdict = exposure_verdict(exp, prof, abs_ev[cid], med_ev, casts[cid], med_cast)
                ev = events.detect_events(an)
                segs = events.build_segments(an, ev, sharp_rank, EXP_Q[verdict["level"]])
                ident = identify.identify_clip(an, self.data["drivers"], models,
                                               self.data["track_overrides"].get(cid, {}),
                                               self.data["clip_overrides"].get(cid),
                                               float(s.get("id_min_prob", 0.55)))
                qual = _quality(an, ident, sharp_rank)
                junk_base[cid] = _junk_reasons(an, qual, float(s.get("junk_min_duration", 1.0)))
                tags = sorted({e["type"] for e in ev["events"]} - {"incident"})
                if any(e["type"] == "incident" for e in ev["events"]):
                    tags.append("incident")
                tags += qual["flags"]
                derived["clips"][cid] = {"exposure": verdict, "events": ev["events"], "identity": ident,
                                         "quality": qual, "tags": tags,
                                         "best_score": max((x["score"] for x in segs), default=0.0)}
                tmap = ident["tracks"]
                for sg in segs:
                    sg["drivers"] = [tmap[str(t)]["driver"] for t in sg["tracks"]
                                     if str(t) in tmap and tmap[str(t)]["driver"]]
                    mt = sg.get("main_track")
                    sg["main_driver"] = tmap[str(mt)]["driver"] if mt is not None and str(mt) in tmap else None
                    if sg["label"].startswith("overtake:"):
                        _, by, on = sg["label"].split(":")
                        sg["label"] = f"Overtake: {self._track_label(tmap, by)} on {self._track_label(tmap, on)}"
                    if ident["drivers"] and not sg["drivers"] and sg["type"] in ("podium", "celebration", "grid", "broll"):
                        sg["drivers"] = [d["id"] for d in ident["drivers"]]
                        sg["main_driver"] = ident["main"]
                derived["segments"].extend(segs)
            dups = _duplicates(ans, self.data["clips"])
            for cid, info in derived["clips"].items():
                reasons = list(junk_base.get(cid, []))
                if cid in dups:
                    reasons.append(f"Duplicate of {self.data['clips'].get(dups[cid], {}).get('name', dups[cid])}")
                ov = self.data["junk_overrides"].get(cid)
                info["junk"] = {"auto": bool(reasons), "reasons": reasons,
                                "is_junk": bool(reasons) if ov is None else bool(ov), "override": ov}
            self.derived = derived
            write_json(self.cache / "derived.json", derived)
            return derived

    def _track_label(self, tmap: dict, tid: str) -> str:
        info = tmap.get(str(tid))
        if info and info.get("driver"):
            return self.driver_label(info["driver"])
        if info and info.get("number_read"):
            return f"#{info['number_read']}"
        return "kart"

    def hero_clip(self) -> str | None:
        hero = self.data.get("hero")
        clips = self.derived.get("clips", {})
        if hero in clips:
            return hero
        best, best_s = None, -1e9
        for cid, info in clips.items():
            if info.get("junk", {}).get("is_junk"):
                continue
            e = info["exposure"]
            an = self.load_analysis(cid) or {}
            s = -abs(e.get("ev_rel", 0)) - abs(e.get("cast_rel", 0)) / 40.0 - (2.0 if e["level"] == "bad" else 0) \
                + an.get("exposure", {}).get("ref_conf", 0) * 2 + (0.6 if an.get("tracks") else 0.0)
            if s > best_s:
                best, best_s = cid, s
        return best

    # ------------------------------------------------------------------ UI views
    def clip_summaries(self) -> list[dict]:
        with self.lock:
            return self._clip_summaries()

    def _clip_summaries(self) -> list[dict]:
        out = []
        dclips = self.derived.get("clips", {})
        for cid, c in self.data["clips"].items():
            d = dclips.get(cid, {})
            an = self.analyses.get(cid) or (self.load_analysis(cid) if c.get("status") == "analysed" else None) or {}
            ident = d.get("identity", {})
            item = {
                "id": cid, "name": c["name"], "path": c["path"], "folder": c.get("folder"), "status": c.get("status"),
                "error": c.get("error"), "duration": c.get("duration"), "fps": c.get("fps"),
                "created": c.get("created"), "camera": c.get("camera", ""),
                "res": f"{c.get('width')}x{c.get('height')}",
                "thumb": an.get("thumb"), "proxy": c.get("proxy"), "waveform": an.get("waveform"),
                "profile": an.get("profile"), "orientation": an.get("orientation"),
                "orientation_source": an.get("orientation_source"),
                "drivers": [{"id": x["id"], "label": self.driver_label(x["id"]), "share": round(x["share"], 2),
                             "prob": x["prob"]} for x in ident.get("drivers", [])],
                "main_driver": ident.get("main"),
                "needs_review": ident.get("needs_review", False),
                "junk": d.get("junk", {}),
                "exposure": d.get("exposure", {}),
                "quality": d.get("quality", {}),
                "tags": d.get("tags", []),
                "best_score": d.get("best_score", 0),
                "category": self._clip_category(ident),
            }
            out.append(item)
        out.sort(key=lambda x: (x.get("created") or "", x["name"]))
        return out

    def _clip_category(self, ident: dict) -> str:
        main = ident.get("main")
        if main and main in self.data["drivers"]:
            return self.data["drivers"][main].get("category", "")
        return ""

    def clip_detail(self, cid: str) -> dict:
        with self.lock:
            return self._clip_detail(cid)

    def _clip_detail(self, cid: str) -> dict:
        c = self.data["clips"][cid]
        an = self.load_analysis(cid) or {}
        d = self.derived.get("clips", {}).get(cid, {})
        ident = d.get("identity", {})
        tracks = []
        for tr in an.get("tracks", []):
            info = ident.get("tracks", {}).get(str(tr["id"]), {})
            F = tr["frames"]
            tracks.append({
                "id": tr["id"], "crop": tr.get("crop"), "t0": F[0][0], "t1": F[-1][0], "n": len(F),
                "driver": info.get("driver"), "driver_label": self.driver_label(info.get("driver")) if info.get("driver") else None,
                "prob": info.get("prob"), "source": info.get("source"), "number_read": info.get("number_read"),
                "alts": [{"id": a, "label": self.driver_label(a), "prob": p} for a, p in info.get("alts", [])],
                "share": round(info.get("share", 0), 3),
                "is_ref": any(r["clip"] == cid and int(r["track"]) == int(tr["id"])
                              for dd in self.data["drivers"].values() for r in dd.get("refs", [])),
            })
        tracks.sort(key=lambda t: -t["share"])
        segs = [s for s in self.derived.get("segments", []) if s["clip"] == cid]
        return {
            "clip": c, "summary": next((s for s in self._clip_summaries() if s["id"] == cid), {}),
            "analysis": {k: an.get(k) for k in ("profile", "profile_source", "orientation", "orientation_source",
                                                 "detector", "analysis_fps", "seconds", "transpose")},
            "probe": an.get("probe", {}), "exposure_raw": {k: v for k, v in (an.get("exposure") or {}).items()},
            "motion": {"shake": (an.get("motion") or {}).get("shake")},
            "tracks": tracks, "segments": segs, "events": d.get("events", []),
        }

    def bins(self) -> dict:
        """Counts for the sidebar: categories -> drivers, plus the special bins."""
        clips = self.clip_summaries()
        per_driver = Counter()
        special = Counter()
        for c in clips:
            if c["status"] != "analysed":
                special["pending"] += 1
                continue
            if c["junk"].get("is_junk"):
                special["junk"] += 1
                continue
            if c["needs_review"]:
                special["review"] += 1
            if not c["drivers"]:
                special["broll" if "broll" in c["tags"] or not c["needs_review"] else "unassigned"] += 1
            if len(c["drivers"]) > 1:
                special["multi"] += 1
            if c["orientation"] == "vertical":
                special["vertical"] += 1
            if c["exposure"].get("level") in ("warn", "bad"):
                special["exposure"] += 1
            if "incident" in c["tags"]:
                special["incident"] += 1
            for d in c["drivers"]:
                per_driver[d["id"]] += 1
        cats: dict[str, list] = {}
        for did, d in sorted(self.data["drivers"].items(), key=lambda kv: (kv[1].get("category", ""),
                                                                          _num_key(kv[1].get("number", "")))):
            cats.setdefault(d.get("category") or "Uncategorised", []).append(
                {"id": did, "label": self.driver_label(did), "count": per_driver.get(did, 0)})
        return {"categories": cats, "special": dict(special), "total": len(clips)}


# ---------------------------------------------------------------------- helpers

def _num_key(n: str):
    return (0, int(n)) if str(n).isdigit() else (1, str(n))


def _abs_ev(exp: dict) -> float:
    if not exp:
        return 0.0
    if exp.get("ref_conf", 0) >= 0.03:
        return math.log2(max(exp.get("ref", 0.11), 1e-5) / 0.11)
    return math.log2(max(exp.get("key", 0.12), 1e-5) / 0.12)


def exposure_verdict(exp: dict, prof: C.Profile, abs_ev: float, med_ev: float, cast: float, med_cast: float) -> dict:
    if not exp:
        return {"level": "good", "issues": [], "ev": 0.0, "ev_rel": 0.0}
    rel = abs_ev - med_ev
    cast_rel = cast - med_cast
    issues: list[tuple[str, str]] = []
    if exp.get("subj_clip", 0) > 0.01:
        issues.append(("bad", f"Highlights clipped on kart/driver ({exp['subj_clip'] * 100:.0f}% of subject)"))
    elif exp.get("clip", 0) > 0.08:
        issues.append(("warn", f"{exp['clip'] * 100:.0f}% of frame clipped (usually sky)"))
    if abs_ev < -1.7 or rel < -1.3:
        lvl = "bad" if (abs_ev < -2.5 or rel < -2.0) else "warn"
        issues.append((lvl, f"Under-exposed: {rel:+.1f} st vs session, {abs_ev:+.1f} st absolute"))
    if prof.is_log and exp.get("near", 0) > 0.05 and abs_ev > 2.3:
        issues.append(("warn", f"Running hot: {abs_ev:+.1f} st, close to clipping"))
    if not prof.is_log and (abs_ev > 1.3 or rel > 1.3):
        issues.append(("warn", f"Over-exposed: {rel:+.1f} st vs session"))
    if exp.get("shadow", 0) > 0.25:
        issues.append(("warn", f"{exp['shadow'] * 100:.0f}% deep shadow, expect noise"))
    if exp.get("drift_ev", 0) > 0.7:
        issues.append(("warn", f"Exposure shifts {exp['drift_ev']:.1f} st during clip (cloud or auto ISO)"))
    if exp.get("drift_mired", 0) > 25:
        issues.append(("warn", f"White balance shifts {exp['drift_mired']:.0f} mired during clip"))
    if abs(cast_rel) > 20:
        issues.append(("warn", f"{'Warmer' if cast_rel > 0 else 'Cooler'} than session by {abs(cast_rel):.0f} mired"))
    level = "bad" if any(l == "bad" for l, _ in issues) else ("warn" if issues else "good")
    return {"level": level, "issues": [m for _, m in issues], "ev": round(abs_ev, 2), "ev_rel": round(rel, 2),
            "clip_pct": round(exp.get("clip", 0) * 100, 1), "subj_clip_pct": round(exp.get("subj_clip", 0) * 100, 1),
            "cct": exp.get("cct"), "cast": round(cast, 1), "cast_rel": round(cast_rel, 1)}


def _quality(an: dict, ident: dict, sharp_rank) -> dict:
    mot = an.get("motion") or {}
    shake = float(mot.get("shake") or 0.0)
    stab = float(np.clip(1.0 - (shake - 0.15) / 0.6, 0.0, 1.0))
    flags = []
    tracks = an.get("tracks", [])
    focus = None
    pan_frac = 0.0
    if tracks:
        tmap = ident.get("tracks", {})
        main = max(tracks, key=lambda t: tmap.get(str(t["id"]), {}).get("share", 0))
        focus = float(np.median([sharp_rank(f[6]) for f in main["frames"]]))
        kin = events.track_kinematics(an).get(int(main["id"]))
        if kin is not None:
            pan_frac = float(kin.pan.mean())
        if focus < 0.25:
            flags.append("missed pan" if pan_frac >= 0.5 else "soft focus")
        elif pan_frac >= 0.5 and focus >= 0.6:
            flags.append("sharp pan")
    if shake > 0.45:
        flags.append("shaky")
    return {"shake": round(shake, 3), "stability": round(stab, 2),
            "focus": round(focus, 2) if focus is not None else None, "pan": round(pan_frac, 2), "flags": flags}


def _junk_reasons(an: dict, qual: dict, min_dur: float) -> list[str]:
    r = []
    dur = float(an.get("probe", {}).get("duration") or 0)
    if dur < min_dur:
        r.append(f"Too short ({dur:.1f}s)")
    exp = an.get("exposure") or {}
    frames = an.get("frames", [])
    if exp and exp.get("key", 1) < 0.003 and np.mean([f.get("luma", 0) for f in frames] or [0]) < 0.07:
        r.append("Black frame (lens cap?)")
    if exp and exp.get("clip", 0) > 0.6:
        r.append("Blown out")
    no_subject = not an.get("tracks") and not any(f.get("p", 0) for f in frames)
    if no_subject and frames:
        detail = float(np.median([f.get("det", 0) for f in frames]))
        pans = (an.get("motion") or {}).get("pan_speed") or [0]
        if detail < 0.06:
            r.append("No detail (ground, sky or out of focus)")
        elif qual["shake"] > 0.8 or float(np.mean(pans)) > 0.6:
            r.append("Accidental record (no subject, camera moving)")
    return r


def _duplicates(ans: dict[str, dict], clips: dict) -> dict[str, str]:
    """Same file imported twice (card copy + backup, re-copied folder).

    Needs matching duration, size and picture. Picture alone isn't enough: a tripod
    at one corner gives near-identical frames for every kart that passes.
    """
    items = []
    for cid, an in ans.items():
        hs = [int(h) for h in an.get("hashes", [])]
        c = clips.get(cid, {})
        nums = {o[0] for tr in an.get("tracks", []) for o in tr.get("ocr", [])}
        if hs:
            items.append((cid, float(an["probe"]["duration"]), hs, c.get("mtime", 0), float(c.get("size") or 0),
                          c.get("name", ""), nums))
    items.sort(key=lambda x: (x[3], x[0]))
    dup: dict[str, str] = {}
    for i in range(len(items)):
        for j in range(i):
            a, b = items[i], items[j]
            if b[0] in dup:
                continue
            if abs(a[1] - b[1]) > 0.01 * max(a[1], b[1]) + 0.05:
                continue
            same_size = abs(a[4] - b[4]) <= 0.015 * max(a[4], b[4], 1.0)
            if not (same_size or a[5] == b[5]):
                continue
            if a[6] and b[6] and not (a[6] & b[6]):
                continue
            k = min(len(a[2]), len(b[2]))
            if k and np.mean([metrics.hamming(a[2][x], b[2][x]) for x in range(k)]) <= 5:
                dup[a[0]] = b[0]
                break
    return dup


def project_dir_for(name: str) -> Path:
    return config.HOME_DIR / safe_name(name)
