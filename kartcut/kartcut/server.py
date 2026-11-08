"""Local web server: the browser UI talks to this. Binds to 127.0.0.1 only."""

from __future__ import annotations

import concurrent.futures as cf
import os
import string
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import (__version__, analyse, autocut, beats, colour, config, export_nle, fingerprint, offload, render,
               shotmatch, sortdisk)
from .detect import Models
from .jobs import Job, JobQueue
from .project import Project, project_dir_for
from .util import (AUDIO_EXTS, IMAGE_EXTS, IS_WINDOWS, LUT_EXTS, VIDEO_EXTS, Cancelled, log, open_in_os,
                   read_json, safe_name, write_json)

STATIC = Path(__file__).parent / "static"
RECENT = config.HOME_DIR / "recent.json"

app = FastAPI(title="KartCut", version=__version__)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class State:
    project: Project | None = None
    models: Models | None = None
    jobs = JobQueue()
    plans: list[autocut.Timeline] = []
    plan_notes: list[str] = []
    music: dict | None = None


S = State()


def proj() -> Project:
    if S.project is None:
        raise HTTPException(400, "Open or create a project first")
    return S.project


def models() -> Models:
    if S.models is None:
        S.models = Models(proj().settings)
    return S.models


def _remember(p: Project) -> None:
    rec = [r for r in (read_json(RECENT, []) or []) if r.get("path") != str(p.root)]
    rec.insert(0, {"path": str(p.root), "name": p.data.get("name"), "opened": time.strftime("%Y-%m-%d %H:%M")})
    write_json(RECENT, rec[:12])


def open_project(path: Path, name: str | None = None) -> Project:
    S.project = Project(path, name)
    S.models = None
    S.plans = []
    S.music = None
    _remember(S.project)
    return S.project


def startup_open_last() -> None:
    rec = read_json(RECENT, []) or []
    for r in rec:
        p = Path(r["path"])
        if (p / "kartcut_project.json").exists():
            try:
                open_project(p)
                log.info("Opened last project %s", p)
            except Exception as e:
                log.warning("Could not reopen %s: %s", p, e)
            return


# ---------------------------------------------------------------------- pages

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/media/{rel:path}")
def media_file(rel: str):
    p = proj()
    f = (p.root / rel).resolve()
    if p.root not in f.parents or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f)


# ---------------------------------------------------------------------- status / project

@app.get("/api/status")
def status():
    p = S.project
    return {
        "version": __version__,
        "system": config.system_status(),
        "models": S.models.status if S.models else {},
        "project": None if p is None else {"name": p.data.get("name"), "root": str(p.root),
                                           "folders": p.data.get("folders", []),
                                           "clips": len(p.data["clips"]),
                                           "analysed": sum(1 for c in p.data["clips"].values() if c.get("status") == "analysed"),
                                           "hero": p.data.get("hero"), "auto_hero": p.hero_clip() if p.derived else None,
                                           "shot_match": p.data.get("shot_match"), "outputs": p.data.get("outputs", [])[-40:],
                                           "offloads": p.data.get("offloads", [])[-10:]},
        "settings": p.settings if p else config.default_settings(),
        "profiles": colour.profile_choices(),
        "recent": read_json(RECENT, []) or [],
        "busy": S.jobs.busy(),
        "home": str(config.HOME_DIR),
    }


@app.post("/api/project/new")
def project_new(body: dict = Body(...)):
    name = (body.get("name") or "").strip() or time.strftime("Race %Y-%m-%d")
    path = Path(body["path"]) / safe_name(name) if body.get("path") else project_dir_for(name)
    if (path / "kartcut_project.json").exists():
        raise HTTPException(400, f"A project already exists at {path}")
    p = open_project(path, name)
    return {"root": str(p.root)}


@app.post("/api/project/open")
def project_open(body: dict = Body(...)):
    path = Path(body["path"])
    if not (path / "kartcut_project.json").exists():
        raise HTTPException(404, "No KartCut project in that folder")
    p = open_project(path)
    return {"root": str(p.root)}


@app.post("/api/settings")
def settings_update(body: dict = Body(...)):
    p = proj()
    with p.lock:
        for k, v in body.items():
            if k in config.DEFAULT_SETTINGS:
                p.settings[k] = v
        p.save()
    if S.models and any(k in body for k in ("detector", "ocr", "appearance_embeddings")):
        S.models = None
    return p.settings


