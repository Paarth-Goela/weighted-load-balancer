"""
Health Monitoring Engine.

Each cycle:
  1. Resolve every instance host and probe its readiness endpoint, in
     parallel and outside the control lock. Probes use per-instance HTTP or
     HTTPS, path, expected status codes, Host header, TLS verification and SNI.
  2. Under the lock, update the consecutive success/failure counters and
     apply the state machine:
        HEALTHY/UNKNOWN --(failure_threshold consecutive failures)--> UNHEALTHY
        UNKNOWN/UNHEALTHY --(recovery_threshold consecutive successes,
                              and at least recovery_hold_seconds after the
                              last transition)--> HEALTHY
     Every transition is timestamped and audited.
  3. The reconciler applies the result to HAProxy: UNHEALTHY -> MAINT
     (removed from routing), HEALTHY -> READY (rejoins).

A readiness probe is only as meaningful as the endpoint it calls. A
successful TCP connection, or even an HTTP 200 from a generic health URL,
does NOT prove that Apigee proxies are deployed, that authentication
policies work, that certificates are valid for clients, or that backends
are reachable. Real OPDK/Hybrid deployments should point health_check_path
at an environment-specific readiness endpoint (e.g. a dedicated health
proxy) and set expected_status_codes accordingly.
"""

from __future__ import annotations

import http.client
import logging
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from app.database.connection import Database, utcnow
from app.services import audit_service as A
from app.services.audit_service import AuditService
from app.services.control import ControlLock, ResolutionError, Resolver
from app.services.gateway_manager import GatewayManager
from app.services.reconciler import Reconciler

log = logging.getLogger(__name__)
USER_AGENT = "traffic-manager-health-monitor/1.0"


@dataclass
class ProbeResult:
    ok: bool
    status_code: Optional[int] = None
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    address: Optional[str] = None


