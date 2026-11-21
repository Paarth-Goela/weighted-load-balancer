"""Gateway Instance: one runtime endpoint (OPDK router, Hybrid ingress, ...)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from app.models.common import (
    HealthPath,
    HealthStatus,
    Host,
    HostHeader,
    Hostname,
    Protocol,
    Reason,
    ServerName,
    StatusCodes,
    StrictModel,
)


class GatewayInstanceCreate(StrictModel):
    name: ServerName = Field(examples=["opdk-router-1"], description="Unique; also the HAProxy server name.")
    pool_id: int = Field(gt=0)
    host: Host = Field(examples=["opdk-router-1"])
    port: int = Field(ge=1, le=65535, strict=True)
    protocol: Protocol = "HTTP"
    health_check_path: HealthPath = Field("/healthz", description="Readiness endpoint probed by the monitor.")
    expected_status_codes: StatusCodes = Field(default_factory=lambda: [200])
    host_header: Optional[HostHeader] = Field(None, description="Host header for health probes.")
    tls_verify: bool = True
    tls_server_name: Optional[Hostname] = Field(None, description="SNI / certificate name (HTTPS only).")
    lb_weight: int = Field(100, ge=1, le=256, strict=True, description="Instance weight inside its pool.")
    enabled: bool = True
    reason: Reason = None

    @model_validator(mode="after")
    def _tls_fields(self):
        if self.protocol == "HTTP" and self.tls_server_name:
            raise ValueError("tls_server_name is only valid for protocol HTTPS")
        return self


class GatewayInstanceUpdate(StrictModel):
    """Partial update. Changing host/port/protocol/TLS replaces the HAProxy
    server: it is drained, re-added in maintenance, and re-enabled only after
    it passes health checks again."""

    host: Optional[Host] = None
    port: Optional[int] = Field(None, ge=1, le=65535, strict=True)
    protocol: Optional[Protocol] = None
    health_check_path: Optional[HealthPath] = None
    expected_status_codes: Optional[StatusCodes] = None
    host_header: Optional[HostHeader] = None
    tls_verify: Optional[bool] = None
    tls_server_name: Optional[Hostname] = None
    lb_weight: Optional[int] = Field(None, ge=1, le=256, strict=True)
    enabled: Optional[bool] = None
    reason: Reason = None


class InstanceRuntime(BaseModel):
    present: bool = Field(description="Server exists in the HAProxy pool backend.")
    address: Optional[str] = None
    admin_state: Optional[str] = Field(None, description="READY, MAINT or DRAIN (HAProxy administrative state).")
    operational_state: Optional[str] = Field(None, description="UP / DOWN / ... (HAProxy native check).")
    check_status: Optional[str] = None
    weight: Optional[int] = None
    current_sessions: Optional[int] = None
    total_sessions: Optional[int] = None
    receiving_traffic: bool = False


class GatewayInstance(BaseModel):
    id: int
    name: str
    pool_id: int
    host: str
    port: int
    protocol: Protocol
    health_check_path: str
    expected_status_codes: list[int]
    host_header: Optional[str]
    tls_verify: bool
    tls_server_name: Optional[str]
    lb_weight: int
    enabled: bool
    current_health_status: HealthStatus
    consecutive_failures: int
    consecutive_successes: int
    last_health_check: Optional[datetime]
    last_health_error: Optional[str]
    last_status_change: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    runtime: Optional[InstanceRuntime] = None
