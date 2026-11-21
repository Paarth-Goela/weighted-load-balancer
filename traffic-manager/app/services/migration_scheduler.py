"""Background scheduler: drives active migrations forward (one tick per second).

All decisions live in MigrationManager.tick(); this thread only provides the
clock pulse, so the same logic is unit-tested by calling tick() directly.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from app.services.migration_manager import MigrationManager

log = logging.getLogger(__name__)


class MigrationScheduler:
    def __init__(self, manager: MigrationManager, interval: float = 1.0) -> None:
        self.manager = manager
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(self.interval):
                try:
                    self.manager.tick()
                except Exception:
                    log.exception("migration scheduler tick failed")

        self._thread = threading.Thread(target=loop, name="migration-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