@app.get("/api/fs")
def fs_list(path: str = "", kind: str = "dir"):
    exts = {"video": VIDEO_EXTS, "audio": AUDIO_EXTS, "lut": LUT_EXTS, "image": IMAGE_EXTS}.get(kind, set())
    if not path:
        roots = []
        if IS_WINDOWS:
            for letter in string.ascii_uppercase:
                d = f"{letter}:\\"
                if os.path.exists(d):
                    roots.append({"name": d, "path": d})
        else:
            for d in ["/", str(Path.home()), "/media", "/mnt", "/Volumes"]:
                if os.path.isdir(d):
                    roots.append({"name": d, "path": d})
        return {"path": "", "parent": None, "dirs": roots, "files": []}
    p = Path(path)
    if not p.is_dir():
        raise HTTPException(404, "Folder not found")
    dirs, files = [], []
    try:
        for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
            if e.name.startswith((".", "$")):
                continue
            try:
                if e.is_dir():
                    dirs.append({"name": e.name, "path": e.path})
                elif exts and Path(e.name).suffix.lower() in exts:
                    files.append({"name": e.name, "path": e.path, "size": e.stat().st_size})
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, "No permission to read that folder")
    parent = str(p.parent) if p.parent != p else ""
    return {"path": str(p), "parent": parent, "dirs": dirs, "files": files}


@app.post("/api/open")
def os_open(body: dict = Body(...)):
    path = Path(body["path"])
    if not path.exists():
        raise HTTPException(404, "Not found")
    open_in_os(path if path.is_dir() else path.parent if body.get("folder") else path)
    return {"ok": True}


# ---------------------------------------------------------------------- footage

@app.post("/api/footage/add")
def footage_add(body: dict = Body(...)):
    p = proj()
    folder = Path(body["folder"])

    def fn(job: Job):
        n = p.add_folder(folder, progress=job.set)
        return {"added": n}

    return S.jobs.submit("scan", f"Scan {folder.name or folder}", fn).view()


@app.post("/api/footage/remove")
def footage_remove(body: dict = Body(...)):
    p = proj()
    p.remove_folder(body["folder"])
    p.finalise()
    return {"ok": True}


@app.post("/api/analyse")
def analyse_start(body: dict = Body(default={})):
    p = proj()
    ids = body.get("clip_ids") or [cid for cid, c in p.data["clips"].items()
                                   if body.get("force") or c.get("status") in ("new", "error")]
    if not ids:
        raise HTTPException(400, "Nothing to analyse: all clips are done. Use force to redo.")
    m = models()

    def fn(job: Job):
        settings = p.settings
        job.set(0.0, "Loading AI models (first run downloads them, a few minutes)")
        _ = m.detector, m.ocr, m.pose, m.embedder
        proxies = cf.ThreadPoolExecutor(max_workers=1) if settings.get("make_proxies", True) else None
        pending = []
        done = errors = 0
        lut_dir = p.cache / "luts"
        for i, cid in enumerate(ids):
            if job.cancelled():
                raise Cancelled()
            c = p.data["clips"].get(cid)
            if not c:
                continue
            base = i / len(ids)

            def prog(f, msg, base=base):
                job.set(base + f / len(ids), f"[{i + 1}/{len(ids)}] {msg}")

            try:
                an = analyse.analyse_clip(c, p.root, settings, m, progress=prog, cancelled=job.cancelled)
                p.save_analysis(cid, an)
                done += 1
                if proxies:
                    out = p.cache / "proxies" / f"{cid}.mp4"
                    pending.append((cid, proxies.submit(analyse.make_proxy, Path(c["path"]).resolve(), an["probe"],
                                                        an["profile"], an["transpose"], out.resolve(),
                                                        lut_dir.resolve(), int(settings.get("proxy_height", 540)))))
            except Cancelled:
                raise
            except Exception as e:
                log.exception("Analysis failed for %s", c["name"])
                with p.lock:
                    c["status"] = "error"
                    c["error"] = str(e)[:400]
                    p.save()
                errors += 1
            if (i + 1) % 10 == 0:
                p.finalise()
        job.set(0.97, "Fingerprinting karts (helmet, suit, kart colours)")
        fingerprint.refresh(p, m)
        job.set(0.99, "Sorting drivers and scoring highlights")
        p.finalise()
        if proxies:
            for cid, fut in pending:
                try:
                    fut.result()
                    with p.lock:
                        p.data["clips"][cid]["proxy"] = f"cache/proxies/{cid}.mp4"
                except Exception as e:
                    log.warning("Proxy failed for %s: %s", cid, e)
            p.save()
            proxies.shutdown()
        return {"analysed": done, "errors": errors, "models": m.status}

    return S.jobs.submit("analyse", f"Analyse {len(ids)} clips", fn).view()


