"""
Gateway Manager: platforms, pools and instances (SQLite and HAProxy).

Instance lifecycle in HAProxy:
  register   -> `add server` in MAINT, health checks enabled. It gets traffic
                only after the health monitor marks it HEALTHY (rise threshold).
  update     -> weight / enable / disable at runtime. A connection change
                (host, port, protocol, TLS) replaces the server and it must
                pass health checks again.
  deregister -> drain, wait for sessions, MAINT, delete, verify absence.
                No traffic after this call returns.

Safety: an operation that would leave a pool with traffic weight > 0 without
any routable instance is refused (409) unless force=true.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

from app.database.connection import Database, utcnow
from app.errors import Conflict, HAProxyUnavailable, HAProxyUpdateFailed, InvalidRequest, NotFound
from app.models.common import POLICY_BACKEND, POLICY_SERVER, POOL_BACKEND, HealthStatus, PlatformType, is_ipv4
from app.models.gateway import GatewayPlatformCreate
from app.models.instance import GatewayInstanceCreate, GatewayInstanceUpdate
from app.models.pool import GatewayPoolCreate
from app.services import audit_service as A
from app.services.audit_service import AuditService
from app.services.control import ControlLock, ResolutionError, Resolver
from app.services.haproxy_client import (
    HAProxyClient,
    RuntimeAPIConnectionError,
    RuntimeAPIError,
    RuntimeServer,
    ServerSpec,
)

CONNECTION_FIELDS = ("host", "port", "protocol", "tls_verify", "tls_server_name")
PROBE_FIELDS = ("health_check_path", "expected_status_codes", "host_header")


@contextmanager
def runtime_errors(action: str) -> Iterator[None]:
    """Map Runtime API exceptions to HTTP-level service errors."""
    try:
        yield
    except RuntimeAPIConnectionError as exc:
        raise HAProxyUnavailable(f"HAProxy Runtime API is unreachable; {action} not applied") from exc
    except RuntimeAPIError as exc:
        raise HAProxyUpdateFailed(f"{action} failed: {exc}") from exc


def instance_row(row: sqlite3.Row | dict) -> dict[str, Any]:
    d = dict(row)
    d["expected_status_codes"] = json.loads(d["expected_status_codes"])
    for k in ("tls_verify", "enabled"):
        d[k] = bool(d[k])
    return d


class GatewayManager:
    def __init__(
        self,
        db: Database,
        client: HAProxyClient,
        audit: AuditService,
        lock: ControlLock,
        resolver: Resolver,
        drain_timeout: float = 10.0,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        self.db = db
        self.client = client
        self.audit = audit
        self.lock = lock
        self.resolver = resolver
        self.drain_timeout = drain_timeout
        self.on_change = on_change or (lambda: None)

    # ============================================================ platforms
    def list_platforms(self) -> list[dict]:
        return [self._platform(r) for r in self.db.query("SELECT * FROM gateway_platforms ORDER BY id")]

    def get_platform(self, platform_id: int) -> dict:
        row = self.db.query_one("SELECT * FROM gateway_platforms WHERE id = ?", (platform_id,))
        if row is None:
            raise NotFound(f"gateway platform {platform_id} not found")
        return self._platform(row)

    @staticmethod
    def _platform(row) -> dict:
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        return d

    def create_platform(self, data: GatewayPlatformCreate, source: str) -> dict:
        now = utcnow()
        try:
            with self.db.transaction() as conn:
                cur = conn.execute(
                    "INSERT INTO gateway_platforms (name, platform_type, description, enabled, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (data.name, data.platform_type.value, data.description, int(data.enabled), now, now),
                )
                pid = cur.lastrowid
                self.audit.record(A.GATEWAY_REGISTERED, "SUCCESS", source, entity_type="gateway_platform",
                                  entity_id=pid, entity_name=data.name, new=data.model_dump(mode="json"))
        except sqlite3.IntegrityError as exc:
            raise Conflict(
                f"a platform named '{data.name}' or of type {data.platform_type.value} already exists "
                "(one platform per type)"
            ) from exc
        return self.get_platform(pid)

    # ================================================================ pools
    _POOL_SQL = (
        "SELECT p.*, g.platform_type, g.enabled AS platform_enabled FROM gateway_pools p"
        " JOIN gateway_platforms g ON g.id = p.platform_id"
    )

    def _pool_row(self, pool_id: int) -> Optional[dict]:
        row = self.db.query_one(f"{self._POOL_SQL} WHERE p.id = ?", (pool_id,))
        return dict(row) if row else None

    def pool_by_type(self, platform_type: PlatformType) -> Optional[dict]:
        row = self.db.query_one(f"{self._POOL_SQL} WHERE g.platform_type = ?", (platform_type.value,))
        return dict(row) if row else None

    def _pool_out(self, row: dict, health: Optional[dict] = None) -> dict:
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        d["haproxy_backend"] = POOL_BACKEND[PlatformType(d["platform_type"])]
        d["health"] = health
        return d

    def list_pools(self, with_health: bool = True) -> list[dict]:
        rows = [dict(r) for r in self.db.query(f"{self._POOL_SQL} ORDER BY p.id")]
        runtime = self.runtime_snapshot() if with_health else None
        return [self._pool_out(r, self.pool_health(r, runtime) if with_health else None) for r in rows]

    def get_pool(self, pool_id: int) -> dict:
        row = self._pool_row(pool_id)
        if row is None:
            raise NotFound(f"gateway pool {pool_id} not found")
        return self._pool_out(row, self.pool_health(row, self.runtime_snapshot()))

    def create_pool(self, data: GatewayPoolCreate, source: str) -> dict:
        platform = self.db.query_one("SELECT * FROM gateway_platforms WHERE id = ?", (data.platform_id,))
        if platform is None:
            raise InvalidRequest(f"platform_id {data.platform_id} does not exist")
        now = utcnow()
        active = self.db.query_one("SELECT * FROM traffic_policies WHERE enabled = 1")
        weight = 0
        if active:
            weight = active["opdk_weight"] if platform["platform_type"] == "APIGEE_OPDK" else active["hybrid_weight"]
        try:
            with self.db.transaction() as conn:
                cur = conn.execute(
                    "INSERT INTO gateway_pools (name, platform_id, description, traffic_weight, enabled,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (data.name, data.platform_id, data.description, weight, int(data.enabled), now, now),
                )
                pool_id = cur.lastrowid
                self.audit.record(A.POOL_CREATED, "SUCCESS", source, entity_type="gateway_pool", entity_id=pool_id,
                                  entity_name=data.name, new=data.model_dump(mode="json"))
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"pool name '{data.name}' exists or platform {data.platform_id} already has a pool") from exc
        return self.get_pool(pool_id)

    # ------------------------------------------------------------- runtime
    def runtime_snapshot(self) -> Optional[dict]:
        """{'servers': {backend: {name: RuntimeServer}}, 'stats': {...}} or None if unreachable."""
        try:
            servers = {b: self.client.servers(b) for b in (*POOL_BACKEND.values(), POLICY_BACKEND)}
            try:
                stats = self.client.stats()
            except RuntimeAPIError:
                stats = {}
            return {"servers": servers, "stats": stats}
        except RuntimeAPIError:
            return None

    def is_routable(self, inst: dict, srv: Optional[RuntimeServer]) -> bool:
        return (
            bool(inst["enabled"])
            and inst["current_health_status"] == HealthStatus.HEALTHY.value
            and srv is not None
            and srv.is_up
        )

    def routable_count(self, pool: dict, runtime: Optional[dict]) -> int:
        if runtime is None:
            return 0
        backend = POOL_BACKEND[PlatformType(pool["platform_type"])]
        servers = runtime["servers"].get(backend, {})
        return sum(
            1 for i in self.instances_in_pool(pool["id"]) if self.is_routable(i, servers.get(i["name"]))
        )

    def pool_health(self, pool: dict, runtime: Optional[dict]) -> dict:
        insts = self.instances_in_pool(pool["id"])
        enabled = [i for i in insts if i["enabled"]]
        count = lambda s: sum(1 for i in enabled if i["current_health_status"] == s)  # noqa: E731
        routable = self.routable_count(pool, runtime)
        pool_up = None
        if runtime is not None:
            sel = runtime["servers"][POLICY_BACKEND].get(POLICY_SERVER[PlatformType(pool["platform_type"])])
            pool_up = bool(sel and sel.op_state == 2)
        if not pool["enabled"] or not pool.get("platform_enabled", 1):
            status = "DISABLED"
        elif routable == 0:
            status = "DOWN"
        elif routable == len(enabled):
            status = "HEALTHY"
        else:
            status = "DEGRADED"
        return {
            "total_instances": len(insts),
            "enabled_instances": len(enabled),
            "healthy_instances": count("HEALTHY"),
            "unhealthy_instances": count("UNHEALTHY"),
            "unknown_instances": count("UNKNOWN"),
            "routable_instances": routable,
            "haproxy_pool_up": pool_up,
            "status": status,
        }

    # ============================================================ instances
    _INST_SQL = (
        "SELECT i.*, p.platform_id, p.traffic_weight AS pool_traffic_weight, g.platform_type"
        " FROM gateway_instances i JOIN gateway_pools p ON p.id = i.pool_id"
        " JOIN gateway_platforms g ON g.id = p.platform_id"
    )

    def all_instance_rows(self) -> list[dict]:
        return [instance_row(r) for r in self.db.query(f"{self._INST_SQL} ORDER BY i.id")]

    def instances_in_pool(self, pool_id: int) -> list[dict]:
        return [instance_row(r) for r in self.db.query(f"{self._INST_SQL} WHERE i.pool_id = ? ORDER BY i.id", (pool_id,))]

    def instance_row(self, instance_id: int) -> Optional[dict]:
        row = self.db.query_one(f"{self._INST_SQL} WHERE i.id = ?", (instance_id,))
        return instance_row(row) if row else None

    @staticmethod
    def backend_of(inst: dict) -> str:
        return POOL_BACKEND[PlatformType(inst["platform_type"])]

    def instance_out(self, inst: dict, runtime: Optional[dict]) -> dict:
        d = dict(inst)
        if runtime is not None:
            backend = self.backend_of(inst)
            srv = runtime["servers"].get(backend, {}).get(inst["name"])
            st = runtime["stats"].get((backend, inst["name"]), {})
            d["runtime"] = {
                "present": srv is not None,
                "address": srv.address if srv else None,
                "admin_state": srv.admin if srv else None,
                "operational_state": srv.operational if srv else None,
                "check_status": st.get("check_status") or None,
                "weight": srv.weight if srv else None,
                "current_sessions": int(st["scur"]) if st.get("scur", "").isdigit() else None,
                "total_sessions": int(st["stot"]) if st.get("stot", "").isdigit() else None,
                "receiving_traffic": bool(srv and srv.is_up and srv.weight > 0 and inst["pool_traffic_weight"] > 0),
            }
        return d

    def list_instances(self, pool_id: Optional[int] = None) -> list[dict]:
        rows = self.instances_in_pool(pool_id) if pool_id else self.all_instance_rows()
        runtime = self.runtime_snapshot()
        return [self.instance_out(r, runtime) for r in rows]

    def get_instance(self, instance_id: int) -> dict:
        inst = self.instance_row(instance_id)
        if inst is None:
            raise NotFound(f"gateway instance {instance_id} not found")
        return self.instance_out(inst, self.runtime_snapshot())

    def _spec(self, data: dict, address: str) -> ServerSpec:
        https = data["protocol"] == "HTTPS"
        sni = data.get("tls_server_name") or (None if not https or is_ipv4(data["host"]) else data["host"])
        return ServerSpec(name=data["name"], address=address, port=data["port"], weight=data["lb_weight"],
                          tls=https, tls_verify=bool(data["tls_verify"]), sni=sni)

    def resolve(self, host: str) -> str:
        try:
            return self.resolver.resolve(host)
        except ResolutionError as exc:
            raise InvalidRequest(str(exc)) from exc

    def register_instance(self, data: GatewayInstanceCreate, source: str, apply_runtime: bool = True) -> dict:
        audit_new = data.model_dump(mode="json", exclude={"reason"})
        with self.lock.hold():
            pool = self._pool_row(data.pool_id)
            if pool is None:
                raise InvalidRequest(f"pool_id {data.pool_id} does not exist")
            if self.db.query_one("SELECT id FROM gateway_instances WHERE name = ?", (data.name,)):
                raise Conflict(f"instance name '{data.name}' already registered")
            backend = POOL_BACKEND[PlatformType(pool["platform_type"])]
            now = utcnow()
            try:
                address = self.resolve(data.host) if apply_runtime else None
                with self.db.transaction() as conn:
                    cur = conn.execute(
                        "INSERT INTO gateway_instances (name, pool_id, host, port, protocol, health_check_path,"
                        " expected_status_codes, host_header, tls_verify, tls_server_name, lb_weight, enabled,"
                        " current_health_status, last_status_change, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'UNKNOWN', ?, ?, ?)",
                        (data.name, data.pool_id, data.host, data.port, data.protocol, data.health_check_path,
                         json.dumps(data.expected_status_codes), data.host_header, int(data.tls_verify),
                         data.tls_server_name, data.lb_weight, int(data.enabled), now, now, now),
                    )
                    iid = cur.lastrowid
                    if apply_runtime:
                        with runtime_errors(f"registering {data.name} in HAProxy"):
                            self.client.add_server(backend, self._spec(audit_new, address))
                    self.audit.record(A.INSTANCE_REGISTERED, "SUCCESS", source, reason=data.reason,
                                      entity_type="gateway_instance", entity_id=iid, entity_name=data.name,
                                      new={**audit_new, "haproxy_backend": backend, "address": address,
                                           "initial_state": "MAINT until health checks pass"})
            except (HAProxyUnavailable, HAProxyUpdateFailed, InvalidRequest) as exc:
                self.audit.record(A.INSTANCE_REGISTERED, "FAILED", source, reason=data.reason,
                                  entity_type="gateway_instance", entity_name=data.name, new=audit_new,
                                  error=exc.detail)
                raise
        self.on_change()
        return self.get_instance(iid)

    def _ensure_not_last_routable(self, inst: dict, force: bool, action: str) -> None:
        if force or inst["pool_traffic_weight"] <= 0:
            return
        runtime = self.runtime_snapshot()
        if runtime is None:
            raise HAProxyUnavailable("HAProxy Runtime API is unreachable; cannot verify safety")
        backend = self.backend_of(inst)
        servers = runtime["servers"].get(backend, {})
        if not self.is_routable(inst, servers.get(inst["name"])):
            return
        others = [i for i in self.instances_in_pool(inst["pool_id"])
                  if i["id"] != inst["id"] and self.is_routable(i, servers.get(i["name"]))]
        if not others:
            raise Conflict(
                f"{action} '{inst['name']}' would leave its pool (traffic weight "
                f"{inst['pool_traffic_weight']}%) with no routable instance. Shift traffic away first "
                "or retry with force=true"
            )

    def update_instance(self, instance_id: int, patch: GatewayInstanceUpdate, source: str, force: bool = False) -> dict:
        with self.lock.hold():
            inst = self.instance_row(instance_id)
            if inst is None:
                raise NotFound(f"gateway instance {instance_id} not found")
            changes = {k: v for k, v in patch.model_dump(exclude_unset=True, exclude={"reason"}).items()
                       if v != inst.get(k)}
            if not changes:
                return self.get_instance(instance_id)
            merged = {**inst, **changes}
            # Re-validate the merged configuration as a whole.
            try:
                GatewayInstanceCreate(**{k: merged[k] for k in GatewayInstanceCreate.model_fields if k != "reason"})
            except ValueError as exc:
                raise InvalidRequest(f"resulting configuration is invalid: {exc}") from exc

            backend = self.backend_of(inst)
            reconnect = any(k in changes for k in CONNECTION_FIELDS)
            disabling = changes.get("enabled") is False
            if reconnect or disabling:
                self._ensure_not_last_routable(inst, force, "updating" if reconnect else "disabling")

            reset_health = reconnect
            previous = {k: inst[k] for k in changes}
            try:
                with runtime_errors(f"updating {inst['name']} in HAProxy"):
                    if reconnect:
                        address = self.resolve(merged["host"])
                        self.client.delete_server(backend, inst["name"], self.drain_timeout)
                        try:
                            self.client.add_server(backend, self._spec(merged, address))
                        except RuntimeAPIError:
                            # Restore the old server (MAINT); the reconciler re-enables it if healthy.
                            try:
                                self.client.add_server(backend, self._spec(inst, self.resolver.resolve(inst["host"])))
                            except Exception:
                                pass
                            raise
                    else:
                        if "lb_weight" in changes:
                            self.client.set_weight(backend, inst["name"], merged["lb_weight"])
                        if disabling:
                            self.client.set_state(backend, inst["name"], "maint")
                        elif changes.get("enabled") is True and inst["current_health_status"] == "HEALTHY":
                            self.client.set_state(backend, inst["name"], "ready")
            except (HAProxyUnavailable, HAProxyUpdateFailed, InvalidRequest) as exc:
                self.audit.record(A.INSTANCE_UPDATED, "FAILED", source, reason=patch.reason,
                                  entity_type="gateway_instance", entity_id=instance_id, entity_name=inst["name"],
                                  previous=previous, new=changes, error=exc.detail)
                raise

            now = utcnow()
            cols = dict(changes)
            if "expected_status_codes" in cols:
                cols["expected_status_codes"] = json.dumps(cols["expected_status_codes"])
            for b in ("tls_verify", "enabled"):
                if b in cols:
                    cols[b] = int(cols[b])
            if reset_health:
                cols.update(current_health_status="UNKNOWN", consecutive_failures=0, consecutive_successes=0,
                            last_status_change=now)
            elif any(k in changes for k in PROBE_FIELDS):
                cols.update(consecutive_failures=0, consecutive_successes=0)
            cols["updated_at"] = now
            with self.db.transaction() as conn:
                conn.execute(
                    f"UPDATE gateway_instances SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                    (*cols.values(), instance_id),
                )
                self.audit.record(A.INSTANCE_UPDATED, "SUCCESS", source, reason=patch.reason,
                                  entity_type="gateway_instance", entity_id=instance_id, entity_name=inst["name"],
                                  previous=previous, new={**changes, **({"health_reset": True} if reset_health else {})})
        self.on_change()
        return self.get_instance(instance_id)

    def deregister_instance(self, instance_id: int, source: str, reason: Optional[str] = None,
                            force: bool = False) -> dict:
        with self.lock.hold():
            inst = self.instance_row(instance_id)
            if inst is None:
                raise NotFound(f"gateway instance {instance_id} not found")
            self._ensure_not_last_routable(inst, force, "deregistering")
            backend = self.backend_of(inst)
            try:
                with runtime_errors(f"removing {inst['name']} from HAProxy"):
                    removed = self.client.delete_server(backend, inst["name"], self.drain_timeout)
            except (HAProxyUnavailable, HAProxyUpdateFailed) as exc:
                self.audit.record(A.INSTANCE_DEREGISTERED, "FAILED", source, reason=reason,
                                  entity_type="gateway_instance", entity_id=instance_id, entity_name=inst["name"],
                                  error=exc.detail)
                raise
            snapshot = {k: inst[k] for k in ("name", "pool_id", "host", "port", "protocol", "current_health_status")}
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM gateway_instances WHERE id = ?", (instance_id,))
                self.audit.record(A.INSTANCE_DEREGISTERED, "SUCCESS", source, reason=reason,
                                  entity_type="gateway_instance", entity_id=instance_id, entity_name=inst["name"],
                                  previous=snapshot, new={"removed_from_haproxy": removed, "forced": force})
        return {"status": "success", "message": f"instance '{inst['name']}' drained and removed from HAProxy",
                "removed_from_haproxy": removed}
