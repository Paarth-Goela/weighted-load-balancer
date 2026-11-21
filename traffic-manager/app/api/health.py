"""Health endpoints. /livez is public (container health check); /api/v1/health* require auth."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request

from app.models.common import AUTH_RESPONSES, POLICY_BACKEND, POLICY_SERVER, PlatformType
from app.models.traffic_policy import AuditPage
from app.security import require_admin
from app.services import audit_service as A
from app.services.haproxy_client import RuntimeAPIError

public = APIRouter(tags=["health"])
router = APIRouter(prefix="/api/v1", tags=["health"], dependencies=[Depends(require_admin)],
                   responses=AUTH_RESPONSES)


@public.get("/livez", summary="Liveness (public, no details)",
            description="For the container health check. Reveals nothing about configuration.")
def livez() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health", summary="Management service health")
def service_health(request: Request) -> dict[str, Any]:
    st = request.app.state
    db_ok = st.db.ping()
    try:
        info = st.client.info()
        hap = {"reachable": True, "version": info.get("Version"),
               "uptime_sec": int(info["Uptime_sec"]) if info.get("Uptime_sec", "").isdigit() else None}
    except RuntimeAPIError:
        hap = {"reachable": False, "error": "HAProxy Runtime API is unreachable"}
    loop = st.control_loop
    return {
        "status": "ok" if (db_ok and hap["reachable"]) else "degraded",
        "service": "traffic-manager",
        "database": {"ok": db_ok, "schema_version": st.db.schema_version() if db_ok else None},
        "haproxy": hap,
        "bootstrap": st.bootstrap_result,
        "control_loop": {
            "running": loop.running if loop else False,
            "last_cycle": st.monitor.last_cycle or None,
        },
    }


@router.get("/health/gateways", summary="Health of both gateway platforms")
def gateway_health(request: Request) -> dict[str, Any]:
    st = request.app.state
    gw = st.gateways
    runtime = gw.runtime_snapshot()
    cfg = st.traffic.get_config()
    by_type = {p["platform_type"]: p for p in cfg["pools"]}
    out = []
    for plat in gw.list_platforms():
        ptype = PlatformType(plat["platform_type"])
        pool = gw.pool_by_type(ptype)
        summary = gw.pool_health(pool, runtime) if pool else None
        sel = runtime["servers"][POLICY_BACKEND].get(POLICY_SERVER[ptype]) if runtime else None
        out.append({
            "platform": plat,
            "pool": {"id": pool["id"], "name": pool["name"]} if pool else None,
            "health": summary,
            "status": summary["status"] if summary else "NO_POOL",
            "configured_percent": by_type.get(ptype.value, {}).get("configured_percent"),
            "effective_percent": by_type.get(ptype.value, {}).get("effective_percent"),
            "haproxy_pool_check": sel.operational if sel else None,
        })
    return {"status": "success", "haproxy_reachable": runtime is not None, "gateways": out}


@router.get("/health/instances", summary="Health of every registered instance")
def instance_health(request: Request, transitions: int = Query(5, ge=0, le=50,
                                                               description="Recent transitions per instance")):
    st = request.app.state
    result = []
    for inst in st.gateways.list_instances():
        _, recent = st.audit.list(event_types=[A.HEALTH_TRANSITION], entity_type="gateway_instance",
                                  entity_id=inst["id"], limit=transitions) if transitions else (0, [])
        result.append({
            "id": inst["id"], "name": inst["name"], "pool_id": inst["pool_id"],
            "platform_type": inst["platform_type"], "enabled": inst["enabled"],
            "health_status": inst["current_health_status"],
            "consecutive_failures": inst["consecutive_failures"],
            "consecutive_successes": inst["consecutive_successes"],
            "last_health_check": inst["last_health_check"], "last_health_error": inst["last_health_error"],
            "last_status_change": inst["last_status_change"],
            "awaiting_fresh_probe": inst["id"] in st.reconciler.fresh_probe_required,
            "runtime": inst.get("runtime"),
            "recent_transitions": [
                {"at": e["timestamp"], "from": (e["previous_state"] or {}).get("health"),
                 "to": (e["new_state"] or {}).get("health"), "error": e["error"]} for e in recent
            ],
        })
    return {"status": "success", "instances": result}


@router.get("/audit", response_model=AuditPage, summary="Audit log (all events)")
def audit_log(request: Request, event_type: Optional[str] = Query(None, max_length=64),
              entity_type: Optional[str] = Query(None, max_length=64),
              entity_id: Optional[int] = Query(None, gt=0),
              limit: int = Query(50, ge=1, le=1000), offset: int = Query(0, ge=0)):
    total, entries = request.app.state.audit.list(
        event_types=[event_type] if event_type else None, entity_type=entity_type, entity_id=entity_id,
        limit=limit, offset=offset)
    return {"total": total, "limit": limit, "offset": offset, "entries": entries}
