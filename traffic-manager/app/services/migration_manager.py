"""
Migration Manager: gradual and scheduled OPDK <-> Hybrid traffic shifts.

Every traffic change goes through TrafficManager.apply() (validation,
eligibility, ordered HAProxy update, read-back, rollback, versioning, audit).
This module only decides *when* to apply *which* allocation, and whether it
is safe to continue. It never touches HAProxy directly.

Stage lifecycle (one migration = ordered stages, allocation stored as OPDK %):

  PENDING --pre-checks--> APPLIED (observation window) --evaluate--> PASSED
                                         |                              |
                                         +--> FAILED (violation)        +--> next stage / COMPLETED

Before each stage (pre-checks; failure -> PAUSED, nothing applied):
  * HAProxy runtime still equals the allocation the migration last applied
    (otherwise someone changed it out-of-band -> PAUSED, never overwritten)
  * destination pool has >= min_healthy_instances routable instances
During observation (every tick):
  * destination health is re-checked -> immediate violation if it drops
At the end of the observation window:
  * destination-pool requests >= min_requests, else PAUSED (insufficient evidence)
  * error rate <= max_error_rate_percent, p95 <= max_p95_ms, else violation
Violation:
  * auto_rollback -> re-apply the last known-good allocation -> ROLLED_BACK
    (FAILED if the rollback itself cannot be applied)
  * otherwise -> PAUSED with the reason recorded

A passing health check is NOT proof of API compatibility. That is why
stages are also gated on observed error rate and latency of real traffic.

Restart safety: observation windows live in memory. After a restart, RUNNING
and WAITING_APPROVAL migrations come back PAUSED. Resume re-checks the actual
HAProxy policy and destination health. Scheduled stages missed during
downtime are never executed late automatically.

Exactly one migration can be active: an in-process lock plus a UNIQUE
`active_slot` column in SQLite.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from app.database.connection import Database
from app.errors import Conflict, InvalidRequest, NotFound, PoolNotEligible, ServiceError
from app.models.common import POOL_BACKEND, PlatformType
from app.models.migration import (
    ACTIVE_STATUSES,
    EmergencyShift,
    GradualMigrationCreate,
    ScheduledMigrationCreate,
)
from app.services import audit_service as A
from app.services.audit_service import AuditService
from app.services.gateway_manager import GatewayManager
from app.services.metrics_collector import MetricsCollector
from app.services.traffic_manager import TrafficManager

log = logging.getLogger(__name__)
OPDK = PlatformType.APIGEE_OPDK


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds") if dt else None


def _dt(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


def dest_share(opdk: int, destination: str) -> int:
    return opdk if destination == OPDK.value else 100 - opdk


def opdk_for(dest_percent: int, destination: str) -> int:
    return dest_percent if destination == OPDK.value else 100 - dest_percent


class MigrationManager:
    def __init__(self, db: Database, traffic: TrafficManager, gateways: GatewayManager, audit: AuditService,
                 metrics: MetricsCollector, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 schedule_grace_seconds: float = 60.0) -> None:
        self.db = db
        self.traffic = traffic
        self.gateways = gateways
        self.audit = audit
        self.metrics = metrics
        self.clock = clock
        self.grace = schedule_grace_seconds
        self._lock = threading.RLock()
        self.last_tick_error: Optional[str] = None
        self.tick_errors_total = 0

    # ================================================================ reads
    def _row(self, mid: int) -> dict:
        row = self.db.query_one("SELECT * FROM traffic_migrations WHERE id = ?", (mid,))
        if row is None:
            raise NotFound(f"migration {mid} not found")
        return dict(row)

    def _stages(self, mid: int) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM traffic_migration_stages WHERE migration_id = ? ORDER BY seq", (mid,))]

    def _active_row(self) -> Optional[dict]:
        row = self.db.query_one("SELECT * FROM traffic_migrations WHERE active_slot = 1")
        return dict(row) if row else None

    def _out(self, m: dict, stages: Optional[list[dict]] = None) -> dict:
        stages = self._stages(m["id"]) if stages is None else stages
        live = [s for s in stages if s["status"] != "CANCELLED"]
        init_d = dest_share(m["initial_opdk"], m["destination_platform"])
        tgt_d = dest_share(m["target_opdk"], m["destination_platform"])
        good_d = dest_share(m["last_good_opdk"], m["destination_platform"])
        span = abs(tgt_d - init_d)
        progress = 100.0 if m["status"] == "COMPLETED" else (
            round(100.0 * abs(good_d - init_d) / span, 1) if span else 0.0)
        out = {**m, "source": m["source_platform"], "destination": m["destination_platform"],
               "auto_rollback": bool(m["auto_rollback"]), "require_approval": bool(m["require_approval"]),
               "total_stages": len(live), "progress_percent": progress}
        out["stages"] = [{
            **s, "planned_hybrid": 100 - s["planned_opdk"],
            "actual_hybrid": None if s["actual_opdk"] is None else 100 - s["actual_opdk"],
            "safety_checks": json.loads(s["safety_checks"]) if s["safety_checks"] else None,
            "metrics": json.loads(s["metrics"]) if s["metrics"] else None,
        } for s in stages]
        return out

    def get(self, mid: int) -> dict:
        return self._out(self._row(mid))

    def active(self) -> Optional[dict]:
        m = self._active_row()
        return self._out(m) if m else None

    def list(self, limit: int = 20) -> list[dict]:
        rows = self.db.query("SELECT * FROM traffic_migrations ORDER BY id DESC LIMIT ?", (limit,))
        return [self._out(dict(r)) for r in rows]

    def history(self, mid: int, limit: int = 200) -> tuple[int, list[dict]]:
        self._row(mid)
        return self.audit.list(entity_type="migration", entity_id=mid, limit=limit)

    # ================================================================ create
    def _plan_gradual(self, body: GradualMigrationCreate, current_opdk: int) -> list[int]:
        dest = body.destination.value
        cur_d = dest_share(current_opdk, dest)
        start_d = cur_d if body.start_destination_percent is None else body.start_destination_percent
        plan = [] if start_d == cur_d else [start_d]
        d = start_d
        while d != body.target_destination_percent:
            d = min(d + body.step_percent, body.target_destination_percent) if body.target_destination_percent > d \
                else max(d - body.step_percent, body.target_destination_percent)
            plan.append(d)
        return [opdk_for(x, dest) for x in plan]

    def preview(self, body: GradualMigrationCreate) -> dict:
        policy = self.traffic.active_policy()
        if policy is None:
            raise Conflict("no active traffic policy")
        plan = self._plan_gradual(body, policy["opdk_weight"])
        return {"source": body.source.value, "destination": body.destination.value,
                "stages": [{"seq": 0, "opdk": policy["opdk_weight"], "hybrid": policy["hybrid_weight"],
                            "note": "current"}] +
                          [{"seq": i, "opdk": o, "hybrid": 100 - o} for i, o in enumerate(plan, 1)]}

    def _insert(self, *, name: str, mode: str, body, actor: str, initial: int, stages: list[tuple[int, Optional[str]]],
                interval: int, step: Optional[int], status: str, next_at: Optional[datetime]) -> int:
        now = _iso(self.clock())
        try:
            with self.db.transaction() as conn:
                cur = conn.execute(
                    "INSERT INTO traffic_migrations (name, mode, source_platform, destination_platform, initial_opdk,"
                    " target_opdk, current_opdk, last_good_opdk, step_percent, interval_seconds, min_requests,"
                    " max_error_rate_percent, max_p95_ms, min_healthy_instances, auto_rollback, require_approval,"
                    " status, current_stage, next_action_at, active_slot, created_by, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,1,?,?,?)",
                    (name, mode, body.source.value, body.destination.value, initial, stages[-1][0], initial, initial,
                     step, interval, body.min_requests, body.max_error_rate_percent, body.max_p95_ms,
                     body.min_healthy_instances, int(body.auto_rollback), int(body.require_approval), status,
                     _iso(next_at), actor, now, now))
                mid = cur.lastrowid
                for seq, (opdk, at) in enumerate(stages, 1):
                    conn.execute("INSERT INTO traffic_migration_stages (migration_id, seq, planned_opdk, scheduled_at,"
                                 " status) VALUES (?,?,?,?, 'PENDING')", (mid, seq, opdk, at))
                self.audit.record(A.MIGRATION_CREATED, "SUCCESS", actor, reason=body.reason, entity_type="migration",
                                  entity_id=mid, entity_name=name,
                                  new={"mode": mode, "initial_opdk": initial, "stages_opdk": [s[0] for s in stages],
                                       **body.model_dump(mode="json", exclude={"stages", "reason", "name"})})
        except sqlite3.IntegrityError as exc:
            raise Conflict("another migration is already active (database guard)") from exc
        return mid

    def _guard_no_active(self) -> int:
        active = self._active_row()
        if active:
            raise Conflict(f"migration {active['id']} is already {active['status']}; "
                           "complete, cancel or roll it back before starting another")
        policy = self.traffic.active_policy()
        if policy is None:
            raise Conflict("no active traffic policy")
        return policy["opdk_weight"]

    def create_gradual(self, body: GradualMigrationCreate, actor: str) -> dict:
        with self._lock:
            current = self._guard_no_active()
            plan = self._plan_gradual(body, current)
            if not plan:
                raise InvalidRequest("traffic is already at the target allocation")
            name = body.name or f"{body.source.value}->{body.destination.value} gradual"
            mid = self._insert(name=name, mode="GRADUAL", body=body, actor=actor, initial=current,
                               stages=[(o, None) for o in plan], interval=body.interval_seconds,
                               step=body.step_percent, status="RUNNING", next_at=self.clock())
            self._advance(self._row(mid))          # first stage now (subject to pre-checks)
            return self.get(mid)

    def create_scheduled(self, body: ScheduledMigrationCreate, actor: str) -> dict:
        with self._lock:
            current = self._guard_no_active()
            now = self.clock()
            if body.stages[0].at <= now:
                raise InvalidRequest("the first scheduled stage must be in the future")
            name = body.name or f"{body.source.value}->{body.destination.value} scheduled"
            mid = self._insert(name=name, mode="SCHEDULED", body=body, actor=actor, initial=current,
                               stages=[(s.opdk_weight, _iso(s.at)) for s in body.stages],
                               interval=body.final_observation_seconds, step=None, status="SCHEDULED",
                               next_at=body.stages[0].at)
            return self.get(mid)

    # ============================================================== updates
    def _update(self, mid: int, **cols) -> None:
        cols["updated_at"] = _iso(self.clock())
        with self.db.transaction() as conn:
            conn.execute(f"UPDATE traffic_migrations SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                         (*cols.values(), mid))

    def _update_stage(self, mid: int, seq: int, **cols) -> None:
        for k in ("safety_checks", "metrics"):
            if k in cols and not isinstance(cols[k], (str, type(None))):
                cols[k] = json.dumps(cols[k])
        with self.db.transaction() as conn:
            conn.execute(f"UPDATE traffic_migration_stages SET {', '.join(f'{k} = ?' for k in cols)}"
                         " WHERE migration_id = ? AND seq = ?", (*cols.values(), mid, seq))

    def _finish(self, m: dict, status: str, reason: str, event: str, actor: str, **extra) -> None:
        now = _iso(self.clock())
        with self.db.transaction() as conn:
            conn.execute("UPDATE traffic_migration_stages SET status = 'CANCELLED', reason = ?"
                         " WHERE migration_id = ? AND status = 'PENDING'", (f"not executed: migration {status}", m["id"]))
            sets = dict(status=status, status_reason=reason, active_slot=None, next_action_at=None,
                        finished_at=now, updated_at=now, **extra)
            conn.execute(f"UPDATE traffic_migrations SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                         (*sets.values(), m["id"]))
            self.audit.record(event, "SUCCESS" if status in ("COMPLETED", "CANCELLED") else "FAILED"
                              if status == "FAILED" else "INFO", actor, reason=reason, entity_type="migration",
                              entity_id=m["id"], entity_name=m["name"], new={"status": status, **extra})
        log.info("migration %s -> %s: %s", m["id"], status, reason)

    def _pause(self, m: dict, reason: str, actor: str = "migration-engine") -> None:
        self._update(m["id"], status="PAUSED", status_reason=reason, next_action_at=None)
        self.audit.record(A.MIGRATION_PAUSED, "INFO", actor, reason=reason, entity_type="migration",
                          entity_id=m["id"], entity_name=m["name"], previous={"status": m["status"]},
                          new={"status": "PAUSED"})
        log.warning("migration %s paused: %s", m["id"], reason)

    # ======================================================= safety checks
    def _routable(self, platform: str, snapshot: Optional[dict]) -> Optional[int]:
        pool = self.gateways.pool_by_type(PlatformType(platform))
        if pool is None or snapshot is None:
            return None if snapshot is None else 0
        return self.gateways.routable_count(pool, snapshot)

    def _health_check(self, m: dict, opdk: int, snapshot: Optional[dict]) -> dict:
        dest = m["destination_platform"]
        needed = dest_share(opdk, dest) > 0
        routable = self._routable(dest, snapshot)
        ok = (not needed) or (routable is not None and routable >= m["min_healthy_instances"])
        return {"check": "destination_healthy_instances", "pool": dest, "required": m["min_healthy_instances"]
                if needed else 0, "observed": routable, "passed": ok}

    def _evaluate_window(self, m: dict, stage: dict, now: datetime) -> tuple[str, list[dict], dict]:
        dest, src = m["destination_platform"], m["source_platform"]
        pool = dest if dest_share(stage["planned_opdk"], dest) > 0 else src
        since = _dt(stage["observe_from"]).timestamp()
        metrics = {p.value: self.metrics.window(p, since, now.timestamp()) for p in PlatformType}
        w = metrics[pool]
        checks = [
            {"check": "min_requests", "pool": pool, "required": m["min_requests"], "observed": w["requests"],
             "passed": w["requests"] >= m["min_requests"]},
            {"check": "max_error_rate_percent", "pool": pool, "limit": m["max_error_rate_percent"],
             "observed": w["error_rate_percent"],
             "passed": w["error_rate_percent"] is None or w["error_rate_percent"] <= m["max_error_rate_percent"]},
            {"check": "max_p95_ms", "pool": pool, "limit": m["max_p95_ms"], "observed": w["p95_ms"],
             "passed": w["p95_ms"] is None or w["p95_ms"] <= m["max_p95_ms"]},
        ]
        if not checks[1]["passed"] or not checks[2]["passed"]:
            outcome = "VIOLATION"
        elif not checks[0]["passed"]:
            outcome = "INSUFFICIENT"
        else:
            outcome = "PASS"
        return outcome, checks, {"evaluated_pool": pool, "window_seconds": round(now.timestamp() - since, 1),
                                 **{k: {kk: v[kk] for kk in ("requests", "errors", "error_rate_percent",
                                                             "p95_ms", "avg_ms")} for k, v in metrics.items()}}

    def _violation(self, m: dict, stage: dict, reason: str, actor: str = "migration-engine") -> None:
        self._update_stage(m["id"], stage["seq"], status="FAILED", reason=reason, completed_at=_iso(self.clock()))
        self.audit.record(A.MIGRATION_STAGE, "FAILED", actor, reason=reason, entity_type="migration",
                          entity_id=m["id"], entity_name=m["name"],
                          new={"stage": stage["seq"], "planned_opdk": stage["planned_opdk"]})
        if not m["auto_rollback"]:
            self._pause(m, f"stage {stage['seq']} failed: {reason}")
            return
        good = m["last_good_opdk"]
        try:
            if good != m["current_opdk"]:
                self.traffic.apply(good, 100 - good, updated_by=f"migration:{m['id']}",
                                   name=f"migration-{m['id']}-auto-rollback", event=A.TRAFFIC_ROLLBACK,
                                   reason=f"automatic rollback: {reason}")
        except ServiceError as exc:
            self._finish(m, "FAILED", f"stage {stage['seq']} failed ({reason}); automatic rollback to "
                                      f"{good}:{100 - good} FAILED: {exc.detail}", A.MIGRATION_FAILED,
                         actor)
            return
        self._finish(m, "ROLLED_BACK", f"stage {stage['seq']} failed ({reason}); automatically rolled back to "
                                       f"last known-good {good}:{100 - good}", A.MIGRATION_ROLLBACK, actor,
                     current_opdk=good)

    # =============================================================== engine
    def tick(self) -> None:
        """Called every second by the scheduler thread."""
        with self._lock:
            m = self._active_row()
            if not m or m["status"] not in ("RUNNING", "SCHEDULED"):
                return
            try:
                self._advance(m)
                self.last_tick_error = None
            except Exception as exc:                      # never kill the scheduler
                self.last_tick_error = str(exc)
                self.tick_errors_total += 1
                log.exception("migration tick failed")

    def _window_seconds(self, m: dict, stages: list[dict], seq: int, now: datetime) -> float:
        if m["mode"] == "SCHEDULED":
            nxt = next((s for s in stages if s["seq"] > seq and s["status"] == "PENDING"), None)
            if nxt:
                return max((_dt(nxt["scheduled_at"]) - now).total_seconds(), 0.0)
        return float(m["interval_seconds"])

    def _advance(self, m: dict) -> None:
        now = self.clock()
        stages = self._stages(m["id"])
        cur = next((s for s in stages if s["seq"] == m["current_stage"]), None)
        snapshot = self.gateways.runtime_snapshot()

        # 1. A stage is under observation.
        if cur and cur["status"] == "APPLIED":
            health = self._health_check(m, cur["planned_opdk"], snapshot)
            if snapshot is not None and not health["passed"]:
                self._violation(m, cur, f"destination {health['pool']} has {health['observed']} healthy "
                                        f"instance(s), {health['required']} required")
                return
            if m["next_action_at"] and now < _dt(m["next_action_at"]):
                return
            outcome, checks, metrics = self._evaluate_window(m, cur, now)
            self._update_stage(m["id"], cur["seq"], metrics=metrics,
                               safety_checks={"pre": (json.loads(cur["safety_checks"]) or {}).get("pre")
                                              if cur["safety_checks"] else None, "window": checks})
            failed = [c for c in checks if not c["passed"]]
            if outcome == "INSUFFICIENT":
                self._pause(m, f"stage {cur['seq']}: insufficient traffic to evaluate "
                               f"({failed[0]['observed']} < {failed[0]['required']} requests on {failed[0]['pool']})")
                return
            if outcome == "VIOLATION":
                self._violation(m, cur, "; ".join(
                    f"{c['check']} observed {c['observed']} > limit {c['limit']}" for c in failed if "limit" in c))
                return
            self._update_stage(m["id"], cur["seq"], status="PASSED", completed_at=_iso(now))
            self._update(m["id"], last_good_opdk=cur["planned_opdk"])
            self.audit.record(A.MIGRATION_STAGE, "SUCCESS", "migration-engine", entity_type="migration",
                              entity_id=m["id"], entity_name=m["name"],
                              reason=f"stage {cur['seq']} passed observation",
                              new={"stage": cur["seq"], "opdk": cur["planned_opdk"], "metrics": metrics})
            m = self._row(m["id"])
            stages = self._stages(m["id"])
            pending = [s for s in stages if s["status"] == "PENDING"]
            if not pending:
                self._finish(m, "COMPLETED", f"reached {m['current_opdk']}:{100 - m['current_opdk']}",
                             A.MIGRATION_COMPLETED, "migration-engine")
                return
            if m["require_approval"]:
                self._update(m["id"], status="WAITING_APPROVAL", next_action_at=None,
                             status_reason=f"stage {cur['seq']} passed; approve to apply stage {pending[0]['seq']}")
                return

        # 2. Apply the next pending stage.
        nxt = next((s for s in stages if s["seq"] > m["current_stage"] and s["status"] == "PENDING"), None)
        if nxt is None:
            self._finish(m, "COMPLETED", f"reached {m['current_opdk']}:{100 - m['current_opdk']}",
                         A.MIGRATION_COMPLETED, "migration-engine")
            return
        if nxt["scheduled_at"]:
            due = _dt(nxt["scheduled_at"])
            if now < due:
                self._update(m["id"], next_action_at=_iso(due))
                return
            if (now - due).total_seconds() > self.grace:
                self._pause(m, f"scheduled stage {nxt['seq']} (due {nxt['scheduled_at']}) was missed by more than "
                               f"{int(self.grace)}s; it will not run late automatically")
                return
        self._apply_stage(m, nxt, snapshot, now)

    def _apply_stage(self, m: dict, stage: dict, snapshot: Optional[dict], now: datetime) -> None:
        if snapshot is None:
            self._update(m["id"], status_reason="HAProxy unreachable; retrying",
                         next_action_at=_iso(now + timedelta(seconds=2)))
            return
        sel = snapshot["servers"]["traffic_policy"]
        runtime_opdk = sel["opdk"].weight if "opdk" in sel else None
        in_sync = runtime_opdk == m["current_opdk"] and sel["hybrid"].weight == 100 - m["current_opdk"]
        health = self._health_check(m, stage["planned_opdk"], snapshot)
        pre = [{"check": "runtime_matches_migration", "expected_opdk": m["current_opdk"],
                "observed_opdk": runtime_opdk, "passed": in_sync}, health]
        self._update_stage(m["id"], stage["seq"], safety_checks={"pre": pre})
        if not in_sync:
            self._pause(m, f"HAProxy policy is {runtime_opdk}:{100 - (runtime_opdk or 0)} but the migration last "
                           f"applied {m['current_opdk']}:{100 - m['current_opdk']} (changed outside the migration)")
            return
        if not health["passed"]:
            self._pause(m, f"pre-check for stage {stage['seq']}: destination {health['pool']} has "
                           f"{health['observed']} healthy instance(s), {health['required']} required")
            return
        opdk = stage["planned_opdk"]
        try:
            out = self.traffic.apply(opdk, 100 - opdk, updated_by=f"migration:{m['id']}",
                                     name=f"migration-{m['id']}-stage-{stage['seq']}",
                                     reason=f"{m['name']}: stage {stage['seq']}")
        except PoolNotEligible as exc:
            self._pause(m, f"stage {stage['seq']} not applied: {exc.detail}")
            return
        except ServiceError as exc:
            if exc.status_code == 503:     # unreachable / busy: transient, retry
                self._update(m["id"], status_reason=f"retrying: {exc.detail}",
                             next_action_at=_iso(now + timedelta(seconds=2)))
                return
            self._pause(m, f"stage {stage['seq']} not applied: {exc.detail}")
            return
        actual = out["runtime"]["opdk_weight"] if out.get("runtime") else opdk
        window = self._window_seconds(m, self._stages(m["id"]), stage["seq"], now)
        self._update_stage(m["id"], stage["seq"], status="APPLIED", applied_at=_iso(now), observe_from=_iso(now),
                           actual_opdk=actual, policy_version=out["version"])
        self._update(m["id"], status="RUNNING", status_reason=None, current_stage=stage["seq"], current_opdk=opdk,
                     next_action_at=_iso(now + timedelta(seconds=window)))
        self.audit.record(A.MIGRATION_STAGE, "INFO", "migration-engine", entity_type="migration", entity_id=m["id"],
                          entity_name=m["name"], reason=f"stage {stage['seq']} applied; observing {int(window)}s",
                          previous={"opdk": m["current_opdk"]},
                          new={"stage": stage["seq"], "opdk": opdk, "actual_opdk": actual,
                               "policy_version": out["version"]})

    # ============================================================= controls
    def _active_for(self, mid: int, allowed: tuple[str, ...], action: str) -> dict:
        m = self._row(mid)
        if m["status"] not in allowed:
            raise Conflict(f"cannot {action} migration {mid} in status {m['status']}")
        return m

    def pause(self, mid: int, actor: str, reason: Optional[str] = None) -> dict:
        with self._lock:
            m = self._active_for(mid, ("RUNNING", "WAITING_APPROVAL", "SCHEDULED"), "pause")
            self._pause(m, reason or "paused by operator", actor)
            return self.get(mid)

    def resume(self, mid: int, actor: str, reason: Optional[str] = None) -> dict:
        with self._lock:
            m = self._active_for(mid, ("PAUSED",), "resume")
            snapshot = self.gateways.runtime_snapshot()
            if snapshot is None:
                raise Conflict("HAProxy Runtime API is unreachable; cannot verify the actual policy")
            sel = snapshot["servers"]["traffic_policy"]
            if sel["opdk"].weight != m["current_opdk"] or sel["hybrid"].weight != 100 - m["current_opdk"]:
                raise Conflict(f"actual HAProxy policy {sel['opdk'].weight}:{sel['hybrid'].weight} differs from the "
                               f"migration's {m['current_opdk']}:{100 - m['current_opdk']}; restore it, roll back "
                               "or cancel the migration")
            stages = self._stages(mid)
            cur = next((s for s in stages if s["seq"] == m["current_stage"]), None)
            nxt = next((s for s in stages if s["seq"] > m["current_stage"] and s["status"] == "PENDING"), None)
            check_opdk = nxt["planned_opdk"] if nxt else m["current_opdk"]
            health = self._health_check(m, check_opdk, snapshot)
            if not health["passed"]:
                raise Conflict(f"destination {health['pool']} has {health['observed']} healthy instance(s); "
                               f"{health['required']} required")
            now = self.clock()
            notes = []
            if m["mode"] == "SCHEDULED":      # never execute missed scheduled stages late
                for s in stages:
                    if s["status"] == "PENDING" and _dt(s["scheduled_at"]) < now:
                        self._update_stage(mid, s["seq"], status="CANCELLED", reason="missed while paused")
                        notes.append(s["seq"])
            if cur and cur["status"] in ("APPLIED", "FAILED"):
                # fresh observation window for the current allocation
                window = self._window_seconds(m, self._stages(mid), cur["seq"], now)
                self._update_stage(mid, cur["seq"], status="APPLIED", observe_from=_iso(now), reason=None)
                next_at = now + timedelta(seconds=window)
            else:
                next_at = now
            self._update(mid, status="RUNNING", status_reason=None, next_action_at=_iso(next_at))
            self.audit.record(A.MIGRATION_RESUMED, "INFO", actor, reason=reason or "resumed by operator",
                              entity_type="migration", entity_id=mid, entity_name=m["name"],
                              new={"verified_policy_opdk": m["current_opdk"], "destination_health": health,
                                   "skipped_missed_stages": notes})
            return self.get(mid)

    def approve(self, mid: int, actor: str, reason: Optional[str] = None) -> dict:
        with self._lock:
            m = self._active_for(mid, ("WAITING_APPROVAL",), "approve")
            self._update(mid, status="RUNNING", status_reason=None, next_action_at=_iso(self.clock()))
            self.audit.record(A.MIGRATION_APPROVED, "INFO", actor, reason=reason or "next stage approved",
                              entity_type="migration", entity_id=mid, entity_name=m["name"])
            self._advance(self._row(mid))
            return self.get(mid)

    def cancel(self, mid: int, actor: str, reason: Optional[str] = None) -> dict:
        with self._lock:
            m = self._active_for(mid, ACTIVE_STATUSES, "cancel")
            self._finish(m, "CANCELLED", reason or f"cancelled by {actor}; traffic left at "
                                                   f"{m['current_opdk']}:{100 - m['current_opdk']}",
                         A.MIGRATION_CANCELLED, actor)
            return self.get(mid)

    def rollback(self, mid: int, to: str, actor: str, reason: Optional[str] = None) -> dict:
        with self._lock:
            m = self._active_for(mid, ACTIVE_STATUSES + ("COMPLETED",), "roll back")
            target = m["last_good_opdk"] if to == "last_good" else m["initial_opdk"]
            if m["status"] == "COMPLETED" and to == "last_good":
                target = m["initial_opdk"]   # a completed migration's last good state is its target
            self.traffic.apply(target, 100 - target, updated_by=actor, name=f"migration-{mid}-rollback",
                               event=A.TRAFFIC_ROLLBACK,
                               reason=reason or f"operator rollback of migration {mid} to {to}")
            if m["status"] == "COMPLETED":
                self.audit.record(A.MIGRATION_ROLLBACK, "INFO", actor, reason=reason, entity_type="migration",
                                  entity_id=mid, entity_name=m["name"], new={"opdk": target})
                self._update(mid, status="ROLLED_BACK", status_reason=f"rolled back after completion to "
                                                                      f"{target}:{100 - target}", current_opdk=target)
            else:
                self._finish(m, "ROLLED_BACK", reason or f"rolled back by {actor} to {to} allocation "
                                                         f"{target}:{100 - target}", A.MIGRATION_ROLLBACK, actor,
                             current_opdk=target)
            return self.get(mid)

    def cancel_stage(self, mid: int, seq: int, actor: str) -> dict:
        with self._lock:
            m = self._active_for(mid, ACTIVE_STATUSES, "modify")
            stage = next((s for s in self._stages(mid) if s["seq"] == seq), None)
            if stage is None:
                raise NotFound(f"stage {seq} not found")
            if stage["status"] != "PENDING":
                raise Conflict(f"stage {seq} is {stage['status']}; only PENDING stages can be cancelled")
            self._update_stage(mid, seq, status="CANCELLED", reason=f"cancelled by {actor} before execution")
            self.audit.record(A.MIGRATION_STAGE, "INFO", actor, reason=f"stage {seq} cancelled before execution",
                              entity_type="migration", entity_id=mid, entity_name=m["name"], new={"stage": seq})
            if m["status"] == "SCHEDULED" and m["current_stage"] == 0:
                nxt = next((s for s in self._stages(mid) if s["status"] == "PENDING"), None)
                if nxt is None:
                    self._finish(m, "CANCELLED", "all scheduled stages cancelled", A.MIGRATION_CANCELLED, actor)
                else:
                    self._update(mid, next_action_at=nxt["scheduled_at"])
            return self.get(mid)

    # ======================================================= external events
    def note_external_policy_change(self, actor: str) -> None:
        """A manual policy change or rollback was made through the API: stop progression."""
        with self._lock:
            m = self._active_row()
            if m and m["status"] in ("RUNNING", "WAITING_APPROVAL", "SCHEDULED"):
                self._pause(m, f"traffic policy changed manually by {actor}; review before resuming", actor)

    def emergency_shift(self, body: EmergencyShift, actor: str) -> dict:
        """Immediate reallocation. Eligibility (destination availability) is validated by
        TrafficManager. Supersedes (cancels) any active migration after success."""
        with self._lock:
            out = self.traffic.apply(body.opdk_weight, body.hybrid_weight, updated_by=actor, name="emergency-shift",
                                     reason=f"EMERGENCY: {body.reason}", event=A.TRAFFIC_EMERGENCY_SHIFT)
            m = self._active_row()
            cancelled = None
            if m:
                self._finish(m, "CANCELLED", f"superseded by emergency shift to {body.opdk_weight}:"
                                             f"{body.hybrid_weight} ({body.reason})", A.MIGRATION_CANCELLED, actor,
                             current_opdk=body.opdk_weight)
                cancelled = m["id"]
            return {**out, "cancelled_migration": cancelled}

    def recover_after_restart(self) -> list[int]:
        """Startup: never continue blindly. In-flight migrations become PAUSED."""
        paused = []
        with self._lock:
            m = self._active_row()
            if m and m["status"] in ("RUNNING", "WAITING_APPROVAL"):
                self._pause(m, "management service restarted: observation data was lost; verify the traffic "
                               "policy and destination health, then resume", "startup-recovery")
                paused.append(m["id"])
        return paused
