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


def sort_on_disk(project, dest: Path, method: str = "link", include_junk: bool = False, progress=None) -> dict:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    summ = [c for c in project.clip_summaries() if c["status"] == "analysed"]
    rows = []
    failed_links = 0
    for i, c in enumerate(summ):
        if progress:
            progress(i / max(1, len(summ)), f"Sorting {c['name']}")
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
        src = Path(c["path"])
        for t in targets:
            for f in [src] + _sidecars(src):
                res = _place(f, t / f.name, method)
                if res == "link-failed":
                    failed_links += 1
                if f == src:
                    rows.append({"clip": c["name"], "folder": str(t.relative_to(dest)), "result": res,
                                 "source": str(src)})
        if failed_links and method == "link" and failed_links >= 3 and not any(r["result"] == "linked" for r in rows):
            raise RuntimeError("This drive can't hold hard links (exFAT/FAT drives can't). "
                               "Pick a folder on the same NTFS drive as the footage, or choose Copy.")
    with open(dest / "_KartCut_index.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["clip", "folder", "result", "source"])
        w.writeheader()
        w.writerows(rows)
    return {"placed": len(rows), "failed_links": failed_links, "folder": str(dest)}
