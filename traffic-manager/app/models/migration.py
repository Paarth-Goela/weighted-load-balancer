"""Traffic Shift Management schemas: gradual / scheduled migrations, emergency shift."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.common import DisplayName, PlatformType, Reason, StrictModel
from app.models.traffic_policy import Percent

MigrationStatus = Literal["SCHEDULED", "RUNNING", "WAITING_APPROVAL", "PAUSED",
                          "COMPLETED", "FAILED", "CANCELLED", "ROLLED_BACK"]
ACTIVE_STATUSES = ("SCHEDULED", "RUNNING", "WAITING_APPROVAL", "PAUSED")
EMERGENCY_CONFIRMATION = "EMERGENCY SHIFT"


class SafetyThresholds(StrictModel):
    """Checked before every stage and at the end of each observation window."""

    min_requests: int = Field(100, ge=0, le=10_000_000, strict=True,
                              description="Minimum destination-pool requests per observation window. "
                                          "Fewer -> migration PAUSES (insufficient evidence).")
    max_error_rate_percent: float = Field(1.0, ge=0, le=100,
                                          description="Max destination-pool error rate (HTTP 5xx / no response).")
    max_p95_ms: int = Field(1000, ge=1, le=600_000, strict=True,
                            description="Max destination-pool p95 response time (HAProxy Tr).")
    min_healthy_instances: int = Field(1, ge=1, le=1000, strict=True,
                                       description="Routable destination instances required.")
    auto_rollback: bool = Field(True, description="On a violation, re-apply the last known-good allocation.")
    require_approval: bool = Field(False, description="Wait for POST /approve between stages.")


class _Direction(StrictModel):
    source: PlatformType = PlatformType.APIGEE_OPDK
    destination: PlatformType = PlatformType.APIGEE_HYBRID

    @model_validator(mode="after")
    def _distinct(self):
        if self.source == self.destination:
            raise ValueError("source and destination pools must differ")
        return self


class GradualMigrationCreate(_Direction, SafetyThresholds):
    name: Optional[DisplayName] = None
    start_destination_percent: Optional[Percent] = Field(
        None, description="Starting destination share. Default: the current active policy.")
    target_destination_percent: Percent = Field(100, description="Destination share at the end.")
    step_percent: int = Field(10, ge=1, le=100, strict=True, description="Percentage points per stage.")
    interval_seconds: int = Field(300, ge=1, le=7 * 86400, strict=True,
                                  description="Observation window per stage (time between steps).")
    reason: Reason = None

    model_config = {"extra": "forbid", "json_schema_extra": {"examples": [{
        "source": "APIGEE_OPDK", "destination": "APIGEE_HYBRID", "target_destination_percent": 100,
        "step_percent": 10, "interval_seconds": 300, "min_requests": 100, "max_error_rate_percent": 1.0,
        "max_p95_ms": 800, "min_healthy_instances": 2, "auto_rollback": True, "require_approval": False}]}}


class ScheduledStage(StrictModel):
    at: datetime = Field(description="When to apply (ISO-8601; timezone required).")
    opdk_weight: Percent
    hybrid_weight: Percent

    @field_validator("at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must include a timezone (e.g. 2026-11-22T09:00:00Z)")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _sum(self):
        if self.opdk_weight + self.hybrid_weight != 100:
            raise ValueError("opdk_weight + hybrid_weight must equal 100")
        return self


class ScheduledMigrationCreate(_Direction, SafetyThresholds):
    name: Optional[DisplayName] = None
    stages: list[ScheduledStage] = Field(min_length=1, max_length=100)
    final_observation_seconds: int = Field(300, ge=1, le=7 * 86400, strict=True,
                                           description="Observation window after the last stage.")
    reason: Reason = None

    @model_validator(mode="after")
    def _ordered(self):
        times = [s.at for s in self.stages]
        if times != sorted(times) or len(set(times)) != len(times):
            raise ValueError("stage times must be strictly increasing")
        return self


class MigrationControl(StrictModel):
    reason: Reason = None


class MigrationRollback(StrictModel):
    to: Literal["last_good", "initial"] = Field("last_good", description="Allocation to restore.")
    reason: Reason = None


class EmergencyShift(StrictModel):
    opdk_weight: Percent
    hybrid_weight: Percent
    reason: str = Field(min_length=3, max_length=500, description="Required: why this emergency action is taken.")
    confirm: str = Field(description=f'Must be exactly "{EMERGENCY_CONFIRMATION}".')

    @model_validator(mode="after")
    def _check(self):
        if self.opdk_weight + self.hybrid_weight != 100:
            raise ValueError("opdk_weight + hybrid_weight must equal 100")
        if self.confirm != EMERGENCY_CONFIRMATION:
            raise ValueError(f'confirmation text must be exactly "{EMERGENCY_CONFIRMATION}"')
        return self


class Stage(BaseModel):
    seq: int
    planned_opdk: int
    planned_hybrid: int
    scheduled_at: Optional[datetime]
    status: str
    applied_at: Optional[datetime]
    observe_from: Optional[datetime]
    completed_at: Optional[datetime]
    actual_opdk: Optional[int]
    actual_hybrid: Optional[int]
    policy_version: Optional[int]
    safety_checks: Optional[Any]
    metrics: Optional[Any]
    reason: Optional[str]


class Migration(BaseModel):
    id: int
    name: str
    mode: str
    source: str
    destination: str
    initial_opdk: int
    target_opdk: int
    current_opdk: int
    last_good_opdk: int
    step_percent: Optional[int]
    interval_seconds: int
    min_requests: int
    max_error_rate_percent: float
    max_p95_ms: int
    min_healthy_instances: int
    auto_rollback: bool
    require_approval: bool
    status: MigrationStatus
    status_reason: Optional[str]
    current_stage: int
    total_stages: int
    progress_percent: float = Field(description="Share of the initial->target distance covered by passed stages.")
    next_action_at: Optional[datetime]
    created_by: str
    created_at: datetime
    updated_at: datetime
    finished_at: Optional[datetime]
    stages: list[Stage] = []


class MigrationPreview(BaseModel):
    source: str
    destination: str
    stages: list[dict]
