"""Verified card offload: one read of the card, written to two drives, then both copies re-read and hashed.

Nothing on the card is ever modified or deleted. A manifest (CSV + JSON) with
xxHash64 checksums lands next to each copy.
"""

from __future__ import annotations

import csv
import os
import shutil
import time
from pathlib import Path

import xxhash

from .util import Cancelled, safe_name, write_json

CHUNK = 8 * 1024 * 1024
SKIP_DIRS = {"system volume information", "$recycle.bin", ".spotlight-v100", ".fseventsd", ".trashes"}
SKIP_FILES = {"thumbs.db", ".ds_store", "desktop.ini"}


def _files(src: Path) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            if f.lower() in SKIP_FILES or f.startswith("._"):
                continue
            out.append(Path(dirpath) / f)
    return sorted(out)


def hash_file(p: Path, cancelled=None) -> str:
    h = xxhash.xxh64()
    with open(p, "rb") as f:
        while True:
            if cancelled and cancelled():
                raise Cancelled()
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def offload(sources: list[dict], dests: list[str], label: str, progress=None, cancelled=None) -> dict:
    """sources: [{"path": "E:/", "reel": "CamA_Card1"}]; dests: up to two folders."""
    dests = [Path(d) for d in dests if d]
    if not dests:
        raise ValueError("Pick at least one destination drive")
    for d in dests:
        d.mkdir(parents=True, exist_ok=True)
    plan = []
    for s in sources:
        src = Path(s["path"])
        if not src.exists():
            raise FileNotFoundError(f"Source not found: {src}")
        reel = safe_name(s.get("reel") or src.name or "Card")
        for f in _files(src):
            plan.append((src, reel, f, f.stat().st_size))
    total = sum(p[3] for p in plan) or 1
    for d in dests:
        free = shutil.disk_usage(d).free
        if free < total * 1.02:
            raise RuntimeError(f"Not enough space on {d}: need {total / 1e9:.1f} GB, free {free / 1e9:.1f} GB")
    session = safe_name(label or time.strftime("%Y-%m-%d"))
    done_bytes = 0
    t0 = time.time()
    rows = []

    def tick(msg: str, phase_weight: float, base: float):
        if progress:
            el = max(0.001, time.time() - t0)
            speed = done_bytes / el / 1e6
            progress(base + phase_weight * min(1.0, done_bytes / total),
                     f"{msg} | {done_bytes / 1e9:.2f}/{total / 1e9:.2f} GB at {speed:.0f} MB/s")

    # pass 1: read source once, hash while writing to every destination
    for src, reel, f, size in plan:
        rel = f.relative_to(src)
        outs = [d / session / reel / rel for d in dests]
        for o in outs:
            o.parent.mkdir(parents=True, exist_ok=True)
        existing = all(o.exists() and o.stat().st_size == size for o in outs)
        h = xxhash.xxh64()
        if existing:
            src_hash = hash_file(f, cancelled)
            status = "skipped (already copied)"
            done_bytes += size
        else:
            tmp = [o.with_name(o.name + ".kcpart") for o in outs]
            handles = [open(t, "wb") for t in tmp]
            try:
                with open(f, "rb") as fin:
                    while True:
                        if cancelled and cancelled():
                            raise Cancelled()
                        b = fin.read(CHUNK)
                        if not b:
                            break
                        h.update(b)
                        for fh in handles:
                            fh.write(b)
                        done_bytes += len(b)
                        tick(f"Copying {rel}", 0.5, 0.0)
                for fh in handles:
                    fh.flush()
                    os.fsync(fh.fileno())
            finally:
                for fh in handles:
                    fh.close()
            for t, o in zip(tmp, outs):
                os.replace(t, o)
                try:
                    shutil.copystat(f, o)
                except OSError:
                    pass
            src_hash = h.hexdigest()
            status = "copied"
        rows.append({"reel": reel, "file": str(rel).replace("\\", "/"), "size": size, "xxh64": src_hash,
                     "status": status, "outs": [str(o) for o in outs]})

    # pass 2: re-read every copy from disk and compare
    done_bytes = 0
    t0 = time.time()
    bad = []
    for r in rows:
        r["verified"] = []
        for o in r["outs"]:
            hv = hash_file(Path(o), cancelled)
            ok = hv == r["xxh64"]
            r["verified"].append(ok)
            if not ok:
                bad.append(o)
        done_bytes += r["size"]
        tick(f"Verifying {r['file']}", 0.5, 0.5)

    ok_all = not bad and bool(rows)
    for di, d in enumerate(dests):
        mdir = d / session
        man = mdir / f"kartcut_offload_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        with open(man, "w", newline="", encoding="utf-8") as fcsv:
            w = csv.writer(fcsv)
            w.writerow(["reel", "file", "bytes", "xxh64", "status", "verified"])
            for r in rows:
                w.writerow([r["reel"], r["file"], r["size"], r["xxh64"], r["status"],
                            "OK" if r["verified"][di] else "MISMATCH"])
        write_json(man.with_suffix(".json"), {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "all_verified": ok_all,
                                              "files": [{k: v for k, v in r.items() if k != "outs"} for r in rows]})
    return {
        "files": len(rows),
        "bytes": total,
        "verified": ok_all,
        "mismatches": bad,
        "dest_folders": [str(d / session) for d in dests],
        "safe_to_format": ok_all and len(dests) >= 2,
        "message": ("All files verified on both drives. Safe to format the cards."
                    if ok_all and len(dests) >= 2 else
                    "All files verified (single destination: keep the card until you have a second copy)."
                    if ok_all else f"{len(bad)} copies FAILED verification. Do not format the cards."),
    }
