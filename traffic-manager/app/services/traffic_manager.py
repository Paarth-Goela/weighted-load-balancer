"""
Traffic Manager: pool-level traffic policy (OPDK vs Hybrid).

Update procedure (under the shared control lock):
  1. Validate (integers 0-100, sum = 100; done by the schema and re-checked here).
  2. Eligibility: every pool given weight > 0 must be enabled and have at
     least one routable instance (enabled + HEALTHY + READY/UP in HAProxy).
  3. Read the current runtime weights (preflight).
  4. Apply them via the Milestone 2 procedure: increases first, read back,
     roll back on failure.
  5. Persist: deactivate the old version, insert the new active version and
     mirror weights onto the pools, in one SQLite transaction with the audit
     record. If persistence fails, restore the previous runtime weights.

HAProxy's Runtime API has no multi-server transaction. Atomicity is
emulated with ordering, verification and compensation.

Every applied policy is a new version. Rollback re-applies an earlier
version as a new version, so history is never rewritten.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from app.database.connection import Database, utcnow
from app.errors import (
    Conflict,
    HAProxyUnavailable,
    HAProxyUpdateFailed,
    InconsistentState,
    InvalidRequest,
    NotFound,
    PersistenceFailed,
    PoolNotEligible,
)
from app.models.common import POLICY_BACKEND, POLICY_SERVER, POOL_BACKEND, PlatformType
from app.services import audit_service as A
from app.services.audit_service import AuditService
from app.services.control import ControlLock
from app.services.gateway_manager import GatewayManager
from app.services.haproxy_client import (
    HAProxyClient,
    RuntimeAPIConnectionError,
    RuntimeAPIError,
    WeightUpdateError,
)

log = logging.getLogger(__name__)

OPDK, HYBRID = PlatformType.APIGEE_OPDK, PlatformType.APIGEE_HYBRID


def _policy(row) -> Optional[dict]:
    if row is None:
        return None
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    return d


class TrafficManager:
    def __init__(self, db: Database, client: HAProxyClient, audit: AuditService,
                 lock: ControlLock, gateways: GatewayManager) -> None:
        self.db = db
        self.client = client
        self.audit = audit
        self.lock = lock
        self.gateways = gateways
        self.needs_runtime_sync = False   # set when a failed compensation left runtime unverified

    # ----------------------------------------------------------------- reads
    def active_policy(self) -> Optional[dict]:
        return _policy(self.db.query_one("SELECT * FROM traffic_policies WHERE enabled = 1"))

    def get_policy(self, version: int) -> Optional[dict]:
        return _policy(self.db.query_one("SELECT * FROM traffic_policies WHERE id = ?", (version,)))

    def get_config(self) -> dict:
        policy = self.active_policy()
        runtime_weights, error = None, None
        snapshot = self.gateways.runtime_snapshot()
        if snapshot is None:
            error = "HAProxy Runtime API is unreachable"
        else:
            sel = snapshot["servers"][POLICY_BACKEND]
            try:
                runtime_weights = {"opdk_weight": sel[POLICY_SERVER[OPDK]].weight,
                                   "hybrid_weight": sel[POLICY_SERVER[HYBRID]].weight}
            except KeyError:
                error = "HAProxy policy backend is missing its pool servers"

        pools, up_weight = [], {}
        for ptype in (OPDK, HYBRID):
            pool = self.gateways.pool_by_type(ptype)
            configured = (policy or {}).get("opdk_weight" if ptype is OPDK else "hybrid_weight", 0)
            pool_up = None
            if snapshot is not None:
                srv = snapshot["servers"][POLICY_BACKEND].get(POLICY_SERVER[ptype])
                pool_up = bool(srv and srv.op_state == 2 and srv.admin == "READY")
                rw = srv.weight if srv else 0
                up_weight[ptype] = rw if pool_up else 0
            pools.append({
                "platform_type": ptype.value,
                "pool_id": pool["id"] if pool else None,
                "pool_name": pool["name"] if pool else None,
                "haproxy_backend": POOL_BACKEND[ptype],
                "configured_percent": configured,
                "routable_instances": self.gateways.routable_count(pool, snapshot) if pool else 0,
                "pool_up": pool_up,
            })
        total_up = sum(up_weight.values())
        for p in pools:
            if snapshot is not None:
                w = up_weight[PlatformType(p["platform_type"])]
                p["effective_percent"] = round(100.0 * w / total_up, 2) if total_up else 0.0

        in_sync = None
        if policy and runtime_weights:
            in_sync = (runtime_weights["opdk_weight"], runtime_weights["hybrid_weight"]) == (
                policy["opdk_weight"], policy["hybrid_weight"])
        return {"policy": policy, "runtime": runtime_weights, "in_sync": in_sync,
                "haproxy_error": error, "pools": pools}

    def history(self, limit: int, offset: int) -> tuple[int, list[dict]]:
        return self.audit.list(event_types=A.TRAFFIC_EVENTS, limit=limit, offset=offset)

    def versions(self) -> list[dict]:
        return [_policy(r) for r in self.db.query("SELECT * FROM traffic_policies ORDER BY id")]

    # ---------------------------------------------------------------- update
    def _check_eligibility(self, weights: dict[PlatformType, int]) -> None:
        snapshot = self.gateways.runtime_snapshot()
        if snapshot is None:
            raise HAProxyUnavailable("HAProxy Runtime API is unreachable; no changes were applied")
        problems = []
        for ptype, w in weights.items():
            if w <= 0:
                continue
            pool = self.gateways.pool_by_type(ptype)
            if pool is None:
                problems.append(f"no {ptype.value} pool is registered")
            elif not pool["enabled"] or not pool["platform_enabled"]:
                problems.append(f"pool '{pool['name']}' or its platform is disabled")
            elif self.gateways.routable_count(pool, snapshot) == 0:
                problems.append(f"pool '{pool['name']}' has no healthy, enabled instance ready in HAProxy")
        if problems:
            raise PoolNotEligible("; ".join(problems) + "; no changes were applied")

    def apply(self, opdk: int, hybrid: int, *, updated_by: str, name: Optional[str] = None,
              reason: Optional[str] = None, event: str = A.TRAFFIC_POLICY_CHANGE,
              check_eligibility: bool = True) -> dict:
        if not (isinstance(opdk, int) and isinstance(hybrid, int)) or opdk < 0 or hybrid < 0 or opdk + hybrid != 100:
            raise InvalidRequest("weights must be non-negative integers summing to 100")
        requested = {"opdk_weight": opdk, "hybrid_weight": hybrid}
        name = name or f"opdk-{opdk}-hybrid-{hybrid}"

        def fail(status: str, error: str, previous=None) -> None:
            self.audit.record(event, status, updated_by, reason=reason, entity_type="traffic_policy",
                              entity_name=name, previous=previous, new=requested, error=error)

        with self.lock.hold():
            prev_policy = self.active_policy()
            try:
                if check_eligibility:
                    self._check_eligibility({OPDK: opdk, HYBRID: hybrid})
            except PoolNotEligible as exc:
                fail("REJECTED", exc.detail)
                raise
            except HAProxyUnavailable as exc:
                fail("FAILED", exc.detail)
                raise

            # Steps 3-4: Milestone 2 transaction (preflight, ordered apply, verify, rollback)
            try:
                result = self.client.set_policy_weights(opdk, hybrid)
            except RuntimeAPIConnectionError as exc:
                fail("FAILED", "HAProxy Runtime API unreachable; no changes applied")
                raise HAProxyUnavailable("HAProxy Runtime API is unreachable; no changes were applied") from exc
            except WeightUpdateError as exc:
                prev = {"opdk_weight": exc.previous.get("opdk"), "hybrid_weight": exc.previous.get("hybrid")}
                fail("FAILED", f"{exc.cause} (rolled_back={exc.rolled_back})", prev)
                if exc.rolled_back:
                    raise HAProxyUpdateFailed(
                        f"HAProxy update failed ({exc.cause}); previous weights "
                        f"{prev['opdk_weight']}:{prev['hybrid_weight']} restored"
                    ) from exc
                self.needs_runtime_sync = True
                raise InconsistentState(
                    "HAProxy update failed and rollback failed; the control loop will re-apply the active "
                    "policy when HAProxy is reachable"
                ) from exc
            except RuntimeAPIError as exc:
                fail("FAILED", str(exc))
                raise HAProxyUpdateFailed(f"{exc}; no changes were applied") from exc

            prev_runtime = {"opdk_weight": result.previous["opdk"], "hybrid_weight": result.previous["hybrid"]}
            now = utcnow()
            # Step 5: persist + audit atomically
            try:
                with self.db.transaction() as conn:
                    conn.execute("UPDATE traffic_policies SET enabled = 0 WHERE enabled = 1")
                    cur = conn.execute(
                        "INSERT INTO traffic_policies (name, opdk_weight, hybrid_weight, enabled, reason,"
                        " updated_by, updated_at, previous_policy_id) VALUES (?,?,?,1,?,?,?,?)",
                        (name, opdk, hybrid, reason, updated_by, now, prev_policy["id"] if prev_policy else None),
                    )
                    version = cur.lastrowid
                    for ptype, w in ((OPDK, opdk), (HYBRID, hybrid)):
                        conn.execute(
                            "UPDATE gateway_pools SET traffic_weight = ?, updated_at = ? WHERE platform_id IN"
                            " (SELECT id FROM gateway_platforms WHERE platform_type = ?)",
                            (w, now, ptype.value),
                        )
                    self.audit.record(
                        event, "SUCCESS", updated_by, reason=reason, entity_type="traffic_policy",
                        entity_id=version, entity_name=name,
                        previous={**prev_runtime, "version": prev_policy["id"] if prev_policy else None},
                        new={**requested, "version": version, "verified": True},
                    )
            except sqlite3.Error as exc:
                try:
                    self.client.set_policy_weights(prev_runtime["opdk_weight"], prev_runtime["hybrid_weight"])
                    rolled = True
                except RuntimeAPIError:
                    rolled, self.needs_runtime_sync = False, True
                fail("FAILED", f"could not persist policy: {exc}; runtime rolled_back={rolled}", prev_runtime)
                raise PersistenceFailed(
                    "policy could not be saved; runtime change " + ("rolled back" if rolled else "NOT rolled back")
                ) from exc

        log.info("traffic policy v%s applied: OPDK %s%% / Hybrid %s%%", version, opdk, hybrid)
        return {"previous": prev_runtime, "version": version, **self.get_config()}

    def rollback(self, to_version: Optional[int], *, updated_by: str, reason: Optional[str] = None) -> dict:
        active = self.active_policy()
        if active is None:
            raise Conflict("no active traffic policy to roll back from")
        target_id = to_version or active["previous_policy_id"]
        if target_id is None:
            raise Conflict("no previous traffic policy exists")
        target = self.get_policy(target_id)
        if target is None:
            raise NotFound(f"traffic policy version {target_id} not found")
        if (target["opdk_weight"], target["hybrid_weight"]) == (active["opdk_weight"], active["hybrid_weight"]) \
                and target_id == active["id"]:
            raise Conflict(f"version {target_id} is already active")
        return self.apply(
            target["opdk_weight"], target["hybrid_weight"], updated_by=updated_by,
            name=f"rollback-to-v{target_id}", reason=reason or f"rollback to version {target_id}",
            event=A.TRAFFIC_ROLLBACK,
        )

    # ----------------------------------------------------- bootstrap / sync
    def seed_policy(self, opdk: int, hybrid: int, source: str) -> Optional[int]:
        """Create the initial active policy in SQLite only (the reconciler applies it)."""
        if self.active_policy() is not None:
            return None
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO traffic_policies (name, opdk_weight, hybrid_weight, enabled, reason, updated_by,"
                " updated_at) VALUES (?,?,?,1,?,?,?)",
                ("initial", opdk, hybrid, "bootstrap", source, utcnow()),
            )
            for ptype, w in ((OPDK, opdk), (HYBRID, hybrid)):
                conn.execute("UPDATE gateway_pools SET traffic_weight = ? WHERE platform_id IN"
                             " (SELECT id FROM gateway_platforms WHERE platform_type = ?)", (w, ptype.value))
            return cur.lastrowid

    def restore_runtime(self, source: str) -> str:
        """Re-apply the active policy to HAProxy (startup / after HAProxy restart).
        Caller holds the control lock. No eligibility check: this restores the
        last approved state."""
        policy = self.active_policy()
        if policy is None:
            return "no-policy"
        desired = (policy["opdk_weight"], policy["hybrid_weight"])
        current = self.client.policy_weights()
        if current == desired:
            self.needs_runtime_sync = False
            return "in-sync"
        prev = {"opdk_weight": current[0], "hybrid_weight": current[1]}
        new = {"opdk_weight": desired[0], "hybrid_weight": desired[1], "version": policy["id"]}
        try:
            self.client.set_policy_weights(*desired)
        except RuntimeAPIError as exc:
            self.audit.record(A.TRAFFIC_POLICY_RESTORE, "FAILED", source, entity_type="traffic_policy",
                              entity_id=policy["id"], entity_name=policy["name"], previous=prev, new=new,
                              error=str(exc))
            raise
        self.audit.record(A.TRAFFIC_POLICY_RESTORE, "SUCCESS", source, entity_type="traffic_policy",
                          entity_id=policy["id"], entity_name=policy["name"], previous=prev, new=new,
                          reason="re-applied saved policy to HAProxy runtime")
        self.needs_runtime_sync = False
        return "restored"
