"""One decode pass per clip: everything KartCut knows about a clip comes from here."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from . import colour as C
from . import config, media, metrics
from .detect import Det, Models, arms_raised, overlap_frac
from .tracker import Tracker
from .util import Cancelled, log, run

ANALYSIS_VERSION = 3
DET_LONG_SIDE = 640


def guess_profile_from_content(frames: list[np.ndarray]) -> str | None:
    """No metadata? Log footage never reaches true black or full white; Rec.709 does."""
    if not frames:
        return None
    lum = np.concatenate([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).ravel() for f in frames]).astype(np.float32) / 255
    lo, hi = np.percentile(lum, 0.5), np.percentile(lum, 99.9)
    if lo >= 0.025 and hi <= 0.95:
        return "slog3-sgamut3cine"
    if lo < 0.012 or hi > 0.97:
        return "rec709"
    return None


def resolve_profile(probe: media.Probe, settings: dict, sample_frames: list[np.ndarray] | None) -> tuple[str, str]:
    if probe.sony.get("profile"):
        return probe.sony["profile"], "camera metadata"
    if probe.color_transfer in ("arib-std-b67",):
        return "hlg", "file metadata"
    default = settings.get("footage_profile", "slog3-sgamut3cine")
    guess = guess_profile_from_content(sample_frames or [])
    if guess and C.get_profile(guess).is_log != C.get_profile(default).is_log:
        return guess, "image content"
    return default, "project default"


def analyse_clip(clip: dict, project_dir: Path, settings: dict, models: Models,
                 progress: Callable[[float, str], None] | None = None,
                 cancelled: Callable[[], bool] | None = None) -> dict:
    t_start = time.time()
    path = Path(clip["path"])
    probe = media.probe(path)
    cache = Path(project_dir) / "cache"
    cid = clip["id"]
    detector = models.detector

    # --- orientation and profile from a few sample frames
    orient, orient_src = media.orientation_from_metadata(probe)
    transpose = int(clip.get("transpose_override", 0) or 0)
    sample_times = [probe.duration * f for f in (0.2, 0.5, 0.8)] if probe.duration > 1 else [0.0]
    samples = media.grab_frames(path, probe, sample_times, long_side=640)
    if transpose:
        orient_src = "manual"
    elif (orient == "horizontal" and probe.rotation == 0 and settings.get("detect_orientation_from_content", True)
          and detector.available and samples):
        prof_tmp = C.get_profile(resolve_profile(probe, settings, samples)[0])
        disp = [C.display_bgr_u8(s, prof_tmp) for s in samples]
        transpose = media.detect_rotation_from_content(disp, detector)
        if transpose:
            orient, orient_src = "vertical", "image content (no rotation flag)"
    if transpose and orient_src == "manual":
        dw, dh = probe.display_size
        orient = "vertical" if dw > dh else "horizontal"
    prof_key, prof_src = resolve_profile(probe, settings, samples)
    if clip.get("profile_override"):
        prof_key, prof_src = clip["profile_override"], "manual"
    profile = C.get_profile(prof_key)

    an_fps = config.resolved_analysis_fps(settings)
    src_fps = probe.fps or 25.0
    m_fps = min(src_fps, float(settings.get("motion_fps_cap", 50)))
    step = max(1, int(round(m_fps / an_fps)))
    reader = media.FrameReader(path, probe, long_side=int(settings.get("analysis_long_side", 1280)),
                               fps=m_fps, transpose=transpose)
    W, H = reader.w, reader.h
    det_scale = min(1.0, DET_LONG_SIDE / max(W, H))
    dw_, dh_ = int(round(W * det_scale / 2) * 2), int(round(H * det_scale / 2) * 2)

    expo = metrics.ExposureAccumulator(profile)
    motion = metrics.GlobalMotion()
    tracker = Tracker()
    tracks: dict[int, dict] = {}
    frames: list[dict] = []
    best_thumb = (-1.0, None)
    hash_targets = [probe.duration * f for f in (0.25, 0.5, 0.75)]
    hashes: list[int] = []
    pose = models.pose if detector.available else None
    want_ocr = models.ocr is not None
    dur = max(probe.duration, 0.1)
    last_prog = 0.0

    for i, t, frame in reader:
        if cancelled and cancelled():
            reader.close()
            raise Cancelled()
        motion.update(frame)
        if i % step:
            continue
        small = cv2.resize(frame, (dw_, dh_), interpolation=cv2.INTER_AREA)
        disp_small = C.display_bgr_u8(small, profile)
        dets = detector.detect(disp_small) if detector.available else []
        inv = 1.0 / det_scale
        for d in dets:
            d.box = d.box * inv
        karts = [d for d in dets if d.cls == "kart" and d.h >= 0.04 * H]
        persons = [d for d in dets if d.cls == "person"]
        trophies = [d for d in dets if d.cls == "trophy"]
        standing = [p for p in persons if not any(overlap_frac(p.box, k.box) > 0.3 for k in karts)]

        disp_full = None
        if karts:
            disp_full = C.display_bgr_u8(frame, profile)
        hists = []
        crops = []
        for k in karts:
            cimg = metrics.crop(disp_full, k.box, pad=0.06)
            crops.append(cimg)
            hists.append(metrics.colour_hist(cimg) if cimg.size else None)
        ids = tracker.update(t, [k.box for k in karts], hists)
        erow = expo.add(frame, t, [k.box for k in karts])

        gray_full = cv2.cvtColor(disp_full if disp_full is not None else C.display_bgr_u8(frame, profile),
                                 cv2.COLOR_BGR2GRAY)
        kart_sharp = []
        for k, tid, cimg, hst in zip(karts, ids, crops, hists):
            g = metrics.crop(gray_full, k.box)
            sh = metrics.sharpness(g)
            kart_sharp.append(sh)
            grass = metrics.grass_under(disp_full, k.box)
            tr = tracks.setdefault(tid, {"frames": [], "hists": [], "cands": []})
            tr["frames"].append([round(t, 3), *[round(float(v), 1) for v in k.box], round(k.conf, 3),
                                 round(sh, 4), round(grass, 3)])
            if hst is not None:
                tr["hists"].append(hst)
            q = (k.w * k.h) * (0.3 + sh) * k.conf
            if cimg.size and k.h >= 40:
                tr["cands"].append((q, cimg.copy()))
                tr["cands"].sort(key=lambda c: -c[0])
                del tr["cands"][6:]

        celebrate = 0.0
        driver_celebrate = 0.0
        big_people = [p for p in persons if p.h >= 0.12 * H]
        if pose is not None and big_people and (len(frames) % 2 == 0):
            for pbox, kp in pose.keypoints(disp_small):
                pb = pbox * inv
                kp = kp.copy()
                kp[:, :2] *= inv
                a = arms_raised(kp, pb)
                if a <= 0:
                    continue
                in_kart = any(overlap_frac(pb, k.box) > 0.3 for k in karts)
                if in_kart:
                    driver_celebrate = max(driver_celebrate, a)
                else:
                    celebrate = max(celebrate, a)

        bg_sharp = metrics.background_sharpness(cv2.resize(gray_full, (dw_, dh_)), [k.box * det_scale for k in karts])
        frames.append({
            "t": round(t, 3),
            "k": len(karts),
            "p": len(standing),
            "ph": round(max([p.h / H for p in standing], default=0.0), 3),
            "cel": celebrate,
            "dcel": driver_celebrate,
            "tro": len(trophies),
            "bgs": round(bg_sharp, 4),
            "det": round(metrics.sharpness(cv2.resize(gray_full, (dw_, dh_))), 4),
            "luma": round(erow["luma"], 4),
        })

        score = sum((k.h / H) * k.conf * (0.5 + s) for k, s in zip(karts, kart_sharp)) + 0.3 * celebrate
        if score > best_thumb[0]:
            best_thumb = (score, disp_full if disp_full is not None else C.display_bgr_u8(frame, profile))
        while hash_targets and t >= hash_targets[0]:
            hash_targets.pop(0)
            hashes.append(metrics.dhash(disp_full if disp_full is not None else C.display_bgr_u8(frame, profile)))
        if progress and (t - last_prog) >= 0.5:
            last_prog = t
            progress(min(0.97, t / dur), f"{path.name}: {t:.1f}/{dur:.1f}s")

    # --- post: per-track identity evidence
    crops_dir = cache / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)
    track_out = []
    embedder = models.embedder if detector.available else None
    for tid, tr in tracks.items():
        if len(tr["frames"]) < 2:
            continue
        hist = np.mean(tr["hists"], axis=0) if tr["hists"] else None
        if hist is not None:
            hist = hist / max(1e-6, hist.sum())
        cands = [c for _, c in tr["cands"]]
        ocr_reads: list[list] = []
        if want_ocr and cands:
            for c in cands[:6]:
                for num, conf in models.ocr.read(c):
                    ocr_reads.append([num, round(conf, 3)])
        emb = None
        if embedder is not None and cands:
            try:
                e = embedder.embed(cands[:4]).mean(axis=0)
                emb = (e / max(1e-6, np.linalg.norm(e))).round(4).tolist()
            except Exception as ex:
                log.warning("Embedding failed: %s", ex)
        crop_rel = None
        if cands:
            c = cands[0]
            scale = 160.0 / max(1, c.shape[0])
            cimg = cv2.resize(c, (max(1, int(c.shape[1] * scale)), 160), interpolation=cv2.INTER_AREA)
            crop_rel = f"cache/crops/{cid}_{tid}.jpg"
            cv2.imwrite(str(Path(project_dir) / crop_rel), cimg, [cv2.IMWRITE_JPEG_QUALITY, 85])
        track_out.append({
            "id": tid,
            "frames": tr["frames"],
            "hist": hist.round(5).tolist() if hist is not None else None,
            "emb": emb,
            "ocr": ocr_reads,
            "crop": crop_rel,
        })

    mot = motion.analyse(m_fps)
    exp_sum = expo.summary()
    wave_rel = f"cache/wave/{cid}.png"
    expo.waveform_png(Path(project_dir) / wave_rel)
    thumb_rel = f"cache/thumbs/{cid}.jpg"
    timg = best_thumb[1]
    if timg is None:
        timg = C.display_bgr_u8(samples[0], profile) if samples else np.zeros((270, 480, 3), np.uint8)
    th = max(2, int(480 * timg.shape[0] / max(1, timg.shape[1])))
    (Path(project_dir) / "cache/thumbs").mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(Path(project_dir) / thumb_rel), cv2.resize(timg, (480, th), interpolation=cv2.INTER_AREA),
                [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not hashes and timg is not None:
        hashes = [metrics.dhash(timg)]

    result = {
        "version": ANALYSIS_VERSION,
        "clip_id": cid,
        "path": str(path),
        "size": path.stat().st_size,
        "mtime": path.stat().st_mtime,
        "probe": probe.to_dict(),
        "profile": prof_key,
        "profile_source": prof_src,
        "orientation": orient,
        "orientation_source": orient_src,
        "transpose": transpose,
        "analysis_w": W,
        "analysis_h": H,
        "analysis_fps": m_fps / step,
        "motion_fps": m_fps,
        "detector": detector.name,
        "frames": frames,
        "tracks": track_out,
        "exposure": exp_sum,
        "motion": mot,
        "hashes": [str(h) for h in hashes],
        "thumb": thumb_rel,
        "waveform": wave_rel,
        "seconds": round(time.time() - t_start, 1),
    }
    if progress:
        progress(1.0, f"{path.name}: done")
    return result


def make_proxy(clip_path: Path, probe_d: dict, profile_key: str, transpose: int, out_path: Path,
               lut_dir: Path, height: int = 540) -> Path:
    """Small H.264 preview the browser can play (10-bit 4:2:2 XAVC can't play in Chrome)."""
    profile = C.get_profile(profile_key)
    lut = C.builtin_display_cube(profile, lut_dir)
    vf = []
    if transpose == 90:
        vf.append("transpose=1")
    elif transpose == -90:
        vf.append("transpose=2")
    vf.append(f"scale=-2:'min({height},ih)'")
    if profile.is_log:
        vf.append(f"lut3d=file={lut.name}")  # run with cwd=lut dir: no drive-letter escaping
    vf.append("format=yuv420p")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".part.mp4")
    enc = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "28"] if config.nvenc_available() else \
          ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28"]
    hw = ["-hwaccel", "cuda"] if config.cuda_decode_available() else []
    cmd = [config.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *hw, "-i", str(clip_path),
           "-vf", ",".join(vf), *enc, "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-movflags", "+faststart", str(tmp)]
    try:
        run(cmd, timeout=3600, cwd=lut.parent)
    except RuntimeError:
        if not hw:
            raise
        config.mark_cuda_decode_broken()
        cmd.remove("-hwaccel")
        cmd.remove("cuda")
        run(cmd, timeout=3600, cwd=lut.parent)
    tmp.replace(out_path)
    return out_path
