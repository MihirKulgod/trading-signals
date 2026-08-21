"""
Background job runner.

The UI event loop must never block. A backtest takes minutes, so it runs on a
worker thread and reports progress back through a ``Job`` handle that the UI
polls. Job functions receive that handle and should call ``job.report`` inside
their loops -- that is also where cancellation takes effect, since ``report``
raises ``JobCancelled`` once ``cancel`` has been requested.
"""

from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from app_logging import get_logger

log = get_logger(__name__)

class JobCancelled(Exception):
    pass

@dataclass
class Job:
    name: str
    state: str = "pending"          # pending | running | done | failed | cancelled
    progress: float = 0.0           # 0.0 - 1.0
    message: str = ""
    result: Any = None
    error: str | None = None
    _cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def finished(self) -> bool:
        return self.state in ("done", "failed", "cancelled")

    @property
    def cancelling(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()

    def report(self, fraction: float = None, message: str = None) -> None:
        """Publish progress; raises JobCancelled if a stop was requested."""
        if self._cancel.is_set():
            raise JobCancelled(self.name)
        if fraction is not None:
            self.progress = max(0.0, min(1.0, fraction))
        if message is not None:
            self.message = message

class JobRunner:
    def __init__(self, max_workers: int = 2):
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="job")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, name: str, target: Callable[[Job], Any]) -> Job:
        with self._lock:
            existing = self._jobs.get(name)
            if existing is not None and not existing.finished:
                return existing
            job = Job(name=name)
            self._jobs[name] = job

        def run():
            job.state = "running"
            log.info("job started: %s", name)
            try:
                job.result = target(job)
                job.state = "done"
                job.progress = 1.0
                log.info("job finished: %s", name)
            except JobCancelled:
                job.state = "cancelled"
                job.message = "cancelled"
                log.info("job cancelled: %s", name)
            except Exception as error:
                job.state = "failed"
                job.error = f"{type(error).__name__}: {error}"
                job.message = job.error
                log.error("job failed: %s\n%s", name, traceback.format_exc())

        self._pool.submit(run)
        return job

    def get(self, name: str) -> Job | None:
        return self._jobs.get(name)

    def all(self) -> dict:
        return dict(self._jobs)

    def shutdown(self) -> None:
        for job in self._jobs.values():
            if not job.finished:
                job.cancel()
        self._pool.shutdown(wait=False)

RUNNER = JobRunner()
