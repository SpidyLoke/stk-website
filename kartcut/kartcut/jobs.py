"""Background job queue: one heavy job at a time so the GPU and disks aren't fighting."""

from __future__ import annotations

import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable

from .util import Cancelled, log, short_id


@dataclass
class Job:
    id: str
    kind: str
    title: str
    status: str = "queued"          # queued | running | done | error | cancelled
    progress: float = 0.0
    message: str = ""
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    result: dict | None = None
    error: str | None = None
    cancel_flag: bool = False

    def set(self, progress: float | None = None, message: str | None = None) -> None:
        if progress is not None:
            self.progress = max(0.0, min(1.0, float(progress)))
        if message is not None:
            self.message = message

    def cancelled(self) -> bool:
        return self.cancel_flag

    def view(self) -> dict:
        eta = None
        if self.status == "running" and self.started and self.progress > 0.02:
            el = time.time() - self.started
            eta = el / self.progress - el
        return {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
                "progress": round(self.progress, 4), "message": self.message, "result": self.result,
                "error": self.error, "eta": round(eta) if eta else None, "created": self.created,
                "finished": self.finished}


class JobQueue:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True, name="kartcut-jobs").start()

    def submit(self, kind: str, title: str, fn: Callable[[Job], dict | None]) -> Job:
        job = Job(short_id("j"), kind, title)
        with self.lock:
            self.jobs[job.id] = job
        self.q.put((job, fn))
        return job

    def cancel(self, jid: str) -> None:
        j = self.jobs.get(jid)
        if j and j.status in ("queued", "running"):
            j.cancel_flag = True
            if j.status == "queued":
                j.status = "cancelled"

    def list(self) -> list[dict]:
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)[:30]
        return [j.view() for j in jobs]

    def busy(self) -> bool:
        return any(j.status in ("queued", "running") for j in self.jobs.values())

    def _worker(self) -> None:
        while True:
            job, fn = self.q.get()
            if job.status == "cancelled":
                continue
            job.status = "running"
            job.started = time.time()
            try:
                job.result = fn(job) or {}
                job.status = "cancelled" if job.cancel_flag else "done"
                job.progress = 1.0 if job.status == "done" else job.progress
            except Cancelled:
                job.status = "cancelled"
            except Exception as e:
                log.error("Job %s failed: %s\n%s", job.title, e, traceback.format_exc())
                job.status = "error"
                job.error = str(e)
            finally:
                job.finished = time.time()