@app.post("/api/finalise")
def finalise():
    proj().finalise()
    return {"ok": True}


@app.get("/api/clips")
def clips():
    p = proj()
    return {"clips": p.clip_summaries(), "bins": p.bins(), "drivers": list(p.data["drivers"].values())}


@app.get("/api/clips/{cid}")
def clip_detail(cid: str):
    p = proj()
    if cid not in p.data["clips"]:
        raise HTTPException(404)
    return p.clip_detail(cid)


@app.post("/api/clips/{cid}/assign")
def clip_assign(cid: str, body: dict = Body(...)):
    p = proj()
    did = body.get("driver")
    p.assign_track(cid, int(body["track"]), did if did else None)
    p.finalise()
    return p.clip_detail(cid)


@app.post("/api/clips/{cid}/drivers")
def clip_drivers(cid: str, body: dict = Body(...)):
    p = proj()
    with p.lock:
        if body.get("drivers") is None:
            p.data["clip_overrides"].pop(cid, None)
        else:
            p.data["clip_overrides"][cid] = list(body["drivers"])
        p.save()
    p.finalise()
    return p.clip_detail(cid)


@app.post("/api/clips/move")
def clips_move(body: dict = Body(...)):
    """Put clips in the right driver's bin. mode: move (replace), add, remove.
    Moving also labels the clip's main kart, so the matcher learns from it."""
    from .identify import track_share

    p = proj()
    did = body.get("driver") or None
    mode = body.get("mode", "move")
    if did is not None and did not in p.data["drivers"]:
        raise HTTPException(400, "Unknown driver")
    with p.lock:
        for cid in body.get("clips", []):
            if cid not in p.data["clips"]:
                continue
            ident = p.derived.get("clips", {}).get(cid, {}).get("identity", {})
            current = [d["id"] for d in ident.get("drivers", [])]
            if mode == "add":
                new = current + ([did] if did and did not in current else [])
            elif mode == "remove":
                new = [d for d in current if d != did]
            else:
                new = [did] if did else []
            p.data["clip_overrides"][cid] = new
            an = p.load_analysis(cid) or {}
            share = track_share(an)
            if share and mode == "move":
                main = max(share, key=share.get)
                p.data["track_overrides"].setdefault(cid, {})[str(main)] = did if did else "none"
        p.save()
    p.finalise()
    return {"ok": True}


@app.post("/api/clips/{cid}/junk")
def clip_junk(cid: str, body: dict = Body(...)):
    p = proj()
    with p.lock:
        v = body.get("junk")
        if v is None:
            p.data["junk_overrides"].pop(cid, None)
        else:
            p.data["junk_overrides"][cid] = bool(v)
        p.save()
    p.finalise()
    return {"ok": True}


@app.post("/api/clips/{cid}/reanalyse")
def clip_reanalyse(cid: str, body: dict = Body(default={})):
    p = proj()
    with p.lock:
        c = p.data["clips"][cid]
        if "transpose" in body:
            c["transpose_override"] = int(body["transpose"])
        if "profile" in body:
            c["profile_override"] = body["profile"] or None
        c["status"] = "new"
        p.save()
    return analyse_start({"clip_ids": [cid]})


# ---------------------------------------------------------------------- drivers

@app.get("/api/drivers")
def drivers():
    return list(proj().data["drivers"].values())


@app.post("/api/drivers")
def driver_upsert(body: dict = Body(...)):
    p = proj()
    did = p.upsert_driver(body)
    p.finalise()
    return p.data["drivers"][did]


