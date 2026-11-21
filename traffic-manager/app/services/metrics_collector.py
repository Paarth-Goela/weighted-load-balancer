"""
Per-request traffic metrics from HAProxy's log stream.

HAProxy's pool frontends send one line per proxied request to a Unix
DATAGRAM socket that this service creates on the shared runtime volume:

    M <pool backend> <server> <HTTP status> <Tr ms> <Tt ms>

  Tr = time for the gateway instance to return response headers (-1 if none)
  Tt = total session time

From these lines the collector computes, per pool and per time window:
request count, errors (HTTP 5xx, or no response at all), error rate, and
p95/avg latency (over Tr). HAProxy's own `show stat` only exposes averages,
which is why the raw stream is used.

Properties:
  * Fire-and-forget: if this service is down, HAProxy drops the lines and
    routing is unaffected.
  * In memory: a bounded deque per pool (METRICS_RETENTION_SECONDS, capped
    at MAX_EVENTS). Windows are lost on restart, so migrations pause after
    a restart (see migration_manager). Per-stage results are persisted in
    SQLite when each stage is evaluated.
"""

from __future__ import annotations

import logging
import math
import os
import socket
import threading
import time
from collections import deque
from typing import Callable, Optional

from app.models.common import POOL_BACKEND, PlatformType

log = logging.getLogger(__name__)

MAX_EVENTS = 300_000          # per pool; bounds memory (~30 MB worst case)
BACKEND_TO_TYPE = {v: k for k, v in POOL_BACKEND.items()}


def p95(values: list[float]) -> Optional[float]:
    """Nearest-rank 95th percentile."""
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)])


class MetricsCollector:
    def __init__(self, socket_path: Optional[str] = None, retention_seconds: float = 900.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.socket_path = socket_path
        self.retention = retention_seconds
        self.clock = clock
        self._lock = threading.Lock()
        # pool backend -> deque[(ts, server, status, tr, tt)]
        self._events: dict[str, deque] = {b: deque(maxlen=MAX_EVENTS) for b in POOL_BACKEND.values()}
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.received = 0
        self.malformed = 0
        self.last_event_at: Optional[float] = None
        self.listening = False
        self.error: Optional[str] = None

    # ----------------------------------------------------------- ingestion
    def ingest(self, line: str, ts: Optional[float] = None) -> bool:
        parts = line.strip().split()
        if len(parts) != 6 or parts[0] != "M" or parts[1] not in self._events:
            self.malformed += 1
            return False
        try:
            status, tr, tt = int(parts[3]), int(parts[4]), int(parts[5])
        except ValueError:
            self.malformed += 1
            return False
        ts = self.clock() if ts is None else ts
        with self._lock:
            self._events[parts[1]].append((ts, parts[2], status, tr, tt))
            self.received += 1
            self.last_event_at = ts
            self._prune(ts)
        return True

    def _prune(self, now: float) -> None:
        cutoff = now - self.retention
        for dq in self._events.values():
            while dq and dq[0][0] < cutoff:
                dq.popleft()

    # --------------------------------------------------------------- query
    def _slice(self, backend: str, since: float, until: float) -> list[tuple]:
        with self._lock:
            return [e for e in self._events[backend] if since <= e[0] < until]

    @staticmethod
    def summarize(events: list[tuple], seconds: float) -> dict:
        n = len(events)
        errors = sum(1 for e in events if e[2] >= 500 or e[2] < 0 or e[3] < 0)
        lat = [e[3] for e in events if e[3] >= 0]
        servers: dict[str, int] = {}
        for e in events:
            servers[e[1]] = servers.get(e[1], 0) + 1
        return {
            "requests": n,
            "errors": errors,
            "error_rate_percent": round(100.0 * errors / n, 3) if n else None,
            "p95_ms": p95(lat),
            "avg_ms": round(sum(lat) / len(lat), 2) if lat else None,
            "rps": round(n / seconds, 2) if seconds > 0 else None,
            "per_server": servers,
        }

    def window(self, platform: PlatformType, since: float, until: Optional[float] = None) -> dict:
        until = self.clock() if until is None else until
        return self.summarize(self._slice(POOL_BACKEND[platform], since, until), max(until - since, 0.0))

    def recent(self, seconds: float = 60.0) -> dict:
        now = self.clock()
        return {p.value: self.window(p, now - seconds, now) for p in PlatformType}

    def series(self, seconds: float = 600.0, bucket: float = 10.0) -> list[dict]:
        """Time buckets (oldest first) with per-pool requests, error rate and p95."""
        now = self.clock()
        start = math.floor((now - seconds) / bucket) * bucket
        out = []
        t = start
        while t < now:
            row = {"t": t}
            for p in PlatformType:
                s = self.summarize(self._slice(POOL_BACKEND[p], t, t + bucket), bucket)
                row[p.value] = {k: s[k] for k in ("requests", "errors", "error_rate_percent", "p95_ms")}
            out.append(row)
            t += bucket
        return out

    def status(self) -> dict:
        now = self.clock()
        return {
            "listening": self.listening,
            "received_total": self.received,
            "malformed_total": self.malformed,
            "seconds_since_last_event": round(now - self.last_event_at, 1) if self.last_event_at else None,
            "retention_seconds": self.retention,
            "error": self.error,
        }

    # -------------------------------------------------------------- socket
    def start(self) -> None:
        if not self.socket_path:
            return
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.bind(self.socket_path)
            os.chmod(self.socket_path, 0o600)   # only uid 99 (HAProxy + this service)
            sock.settimeout(1.0)
            self._sock = sock
            self.listening = True
        except OSError as exc:
            self.error = f"metrics socket unavailable: {exc.strerror or exc}"
            log.error("metrics collector disabled: %s", exc)
            return

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    data = self._sock.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                for line in data.decode("utf-8", "replace").splitlines():
                    if line.strip():
                        self.ingest(line)

        self._thread = threading.Thread(target=loop, name="metrics-collector", daemon=True)
        self._thread.start()
        log.info("metrics collector listening on %s", self.socket_path)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._sock:
            self._sock.close()
        self.listening = False
