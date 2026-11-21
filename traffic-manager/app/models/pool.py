"""Gateway Pool: the runtime endpoints of one platform (one HAProxy pool backend)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.models.common import Description, DisplayName, StrictModel


class GatewayPoolCreate(StrictModel):
    name: DisplayName = Field(examples=["OPDK Pool"])
    platform_id: int = Field(gt=0)
    description: Description = ""
    enabled: bool = True


class PoolHealthSummary(BaseModel):
    total_instances: int
    enabled_instances: int
    healthy_instances: int
    unhealthy_instances: int
    unknown_instances: int
    routable_instances: int = Field(description="Enabled, HEALTHY, and READY+UP in HAProxy.")
    haproxy_pool_up: Optional[bool] = Field(None, description="HAProxy's pool readiness (null if HAProxy unreachable).")
    status: str = Field(description="HEALTHY (all enabled instances routable), DEGRADED (some), DOWN (none), DISABLED.")


class GatewayPool(BaseModel):
    id: int
    name: str
    platform_id: int
    platform_type: str
    haproxy_backend: str
    description: str
    traffic_weight: int = Field(description="Pool-level share (%) from the active traffic policy.")
    enabled: bool
    created_at: datetime
    updated_at: datetime
    health: Optional[PoolHealthSummary] = None