@app.post("/api/drivers/import")
def drivers_import(body: dict = Body(...)):
    p = proj()
    n = p.import_entry_list(body.get("text", ""))
    p.finalise()
    return {"imported": n}


@app.delete("/api/drivers/{did}")
def driver_delete(did: str):
    p = proj()
    p.delete_driver(did)
    p.finalise()
    return {"ok": True}


@app.post("/api/drivers/{did}/ref")
def driver_ref(did: str, body: dict = Body(...)):
    p = proj()
    if did not in p.data["drivers"]:
        raise HTTPException(404)
    add = body.get("add", True)
    p.set_reference(did, body["clip"], int(body["track"]), add)
    if add:  # a reference is also a confirmed identity for that track
        p.assign_track(body["clip"], int(body["track"]), did)
    p.finalise()
    return p.data["drivers"][did]


@app.get("/api/drivers/candidates")
def driver_candidates():
    """Every kart in the project for the sorting desk, with its status and lookalike group."""
    p = proj()
    out = []
    groups = p.derived.get("track_group", {})
    sizes: dict[int, int] = {}
    for g in groups.values():
        sizes[g] = sizes.get(g, 0) + 1
    for cid, info in p.derived.get("clips", {}).items():
        an = p.load_analysis(cid) or {}
        tmap = info.get("identity", {}).get("tracks", {})
        ovr = p.data["track_overrides"].get(cid, {})
        for tr in an.get("tracks", []):
            if not tr.get("crop"):
                continue
            t = tmap.get(str(tr["id"]), {})
            notkart = ovr.get(str(tr["id"])) == "notkart"
            gid = groups.get(f"{cid}|{tr['id']}", -1)
            out.append({"clip": cid, "clip_name": p.data["clips"][cid]["name"], "track": tr["id"], "crop": tr["crop"],
                        "orientation": an.get("orientation", "horizontal"), "driver": None if notkart else t.get("driver"),
                        "number_read": t.get("number_read"), "other": bool(t.get("other")) and not notkart,
                        "notkart": notkart, "source": "manual" if notkart else t.get("source"),
                        "prob": t.get("prob", 0), "group": gid, "group_size": sizes.get(gid, 1),
                        "t0": tr["frames"][0][0], "t1": tr["frames"][-1][0], "frames": len(tr["frames"])})
    out.sort(key=lambda x: (-x["group_size"], x["group"], -x["frames"]))
    return out


@app.post("/api/karts/assign")
def karts_assign(body: dict = Body(...)):
    """Sort karts from the desk. driver: a driver id, "none" (other racer), "notkart"
    (wrong detection: removed everywhere) or null (back to automatic)."""
    p = proj()
    did = body.get("driver")
    if did is not None and did not in p.data["drivers"] and did not in ("none", "notkart"):
        raise HTTPException(400, "Unknown driver")
    with p.lock:
        for cid, tid in body.get("tracks", []):
            tmap = p.data["track_overrides"].setdefault(cid, {})
            if did is None:
                tmap.pop(str(int(tid)), None)
            else:
                tmap[str(int(tid))] = did
        p.save()
    p.finalise()
    return {"ok": True, "stats": p.derived.get("id_stats", {})}


@app.get("/api/karts/context")
def kart_context(clip: str, track: int):
    p = proj()
    rel = fingerprint.context_image(p, clip, int(track))
    if not rel:
        raise HTTPException(404, "Could not read that frame from the clip")
    return {"url": rel}


@app.post("/api/rematch")
def rematch(body: dict = Body(default={})):
    p = proj()
    m = models()

    def fn(job: Job):
        hd = fingerprint.hd_crops(p, progress=lambda f, msg: job.set(0.6 * f, msg), cancelled=job.cancelled)
        job.set(0.6, "Loading appearance model")
        n = fingerprint.refresh(p, m, progress=lambda f, msg: job.set(0.6 + 0.35 * f, msg),
                                force=bool(body.get("force")))
        job.set(0.98, "Matching karts to drivers")
        p.finalise()
        st = p.derived.get("id_stats", {})
        return {"clips_updated": n, "hd_previews": hd, **st}

    return S.jobs.submit("rematch", "Re-match karts", fn).view()


