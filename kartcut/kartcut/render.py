"""Render a planned Timeline with ffmpeg: per-shot encode (NVENC when present), concat, music mix."""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import shutil
from pathlib import Path

from . import colour as C
from . import config
from .autocut import TLClip, Timeline
from .util import Cancelled, log, run, safe_name

SIZES = {"16:9": (1920, 1080), "9:16": (1080, 1920)}


def encoder_args(settings: dict, fps: float) -> list[str]:
    choice = settings.get("encoder", "auto")
    use_nv = (choice == "nvenc") or (choice == "auto" and config.nvenc_available())
    gop = str(max(1, int(round(fps * 2))))
    if use_nv:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "19", "-b:v", "0",
                "-maxrate", "50M", "-bufsize", "100M", "-profile:v", "high", "-g", gop]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high", "-g", gop]


def _crop_expr(crop: list) -> str:
    """Piecewise-linear x(t) for ffmpeg's crop filter, as a fraction of input width."""
    pts = [(t, x) for t, x in crop if t >= -0.7]
    if not pts:
        return "(iw-ow)/2"
    if len(pts) == 1:
        return f"iw*{pts[0][1]:.4f}"
    expr = f"{pts[-1][1]:.4f}"
    for (t0, x0), (t1, x1) in reversed(list(zip(pts[:-1], pts[1:]))):
        dt = max(1e-3, t1 - t0)
        seg = f"{x0:.4f}+({x1 - x0:.4f})*(t-{t0:.3f})/{dt:.3f}"
        expr = f"if(lt(t\\,{t1:.3f})\\,{seg}\\,{expr})"
    first_t, first_x = pts[0]
    expr = f"if(lt(t\\,{first_t:.3f})\\,{first_x:.4f}\\,{expr})"
    return f"max(0\\,min(iw-ow\\,iw*({expr})))"


