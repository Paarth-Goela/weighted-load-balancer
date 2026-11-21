"""
Minimal client for the HAProxy Runtime API (stats socket), used to read and
change backend server weights while HAProxy keeps running.

Transport
---------
The admin socket is a Unix socket *inside* the HAProxy container
(/var/lib/haproxy/admin.sock, mode 600). It is never published as a port.
The host reaches it with:

    docker exec -i haproxy socat stdio unix-connect:/var/lib/haproxy/admin.sock

so only users who already control the Docker daemon can change weights.

Routing decisions are always made by HAProxy. This module only reads and
writes server weights; it never picks a backend for a request.

Standard library only. Compatible with Python 3.9+.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Defaults (overridable through environment variables or CLI flags)
# --------------------------------------------------------------------------
DEFAULT_CONTAINER = os.environ.get("HAPROXY_CONTAINER", "haproxy")
DEFAULT_SOCKET = os.environ.get("HAPROXY_SOCKET", "/var/lib/haproxy/admin.sock")
DEFAULT_BACKEND = os.environ.get("HAPROXY_BACKEND", "http_backends")
DEFAULT_SERVER_A = os.environ.get("HAPROXY_SERVER_A", "backend-a")
DEFAULT_SERVER_B = os.environ.get("HAPROXY_SERVER_B", "backend-b")

# HAProxy accepts absolute weights in the range 0..256.
MIN_WEIGHT = 0
MAX_WEIGHT = 256

# Named traffic configurations from the Milestone 2 specification.
# Values are (weight A, weight B); with a 100-point scale weight == percent.
SCENARIOS: Dict[str, Tuple[int, int]] = {
    "baseline": (100, 0),
    "canary-1": (90, 10),
    "canary-2": (80, 20),
    "canary-3": (75, 25),
    "equal": (50, 50),
    "final": (0, 100),
}


class RuntimeAPIError(RuntimeError):
    """The Runtime API could not be reached or rejected a command."""


class WeightValidationError(ValueError):
    """A requested weight configuration is invalid."""


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def parse_weight(raw: object, name: str) -> int:
    """Parse and validate one weight. Accepts ints or decimal-integer strings."""
    if raw is None:
        raise WeightValidationError(f"missing weight for {name}")
    if isinstance(raw, bool):  # bool is a subclass of int; reject explicitly
        raise WeightValidationError(f"weight for {name} must be an integer, got {raw!r}")
    if isinstance(raw, int):
        value = raw
    else:
        text = str(raw).strip()
        # Only plain non-negative/negative decimal integers. Rejects "1.5",
        # "1e2", "0x10", "50%", "", "abc".
        body = text[1:] if text[:1] in "+-" else text
        if not body.isdigit() or not body.isascii():
            raise WeightValidationError(
                f"weight for {name} must be an integer, got {raw!r}"
            )
        value = int(text)
    if value < MIN_WEIGHT:
        raise WeightValidationError(
            f"weight for {name} must be non-negative, got {value}"
        )
    if value > MAX_WEIGHT:
        raise WeightValidationError(
            f"weight for {name} must be <= {MAX_WEIGHT} (HAProxy limit), got {value}"
        )
    return value


def validate_weights(weight_a: object, weight_b: object) -> Tuple[int, int]:
    """Validate a pair of weights. Returns (a, b) as ints or raises."""
    a = parse_weight(weight_a, "backend A")
    b = parse_weight(weight_b, "backend B")
    if a + b <= 0:
        raise WeightValidationError(
            "combined weight must be greater than zero "
            "(0:0 would leave no server eligible for traffic)"
        )
    return a, b


def percentages(weight_a: int, weight_b: int) -> Tuple[float, float]:
    """Convert a weight pair to the traffic share each server receives."""
    total = weight_a + weight_b
    if total <= 0:
        return 0.0, 0.0
    return 100.0 * weight_a / total, 100.0 * weight_b / total


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------
Transport = Callable[[str], str]


def docker_exec_transport(
    container: str = DEFAULT_CONTAINER,
    socket_path: str = DEFAULT_SOCKET,
    timeout: float = 10.0,
) -> Transport:
    """Return a transport that sends one command via `docker exec ... socat`."""

    def send(command: str) -> str:
        argv = [
            "docker", "exec", "-i", container,
            "socat", "-t", "5", "stdio", f"unix-connect:{socket_path}",
        ]
        try:
            proc = subprocess.run(
                argv,
                input=command.rstrip("\n") + "\n",
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise RuntimeAPIError("'docker' executable not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeAPIError(
                f"timed out talking to HAProxy runtime socket in '{container}'"
            ) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise RuntimeAPIError(
                f"cannot reach HAProxy runtime socket {socket_path} in container "
                f"'{container}': {detail or 'exit code ' + str(proc.returncode)}"
            )
        return proc.stdout

    return send


# --------------------------------------------------------------------------
# Runtime API client
# --------------------------------------------------------------------------
@dataclass
class ServerState:
    name: str
    address: str
    weight: int          # current (user) weight
    initial_weight: int  # weight from haproxy.cfg
    op_state: int        # 0=stopped 1=starting 2=running 3=stopping
    admin_state: int     # bit field; 0 = no admin flags (not in maintenance)

    @property
    def is_up(self) -> bool:
        return self.op_state == 2 and self.admin_state == 0

    @property
    def status(self) -> str:
        if self.admin_state != 0:
            return "MAINT"
        return {0: "DOWN", 1: "STARTING", 2: "UP", 3: "STOPPING"}.get(
            self.op_state, f"op_state={self.op_state}"
        )


@dataclass
class WeightChangeResult:
    previous: Dict[str, int]
    applied: Dict[str, int]


class HAProxyRuntime:
    def __init__(
        self,
        transport: Optional[Transport] = None,
        backend: str = DEFAULT_BACKEND,
        server_a: str = DEFAULT_SERVER_A,
        server_b: str = DEFAULT_SERVER_B,
    ) -> None:
        self.send = transport or docker_exec_transport()
        self.backend = backend
        self.server_a = server_a
        self.server_b = server_b

    # ---- low level -------------------------------------------------------
    def command(self, cmd: str) -> str:
        return self.send(cmd)

    # ---- reads -----------------------------------------------------------
    def servers_state(self) -> Dict[str, ServerState]:
        """Parse `show servers state <backend>` into ServerState objects."""
        out = self.command(f"show servers state {self.backend}")
        lines = [ln for ln in out.splitlines() if ln.strip()]
        header: Optional[List[str]] = None
        states: Dict[str, ServerState] = {}
        for line in lines:
            if line.startswith("#"):
                header = line.lstrip("#").split()
                continue
            if header is None:
                # First line is the format version ("1"), or an error message.
                if not line.strip().isdigit():
                    raise RuntimeAPIError(f"unexpected Runtime API response: {line.strip()}")
                continue
            row = dict(zip(header, line.split()))
            if row.get("be_name") != self.backend:
                continue
            try:
                state = ServerState(
                    name=row["srv_name"],
                    address=row["srv_addr"],
                    weight=int(row["srv_uweight"]),
                    initial_weight=int(row["srv_iweight"]),
                    op_state=int(row["srv_op_state"]),
                    admin_state=int(row["srv_admin_state"]),
                )
            except (KeyError, ValueError) as exc:
                raise RuntimeAPIError(f"cannot parse server state line: {line!r}") from exc
            states[state.name] = state
        if not states:
            raise RuntimeAPIError(
                f"backend '{self.backend}' not found or has no servers "
                f"(response: {out.strip()!r})"
            )
        return states

    def get_weights(self) -> Tuple[int, int]:
        states = self.servers_state()
        for name in (self.server_a, self.server_b):
            if name not in states:
                raise RuntimeAPIError(
                    f"server '{name}' not found in backend '{self.backend}' "
                    f"(known: {', '.join(sorted(states))})"
                )
        return states[self.server_a].weight, states[self.server_b].weight

    # ---- writes ----------------------------------------------------------
    def set_server_weight(self, server: str, weight: int) -> None:
        reply = self.command(
            f"set server {self.backend}/{server} weight {int(weight)}"
        ).strip()
        # HAProxy replies with an empty line on success, text on failure.
        if reply:
            raise RuntimeAPIError(f"HAProxy rejected weight {weight} for {server}: {reply}")

    def set_weights(self, weight_a: object, weight_b: object) -> WeightChangeResult:
        """Validate and apply a new A/B weight pair, rolling back on failure.

        Guarantees:
          * Invalid input is rejected before anything is sent to HAProxy.
          * The current weights are read first; if they can't be read,
            nothing is changed.
          * Increases are applied before decreases, so the combined weight
            never passes through 0 (e.g. 100:0 -> 0:100 never becomes 0:0).
          * After applying, weights are read back and verified.
          * If any step fails, servers already changed are restored to their
            previous weights.
        """
        a, b = validate_weights(weight_a, weight_b)
        prev_a, prev_b = self.get_weights()  # also checks that both servers exist
        previous = {self.server_a: prev_a, self.server_b: prev_b}
        target = {self.server_a: a, self.server_b: b}

        # Increases first, then decreases -> sum stays > 0 throughout.
        order = sorted(target, key=lambda s: target[s] - previous[s], reverse=True)
        changed: List[str] = []
        try:
            for server in order:
                if target[server] == previous[server]:
                    continue
                self.set_server_weight(server, target[server])
                changed.append(server)

            got_a, got_b = self.get_weights()
            if (got_a, got_b) != (a, b):
                raise RuntimeAPIError(
                    f"verification failed: expected {a}:{b}, HAProxy reports {got_a}:{got_b}"
                )
        except Exception as exc:
            rollback_errors = []
            for server in reversed(changed):
                try:
                    self.set_server_weight(server, previous[server])
                except Exception as rb_exc:  # pragma: no cover - reported below
                    rollback_errors.append(f"{server}: {rb_exc}")
            msg = f"weight update failed, previous configuration {prev_a}:{prev_b} restored: {exc}"
            if rollback_errors:
                msg = (
                    f"weight update failed AND rollback failed ({'; '.join(rollback_errors)}); "
                    f"original error: {exc}"
                )
            raise RuntimeAPIError(msg) from exc

        return WeightChangeResult(previous=previous, applied=target)

    def process_info(self) -> Dict[str, str]:
        """Return `show info` as a dict (Pid, Uptime_sec, Version, ...)."""
        out = self.command("show info")
        info: Dict[str, str] = {}
        for line in out.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                info[k.strip()] = v.strip()
        return info