def _groups_payload(p: Project) -> dict:
    """Groups plus the orientation of each member's clip, for the Horizontal / Vertical toggle."""
    orient = {cid: (p.load_analysis(cid) or {}).get("orientation", "horizontal") for cid in p.derived.get("clips", {})}
    out = []
    for g in p.derived.get("groups", []):
        out.append({**g, "orients": [orient.get(cid, "horizontal") for cid, _ in g["tracks"]]})
    return {"groups": out, "stats": p.derived.get("id_stats", {})}


@app.get("/api/groups")
def groups():
    return _groups_payload(proj())


@app.post("/api/groups/assign")
def groups_assign(body: dict = Body(...)):
    """Name a whole group of lookalike karts at once (driver id, or "none" for another racer)."""
    p = proj()
    did = body.get("driver")
    if did not in p.data["drivers"] and did != "none":
        raise HTTPException(400, "Unknown driver")
    with p.lock:
        for cid, tid in body.get("tracks", []):
            p.data["track_overrides"].setdefault(cid, {})[str(int(tid))] = did
        p.save()
    p.finalise()
    return _groups_payload(p)


# ---------------------------------------------------------------------- colour

@app.post("/api/hero")
def set_hero(body: dict = Body(...)):
    p = proj()
    with p.lock:
        p.data["hero"] = body.get("clip")
        p.save()
    return {"hero": p.hero_clip()}


@app.post("/api/shotmatch")
def shot_match():
    p = proj()
    if not p.derived.get("clips"):
        raise HTTPException(400, "Analyse footage first")

    def fn(job: Job):
        return shotmatch.run_shot_match(p, progress=job.set)

    return S.jobs.submit("shotmatch", "Shot match + exposure report", fn).view()


@app.get("/api/exposure")
def exposure_table():
    p = proj()
    rows = []
    corr = (p.data.get("shot_match") or {}).get("corrections", {})
    for c in p.clip_summaries():
        if c["status"] != "analysed":
            continue
        an = p.load_analysis(c["id"]) or {}
        e = c["exposure"]
        rows.append({"id": c["id"], "name": c["name"], "created": c["created"], "thumb": c["thumb"],
                     "driver": c["drivers"][0]["label"] if c["drivers"] else "", "level": e.get("level"),
                     "issues": e.get("issues", []), "ev": e.get("ev"), "ev_rel": e.get("ev_rel"),
                     "clip_pct": e.get("clip_pct"), "subj_clip_pct": e.get("subj_clip_pct"), "cct": e.get("cct"),
                     "cast_rel": e.get("cast_rel"), "profile": an.get("profile"), "waveform": c["waveform"],
                     "hist": (an.get("exposure") or {}).get("hist"), "junk": c["junk"].get("is_junk"),
                     "match": corr.get(c["id"])})
    return {"rows": rows, "session": p.derived.get("session", {}), "hero": p.hero_clip()}


# ---------------------------------------------------------------------- ingest / sort

@app.post("/api/offload")
def offload_start(body: dict = Body(...)):
    p = S.project
    sources = body.get("sources") or []
    dests = body.get("dests") or []
    label = body.get("label") or time.strftime("%Y-%m-%d")

    def fn(job: Job):
        res = offload.offload(sources, dests, label, progress=job.set, cancelled=job.cancelled)
        if p is not None:
            with p.lock:
                p.data.setdefault("offloads", []).append({"when": time.strftime("%Y-%m-%d %H:%M"), **res})
                p.save()
            if body.get("add_to_project", True) and res["verified"]:
                p.add_folder(Path(res["dest_folders"][0]))
        return res

    return S.jobs.submit("offload", f"Offload {len(sources)} card(s)", fn).view()


@app.post("/api/sort")
def sort_start(body: dict = Body(...)):
    p = proj()

    def fn(job: Job):
        return sortdisk.sort_on_disk(p, Path(body["dest"]), body.get("method", "link"),
                                     bool(body.get("include_junk")), progress=job.set,
                                     orientation=body.get("orientation", "all"))

    return S.jobs.submit("sort", "Sort clips into driver folders", fn).view()


# ---------------------------------------------------------------------- montage