class _SNIHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a resolved IP but use a chosen TLS server name (SNI + cert check)."""

    def __init__(self, address: str, port: int, *, server_hostname: Optional[str], **kw) -> None:
        super().__init__(address, port, **kw)
        self._sni = server_hostname

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._sni)


class Prober:
    def __init__(self, timeout: float = 2.0, ca_file: Optional[str] = None,
                 resolver: Optional[Resolver] = None) -> None:
        self.timeout = timeout
        self.ca_file = ca_file
        self.resolver = resolver or Resolver()

    def _context(self, verify: bool) -> ssl.SSLContext:
        if verify:
            ctx = ssl.create_default_context()
            if self.ca_file:
                ctx.load_verify_locations(self.ca_file)
            return ctx
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def probe(self, inst: dict) -> ProbeResult:
        try:
            address = self.resolver.resolve(inst["host"])
        except ResolutionError as exc:
            return ProbeResult(False, error=str(exc))
        https = inst["protocol"] == "HTTPS"
        host = inst["host"]
        default_port = 443 if https else 80
        host_header = inst.get("host_header") or (host if inst["port"] == default_port else f"{host}:{inst['port']}")
        start = time.monotonic()
        conn: Optional[http.client.HTTPConnection] = None
        try:
            if https:
                sni = inst.get("tls_server_name") or host
                conn = _SNIHTTPSConnection(address, inst["port"], server_hostname=sni, timeout=self.timeout,
                                           context=self._context(bool(inst["tls_verify"])))
            else:
                conn = http.client.HTTPConnection(address, inst["port"], timeout=self.timeout)
            conn.request("GET", inst["health_check_path"],
                         headers={"Host": host_header, "User-Agent": USER_AGENT, "Connection": "close"})
            resp = conn.getresponse()
            resp.read(65536)
            latency = round((time.monotonic() - start) * 1000, 1)
            ok = resp.status in inst["expected_status_codes"]
            return ProbeResult(ok, resp.status, latency,
                               None if ok else f"unexpected HTTP status {resp.status}", address)
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            return ProbeResult(False, None, round((time.monotonic() - start) * 1000, 1),
                               f"{type(exc).__name__}: {exc}"[:300], address)
        finally:
            if conn is not None:
                conn.close()


class HealthMonitor:
    def __init__(self, db: Database, gateways: GatewayManager, reconciler: Reconciler, audit: AuditService,
                 lock: ControlLock, prober: Prober, failure_threshold: int = 3, recovery_threshold: int = 2,
                 recovery_hold_seconds: float = 0.0, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 max_workers: int = 8) -> None:
        self.db = db
        self.gateways = gateways
        self.reconciler = reconciler
        self.audit = audit
        self.lock = lock
        self.prober = prober
        self.failure_threshold = failure_threshold
        self.recovery_threshold = recovery_threshold
        self.recovery_hold_seconds = recovery_hold_seconds
        self.clock = clock
        self.max_workers = max_workers
        self.last_cycle: dict = {}
        # Cumulative counters for the Prometheus exporter.
        self.cycles_total = 0
        self.probes_total: dict[str, int] = {"success": 0, "failure": 0}
        self.transitions_total: dict[str, int] = {}          # to-state -> n
        self.last_cycle_seconds: Optional[float] = None
        self.last_cycle_end: Optional[float] = None           # unix time

    def run_cycle(self) -> dict:
        """One probe pass + state update + reconciliation. Safe to call anytime."""
        started = utcnow()
        t0 = time.monotonic()
        instances = self.gateways.all_instance_rows()
        results: dict[int, ProbeResult] = {}
        if instances:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(instances))) as pool:
                for inst, res in zip(instances, pool.map(self.prober.probe, instances)):
                    results[inst["id"]] = res
        transitions = []
        with self.lock.hold():
            for inst in instances:
                t = self._apply(inst, results[inst["id"]])
                if t:
                    transitions.append(t)
        reconcile = self.reconciler.reconcile()
        for res in results.values():
            self.probes_total["success" if res.ok else "failure"] += 1
        for t in transitions:
            self.transitions_total[t["to"]] = self.transitions_total.get(t["to"], 0) + 1
        self.cycles_total += 1
        self.last_cycle_seconds = time.monotonic() - t0
        self.last_cycle_end = time.time()
        self.last_cycle = {"started_at": started, "finished_at": utcnow(), "probed": len(instances),
                           "transitions": transitions, "reconcile": reconcile}
        return self.last_cycle

    def _apply(self, snapshot: dict, res: ProbeResult) -> Optional[dict]:
        cur = self.gateways.instance_row(snapshot["id"])
        if cur is None or cur["updated_at"] != snapshot["updated_at"]:
            return None  # deleted or reconfigured during the probe: discard stale result
        status = cur["current_health_status"]
        cf, cs = cur["consecutive_failures"], cur["consecutive_successes"]
        if res.ok:
            cs, cf = cs + 1, 0
        else:
            cf, cs = cf + 1, 0

        new = status
        now_dt = self.clock()
        if not res.ok and cf >= self.failure_threshold and status != "UNHEALTHY":
            new = "UNHEALTHY"
        elif res.ok and cs >= self.recovery_threshold and status != "HEALTHY":
            held = True
            if status == "UNHEALTHY" and self.recovery_hold_seconds > 0 and cur["last_status_change"]:
                since = (now_dt - datetime.fromisoformat(cur["last_status_change"])).total_seconds()
                held = since >= self.recovery_hold_seconds
            if held:
                new = "HEALTHY"
        if res.ok and new == "HEALTHY":
            self.reconciler.fresh_probe_required.discard(cur["id"])

        now = now_dt.isoformat(timespec="milliseconds")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE gateway_instances SET consecutive_failures = ?, consecutive_successes = ?,"
                " last_health_check = ?, last_health_error = ?, current_health_status = ?,"
                " last_status_change = CASE WHEN ? THEN ? ELSE last_status_change END WHERE id = ?",
                (cf, cs, now, res.error, new, int(new != status), now, cur["id"]),
            )
            if new == status:
                return None
            detail = {"status_code": res.status_code, "latency_ms": res.latency_ms, "address": res.address,
                      "consecutive_failures": cf, "consecutive_successes": cs,
                      "routing": "removed (MAINT)" if new == "UNHEALTHY" else "eligible (READY if enabled)"}
            self.audit.record(A.HEALTH_TRANSITION, "INFO", "health-monitor", entity_type="gateway_instance",
                              entity_id=cur["id"], entity_name=cur["name"],
                              previous={"health": status}, new={"health": new, **detail}, error=res.error,
                              reason=f"{status} -> {new}")
        log.log(logging.WARNING if new == "UNHEALTHY" else logging.INFO,
                "health transition %s: %s -> %s (%s)", cur["name"], status, new, res.error or "ok")
        return {"instance": cur["name"], "from": status, "to": new, "at": now, "error": res.error}
