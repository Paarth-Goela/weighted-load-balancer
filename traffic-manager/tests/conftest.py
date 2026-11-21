"""Fixtures: an in-memory fake HAProxy Runtime API, fake DNS and fake prober."""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT.parent / "runtime", Path("/app/lib")):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.services.control import ResolutionError  # noqa: E402
from app.services.haproxy_client import RuntimeAPIConnectionError  # noqa: E402
from app.services.health_monitor import ProbeResult  # noqa: E402
from app.settings import Settings  # noqa: E402

TOKEN = "unit-test-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
POOLS = ("apigee_opdk_pool", "apigee_hybrid_pool")
SELECTOR = {"opdk": "apigee_opdk_pool", "hybrid": "apigee_hybrid_pool"}


@dataclass
class FakeServer:
    name: str
    addr: str
    port: int
    weight: int
    iweight: int
    admin: int = 0          # 0 READY, 1 MAINT, 8 DRAIN
    op: int = 2
    scur: int = 0
    stot: int = 0
    cmd: str = ""


class FakeHAProxy:
    """Emulates the HAProxy 3.0 Runtime API commands the service uses."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.start_time = 1_700_000_000
        self.reachable = True
        self.native_down: set[str] = set()      # servers failing HAProxy's own check
        self.fail_once: dict[str, str] = {}     # command prefix -> error reply (once)
        self.disconnect_on: Optional[str] = None  # command prefix that makes HAProxy vanish
        self.commands: list[str] = []
        self.policy_history: list[tuple[int, int]] = []
        self._boot()

    def _boot(self) -> None:
        self.backends: dict[str, dict[str, FakeServer]] = {
            "traffic_policy": {
                "opdk": FakeServer("opdk", "-", 0, 100, 100),
                "hybrid": FakeServer("hybrid", "-", 0, 0, 0),
            },
            **{b: {} for b in POOLS},
        }

    def restart(self) -> None:
        """HAProxy restart: dynamic servers lost, weights back to haproxy.cfg."""
        with self.lock:
            self._boot()
            self.start_time += 30

    # helpers -------------------------------------------------------------
    def policy(self) -> tuple[int, int]:
        tp = self.backends["traffic_policy"]
        return tp["opdk"].weight, tp["hybrid"].weight

    def server(self, backend: str, name: str) -> Optional[FakeServer]:
        return self.backends[backend].get(name)

    def admin(self, backend: str, name: str) -> Optional[str]:
        s = self.server(backend, name)
        return None if s is None else {0: "READY", 1: "MAINT", 8: "DRAIN"}[s.admin]

    def _pool_up(self, pool: str) -> bool:
        return any(s.admin == 0 and s.op == 2 for s in self.backends[pool].values())

    def _refresh(self) -> None:
        for name, pool in SELECTOR.items():
            self.backends["traffic_policy"][name].op = 2 if self._pool_up(pool) else 0
        for pool in POOLS:
            for s in self.backends[pool].values():
                s.op = 0 if s.name in self.native_down else 2

    # protocol ------------------------------------------------------------
    def __call__(self, cmd: str) -> str:
        with self.lock:
            self.commands.append(cmd)
            if not self.reachable:
                raise RuntimeAPIConnectionError("HAProxy Runtime API is unreachable")
            if self.disconnect_on and cmd.startswith(self.disconnect_on):
                self.reachable = False
                raise RuntimeAPIConnectionError("HAProxy Runtime API is unreachable")
            for prefix, reply in list(self.fail_once.items()):
                if cmd.startswith(prefix):
                    del self.fail_once[prefix]
                    return reply
            self._refresh()
            return self._dispatch(cmd.split())

    def _dispatch(self, p: list[str]) -> str:
        if p[:3] == ["show", "servers", "state"]:
            b = p[3]
            if b not in self.backends:
                return "Can't find backend.\n"
            out = ("1\n# be_id be_name srv_id srv_name srv_addr srv_op_state srv_admin_state srv_uweight "
                   "srv_iweight srv_time_since_last_change srv_check_status srv_check_result srv_check_health "
                   "srv_check_state srv_agent_state bk_f_forced_id srv_f_forced_id srv_fqdn srv_port\n")
            for i, s in enumerate(self.backends[b].values(), 1):
                out += f"3 {b} {i} {s.name} {s.addr} {s.op} {s.admin} {s.weight} {s.iweight} 1 15 3 4 6 0 0 0 - {s.port}\n"
            return out + "\n"
        if p[:2] == ["show", "stat"]:
            out = "# pxname,svname,scur,stot,status,weight,check_status,\n"
            for b, servers in self.backends.items():
                for s in servers.values():
                    status = "MAINT" if s.admin == 1 else ("DRAIN" if s.admin == 8 else ("UP" if s.op == 2 else "DOWN"))
                    out += f"{b},{s.name},{s.scur},{s.stot},{status},{s.weight},{'L7OK' if s.op == 2 else 'L7STS'},\n"
            return out + "\n"
        if p[:2] == ["show", "info"]:
            return f"Name: HAProxy\nVersion: 3.0.31-fake\nUptime_sec: 42\nStart_time_sec: {self.start_time}\n\n"
        if p[:2] == ["add", "server"]:
            b, n = p[2].split("/", 1)
            if b not in self.backends:
                return "No such backend.\n"
            if n in self.backends[b]:
                return "Already exists a server with the same name in backend.\n"
            addr, port = p[3].rsplit(":", 1)
            w = int(p[p.index("weight") + 1])
            self.backends[b][n] = FakeServer(n, addr, int(port), w, w, admin=1, op=0, cmd=" ".join(p))
            return "New server registered.\n"
        if p[:2] == ["enable", "health"]:
            return "\n"
        if p[:2] == ["shutdown", "sessions"]:
            return "\n"
        if p[:2] == ["del", "server"]:
            b, n = p[2].split("/", 1)
            s = self.backends[b].get(n)
            if s is None:
                return "No such server.\n"
            if s.admin != 1:
                return "Only servers in maintenance mode can be deleted.\n"
            del self.backends[b][n]
            return "Server deleted.\n"
        if p[:2] == ["set", "server"]:
            b, n = p[2].split("/", 1)
            s = self.backends.get(b, {}).get(n)
            if s is None:
                return "No such server.\n"
            if p[3] == "weight":
                w = int(p[4])
                if not 0 <= w <= 256:
                    return "Absolute weight can only be between 0 and 256 inclusive.\n"
                s.weight = w
                if b == "traffic_policy":
                    self.policy_history.append(self.policy())
                return "\n"
            if p[3] == "state":
                s.admin = {"ready": 0, "maint": 1, "drain": 8}[p[4]]
                return "\n"
            if p[3] == "addr":
                old, s.addr, s.port = s.addr, p[4], int(p[6])
                return f"IP changed from '{old}' to '{s.addr}'\n"
        return "Unknown command.\n"


class FakeResolver:
    def __init__(self) -> None:
        self.overrides: dict[str, str] = {}
        self.unresolvable: set[str] = set()

    def resolve(self, host: str) -> str:
        if host in self.unresolvable:
            raise ResolutionError(f"cannot resolve host '{host}'")
        if host in self.overrides:
            return self.overrides[host]
        if all(c.isdigit() or c == "." for c in host):
            return host
        return f"172.30.0.{(sum(map(ord, host)) % 200) + 10}"


class FakeProber:
    def __init__(self) -> None:
        self.unhealthy: set[str] = set()
        self.calls: list[str] = []

    def probe(self, inst: dict) -> ProbeResult:
        self.calls.append(inst["name"])
        if inst["name"] in self.unhealthy:
            return ProbeResult(False, 503, 1.0, "unexpected HTTP status 503", "172.30.0.1")
        return ProbeResult(True, 200, 1.0, None, "172.30.0.1")


@dataclass
class Env:
    tmp: Path
    fake: FakeHAProxy = field(default_factory=FakeHAProxy)
    resolver: FakeResolver = field(default_factory=FakeResolver)
    prober: FakeProber = field(default_factory=FakeProber)
    clients: list = field(default_factory=list)

    def settings(self, **kw) -> Settings:
        base = dict(api_token=TOKEN, database_path=self.tmp / "tm.db", run_background=False,
                    failure_threshold=3, recovery_threshold=2, lock_timeout=5, drain_timeout=0.2,
                    pool_release_settle_seconds=0)
        base.update(kw)
        return Settings(**base)

    def client(self, **kw) -> TestClient:
        """A fresh 'process start' of the management API on the same database."""
        app = create_app(self.settings(**kw), transport=self.fake, resolver=self.resolver, prober=self.prober)
        c = TestClient(app)
        c.__enter__()
        self.clients.append(c)
        return c

    def close(self) -> None:
        while self.clients:
            self.clients.pop().__exit__(None, None, None)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


@pytest.fixture
def client(env) -> TestClient:
    return env.client()


def cycle(client: TestClient, n: int = 1) -> dict:
    out = {}
    for _ in range(n):
        out = client.app.state.monitor.run_cycle()
    return out


INSTANCES = {
    "opdk-router-1": "opdk", "opdk-router-2": "opdk",
    "hybrid-ingress-1": "hybrid", "hybrid-ingress-2": "hybrid",
}


def seed(client: TestClient, healthy: bool = True, policy: Optional[tuple[int, int]] = (100, 0)) -> dict:
    """Register both platforms, pools, 2 instances each; optionally make them healthy
    and set an initial policy. Returns ids."""
    ids: dict = {}
    for key, ptype, name in (("opdk", "APIGEE_OPDK", "Apigee OPDK (simulated)"),
                             ("hybrid", "APIGEE_HYBRID", "Apigee Hybrid (simulated)")):
        r = client.post("/api/v1/gateways", json={"name": name, "platform_type": ptype}, headers=AUTH)
        assert r.status_code == 201, r.text
        r = client.post("/api/v1/pools", json={"name": f"{key.upper()} Pool", "platform_id": r.json()["id"]},
                        headers=AUTH)
        assert r.status_code == 201, r.text
        ids[f"{key}_pool"] = r.json()["id"]
    for name, key in INSTANCES.items():
        r = client.post("/api/v1/instances", json={"name": name, "pool_id": ids[f"{key}_pool"], "host": name,
                                                   "port": 8000}, headers=AUTH)
        assert r.status_code == 201, r.text
        ids[name] = r.json()["id"]
    if healthy:
        cycle(client, 2)  # recovery_threshold = 2
    if policy:
        r = client.put("/api/v1/traffic/config", json={"opdk_weight": policy[0], "hybrid_weight": policy[1]},
                       headers=AUTH)
        assert r.status_code == 200, r.text
    return ids


def write_json(path: Path, obj) -> Path:
    path.write_text(json.dumps(obj))
    return path
