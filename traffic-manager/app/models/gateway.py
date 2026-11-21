"""Gateway Platform: an Apigee runtime environment (OPDK or Hybrid)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.common import Description, DisplayName, PlatformType, StrictModel


class GatewayPlatformCreate(StrictModel):
    name: DisplayName = Field(examples=["Apigee OPDK (simulated)"])
    platform_type: PlatformType
    description: Description = ""
    enabled: bool = True


class GatewayPlatform(BaseModel):
    id: int
    name: str
    platform_type: PlatformType
    description: str
    enabled: bool
    created_at: datetime
    updated_at: datetime
