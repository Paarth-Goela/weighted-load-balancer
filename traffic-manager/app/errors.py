"""Service errors mapped to HTTP responses ({"status":"error","error":code,"detail":...})."""

from __future__ import annotations


class ServiceError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFound(ServiceError):
    status_code = 404
    code = "not_found"


class Conflict(ServiceError):
    status_code = 409
    code = "conflict"


class PoolNotEligible(ServiceError):
    """Target pool has no routable (enabled + healthy + ready) instance."""
    status_code = 409
    code = "pool_not_eligible"


class InvalidRequest(ServiceError):
    status_code = 422
    code = "invalid_request"


class HAProxyUnavailable(ServiceError):
    status_code = 503
    code = "haproxy_unavailable"


class Busy(ServiceError):
    status_code = 503
    code = "update_in_progress"


class HAProxyUpdateFailed(ServiceError):
    """HAProxy rejected/failed the change; previous runtime state restored."""
    status_code = 502
    code = "haproxy_update_failed"


class InconsistentState(ServiceError):
    """A change failed AND compensation failed; the control loop will repair it."""
    status_code = 500
    code = "inconsistent_state"


class PersistenceFailed(ServiceError):
    status_code = 500
    code = "persistence_failed"