@app.post("/api/music")
def music(body: dict = Body(...)):
    p = proj()
    path = body.get("path") or p.settings.get("music_path")
    if not path:
        S.music = None
        return {"music": None}
    start = float(body.get("start", p.settings.get("music_start", 0.0)) or 0.0)
    with p.lock:
        p.settings["music_path"] = path
        p.settings["music_start"] = start
        p.save()
    S.music = beats.analyse_music(Path(path), start, p.cache)
    m = dict(S.music)
    m.pop("energy_t", None)
    return {"music": {**m, "beats": len(m["beats"]), "energy": S.music["energy"][::4]}}


def _plans(p: Project, body: dict) -> list[autocut.Timeline]:
    s = p.settings
    aspects = body.get("aspects") or s.get("aspects") or ["16:9"]
    seed = int(body.get("seed", s.get("seed", 1)))
    music_d = S.music
    if music_d is None and s.get("music_path") and Path(s["music_path"]).exists():
        music_d = S.music = beats.analyse_music(Path(s["music_path"]), float(s.get("music_start", 0)), p.cache)
    plans = []
    notes = []
    if body.get("event", True):
        for a in aspects:
            plans.append(autocut.plan(p, "event", None, a, float(body.get("event_length") or s["event_length"]),
                                      music_d, seed))
    drivers = body.get("drivers")
    if drivers is None and body.get("reels", True):
        drivers = list(p.data["drivers"].keys())
    for did in drivers or []:
        for a in aspects:
            try:
                plans.append(autocut.plan(p, "driver", did, a, float(body.get("reel_length") or s["reel_length"]),
                                          music_d, seed))
            except RuntimeError as e:
                notes.append(f"{p.driver_label(did)}: {e}")
    if not plans:
        raise HTTPException(400, "Nothing to cut: " + ("; ".join(notes) or "no usable highlights"))
    S.plans = plans
    S.plan_notes = notes
    return plans


@app.post("/api/montage/plan")
def montage_plan(body: dict = Body(default={})):
    p = proj()
    try:
        plans = _plans(p, body)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    out = []
    for pl in plans:
        d = pl.to_dict()
        for c in d["clips"]:
            an = p.load_analysis(c["clip_id"]) or {}
            c["thumb"] = an.get("thumb")
            c["crop"] = None
        out.append(d)
    return {"plans": out, "notes": S.plan_notes}


@app.post("/api/montage/render")
def montage_render(body: dict = Body(default={})):
    p = proj()
    if not S.plans:
        try:
            _plans(p, body)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
    plans = list(S.plans)
    do_render = body.get("render", True)
    do_export = body.get("export", True)

    def fn(job: Job):
        outs = []
        stamp = time.strftime("%Y%m%d_%H%M")
        n = len(plans) if do_render else 0
        for i, tl in enumerate(plans if do_render else []):
            fname = safe_name(f"{tl.name}_{stamp}").replace(" ", "_").replace(":", "x") + ".mp4"
            out = p.root / "renders" / fname

            def prog(f, msg, i=i):
                job.set((i + f) / max(1, n) * (0.95 if do_export else 1.0), f"{tl.name}: {msg}")

            render.render_timeline(p, tl, out, progress=prog, cancelled=job.cancelled)
            outs.append({"kind": "render", "name": tl.name, "path": str(out),
                         "url": f"renders/{fname}", "duration": tl.duration, "when": time.strftime("%H:%M")})
        if do_export:
            job.set(0.97, "Writing Resolve + Premiere files")
            ex = export_nle.export_all(p, plans)
            outs.append({"kind": "export", "name": "Resolve script + Premiere XML", "path": ex["folder"],
                         "lua": ex["lua"], "xml": ex["xml"], "when": time.strftime("%H:%M")})
        with p.lock:
            p.data.setdefault("outputs", []).extend(outs)
            p.save()
        return {"outputs": outs}

    title = f"Render {len(plans)} edit(s)" if do_render else "Export timelines"
    return S.jobs.submit("render", title, fn).view()


# ---------------------------------------------------------------------- jobs

@app.get("/api/jobs")
def jobs():
    return S.jobs.list()


@app.post("/api/jobs/{jid}/cancel")
def job_cancel(jid: str):
    S.jobs.cancel(jid)
    return {"ok": True}


@app.exception_handler(FileNotFoundError)
def _fnf(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=404)
