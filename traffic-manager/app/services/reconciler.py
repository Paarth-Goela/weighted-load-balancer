"""
Reconciler: makes HAProxy's runtime state match SQLite (the source of truth).

Runs at startup and on every control-loop cycle:

* Instances: re-add any registered instance missing from HAProxy (in MAINT),
  fix address/port drift (IP changes, since HAProxy 3.0 dynamic servers
  can't use DNS), fix weight drift, and remove servers that are not
  registered.
* Admin state: READY only if enabled AND HEALTHY AND (if the server was just
  re-added) a fresh probe has succeeded since. Otherwise MAINT. This stops
  unhealthy or disabled instances from being re-enabled by accident.
* Policy: re-apply the active traffic policy when the service starts, when
  HAProxy has restarted (Start_time_sec changed), or after a failed
  compensation. Out-of-band weight changes made while HAProxy keeps running
  are reported as drift (in_sync=false) but not reverted.
* Pool hold after an HAProxy restart: both pool links in `traffic_policy`
  are held in MAINT until every pool with weight > 0 is ready (or cannot
  become ready, or a timeout expires). Both are then released back-to-back.
  Without this, the pools' independent 1s readiness checks pass at
  different moments, and whichever pool came back first would briefly
  receive 100% of traffic, e.g. a 10% Hybrid canary taking everything.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from app.models.common import POLICY_BACKEND, POLICY_SERVER, POOL_BACKEND, PlatformType
from app.services import audit_service as A
from app.services.audit_service import AuditService
from app.services.control import ControlLock, ResolutionError, Resolver
from app.services.gateway_manager import GatewayManager
from app.services.haproxy_client import HAProxyClient, RuntimeAPIConnectionError, RuntimeAPIError
from app.services.traffic_manager import TrafficManager

log = logging.getLogger(__name__)
SOURCE = "reconciler"


class Reconciler:
    def __init__(self, client: HAProxyClient, gateways: GatewayManager, traffic: TrafficManager,
                 audit: AuditService, lock: ControlLock, resolver: Resolver,
                 settle_seconds: float = 1.5, release_timeout: float = 20.0,
                 clock=time.monotonic) -> None:
        self.client = client
        self.gateways = gateways
        self.traffic = traffic
        self.audit = audit
        self.lock = lock
        self.resolver = resolver
        self.known_start_time: Optional[int] = None
        self.fresh_probe_required: set[int] = set()
        self.last_result: dict = {}
        self.settle_seconds = settle_seconds
        self.release_timeout = release_timeout
        self.clock = clock
        self.pool_hold_since: Optional[float] = None
        self._ready_since: Optional[float] = None
        # Cumulative counters for the Prometheus exporter.
        self.runs_total = 0
        self.restarts_detected_total = 0
        self.unreachable_total = 0
        self.errors_total = 0
        self.actions_total = 0

    def reconcile(self) -> dict:
        summary: dict = {"haproxy": "reachable", "actions": [], "errors": []}
        try:
            with self.lock.hold():
                start = self.client.start_time()
                first = self.known_start_time is None
                restarted = not first and start != self.known_start_time
                summary["haproxy_restarted"] = restarted
                fresh = first and self._instances_missing()   # e.g. host reboot: both started together
                self._hold_pools_if_needed(summary, restarted or fresh)
                self._sync_instances(summary, restarted, first)
                if first or restarted or self.traffic.needs_runtime_sync:
                    src = "startup-reconcile" if first else ("haproxy-restart-recovery" if restarted else SOURCE)
                    summary["policy"] = self.traffic.restore_runtime(src)
                self._release_pools_if_ready(summary)
                self.known_start_time = start
        except RuntimeAPIConnectionError:
            summary["haproxy"] = "unreachable"
        except RuntimeAPIError as exc:
            summary["errors"].append(str(exc))
        self.runs_total += 1
        self.restarts_detected_total += int(bool(summary.get("haproxy_restarted")))
        self.unreachable_total += int(summary["haproxy"] == "unreachable")
        self.errors_total += len(summary["errors"])
        self.actions_total += len(summary["actions"])
        self.last_result = summary
        return summary

    def _audit(self, inst_or_name, message: str, status: str = "SUCCESS", **kw) -> None:
        if isinstance(inst_or_name, dict):
            kw.update(entity_id=inst_or_name["id"], entity_name=inst_or_name["name"])
        else:
            kw.update(entity_name=inst_or_name)
        self.audit.record(A.INSTANCE_RUNTIME_SYNC, status, SOURCE, entity_type="gateway_instance",
                          reason=message, **kw)

    def _sync_instances(self, summary: dict, restarted: bool, first: bool = False) -> None:
        instances = self.gateways.all_instance_rows()
        registered = {(self.gateways.backend_of(i), i["name"]) for i in instances}
        runtime = {b: self.client.servers(b) for b in POOL_BACKEND.values()}

        for inst in instances:
            backend = self.gateways.backend_of(inst)
            srv = runtime[backend].get(inst["name"])
            try:
                address = self.resolver.resolve(inst["host"])
            except ResolutionError as exc:
                address = None
                summary["errors"].append(f"{inst['name']}: {exc}")
            try:
                if srv is None:
                    if address is None:
                        continue
                    srv = self.client.add_server(backend, self.gateways._spec(inst, address))
                    self.fresh_probe_required.add(inst["id"])
                    why = "HAProxy restarted" if restarted else "server missing from HAProxy"
                    self._audit(inst, f"re-added to {backend} in MAINT ({why}); awaiting fresh health check",
                                new={"address": address, "port": inst["port"]})
                    summary["actions"].append(f"re-added {inst['name']}")
                else:
                    if address and (srv.address, srv.port) != (address, inst["port"]):
                        old = {"address": srv.address, "port": srv.port}
                        srv = self.client.set_addr(backend, inst["name"], address, inst["port"])
                        self._audit(inst, "address changed (DNS re-resolution)", previous=old,
                                    new={"address": address, "port": inst["port"]})
                        summary["actions"].append(f"readdressed {inst['name']}")
                    if srv.weight != inst["lb_weight"]:
                        srv = self.client.set_weight(backend, inst["name"], inst["lb_weight"])
                        summary["actions"].append(f"reweighted {inst['name']}")

                if first and srv.admin == "MAINT":
                    # Service just started and finds the server out of routing (e.g. it
                    # restarted mid-recovery): never re-enable without a fresh probe.
                    self.fresh_probe_required.add(inst["id"])
                desired = "READY" if (
                    inst["enabled"] and inst["current_health_status"] == "HEALTHY"
                    and inst["id"] not in self.fresh_probe_required
                ) else "MAINT"
                if srv.admin != desired:
                    self.client.set_state(backend, inst["name"], desired.lower())
                    summary["actions"].append(f"{inst['name']} -> {desired}")
                    why = ("routable: enabled and HEALTHY" if desired == "READY" else
                           "not routable: " + ("disabled" if not inst["enabled"] else
                                               f"health {inst['current_health_status']}"
                                               if inst["current_health_status"] != "HEALTHY"
                                               else "awaiting fresh health check"))
                    self._audit(inst, f"routing {srv.admin} -> {desired} ({why})",
                                previous={"admin_state": srv.admin}, new={"admin_state": desired})
            except RuntimeAPIConnectionError:
                raise
            except RuntimeAPIError as exc:
                summary["errors"].append(f"{inst['name']}: {exc}")
                self._audit(inst, "runtime sync failed", status="FAILED", error=str(exc))

        # Servers present in HAProxy but not registered must not receive traffic.
        for backend, servers in runtime.items():
            for name in servers:
                if (backend, name) not in registered:
                    try:
                        self.client.delete_server(backend, name, drain_timeout=0)
                        self._audit(name, f"removed unregistered server from {backend}")
                        summary["actions"].append(f"removed orphan {name}")
                    except RuntimeAPIError as exc:
                        summary["errors"].append(f"orphan {name}: {exc}")

        live_ids = {i["id"] for i in instances}
        self.fresh_probe_required &= live_ids

    # ------------------------------------------------------- pool hold/release
    def _instances_missing(self) -> bool:
        runtime = {b: self.client.servers(b) for b in POOL_BACKEND.values()}
        return any(i["name"] not in runtime[self.gateways.backend_of(i)] for i in self.gateways.all_instance_rows())

    def _hold_pools_if_needed(self, summary: dict, restarted: bool) -> None:
        selectors = self.client.servers(POLICY_BACKEND)
        held_in_haproxy = bool(selectors) and all(s.admin == "MAINT" for s in selectors.values())
        if self.pool_hold_since is None and (restarted or held_in_haproxy):
            # restarted HAProxy, or this service restarted in the middle of a hold
            self.pool_hold_since = self.clock()
            self._ready_since = None
            self.audit.record(A.POOL_ROUTING_HOLD, "INFO", SOURCE, entity_type="traffic_policy",
                              reason="HAProxy (re)started without its instances: pool links held in MAINT "
                                     "until pools are ready")
        if self.pool_hold_since is not None:
            for name, srv in selectors.items():
                if srv.admin != "MAINT":
                    self.client.set_state(POLICY_BACKEND, name, "maint")
            summary["pool_hold"] = "holding"

    def _pool_settled(self, ptype: PlatformType, servers: dict) -> bool:
        """Pool is ready (>=1 routable server), or nothing in it can become ready soon."""
        pool = self.gateways.pool_by_type(ptype)
        if pool is None:
            return True
        insts = self.gateways.instances_in_pool(pool["id"])
        if any(self.gateways.is_routable(i, servers.get(i["name"])) for i in insts):
            return True
        could_recover = [i for i in insts if i["enabled"] and i["current_health_status"] != "UNHEALTHY"]
        return not could_recover

    def _release_pools_if_ready(self, summary: dict) -> None:
        if self.pool_hold_since is None:
            return
        policy = self.traffic.active_policy()
        weights = ({PlatformType.APIGEE_OPDK: policy["opdk_weight"], PlatformType.APIGEE_HYBRID: policy["hybrid_weight"]}
                   if policy else {})
        settled = all(
            self._pool_settled(ptype, self.client.servers(POOL_BACKEND[ptype]))
            for ptype, w in weights.items() if w > 0
        )
        now = self.clock()
        if settled and self._ready_since is None:
            self._ready_since = now
        elif not settled:
            self._ready_since = None
        timed_out = now - self.pool_hold_since >= self.release_timeout
        # Wait > one selector check interval after the pools are ready, so both
        # selector checks already pass when released (they keep running in MAINT).
        if not timed_out and not (settled and now - self._ready_since >= self.settle_seconds):
            return
        for ptype in (PlatformType.APIGEE_OPDK, PlatformType.APIGEE_HYBRID):
            self.client.set_state(POLICY_BACKEND, POLICY_SERVER[ptype], "ready")
        held = round(now - self.pool_hold_since, 1)
        self.pool_hold_since = self._ready_since = None
        summary["pool_hold"] = "released"
        self.audit.record(A.POOL_ROUTING_HOLD, "INFO", SOURCE, entity_type="traffic_policy",
                          reason=f"pool links released together after {held}s"
                                 + (" (timeout)" if timed_out and not settled else ""))