def _video_filter(tc: TLClip, aspect: str, lut_name: str, fade_out: float | None, fps: float,
                  fade_in: bool = False) -> str:
    W, H = SIZES[aspect]
    pre = ["setpts=PTS-STARTPTS"]
    if tc.transpose == 90:
        pre.append("transpose=1")
    elif tc.transpose == -90:
        pre.append("transpose=2")
    post = []
    if tc.speed != 1.0:
        post.append(f"setpts=PTS/{tc.speed:.4f}")
    post += [f"fps={fps:.4f}", "tpad=stop_mode=clone:stop_duration=2", "format=yuv420p"]
    if fade_in:
        post.append("fade=t=in:st=0:d=0.5")
    if fade_out:
        post.append(f"fade=t=out:st={fade_out:.3f}:d=0.5")
    lut = f"lut3d=file={lut_name}"
    if aspect == "9:16" and tc.orientation != "vertical":
        mid = []
        if tc.crop:
            mid.append(f"crop=w=trunc(ih*9/16/2)*2:h=ih:x={_crop_expr(tc.crop)}:y=0")
        else:
            mid.append("crop=w=trunc(ih*9/16/2)*2:h=ih")
        mid += [f"scale={W}:{H}:flags=lanczos", lut, "setsar=1"]
        return "[0:v]" + ",".join(pre + mid + post) + "[v]"
    if aspect == "16:9" and tc.orientation == "vertical":
        # pillarbox with a blurred, darkened copy instead of black bars
        return ("[0:v]" + ",".join(pre + [f"scale=-2:{H}:flags=lanczos", lut, "split=2[fg][bgs]"]) + ";"
                f"[bgs]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:3,"
                "eq=brightness=-0.06:saturation=0.8[bg];"
                "[bg][fg]overlay=(W-w)/2:0,setsar=1," + ",".join(post) + "[v]")
    mid = [f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos", f"crop={W}:{H}", lut, "setsar=1"]
    return "[0:v]" + ",".join(pre + mid + post) + "[v]"


def _lut_for(project, tc: TLClip, lut_dir: Path, custom) -> str:
    corr = (project.data.get("shot_match") or {}).get("corrections", {}).get(tc.clip_id, {})
    ev = float(corr.get("ev", 0.0))
    gains = corr.get("gains", [1.0, 1.0, 1.0])
    look = project.settings.get("look", "builtin")
    if look == "custom" and custom is None:
        look = "builtin"
    key = f"{tc.profile}|{ev:.3f}|{','.join(f'{g:.4f}' for g in gains)}|{look}|{project.settings.get('custom_lut', '')}"
    name = "r_" + hashlib.sha1(key.encode()).hexdigest()[:12] + ".cube"
    path = lut_dir / name
    if not path.exists():
        C.make_render_cube(C.get_profile(tc.profile), ev, gains, look, custom, path)
    return name


def render_timeline(project, tl: Timeline, out_path: Path, progress=None, cancelled=None) -> Path:
    s = project.settings
    work = project.root / "cache" / "render" / safe_name(tl.name).replace(" ", "_")
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    lut_dir = work / "luts"
    lut_dir.mkdir(parents=True, exist_ok=True)
    custom = None
    if s.get("look") == "custom" and s.get("custom_lut"):
        try:
            custom = C.read_cube(Path(s["custom_lut"]))
        except Exception as e:
            log.warning("Custom LUT unusable (%s), using KartCut look", e)
    enc = encoder_args(s, tl.fps)
    ff = config.ffmpeg()
    hw = ["-hwaccel", "cuda"] if config.cuda_decode_available() else []
    n = len(tl.clips)
    done = [0]
    eng = float(s.get("engine_mix", 0.25))

    def job(i: int, tc: TLClip):
        if cancelled and cancelled():
            raise Cancelled()
        lut_name = _lut_for(project, tc, lut_dir, custom)
        dur = tc.frames / tl.fps
        fade = (dur - 0.5) if i == n - 1 and dur > 0.6 and getattr(tl, "fade_out", True) else None
        fade_in = i == 0 and getattr(tl, "fade_in", False)
        vpath = work / f"v{i:03d}.mp4"
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", *hw, "-ss", f"{tc.src_in:.4f}",
               "-t", f"{tc.src_dur + 0.25:.4f}", "-i", tc.path,
               "-filter_complex", _video_filter(tc, tl.aspect, lut_name, fade, tl.fps, fade_in), "-map", "[v]",
               "-frames:v", str(tc.frames), "-an", *enc, "-pix_fmt", "yuv420p",
               "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
               "-video_track_timescale", "90000", str(vpath)]
        try:
            run(cmd, timeout=1800, cwd=lut_dir)
        except RuntimeError:
            if not hw:
                raise
            config.mark_cuda_decode_broken()
            cmd2 = [c for c in cmd if c not in ("-hwaccel", "cuda")]
            run(cmd2, timeout=1800, cwd=lut_dir)
        apath = work / f"a{i:03d}.wav"
        has_audio = (project.load_analysis(tc.clip_id) or {}).get("probe", {}).get("has_audio")
        if has_audio and tc.speed == 1.0 and eng > 0:
            fo = max(0.0, dur - 0.03)
            acmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{tc.src_in:.4f}", "-t",
                    f"{tc.src_dur + 0.1:.4f}", "-i", tc.path, "-vn",
                    "-af", f"asetpts=PTS-STARTPTS,aresample=48000,afade=t=in:d=0.02,afade=t=out:st={fo:.3f}:d=0.03,apad",
                    "-t", f"{dur:.6f}", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(apath)]
        else:
            acmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "anullsrc=r=48000:cl=stereo", "-t", f"{dur:.6f}", "-c:a", "pcm_s16le", str(apath)]
        run(acmd, timeout=600)
        done[0] += 1
        if progress:
            progress(0.9 * done[0] / n, f"Rendered shot {done[0]}/{n}")
        return vpath, apath

    workers = 3 if config.nvenc_available() else 2
    results: dict[int, tuple[Path, Path]] = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(job, i, tc): i for i, tc in enumerate(tl.clips)}
        try:
            for f in cf.as_completed(futs):
                results[futs[f]] = f.result()
        except BaseException:
            for f in futs:
                f.cancel()
            raise
    vlist = work / "video.txt"
    alist = work / "audio.txt"
    vlist.write_text("".join(f"file '{results[i][0].name}'\n" for i in range(n)), encoding="utf-8")
    alist.write_text("".join(f"file '{results[i][1].name}'\n" for i in range(n)), encoding="utf-8")
    run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", vlist.name,
         "-c", "copy", "video.mp4"], cwd=work, timeout=1800)
    run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", alist.name,
         "-c", "copy", "engine.wav"], cwd=work, timeout=600)
    if progress:
        progress(0.95, "Mixing music")
    T = tl.duration
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    music = tl.music
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(work / "video.mp4"), "-i", str(work / "engine.wav")]
    if music and music.get("path"):
        if music.get("start"):
            cmd += ["-ss", f"{music['start']:.3f}"]
        cmd += ["-i", music["path"]]
        fc = (f"[1:a]volume={eng:.3f}[e];"
              f"[2:a]aresample=48000,atrim=0:{T:.4f},asetpts=PTS-STARTPTS,"
              f"afade=t=in:d={0.5 if getattr(tl, 'fade_in', False) else 0.03},"
              f"afade=t=out:st={max(0.0, T - (2.0 if getattr(tl, 'fade_out', True) else 0.05)):.3f}:"
              f"d={2.0 if getattr(tl, 'fade_out', True) else 0.05}[m];"
              "[m][e]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]")
    else:
        fc = f"[1:a]volume=1.0,afade=t=out:st={max(0.0, T - 1.0):.3f}:d=1[a]"
    cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "320k",
            "-t", f"{T:.4f}", "-movflags", "+faststart", str(out_path)]
    run(cmd, timeout=1800)
    if progress:
        progress(1.0, f"Saved {out_path.name}")
    shutil.rmtree(work, ignore_errors=True)
    return out_path
