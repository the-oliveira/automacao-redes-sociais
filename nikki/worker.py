"""Loop do worker (thread dentro do processo do servidor)."""

from __future__ import annotations

import threading
import time

from nikki.db import session_scope
from nikki.events import get_logger, log_event
from nikki.pipeline import orchestrator
from nikki.settings import get_settings


class Worker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_tick: float | None = None
        self.last_sync: float = 0.0
        self.last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="nikki-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def tick(self) -> None:
        s = get_settings()
        orchestrator.process_queued_promotions(s=s)
        orchestrator.process_due_jobs(s=s)
        orchestrator.run_aggregate_schedule(s=s)
        if time.time() - self.last_sync >= s.sync_interval_seconds:
            self.last_sync = time.time()
            orchestrator.sync_external_status(s=s)
        self.last_tick = time.time()

    def _loop(self) -> None:
        interval = get_settings().worker_interval_seconds
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = None
            except Exception as e:  # noqa: BLE001 - o loop nunca morre
                self.last_error = str(e)[:500]
                get_logger().exception("worker_tick_failed")
                try:
                    with session_scope() as db:
                        log_event(db, "worker_tick_failed", level="error", error=self.last_error)
                except Exception:  # noqa: BLE001
                    pass
            self._stop.wait(interval)


worker = Worker()
