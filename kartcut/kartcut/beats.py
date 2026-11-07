"""Music analysis for beat-synced cutting: beats, bars, energy curve and the drop."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import numpy as np

from . import config
from .util import popen_flags, read_json, write_json

SR = 22050


def load_audio(path: Path, start: float = 0.0, max_len: float = 600.0) -> np.ndarray:
    cmd = [config.ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path), "-t", f"{max_len:.1f}", "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"]
    cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_flags())
    if cp.returncode != 0:
        raise RuntimeError(f"Could not read music: {cp.stderr.decode('utf-8', 'replace')[-300:]}")
    return np.frombuffer(cp.stdout, np.float32).copy()


def analyse_music(path: Path, start: float = 0.0, cache_dir: Path | None = None) -> dict:
    path = Path(path)
    st = path.stat()
    key = f"{path.name}_{st.st_size}_{int(st.st_mtime)}_{start:.2f}"
    cache = (Path(cache_dir) / "music" / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.json") if cache_dir else None
    if cache:
        hit = read_json(cache)
        if hit and hit.get("key") == key:
            return hit
    import librosa

    y = load_audio(path, start)
    if len(y) < SR * 2:
        raise RuntimeError("Music track is shorter than 2 seconds")
    hop = 512
    onset = librosa.onset.onset_strength(y=y, sr=SR, hop_length=hop)
    tempo, beats = librosa.beat.beat_track(onset_envelope=onset, sr=SR, hop_length=hop, units="frames")
    tempo = float(np.atleast_1d(tempo)[0])
    beat_t = librosa.frames_to_time(beats, sr=SR, hop_length=hop)
    if len(beat_t) < 8:
        period = 60.0 / (tempo or 120.0)
        beat_t = np.arange(0.0, len(y) / SR, period)
    # Downbeat phase: the beat position (mod 4) carrying the most onset energy.
    strength = onset[np.clip(beats, 0, len(onset) - 1)] if len(beats) else np.ones(len(beat_t))
    if len(strength) != len(beat_t):
        strength = np.ones(len(beat_t))
    phase = int(np.argmax([strength[p::4].mean() if len(strength[p::4]) else 0 for p in range(4)]))
    # Energy curve and drop: biggest sustained jump in loudness.
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    rt = librosa.frames_to_time(np.arange(len(rms)), sr=SR, hop_length=hop)
    win = max(1, int(2.0 * SR / hop))
    smooth = np.convolve(rms, np.ones(win) / win, mode="same")
    drop = None
    if len(smooth) > 3 * win:
        jump = smooth[win:] - smooth[:-win]
        lo = int(4.0 * SR / hop)
        if len(jump) > lo:
            i = lo + int(np.argmax(jump[lo:]))
            if jump[i] > 0.25 * smooth.max():
                drop = float(rt[min(len(rt) - 1, i + win // 2)])
    res = {
        "key": key,
        "path": str(path),
        "start": start,
        "duration": float(len(y) / SR),
        "tempo": round(tempo, 1),
        "beats": [round(float(b), 4) for b in beat_t],
        "downbeat_phase": phase,
        "drop": round(drop, 3) if drop is not None else None,
        "energy_t": [round(float(t), 2) for t in rt[::8]],
        "energy": [round(float(e), 4) for e in (smooth / max(1e-9, smooth.max()))[::8]],
    }
    if cache:
        write_json(cache, res)
    return res


def snap_to_downbeat(music: dict, t: float) -> float:
    beats = music["beats"]
    ph = music["downbeat_phase"]
    downs = beats[ph::4]
    if not downs:
        return t
    return float(min(downs, key=lambda b: abs(b - t)))
