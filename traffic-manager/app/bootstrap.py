"""
Optional first-start bootstrap from a JSON file (BOOTSTRAP_FILE).

Runs only when the database has no gateway platforms. It creates platforms,
pools, instances and the initial traffic policy in SQLite. The reconciler and
health monitor then register the instances in HAProxy and enable them once
they are healthy.

Endpoint values live in the file, not in code. Point BOOTSTRAP_FILE at a
different file (or register through the API) to use real OPDK/Hybrid
endpoints instead of the local simulators.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, ValidationError

from app.models.common import DisplayName, PlatformType
from app.models.gateway import GatewayPlatformCreate
from app.models.instance import GatewayInstanceCreate
from app.models.pool import GatewayPoolCreate
from app.models.traffic_policy import TrafficPolicyUpdate
from app.services import audit_service as A
from app.services.audit_service import AuditService
from app.services.gateway_manager import GatewayManager
from app.services.traffic_manager import TrafficManager

log = logging.getLogger(__name__)
SOURCE = "bootstrap"


class _Pool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: DisplayName
    platform_type: PlatformType
    description: str = ""
    enabled: bool = True


class BootstrapFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platforms: list[GatewayPlatformCreate]
    pools: list[_Pool]
    instances: list[dict]          # GatewayInstanceCreate fields, with "pool" (name) instead of pool_id
    initial_policy: TrafficPolicyUpdate


def run_bootstrap(path: Optional[Path], gateways: GatewayManager, traffic: TrafficManager,
                  audit: AuditService) -> str:
    if path is None:
        return "disabled"
    if gateways.list_platforms():
        return "skipped (database already initialised)"
    try:
        spec = BootstrapFile.model_validate_json(Path(path).read_text(encoding="utf-8"))
        with gateways.db.transaction():
            platform_ids = {}
            for p in spec.platforms:
                platform_ids[p.platform_type] = gateways.create_platform(p, SOURCE)["id"]
            pool_ids = {}
            for p in spec.pools:
                if p.platform_type not in platform_ids:
                    raise ValueError(f"pool '{p.name}' references unknown platform type {p.platform_type.value}")
                pool = gateways.create_pool(GatewayPoolCreate(name=p.name, platform_id=platform_ids[p.platform_type],
                                                              description=p.description, enabled=p.enabled), SOURCE)
                pool_ids[p.name] = pool["id"]
            for raw in spec.instances:
                raw = dict(raw)
                pool_name = raw.pop("pool", None)
                if pool_name not in pool_ids:
                    raise ValueError(f"instance '{raw.get('name')}' references unknown pool {pool_name!r}")
                inst = GatewayInstanceCreate(pool_id=pool_ids[pool_name], **raw)
                gateways.register_instance(inst, SOURCE, apply_runtime=False)
            traffic.seed_policy(spec.initial_policy.opdk_weight, spec.initial_policy.hybrid_weight, SOURCE)
            audit.record(A.BOOTSTRAP, "SUCCESS", SOURCE, reason=f"initialised from {Path(path).name}",
                         new={"platforms": len(spec.platforms), "pools": len(spec.pools),
                              "instances": len(spec.instances),
                              "initial_policy": spec.initial_policy.model_dump(exclude_none=True)})
        return "applied"
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        # Transaction rolled back: nothing half-imported. Start with an empty DB.
        log.error("bootstrap failed: %s", exc)
        audit.record(A.BOOTSTRAP, "FAILED", SOURCE, error=str(exc)[:500])
        return f"failed: {exc}"
