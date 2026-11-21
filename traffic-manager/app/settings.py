"""Environment-driven settings. Secrets come only from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

MIN_TOKEN_LENGTH = 16


class SettingsError(RuntimeError):
    """Invalid configuration: the service refuses to start."""


@dataclass(frozen=True)
class Settings:
    api_token: str = field(repr=False)
    haproxy_socket: str = "/run/haproxy-runtime/admin.sock"
    database_path: Path = Path("/app/data/traffic-manager.db")
    bootstrap_file: Optional[Path] = None

    # Health monitoring
    health_interval: float = 5.0          # seconds between control-loop cycles
    health_timeout: float = 2.0           # per-probe timeout
    failure_threshold: int = 3            # consecutive failures -> UNHEALTHY
    recovery_threshold: int = 2           # consecutive successes -> HEALTHY
    recovery_hold_seconds: float = 0.0    # min time UNHEALTHY before re-enable
    probe_ca_file: Optional[str] = None   # extra CA bundle for HTTPS probes

    # HAProxy
    socket_timeout: float = 5.0
    lock_timeout: float = 10.0
    drain_timeout: float = 10.0           # wait for sessions to finish on deregistration
    haproxy_ca_file: str = "@system-ca"   # CA for 'ssl verify required' instances
    native_check_inter: str = "3s"
    # After an HAProxy restart both pool links are held in MAINT until the pools
    # are ready, then released together (avoids a pool briefly taking 100%).
    pool_release_settle_seconds: float = 1.5   # > selector check interval (1s)
    pool_release_timeout_seconds: float = 20.0  # release anyway after this long

    # Traffic shift management
    metrics_socket: Optional[str] = None       # HAProxy per-request log stream (Unix DGRAM)
    metrics_retention_seconds: float = 900.0
    migration_tick_seconds: float = 1.0
    schedule_grace_seconds: float = 60.0       # scheduled stage later than this -> not run, migration pauses

    # Prometheus scrape token (read-only: valid ONLY for GET /metrics). Optional;
    # the admin token is also accepted there.
    metrics_token: Optional[str] = field(default=None, repr=False)

    run_background: bool = True           # start the control loop (off in unit tests)

    def __post_init__(self) -> None:
        if len(self.api_token or "") < MIN_TOKEN_LENGTH:
            raise SettingsError(
                f"TRAFFIC_API_TOKEN must be set to at least {MIN_TOKEN_LENGTH} characters "
                "(e.g. `openssl rand -hex 32`)"
            )
        if self.failure_threshold < 1 or self.recovery_threshold < 1:
            raise SettingsError("health thresholds must be >= 1")
        if self.metrics_token is not None and (
                len(self.metrics_token) < MIN_TOKEN_LENGTH or self.metrics_token == self.api_token):
            raise SettingsError(
                f"METRICS_TOKEN must be at least {MIN_TOKEN_LENGTH} characters and differ from TRAFFIC_API_TOKEN")
        if self.health_interval <= 0 or self.health_timeout <= 0:
            raise SettingsError("health interval/timeout must be > 0")

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ

        def opt(name: str) -> Optional[str]:
            v = e.get(name, "").strip()
            return v or None

        bootstrap = opt("BOOTSTRAP_FILE")
        return cls(
            api_token=e.get("TRAFFIC_API_TOKEN", "").strip(),
            haproxy_socket=e.get("HAPROXY_RUNTIME_SOCKET", cls.haproxy_socket),
            database_path=Path(e.get("DATABASE_PATH", str(cls.database_path))),
            bootstrap_file=Path(bootstrap) if bootstrap else None,
            health_interval=float(e.get("HEALTH_CHECK_INTERVAL_SECONDS", cls.health_interval)),
            health_timeout=float(e.get("HEALTH_CHECK_TIMEOUT_SECONDS", cls.health_timeout)),
            failure_threshold=int(e.get("HEALTH_FAILURE_THRESHOLD", cls.failure_threshold)),
            recovery_threshold=int(e.get("HEALTH_RECOVERY_THRESHOLD", cls.recovery_threshold)),
            recovery_hold_seconds=float(e.get("HEALTH_RECOVERY_HOLD_SECONDS", cls.recovery_hold_seconds)),
            probe_ca_file=opt("PROBE_CA_FILE"),
            socket_timeout=float(e.get("HAPROXY_SOCKET_TIMEOUT_SECONDS", cls.socket_timeout)),
            lock_timeout=float(e.get("LOCK_TIMEOUT_SECONDS", cls.lock_timeout)),
            drain_timeout=float(e.get("DRAIN_TIMEOUT_SECONDS", cls.drain_timeout)),
            haproxy_ca_file=e.get("HAPROXY_CA_FILE", cls.haproxy_ca_file),
            pool_release_settle_seconds=float(e.get("POOL_RELEASE_SETTLE_SECONDS", cls.pool_release_settle_seconds)),
            pool_release_timeout_seconds=float(e.get("POOL_RELEASE_TIMEOUT_SECONDS", cls.pool_release_timeout_seconds)),
            metrics_socket=opt("METRICS_SOCKET"),
            metrics_retention_seconds=float(e.get("METRICS_RETENTION_SECONDS", cls.metrics_retention_seconds)),
            migration_tick_seconds=float(e.get("MIGRATION_TICK_SECONDS", cls.migration_tick_seconds)),
            schedule_grace_seconds=float(e.get("SCHEDULE_GRACE_SECONDS", cls.schedule_grace_seconds)),
            metrics_token=opt("METRICS_TOKEN"),
        )
