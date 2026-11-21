"""/api/v1/instances: gateway instances (runtime endpoints)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request, status

from app.models.common import AUTH_RESPONSES, ErrorResponse, Reason
from app.models.instance import GatewayInstance, GatewayInstanceCreate, GatewayInstanceUpdate
from app.security import actor, require_admin

router = APIRouter(prefix="/api/v1/instances", tags=["instances"], dependencies=[Depends(require_admin)],
                   responses=AUTH_RESPONSES)

RUNTIME = {
    503: {"model": ErrorResponse, "description": "HAProxy unreachable or lock busy; nothing changed."},
    502: {"model": ErrorResponse, "description": "HAProxy rejected the change; nothing changed."},
}
SAFETY = {409: {"model": ErrorResponse, "description": "Would leave a pool with traffic but no routable instance."}}


@router.post("", response_model=GatewayInstance, status_code=status.HTTP_201_CREATED,
             summary="Register a gateway instance",
             description="Adds the instance to its pool in HAProxy in MAINT. It gets traffic only after the health "
                         "monitor sees `recovery_threshold` consecutive successful readiness probes.",
             responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, **RUNTIME})
def register_instance(body: GatewayInstanceCreate, request: Request):
    return request.app.state.gateways.register_instance(body, actor(request))


@router.get("", response_model=list[GatewayInstance], summary="List registered instances (with runtime state)")
def list_instances(request: Request, pool_id: Optional[int] = Query(None, gt=0)):
    return request.app.state.gateways.list_instances(pool_id)


@router.get("/{instance_id}", response_model=GatewayInstance, summary="Get instance details",
            responses={404: {"model": ErrorResponse}})
def get_instance(instance_id: int, request: Request):
    return request.app.state.gateways.get_instance(instance_id)


@router.patch("/{instance_id}", response_model=GatewayInstance, summary="Update endpoint configuration",
              description="Changing host/port/protocol/TLS settings replaces the HAProxy server; the instance must "
                          "pass health checks again before it rejoins. `enabled=false` removes it from routing.",
              responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, **SAFETY, **RUNTIME})
def update_instance(instance_id: int, body: GatewayInstanceUpdate, request: Request,
                    force: bool = Query(False, description="Override the last-routable-instance safety check.")):
    return request.app.state.gateways.update_instance(instance_id, body, actor(request), force=force)


@router.delete("/{instance_id}", summary="Deregister an instance safely",
               description="Drains the server, waits for sessions to end (DRAIN_TIMEOUT_SECONDS), sets MAINT, "
                           "deletes it from HAProxy and verifies it is gone. It receives no traffic afterwards.",
               responses={404: {"model": ErrorResponse}, **SAFETY, **RUNTIME})
def deregister_instance(instance_id: int, request: Request,
                        force: bool = Query(False, description="Override the last-routable-instance safety check."),
                        reason: Reason = Query(None)) -> dict[str, Any]:
    return request.app.state.gateways.deregister_instance(instance_id, actor(request), reason=reason, force=force)
