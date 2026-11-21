"""/api/v1/traffic: OPDK / Hybrid traffic policy, rollback, history."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, Query, Request

from app.models.common import AUTH_RESPONSES, ErrorResponse
from app.models.traffic_policy import (
    AuditPage,
    RollbackRequest,
    TrafficChangeResult,
    TrafficConfig,
    TrafficPolicy,
    TrafficPolicyUpdate,
)
from app.security import actor, require_admin

router = APIRouter(prefix="/api/v1/traffic", tags=["traffic"], dependencies=[Depends(require_admin)],
                   responses=AUTH_RESPONSES)

CHANGE_ERRORS = {
    409: {"model": ErrorResponse, "description": "A target pool has no routable instance (pool_not_eligible), "
                                                 "or nothing to roll back to."},
    422: {"model": ErrorResponse, "description": "Invalid weights."},
    502: {"model": ErrorResponse, "description": "HAProxy rejected the change; previous weights restored."},
    503: {"model": ErrorResponse, "description": "HAProxy unreachable or another change in progress; nothing changed."},
    500: {"model": ErrorResponse, "description": "Persistence or rollback failed (inconsistent_state)."},
}


@router.get("/config", response_model=TrafficConfig, summary="Current OPDK / Hybrid traffic weights",
            description="Returns the active saved policy, the weights read live from HAProxy, whether they match, "
                        "and each pool's availability and effective share.")
def get_config(request: Request):
    return request.app.state.traffic.get_config()


def _result(out: dict, message: str) -> dict:
    return {**out, "message": message, "verified": True}


@router.put("/config", response_model=TrafficChangeResult, summary="Update the traffic policy",
            description="Integers 0-100 summing to 100. Checks pool eligibility, then applies to HAProxy without a "
                        "restart (increases first), verifies by read-back, rolls back on failure, saves a new "
                        "policy version and writes an audit record.",
            responses=CHANGE_ERRORS)
def update_config(body: TrafficPolicyUpdate, request: Request):
    out = request.app.state.traffic.apply(body.opdk_weight, body.hybrid_weight, updated_by=actor(request),
                                          name=body.name, reason=body.reason)
    p = out["previous"]
    return _result(out, f"OPDK/Hybrid {p['opdk_weight']}:{p['hybrid_weight']} -> "
                        f"{body.opdk_weight}:{body.hybrid_weight} applied without restart and verified "
                        f"(policy v{out['version']})")


@router.post("/rollback", response_model=TrafficChangeResult, summary="Roll back to a previous policy",
             description="Default: re-applies the version that was active before the current one, as a new "
                         "version. Rolling back twice returns to the original policy. `to_version` selects a "
                         "specific version. Same safety checks as PUT /config.",
             responses=CHANGE_ERRORS)
def rollback(request: Request, body: Optional[RollbackRequest] = Body(None)):
    body = body or RollbackRequest()
    out = request.app.state.traffic.rollback(body.to_version, updated_by=actor(request), reason=body.reason)
    p = out["previous"]
    pol = out["policy"]
    return _result(out, f"rolled back: {p['opdk_weight']}:{p['hybrid_weight']} -> "
                        f"{pol['opdk_weight']}:{pol['hybrid_weight']} (policy v{out['version']})")


@router.get("/history", response_model=AuditPage, summary="Traffic policy change history",
            description="Policy changes, rollbacks and restores, including failed and rejected attempts. "
                        "Chronological (oldest first). `offset` skips the N newest entries.")
def history(request: Request, limit: int = Query(50, ge=1, le=1000), offset: int = Query(0, ge=0)):
    total, entries = request.app.state.traffic.history(limit, offset)
    return {"total": total, "limit": limit, "offset": offset, "entries": entries}


@router.get("/policies", response_model=list[TrafficPolicy], summary="All applied policy versions")
def versions(request: Request):
    return request.app.state.traffic.versions()
