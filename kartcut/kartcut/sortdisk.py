"""Sort clips into Category/Driver folders on disk.

In Resolve: right-click the folder in Media Storage > 'Add Folder and SubFolders into
Media Pool (Create Bins)'. Premiere: Import > folder. Either way you get one bin per driver.
"""

from __future__ import annotations

import csv
import os
import shutil
from pathlib import Path

from .util import safe_name


def _sidecars(p: Path) -> list[Path]:
    out = []
    for cand in (p.with_name(p.stem + "M01.XML"), p.with_name(p.stem + "M01.xml")):
        if cand.exists():
            out.append(cand)
    return out


def _place(src: Path, dst: Path, method: str) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return "exists"
    if method == "link":
        try:
            os.link(src, dst)
            return "linked"
        except OSError:
            return "link-failed"
    if method == "copy":
        shutil.copy2(src, dst)
        return "copied"
    raise ValueError(method)


ORIENTATIONS = ("all", "split", "horizontal", "vertical")


def sort_on_disk(project, dest: Path, method: str = "link", include_junk: bool = False, progress=None,
                 orientation: str = "all") -> dict:
    """orientation: all (together) | split (Horizontal/Vertical subfolders per driver) |
    horizontal (only horizontal clips) | vertical (only vertical clips)."""
    if orientation not in ORIENTATIONS:
        raise ValueError(f"orientation must be one of {ORIENTATIONS}")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    previous = _read_index(dest)
    prev_res = {(r.get("folder"), r.get("clip")): r.get("result") for r in previous}
    summ = [c for c in project.clip_summaries() if c["status"] == "analysed"]
    rows = []
    in_scope: set[str] = set()
    failed_links = 0
    for i, c in enumerate(summ):
        if progress:
            progress(i / max(1, len(summ)), f"Sorting {c['name']}")
        vertical = c.get("orientation") == "vertical"
        if (orientation == "horizontal" and vertical) or (orientation == "vertical" and not vertical):
            continue
        in_scope.add(c["name"])
        if c["junk"].get("is_junk"):
            if not include_junk:
                continue
            targets = [dest / "_Junk"]
        elif not c["drivers"]:
            targets = [dest / ("_Needs review" if c["needs_review"] else "_B-roll")]
        else:
            targets = []
            for d in c["drivers"]:
                drv = project.data["drivers"].get(d["id"], {})
                cat = safe_name(drv.get("category") or "Drivers")
                targets.append(dest / cat / safe_name(project.driver_label(d["id"])))
        if orientation == "split":
            targets = [t / ("Vertical" if vertical else "Horizontal") for t in targets]
        src = Path(c["path"])
        for t in targets:
            for f in [src] + _sidecars(src):
                res = _place(f, t / f.name, method)
                if res == "link-failed":
                    failed_links += 1
                if f == src:
                    rel = str(t.relative_to(dest))
                    if res == "exists" and prev_res.get((rel, c["name"])) in ("linked", "copied"):
                        res = prev_res[(rel, c["name"])]  # ours from an earlier sort, still ours
                    rows.append({"clip": c["name"], "folder": rel, "result": res, "source": str(src)})
        if failed_links and method == "link" and failed_links >= 3 and not any(r["result"] == "linked" for r in rows):
            raise RuntimeError("This drive can't hold hard links (exFAT/FAT drives can't). "
                               "Pick a folder on the same NTFS drive as the footage, or choose Copy.")
    # clips outside this run's orientation filter keep their old entries untouched
    rows += [r for r in previous if r.get("clip") not in in_scope]
    removed = _remove_stale(dest, [r for r in previous if r.get("clip") in in_scope], rows)
    with open(dest / "_KartCut_index.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["clip", "folder", "result", "source"])
        w.writeheader()
        w.writerows(rows)
    return {"placed": len(rows), "failed_links": failed_links, "folder": str(dest), "moved_out": removed}


def _read_index(dest: Path) -> list[dict]:
    f = dest / "_KartCut_index.csv"
    if not f.exists():
        return []
    try:
        with open(f, newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))
    except (OSError, csv.Error):
        return []


def _remove_stale(dest: Path, previous: list[dict], rows: list[dict]) -> int:
    """A clip moved to another driver: take it out of the old driver's folder.

    Only touches files KartCut itself put there (listed in its own index), never the
    original footage: a file is removed only if it sits inside the sorted folder,
    isn't the source path, and the source still exists.
    """
    keep = {(r["folder"], r["clip"]) for r in rows}
    dest_r = dest.resolve()
    removed = 0
    for r in previous:
        if (r.get("folder"), r.get("clip")) in keep or r.get("result") not in ("linked", "copied"):
            continue
        src = Path(r.get("source", ""))
        folder = (dest / r["folder"]).resolve()
        if dest_r not in folder.parents or not src.exists():
            continue
        stem = Path(r["clip"]).stem
        for name in (r["clip"], stem + "M01.XML", stem + "M01.xml"):
            f = folder / name
            if not f.exists() or f.resolve() == src.resolve():
                continue
            try:
                f.unlink()
                if name == r["clip"]:
                    removed += 1
            except OSError:
                pass
        try:
            if folder.exists() and not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass
    return removed
