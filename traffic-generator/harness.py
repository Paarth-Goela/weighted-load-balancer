"""
Test harness for the load/failure suite: management-API client, Prometheus
scrape parser and fault injection against the LOCAL SIMULATION stack.

Fault types
  * error_rate        simulator returns HTTP 500 for a fraction of requests
  * latency           fixed delay + uniform jitter on every API request
  * drop_rate         simulator resets a fraction of connections with no
                      response (application-level stand-in for packet loss;
                      not network-level loss)
  * unready           readiness endpoint returns 503 (graceful removal path)
  * crash             `docker kill` the instance container (abrupt failure),
                      restored with `docker start` (new process, possibly a new
                      IP address - exercises DNS re-resolution)
  * haproxy_restart   `docker restart haproxy` (dynamic servers lost, rebuilt
                      by the reconciler)

Container names and host ports are those of docker-compose.yml; they are
local-simulation details and never used by the traffic-manager itself.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]

# instance -> (container, host port of its simulator control endpoint)
INSTANCES: Dict[str, Tuple[str, int]] = {
    "opdk-router-1": ("backend-a", 8001),
    "opdk-router-2": ("opdk-router-2", 8003),
    "hybrid-ingress-1": ("backend-b", 8002),
    "hybrid-ingress-2": ("hybrid-ingress-2", 8004),
}
POOL_INSTANCES = {
    "APIGEE_OPDK": ("opdk-router-1", "opdk-router-2"),
    "APIGEE_HYBRID": ("hybrid-ingress-1", "hybrid-ingress-2"),
}
NO_FAULTS = {"error_rate": 0.0, "latency_ms": 0, "latency_jitter_ms": 0, "drop_rate": 0.0}


def load_token(var: str = "TRAFFIC_API_TOKEN") -> str:
    tok = os.environ.get(var, "").strip()
    env_file = ROOT / ".env"
    if not tok and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(var + "="):
                tok = line.split("=", 1)[1].strip()
    if not tok:
        raise SystemExit(f"{var} is not set (export it or put it in {env_file})")
    return tok


def _http(method: str, url: str, body: Optional[dict] = None, headers: Optional[dict] = None,
          timeout: float = 10.0) -> Tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        **({"Content-Type": "application/json"} if data is not None else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class ApiError(RuntimeError):
    pass


class TrafficManagerAPI:
    def __init__(self, base: str = "http://127.0.0.1:9000", token: Optional[str] = None) -> None:
        self.base = base.rstrip("/")
        self.token = token or load_token()

    def call(self, method: str, path: str, body: Optional[dict] = None, ok=(200, 201)) -> dict:
        status, text = _http(method, self.base + path, body, {
            "Authorization": f"Bearer {self.token}", "X-Actor": "load-test-suite"})
        if status not in ok:
            raise ApiError(f"{method} {path} -> HTTP {status}: {text[:400]}")
        return json.loads(text) if text else {}

    # convenience
    def config(self) -> dict:
        return self.call("GET", "/api/v1/traffic/config")

    def policy(self) -> Tuple[int, int]:
        p = self.config()["policy"]
        return p["opdk_weight"], p["hybrid_weight"]

    def set_policy(self, opdk: int, reason: str = "load-test") -> dict:
        return self.call("PUT", "/api/v1/traffic/config",
                         {"opdk_weight": opdk, "hybrid_weight": 100 - opdk, "reason": reason})

    def instances(self) -> Dict[str, dict]:
        return {i["name"]: i for i in self.call("GET", "/api/v1/health/instances")["instances"]}

    def gateways(self) -> Dict[str, dict]:
        return {g["platform"]["platform_type"]: g for g in self.call("GET", "/api/v1/health/gateways")["gateways"]}

    def active_migration(self) -> Optional[dict]:
        return self.call("GET", "/api/v1/migrations/active") or None     # JSON null when none

    def migration(self, mid: int) -> dict:
        return self.call("GET", f"/api/v1/migrations/{mid}")

    def cancel_active(self, reason: str = "load-test cleanup") -> None:
        m = self.active_migration()
        if m:
            self.call("POST", f"/api/v1/migrations/{m['id']}/cancel", {"reason": reason})

    def metrics(self) -> Dict[Tuple[str, frozenset], float]:
        status, text = _http("GET", self.base + "/metrics", headers={"Authorization": f"Bearer {self.token}"})
        if status != 200:
            raise ApiError(f"GET /metrics -> HTTP {status}")
        return parse_prometheus(text)


# ------------------------------------------------------------ Prometheus
_SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})? (\S+)$')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_prometheus(text: str) -> Dict[Tuple[str, frozenset], float]:
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE.match(line)
        if m:
            out[(m.group(1), frozenset(_LABEL.findall(m.group(2) or "")))] = float(m.group(3).replace("+Inf", "inf"))
    return out


def metric_sum(samples: dict, name: str, **labels) -> float:
    want = set(labels.items())
    return sum(v for (n, lbl), v in samples.items() if n == name and want <= lbl)


# ----------------------------------------------------------------- faults
def _docker(*args: str, timeout: float = 60) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True, timeout=timeout).stdout


class Faults:
    """Injects faults and remembers how to undo every one of them (restore_all)."""

    def __init__(self) -> None:
        self._stopped: set = set()
        self._faulted: set = set()
        self._unready: set = set()

    @staticmethod
    def _sim(instance: str, path: str, body: dict) -> dict:
        port = INSTANCES[instance][1]
        status, text = _http("POST", f"http://127.0.0.1:{port}{path}", body, timeout=5)
        if status != 200:
            raise RuntimeError(f"{instance} {path} -> HTTP {status}: {text[:200]}")
        return json.loads(text)

    def set_faults(self, instances, **faults) -> None:
        body = {**NO_FAULTS, **faults}
        for inst in instances:
            self._sim(inst, "/__sim/faults", body)
            self._faulted.add(inst)

    def clear_faults(self, instances=None) -> None:
        for inst in list(instances or self._faulted):
            self._sim(inst, "/__sim/faults", NO_FAULTS)
            self._faulted.discard(inst)

    def set_ready(self, instances, ready: bool) -> None:
        for inst in instances:
            self._sim(inst, "/__sim/health", {"ready": ready})
            (self._unready.discard if ready else self._unready.add)(inst)

    def crash(self, instances) -> None:
        for inst in instances:
            _docker("kill", INSTANCES[inst][0])
            self._stopped.add(inst)

    def start(self, instances) -> None:
        for inst in instances:
            _docker("start", INSTANCES[inst][0])
            self._stopped.discard(inst)
        for inst in instances:                      # wait until the simulator answers again
            wait_until(lambda i=inst: _sim_up(i), 30, f"{inst} simulator up")

    @staticmethod
    def restart_haproxy() -> None:
        _docker("restart", "haproxy")

    def restore_all(self) -> list:
        """Best effort; returns a list of problems (empty if everything was restored)."""
        problems = []
        if self._stopped:
            try:
                self.start(sorted(self._stopped))
            except Exception as exc:                # noqa: BLE001
                problems.append(f"restart containers: {exc}")
        for inst in INSTANCES:
            try:
                self._sim(inst, "/__sim/faults", NO_FAULTS)
                self._sim(inst, "/__sim/health", {"ready": True})
            except Exception as exc:                # noqa: BLE001
                problems.append(f"reset {inst}: {exc}")
        self._faulted.clear()
        self._unready.clear()
        return problems


def _sim_up(instance: str) -> bool:
    try:
        status, _ = _http("GET", f"http://127.0.0.1:{INSTANCES[instance][1]}/__sim/faults", timeout=2)
        return status == 200
    except OSError:
        return False


def wait_until(cond: Callable[[], object], timeout: float, what: str, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    last_exc = None
    while time.monotonic() < deadline:
        try:
            v = cond()
            if v:
                return v
        except Exception as exc:                    # noqa: BLE001
            last_exc = exc
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout}s waiting for {what}" + (f" (last error: {last_exc})" if last_exc else ""))
