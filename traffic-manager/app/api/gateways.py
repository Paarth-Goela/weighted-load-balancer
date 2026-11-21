"""/api/v1/gateways: gateway platforms (Apigee OPDK, Apigee Hybrid)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from app.models.common import AUTH_RESPONSES, ErrorResponse
from app.models.gateway import GatewayPlatform, GatewayPlatformCreate
from app.security import actor, require_admin

router = APIRouter(prefix="/api/v1/gateways", tags=["gateways"], dependencies=[Depends(require_admin)],
                   responses=AUTH_RESPONSES)


@router.get("", response_model=list[GatewayPlatform], summary="List gateway platforms")
def list_gateways(request: Request):
    return request.app.state.gateways.list_platforms()


@router.post("", response_model=GatewayPlatform, status_code=status.HTTP_201_CREATED,
             summary="Register a gateway platform",
             description="One platform per type (APIGEE_OPDK, APIGEE_HYBRID). Each maps to one HAProxy pool backend.",
             responses={409: {"model": ErrorResponse, "description": "Name or type already registered."}})
def create_gateway(body: GatewayPlatformCreate, request: Request):
    return request.app.state.gateways.create_platform(body, actor(request))


@router.get("/{gateway_id}", response_model=GatewayPlatform, summary="Get gateway platform",
            responses={404: {"model": ErrorResponse}})
def get_gateway(gateway_id: int, request: Request):
    return request.app.state.gateways.get_platform(gateway_id)
