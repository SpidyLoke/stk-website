"""Shot matching: per-clip exposure + white balance correction towards one hero clip.

Corrections are done in scene-linear light (exposure in stops, per-channel gains),
then re-encoded to the camera curve, so the exported LUTs are log-in / log-out and
sit before your normal S-Log3 to Rec.709 LUT or CST in Resolve.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

import numpy as np

from . import colour as C
from .util import safe_name


def correction_for(an: dict, hero: dict, exp_strength: float = 1.0, wb_strength: float = 1.0) -> dict:
    e, h = an.get("exposure") or {}, hero.get("exposure") or {}
    if not e or not h:
        return {"ev": 0.0, "gains": [1.0, 1.0, 1.0], "confidence": 0.0}
    conf = min(e.get("ref_conf", 0), h.get("ref_conf", 0))
    if conf >= 0.03:
        ev_ref = math.log2(max(h["ref"], 1e-5) / max(e["ref"], 1e-5))
    else:
        ev_ref = None
    ev_key = math.log2(max(h["key"], 1e-5) / max(e["key"], 1e-5))
    ev = ev_key if ev_ref is None else 0.7 * ev_ref + 0.3 * ev_key
    ev = float(np.clip(ev * exp_strength, -2.5, 2.5))
    g_clip = np.asarray(e["grey"], np.float64)
    g_hero = np.asarray(h["grey"], np.float64)
    g_clip = g_clip / max(1e-6, g_clip[1])
    g_hero = g_hero / max(1e-6, g_hero[1])
    gains = g_hero / np.maximum(g_clip, 1e-6)
    gains = np.clip(gains, 0.5, 2.0) ** wb_strength
    # keep overall brightness neutral: gains only move colour, ev moves exposure
    lum = np.asarray(C.get_profile(an.get("profile")).m_to_xyz[1], np.float64)
    gains = gains / float(np.dot(lum, gains) / lum.sum())
    return {"ev": round(ev, 3), "gains": [round(float(g), 4) for g in gains],
            "confidence": round(float(min(1.0, conf / 0.1)), 2)}


def cdl_from(corr: dict) -> dict:
    """ASC CDL approximation in S-Log3 code space (offset only). Exact above the toe."""
    k = (2.0 ** corr["ev"]) * np.asarray(corr["gains"])
    off = 261.5 * np.log10(np.maximum(k, 1e-6)) / 876.0
    return {"slope": [1.0, 1.0, 1.0], "offset": [round(float(o), 5) for o in off], "power": [1.0, 1.0, 1.0],
            "sat": 1.0}


def write_cdl(path: Path, cdl: dict, cid: str) -> None:
    s = " ".join(f"{v:.5f}" for v in cdl["slope"])
    o = " ".join(f"{v:.5f}" for v in cdl["offset"])
    p = " ".join(f"{v:.5f}" for v in cdl["power"])
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ColorDecisionList xmlns="urn:ASC:CDL:v1.01">\n'
        f'  <ColorDecision><ColorCorrection id="{cid}">\n'
        f"    <SOPNode><Slope>{s}</Slope><Offset>{o}</Offset><Power>{p}</Power></SOPNode>\n"
        f"    <SatNode><Saturation>{cdl['sat']:.4f}</Saturation></SatNode>\n"
        "  </ColorCorrection></ColorDecision>\n</ColorDecisionList>\n", encoding="utf-8")


def run_shot_match(project, progress=None) -> dict:
    hero_id = project.hero_clip()
    if not hero_id:
        raise RuntimeError("No analysed clips to match yet")
    hero = project.load_analysis(hero_id)
    s = project.settings
    out_dir = project.root / "exports" / "shot_match"
    lut_dir = out_dir / "luts"
    cdl_dir = out_dir / "cdl"
    lut_dir.mkdir(parents=True, exist_ok=True)
    cdl_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    corrections = {}
    clips = project.derived.get("clips", {})
    ids = [cid for cid in clips if project.data["clips"].get(cid)]
    for i, cid in enumerate(ids):
        an = project.load_analysis(cid)
        if not an:
            continue
        if progress:
            progress(i / max(1, len(ids)), f"Matching {project.data['clips'][cid]['name']}")
        corr = correction_for(an, hero, float(s.get("match_exposure_strength", 1.0)),
                              float(s.get("match_wb_strength", 1.0)))
        prof = C.get_profile(an.get("profile"))
        name = safe_name(Path(an["path"]).stem)
        g = C.lut_grid(33)
        lut = lut_dir / f"{name}_{cid}.cube"
        C.write_cube(lut, C.correction_fn(prof, corr["ev"], corr["gains"])(g), 33,
                     f"KartCut match {name} -> hero ({prof.label} in/out)")
        cdl = cdl_from(corr)
        if prof.is_log:
            write_cdl(cdl_dir / f"{name}_{cid}.cdl", cdl, name)
        corr["lut"] = str(lut)
        corrections[cid] = corr
        info = clips[cid]
        e = info.get("exposure", {})
        rows.append({
            "clip": project.data["clips"][cid]["name"], "path": an["path"], "profile": prof.label,
            "driver": project.driver_label(info.get("identity", {}).get("main")),
            "verdict": e.get("level"), "issues": " | ".join(e.get("issues", [])),
            "exposure_abs_stops": e.get("ev"), "exposure_vs_session_stops": e.get("ev_rel"),
            "clipped_pct": e.get("clip_pct"), "subject_clipped_pct": e.get("subj_clip_pct"),
            "est_cct_K": e.get("cct"), "cast_mired": e.get("cast"),
            "match_ev": corr["ev"], "gain_r": corr["gains"][0], "gain_g": corr["gains"][1], "gain_b": corr["gains"][2],
            "match_confidence": corr["confidence"], "lut": lut.name,
        })
    report = out_dir / "exposure_report.csv"
    with open(report, "w", newline="", encoding="utf-8-sig") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    with project.lock:
        project.data["shot_match"] = {"hero": hero_id, "corrections": corrections, "folder": str(out_dir),
                                      "report": str(report)}
        project.save()
    return {"hero": hero_id, "count": len(rows), "folder": str(out_dir), "report": str(report)}
