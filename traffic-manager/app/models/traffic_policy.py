"""Traffic Policy: pool-level split between Apigee OPDK and Apigee Hybrid."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.models.common import DisplayName, Reason, StrictModel

# strict: only JSON integers ("90", 90.0, true are rejected)
Percent = Annotated[int, Field(strict=True, ge=0, le=100)]


class TrafficPolicyUpdate(StrictModel):
    opdk_weight: Percent = Field(description="Percent of traffic to the Apigee OPDK pool (0-100).")
    hybrid_weight: Percent = Field(description="Percent of traffic to the Apigee Hybrid pool (0-100).")
    name: Optional[DisplayName] = Field(None, description="Optional label, e.g. 'canary-10'.")
    reason: Reason = None

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {"examples": [{"opdk_weight": 90, "hybrid_weight": 10, "reason": "canary step 1"}]},
    }

    @model_validator(mode="after")
    def _sum(self):
        if self.opdk_weight == 0 and self.hybrid_weight == 0:
            raise ValueError("both pools cannot have zero weight")
        if self.opdk_weight + self.hybrid_weight != 100:
            raise ValueError(
                f"opdk_weight + hybrid_weight must equal 100 (got {self.opdk_weight + self.hybrid_weight})"
            )
        return self


class RollbackRequest(StrictModel):
    to_version: Optional[int] = Field(
        None, gt=0, description="Policy version to restore. Default: the version active before the current one."
    )
    reason: Reason = None


class TrafficPolicy(BaseModel):
    id: int = Field(description="Policy version (monotonic).")
    name: str
    opdk_weight: int
    hybrid_weight: int
    enabled: bool = Field(description="True for the active policy.")
    reason: Optional[str]
    updated_at: datetime
    updated_by: str
    previous_policy_id: Optional[int]


class RuntimeWeights(BaseModel):
    opdk_weight: int
    hybrid_weight: int
    source: Literal["haproxy-runtime"] = "haproxy-runtime"


class PoolTraffic(BaseModel):
    platform_type: str
    pool_id: Optional[int]
    pool_name: Optional[str]
    haproxy_backend: str
    configured_percent: int
    routable_instances: int
    pool_up: Optional[bool]
    effective_percent: Optional[float] = Field(
        None, description="Share of new requests expected now, given pool availability."
    )


class TrafficConfig(BaseModel):
    status: Literal["success"] = "success"
    policy: Optional[TrafficPolicy] = Field(None, description="Active policy saved in SQLite.")
    runtime: Optional[RuntimeWeights] = Field(None, description="Weights read live from HAProxy.")
    in_sync: Optional[bool] = Field(None, description="Runtime weights equal the active policy.")
    haproxy_error: Optional[str] = None
    pools: list[PoolTraffic] = []


class TrafficChangeResult(TrafficConfig):
    message: str
    previous: Optional[dict[str, int]] = None
    verified: bool = True


class EmergencyShiftResult(TrafficChangeResult):
    cancelled_migration: Optional[int] = Field(None, description="Active migration cancelled by this shift, if any.")


class AuditEntry(BaseModel):
    id: int
    timestamp: datetime
    event_type: str
    status: str
    source: str
    reason: Optional[str]
    entity_type: Optional[str]
    entity_id: Optional[int]
    entity_name: Optional[str]
    previous_state: Optional[Any]
    new_state: Optional[Any]
    error: Optional[str]


class AuditPage(BaseModel):
    status: Literal["success"] = "success"
    total: int
    limit: int
    offset: int
    entries: list[AuditEntry] = Field(description="Chronological (oldest first).")
