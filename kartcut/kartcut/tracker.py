"""Lightweight multi-object tracker for karts at low analysis frame rates.

Constant-velocity prediction + IoU / centre-distance / colour cost, solved with the
Hungarian algorithm. Built for 4-10 fps sampling where karts jump a long way
between frames, which is where IoU-only trackers fall apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment

from .detect import iou


@dataclass
class _Track:
    tid: int
    box: np.ndarray
    vel: np.ndarray = field(default_factory=lambda: np.zeros(4, np.float32))
    t: float = 0.0
    hits: int = 1
    missed: int = 0
    hist: np.ndarray | None = None

    def predict(self, t: float) -> np.ndarray:
        dt = max(0.0, t - self.t)
        return self.box + self.vel * dt


class Tracker:
    def __init__(self, max_missed_s: float = 0.9, gate: float = 3.0):
        self.tracks: list[_Track] = []
        self.next_id = 1
        self.max_missed_s = max_missed_s
        self.gate = gate
        self.last_t = 0.0

    def update(self, t: float, boxes: list[np.ndarray], hists: list[np.ndarray | None] | None = None) -> list[int]:
        """Returns a track id for each input box (same order)."""
        hists = hists or [None] * len(boxes)
        ids = [-1] * len(boxes)
        live = [tr for tr in self.tracks if (t - tr.t) <= self.max_missed_s]
        if live and boxes:
            cost = np.full((len(live), len(boxes)), 10.0, np.float32)
            for i, tr in enumerate(live):
                p = tr.predict(t)
                pw, ph = max(1.0, p[2] - p[0]), max(1.0, p[3] - p[1])
                pc = np.array([(p[0] + p[2]) / 2, (p[1] + p[3]) / 2])
                for j, b in enumerate(boxes):
                    bw, bh = max(1.0, b[2] - b[0]), max(1.0, b[3] - b[1])
                    size_ratio = max(bw * bh, pw * ph) / max(1.0, min(bw * bh, pw * ph))
                    if size_ratio > 4.0:
                        continue
                    bc = np.array([(b[0] + b[2]) / 2, (b[1] + b[3]) / 2])
                    dist = float(np.linalg.norm(bc - pc)) / max(pw, ph, bw, bh)
                    if dist > self.gate:
                        continue
                    ov = iou(p, b)
                    c = (1.0 - ov) if ov > 0 else (1.0 + dist / self.gate)
                    c += 0.15 * np.log(size_ratio)
                    if tr.hist is not None and hists[j] is not None:
                        c += 0.6 * (1.0 - float(np.sum(np.sqrt(tr.hist * hists[j]))))
                    cost[i, j] = c
            rows, cols = linear_sum_assignment(cost)
            for i, j in zip(rows, cols):
                if cost[i, j] >= 2.2:
                    continue
                tr = live[i]
                dt = max(1e-3, t - tr.t)
                newv = (boxes[j] - tr.box) / dt
                tr.vel = newv if tr.hits == 1 else 0.6 * newv + 0.4 * tr.vel
                tr.box = boxes[j].astype(np.float32)
                tr.t = t
                tr.hits += 1
                if hists[j] is not None:
                    tr.hist = hists[j] if tr.hist is None else 0.7 * tr.hist + 0.3 * hists[j]
                ids[j] = tr.tid
        for j, b in enumerate(boxes):
            if ids[j] == -1:
                tr = _Track(self.next_id, b.astype(np.float32), t=t, hist=hists[j])
                self.next_id += 1
                self.tracks.append(tr)
                ids[j] = tr.tid
        self.tracks = [tr for tr in self.tracks if (t - tr.t) <= self.max_missed_s * 3]
        self.last_t = t
        return ids
