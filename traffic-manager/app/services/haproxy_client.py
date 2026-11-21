"""
HAProxy Runtime API client.

Builds on the Milestone 2 module `haproxy_runtime` (runtime/haproxy_runtime.py).
Pool-level policy weights reuse its validated, ordered, verified set_weights()
with rollback. This module adds:

  * a private Unix-socket transport,
  * dynamic gateway-instance servers (add / delete / state / weight / addr),
  * read-back verification of every change,
  * strict parsing that rejects malformed responses,
  * refusal to send commands containing Runtime API separators.

Limitations of HAProxy 3.0 that this client handles:
  * Dynamic servers cannot use DNS `resolvers`, so callers pass an IPv4
    address and re-resolve hostnames themselves.
  * Dynamic servers start in maintenance with health checks disabled. They
    are enabled explicitly (`enable health`, then `set server ... state ready`).
  * Dynamic servers disappear when HAProxy restarts. The reconciler re-adds
    them from SQLite.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import socket
import time
from dataclasses import dataclass
from typing import Callable, Optional

from haproxy_runtime import (  # Milestone 2 module
    HAProxyRuntime,
    RuntimeAPIConnectionError,
    RuntimeAPIError,
    WeightChangeResult,
    WeightUpdateError,
)

from app.models.common import POLICY_BACKEND, POLICY_SERVER, PlatformType, check_hostname, check_server_name, is_ipv4

__all__ = [
    "HAProxyClient", "RuntimeServer", "ServerSpec", "unix_socket_transport",
    "RuntimeAPIConnectionError", "RuntimeAPIError", "WeightUpdateError",
]

log = logging.getLogger(__name__)

Transport = Callable[[str], str]

# srv_admin_state bits (HAProxy src/server.h)
_ADMIN_MAINT = 0x01 | 0x02 | 0x04 | 0x20 | 0x40   # FMAINT IMAINT CMAINT RMAINT HMAINT
_ADMIN_DRAIN = 0x08 | 0x10                         # FDRAIN IDRAIN
_OP_STATES = {0: "DOWN", 1: "STARTING", 2: "UP", 3: "STOPPING"}
_FORBIDDEN = re.compile(r"[;\r\n]")
_CA_RE = re.compile(r"^(@system-ca|/[A-Za-z0-9._/-]{1,255})$")


def unix_socket_transport(path: str, timeout: float = 5.0) -> Transport:
    """One command per connection (non-interactive CLI mode)."""

    def send(command: str) -> str:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(path)
                s.sendall((command + "\n").encode())
                chunks = []
                while True:
                    data = s.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
        except OSError as exc:
            # Socket path only in the service log, never in API responses.
            log.warning("HAProxy runtime socket error on %s: %r", path, exc)
            raise RuntimeAPIConnectionError("HAProxy Runtime API is unreachable") from exc
        return b"".join(chunks).decode("utf-8", errors="replace")

    return send


@dataclass
class RuntimeServer:
    backend: str
    name: str
    address: str
    port: int
    op_state: int
    admin_state: int
    weight: int
    initial_weight: int

    @property
    def admin(self) -> str:
        if self.admin_state & _ADMIN_MAINT:
            return "MAINT"
        if self.admin_state & _ADMIN_DRAIN:
            return "DRAIN"
        return "READY"

    @property
    def operational(self) -> str:
        return _OP_STATES.get(self.op_state, f"STATE_{self.op_state}")

    @property
    def is_up(self) -> bool:
        """Eligible for new traffic: READY and UP (weight is separate)."""
        return self.admin == "READY" and self.op_state == 2


@dataclass
class ServerSpec:
    name: str
    address: str          # IPv4 (dynamic servers cannot resolve DNS)
    port: int
    weight: int = 100
    tls: bool = False
    tls_verify: bool = True
    sni: Optional[str] = None


class HAProxyClient:
    def __init__(self, transport: Transport, ca_file: str = "@system-ca", check_inter: str = "3s") -> None:
        if not _CA_RE.match(ca_file):
            raise ValueError("invalid HAPROXY_CA_FILE")
        if not re.match(r"^\d{1,5}(ms|s)?$", check_inter):
            raise ValueError("invalid check interval")
        self._send = transport
        self.ca_file = ca_file
        self.check_inter = check_inter
        self.policy = HAProxyRuntime(
            transport=self.command,
            backend=POLICY_BACKEND,
            server_a=POLICY_SERVER[PlatformType.APIGEE_OPDK],
            server_b=POLICY_SERVER[PlatformType.APIGEE_HYBRID],
        )

    # ------------------------------------------------------------ transport
    def command(self, cmd: str) -> str:
        if _FORBIDDEN.search(cmd):
            raise ValueError("refusing to send Runtime API command containing ';' or newline")
        return self._send(cmd)

    @staticmethod
    def _malformed(what: str, out: str) -> RuntimeAPIError:
        first = (out.strip().splitlines() or [""])[0][:120]
        return RuntimeAPIError(f"malformed '{what}' response from HAProxy: {first!r}")

    # ---------------------------------------------------------------- reads
    def servers(self, backend: str) -> dict[str, RuntimeServer]:
        out = self.command(f"show servers state {backend}")
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if not lines or lines[0].strip() != "1":
            raise self._malformed("show servers state", out)
        header: Optional[list[str]] = None
        result: dict[str, RuntimeServer] = {}
        for line in lines[1:]:
            if line.startswith("#"):
                header = line.lstrip("#").split()
                continue
            if header is None:
                raise self._malformed("show servers state", out)
            row = dict(zip(header, line.split()))
            if row.get("be_name") != backend:
                continue
            try:
                srv = RuntimeServer(
                    backend=backend,
                    name=row["srv_name"],
                    address=row["srv_addr"],
                    port=int(row["srv_port"]),
                    op_state=int(row["srv_op_state"]),
                    admin_state=int(row["srv_admin_state"]),
                    weight=int(row["srv_uweight"]),
                    initial_weight=int(row["srv_iweight"]),
                )
            except (KeyError, ValueError) as exc:
                raise self._malformed("show servers state", out) from exc
            result[srv.name] = srv
        return result

    def stats(self) -> dict[tuple[str, str], dict[str, str]]:
        out = self.command("show stat")
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if not lines or not lines[0].startswith("# pxname"):
            raise self._malformed("show stat", out)
        reader = csv.DictReader(io.StringIO("\n".join([lines[0][2:]] + lines[1:])))
        return {(r["pxname"], r["svname"]): r for r in reader if r.get("pxname") and r.get("svname")}

    def info(self) -> dict[str, str]:
        out = self.command("show info")
        info = {}
        for line in out.splitlines():
            k, sep, v = line.partition(":")
            if sep:
                info[k.strip()] = v.strip()
        if "Version" not in info:
            raise self._malformed("show info", out)
        return info

    def start_time(self) -> int:
        info = self.info()
        try:
            return int(info["Start_time_sec"])
        except (KeyError, ValueError) as exc:
            raise RuntimeAPIError("'show info' did not report Start_time_sec") from exc

    # ------------------------------------------------- pool-level weights
    def policy_weights(self) -> tuple[int, int]:
        return self.policy.get_weights()

    def set_policy_weights(self, opdk: int, hybrid: int) -> WeightChangeResult:
        """Milestone 2 procedure: validate, preflight, increases first, verify, rollback."""
        return self.policy.set_weights(opdk, hybrid)

    # ------------------------------------------------- instance servers
    def _validate(self, backend: str, name: str) -> None:
        check_server_name(name)
        check_server_name(backend)

    def add_server(self, backend: str, spec: ServerSpec) -> RuntimeServer:
        """Add a server in MAINT with health checks enabled; verify it exists."""
        self._validate(backend, spec.name)
        if not is_ipv4(spec.address):
            raise ValueError("dynamic servers require an IPv4 address")
        if not (1 <= spec.port <= 65535 and 1 <= spec.weight <= 256):
            raise ValueError("invalid port or weight")
        cmd = (f"add server {backend}/{spec.name} {spec.address}:{spec.port} "
               f"weight {spec.weight} check inter {self.check_inter} fall 3 rise 2")
        if spec.tls:
            cmd += f" ssl verify required ca-file {self.ca_file}" if spec.tls_verify else " ssl verify none"
            cmd += " check-ssl"
            if spec.sni:
                check_hostname(spec.sni)
                cmd += f" sni str({spec.sni}) check-sni {spec.sni}"
        reply = self.command(cmd).strip()
        if reply != "New server registered.":
            raise RuntimeAPIError(f"HAProxy rejected 'add server {backend}/{spec.name}': {reply[:200]}")
        try:
            reply = self.command(f"enable health {backend}/{spec.name}").strip()
            if reply:
                raise RuntimeAPIError(f"HAProxy rejected 'enable health': {reply[:200]}")
            srv = self.servers(backend).get(spec.name)
            if srv is None or srv.admin != "MAINT":
                raise RuntimeAPIError(f"verification failed: {spec.name} not present in MAINT after add")
        except Exception:
            try:  # compensate: never leave a half-configured server behind
                self.command(f"del server {backend}/{spec.name}")
            except Exception:
                pass
            raise
        return srv

    def set_state(self, backend: str, name: str, state: str) -> RuntimeServer:
        self._validate(backend, name)
        if state not in ("ready", "maint", "drain"):
            raise ValueError("state must be ready|maint|drain")
        reply = self.command(f"set server {backend}/{name} state {state}").strip()
        if reply:
            raise RuntimeAPIError(f"HAProxy rejected state {state} for {name}: {reply[:200]}")
        srv = self.servers(backend).get(name)
        if srv is None or srv.admin != state.upper():
            got = srv.admin if srv else "absent"
            raise RuntimeAPIError(f"verification failed: {name} is {got}, expected {state.upper()}")
        return srv

    def set_weight(self, backend: str, name: str, weight: int) -> RuntimeServer:
        self._validate(backend, name)
        if not 0 <= int(weight) <= 256:
            raise ValueError("weight must be 0..256")
        reply = self.command(f"set server {backend}/{name} weight {int(weight)}").strip()
        if reply:
            raise RuntimeAPIError(f"HAProxy rejected weight for {name}: {reply[:200]}")
        srv = self.servers(backend).get(name)
        if srv is None or srv.weight != int(weight):
            raise RuntimeAPIError(f"verification failed: weight of {name} not {weight}")
        return srv

    def set_addr(self, backend: str, name: str, address: str, port: int) -> RuntimeServer:
        self._validate(backend, name)
        if not is_ipv4(address) or not 1 <= port <= 65535:
            raise ValueError("invalid address/port")
        reply = self.command(f"set server {backend}/{name} addr {address} port {port}").strip()
        if reply and "changed" not in reply and "no need" not in reply:
            raise RuntimeAPIError(f"HAProxy rejected addr for {name}: {reply[:200]}")
        srv = self.servers(backend).get(name)
        if srv is None or (srv.address, srv.port) != (address, port):
            raise RuntimeAPIError(f"verification failed: address of {name} not {address}:{port}")
        return srv

    def delete_server(self, backend: str, name: str, drain_timeout: float = 10.0,
                      sleep: Callable[[float], None] = time.sleep) -> bool:
        """Drain -> wait for sessions -> maint -> kill leftovers -> delete -> verify."""
        self._validate(backend, name)
        if name not in self.servers(backend):
            return False
        self.set_state(backend, name, "drain")
        deadline = time.monotonic() + drain_timeout
        while time.monotonic() < deadline:
            row = self.stats().get((backend, name), {})
            if int(row.get("scur") or 0) == 0:
                break
            sleep(0.2)
        self.set_state(backend, name, "maint")
        self.command(f"shutdown sessions server {backend}/{name}")
        reply = self.command(f"del server {backend}/{name}").strip()
        if reply != "Server deleted.":
            raise RuntimeAPIError(f"HAProxy rejected 'del server {backend}/{name}': {reply[:200]}")
        if name in self.servers(backend):
            raise RuntimeAPIError(f"verification failed: {name} still present after delete")
        return True
