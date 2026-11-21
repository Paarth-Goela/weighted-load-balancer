"""/api/v1/pools: gateway pools."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from app.models.common import AUTH_RESPONSES, ErrorResponse
from app.models.pool import GatewayPool, GatewayPoolCreate
from app.security import actor, require_admin

router = APIRouter(prefix="/api/v1/pools", tags=["pools"], dependencies=[Depends(require_admin)],
                   responses=AUTH_RESPONSES)


@router.get("", response_model=list[GatewayPool], summary="List gateway pools (with health summary)")
def list_pools(request: Request):
    return request.app.state.gateways.list_pools()


@router.post("", response_model=GatewayPool, status_code=status.HTTP_201_CREATED, summary="Create a gateway pool",
             description="One pool per platform. traffic_weight mirrors the active traffic policy and is "
                         "changed only through PUT /api/v1/traffic/config.",
             responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}})
def create_pool(body: GatewayPoolCreate, request: Request):
    return request.app.state.gateways.create_pool(body, actor(request))


@router.get("/{pool_id}", response_model=GatewayPool, summary="Get pool configuration and health summary",
            responses={404: {"model": ErrorResponse}})
def get_pool(pool_id: int, request: Request):
    return request.app.state.gateways.get_pool(pool_id)
