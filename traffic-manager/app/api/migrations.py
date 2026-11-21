"""Traffic Shift Management API: migrations, emergency shift, metrics, dashboard overview.

All endpoints require the admin bearer token. Every traffic change goes
through the traffic policy manager; nothing here talks to HAProxy directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Query, Request, status

from app.models.common import AUTH_RESPONSES, ErrorResponse, PlatformType
from app.models.migration import (
    EmergencyShift,
    GradualMigrationCreate,
    Migration,
    MigrationControl,
    MigrationPreview,
    MigrationRollback,
    ScheduledMigrationCreate,
)
from app.models.traffic_policy import AuditPage, EmergencyShiftResult
from app.security import actor, require_admin
from app.services import audit_service as A

router = APIRouter(prefix="/api/v1", tags=["traffic-shift"], dependencies=[Depends(require_admin)],
                   responses=AUTH_RESPONSES)

ERRORS = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse, "description": "Another migration is active, invalid state transition, "
                                                 "policy drift, or destination pool not healthy."},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse, "description": "HAProxy unreachable or update lock busy."},
}


def mgr(request: Request):
    return request.app.state.migrations


def _control(request: Request, body: Optional[MigrationControl]) -> Optional[str]:
    return body.reason if body else None


# ------------------------------------------------------------- migrations
@router.post("/migrations/preview", response_model=MigrationPreview, summary="Preview gradual stages",
             description="Computes the stage progression without starting anything.", responses=ERRORS)
def preview(body: GradualMigrationCreate, request: Request):
    return mgr(request).preview(body)


@router.post("/migrations", response_model=Migration, status_code=status.HTTP_201_CREATED,
             summary="Start a gradual migration", responses=ERRORS,
             description="Shifts traffic from `source` to `destination` in `step_percent` steps, observing each "
                         "stage for `interval_seconds`. Before each stage it checks destination health and that "
                         "the HAProxy policy is unchanged. After each window it checks request volume, error rate "
                         "and p95 latency. Only one migration can be active (409 otherwise).")
def start_gradual(body: GradualMigrationCreate, request: Request):
    return mgr(request).create_gradual(body, actor(request))


@router.post("/migrations/scheduled", response_model=Migration, status_code=status.HTTP_201_CREATED,
             summary="Create a scheduled migration", responses=ERRORS,
             description="Stages are applied at the given times, with the same safety checks. Pending stages "
                         "can be cancelled. Stages missed during downtime are never executed late.")
def start_scheduled(body: ScheduledMigrationCreate, request: Request):
    return mgr(request).create_scheduled(body, actor(request))


@router.get("/migrations", response_model=list[Migration], summary="Recent migrations (newest first)")
def list_migrations(request: Request, limit: int = Query(20, ge=1, le=200)):
    return mgr(request).list(limit)


@router.get("/migrations/active", response_model=Optional[Migration], summary="The active migration, if any")
def active(request: Request):
    return mgr(request).active()


@router.get("/migrations/{mid}", response_model=Migration, summary="Migration with stages", responses=ERRORS)
def get_migration(mid: int, request: Request):
    return mgr(request).get(mid)


@router.get("/migrations/{mid}/history", response_model=AuditPage, summary="Migration audit trail")
def migration_history(mid: int, request: Request, limit: int = Query(200, ge=1, le=1000)):
    total, entries = mgr(request).history(mid, limit)
    return {"total": total, "limit": limit, "offset": 0, "entries": entries}


@router.post("/migrations/{mid}/pause", response_model=Migration, summary="Pause", responses=ERRORS)
def pause(mid: int, request: Request, body: Optional[MigrationControl] = Body(None)):
    return mgr(request).pause(mid, actor(request), _control(request, body))


@router.post("/migrations/{mid}/resume", response_model=Migration, summary="Resume (after re-verification)",
             responses=ERRORS, description="Verifies that the actual HAProxy policy equals the migration's current "
                                           "allocation and that the destination pool is healthy. The current stage "
                                           "gets a fresh observation window.")
def resume(mid: int, request: Request, body: Optional[MigrationControl] = Body(None)):
    return mgr(request).resume(mid, actor(request), _control(request, body))


@router.post("/migrations/{mid}/approve", response_model=Migration, summary="Approve the next stage",
             responses=ERRORS)
def approve(mid: int, request: Request, body: Optional[MigrationControl] = Body(None)):
    return mgr(request).approve(mid, actor(request), _control(request, body))


@router.post("/migrations/{mid}/cancel", response_model=Migration, summary="Cancel (traffic left as is)",
             responses=ERRORS)
def cancel(mid: int, request: Request, body: Optional[MigrationControl] = Body(None)):
    return mgr(request).cancel(mid, actor(request), _control(request, body))


@router.post("/migrations/{mid}/rollback", response_model=Migration, summary="Roll back the migration",
             responses=ERRORS, description="Re-applies the last known-good allocation (default) or the "
                                           "initial allocation, through the standard verified policy update.")
def rollback(mid: int, request: Request, body: Optional[MigrationRollback] = Body(None)):
    body = body or MigrationRollback()
    return mgr(request).rollback(mid, body.to, actor(request), body.reason)


@router.delete("/migrations/{mid}/stages/{seq}", response_model=Migration,
               summary="Cancel a pending stage before it runs", responses=ERRORS)
def cancel_stage(mid: int, seq: int, request: Request):
    return mgr(request).cancel_stage(mid, seq, actor(request))


# -------------------------------------------------------------- emergency
@router.post("/traffic/emergency", response_model=EmergencyShiftResult, summary="Emergency traffic reallocation",
             responses=ERRORS,
             description='Immediate shift. Requires `reason` and `confirm: "EMERGENCY SHIFT"`. Every pool that '
                         "receives traffic must have a routable instance. Cancels any active migration and is "
                         "audited as TRAFFIC_EMERGENCY_SHIFT.")
def emergency(body: EmergencyShift, request: Request):
    out = mgr(request).emergency_shift(body, actor(request))
    msg = f"EMERGENCY shift applied: OPDK {body.opdk_weight}% / Hybrid {body.hybrid_weight}%"
    if out.get("cancelled_migration"):
        msg += f"; migration {out['cancelled_migration']} cancelled"
    return {**out, "message": msg, "verified": True}


# ---------------------------------------------------------------- metrics
@router.get("/metrics/traffic", summary="Per-pool traffic metrics over a recent window")
def traffic_metrics(request: Request, window_seconds: int = Query(60, ge=5, le=3600)) -> dict[str, Any]:
    c = request.app.state.metrics
    return {"status": "success", "window_seconds": window_seconds, "pools": c.recent(window_seconds),
            "collector": c.status()}


@router.get("/metrics/timeseries", summary="Per-pool request/error/p95 time buckets")
def timeseries(request: Request, seconds: int = Query(600, ge=30, le=3600),
               bucket_seconds: int = Query(10, ge=1, le=300)) -> dict[str, Any]:
    c = request.app.state.metrics
    return {"status": "success", "bucket_seconds": bucket_seconds, "series": c.series(seconds, bucket_seconds),
            "collector": c.status()}


# ------------------------------------------------------------- dashboard
@router.get("/traffic-shift/overview", summary="Everything the Traffic Shift dashboard shows, in one call")
def overview(request: Request, window_seconds: int = Query(60, ge=5, le=3600)) -> dict[str, Any]:
    st = request.app.state
    cfg = st.traffic.get_config()
    migration = st.migrations.active()
    _, last = st.audit.list(event_types=A.TRAFFIC_EVENTS, limit=50)
    last_ok = next((e for e in reversed(last) if e["status"] == "SUCCESS"), None)
    runtime = st.gateways.runtime_snapshot()
    gateways = []
    for ptype in PlatformType:
        pool = st.gateways.pool_by_type(ptype)
        gateways.append({"platform_type": ptype.value, "pool": pool["name"] if pool else None,
                         "health": st.gateways.pool_health(pool, runtime) if pool else None})
    if migration:
        target = {"opdk_weight": migration["target_opdk"], "hybrid_weight": 100 - migration["target_opdk"],
                  "source": f"migration {migration['id']}"}
    elif cfg["policy"]:
        target = {"opdk_weight": cfg["policy"]["opdk_weight"], "hybrid_weight": cfg["policy"]["hybrid_weight"],
                  "source": "active policy"}
    else:
        target = None
    return {
        "status": "success",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "traffic": cfg,
        "target_allocation": target,
        "migration": migration,
        "last_successful_shift": last_ok,
        "gateways": gateways,
        "metrics": {"window_seconds": window_seconds, "pools": st.metrics.recent(window_seconds),
                    "collector": st.metrics.status()},
        "engine": {"scheduler_running": st.migration_scheduler.running if st.migration_scheduler else False,
                   "last_tick_error": st.migrations.last_tick_error},
    }
