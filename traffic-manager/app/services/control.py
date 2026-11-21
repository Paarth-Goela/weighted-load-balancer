"""Shared control-plane primitives: the update lock and DNS resolution."""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from app.errors import Busy
from app.models.common import is_ipv4


class ControlLock:
    """One lock for all HAProxy-mutating operations: API updates, health
    transitions and reconciliation. Re-entrant; bounded wait -> 503 Busy.

    Serializing every mutation means two concurrent requests can never
    interleave their Runtime API commands or DB writes."""

    def __init__(self, timeout: float = 10.0) -> None:
        self._lock = threading.RLock()
        self.timeout = timeout

    @contextmanager
    def hold(self, timeout: Optional[float] = None) -> Iterator[None]:
        if not self._lock.acquire(timeout=self.timeout if timeout is None else timeout):
            raise Busy("another configuration change is in progress; retry shortly")
        try:
            yield
        finally:
            self._lock.release()


class ResolutionError(Exception):
    pass


class Resolver:
    """Hostname -> IPv4. Needed because HAProxy 3.0 dynamic servers cannot
    use DNS resolvers. Results are deterministic (lowest address)."""

    def resolve(self, host: str) -> str:
        if is_ipv4(host):
            return host
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError) as exc:
            raise ResolutionError(f"cannot resolve host '{host}'") from exc
        addrs = sorted({i[4][0] for i in infos})
        if not addrs:
            raise ResolutionError(f"no IPv4 address for host '{host}'")
        return addrs[0]
