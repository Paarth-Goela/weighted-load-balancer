"""Shared enums, constants and validators.

Every value that can reach an HAProxy Runtime API command (server names,
hosts, SNI names) is validated here against a strict allow-list. This
prevents command injection: the Runtime API treats ';' and newlines as
command separators.
"""

from __future__ import annotations

import ipaddress
import re
from enum import Enum
from typing import Annotated, Any, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


class PlatformType(str, Enum):
    APIGEE_OPDK = "APIGEE_OPDK"
    APIGEE_HYBRID = "APIGEE_HYBRID"


class HealthStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    UNHEALTHY = "UNHEALTHY"


Protocol = Literal["HTTP", "HTTPS"]

# HAProxy object names (see haproxy/haproxy.cfg)
POLICY_BACKEND = "traffic_policy"
POLICY_SERVER = {PlatformType.APIGEE_OPDK: "opdk", PlatformType.APIGEE_HYBRID: "hybrid"}
POOL_BACKEND = {PlatformType.APIGEE_OPDK: "apigee_opdk_pool", PlatformType.APIGEE_HYBRID: "apigee_hybrid_pool"}

_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_DISPLAY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,79}$")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?"
_HOSTNAME_RE = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*$")
_PATH_RE = re.compile(r"^/[\x21-\x7e]{0,254}$")


def check_server_name(v: str) -> str:
    if not _SERVER_NAME_RE.match(v):
        raise ValueError("must be 1-63 chars: letters, digits, '.', '_', '-' (start alphanumeric)")
    return v


def check_display_name(v: str) -> str:
    if not _DISPLAY_NAME_RE.match(v):
        raise ValueError("must be 1-80 chars: letters, digits, space, '.', '_', '-', '(', ')'")
    return v


def is_ipv4(v: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(v), ipaddress.IPv4Address)
    except ValueError:
        return False


def check_host(v: str) -> str:
    if is_ipv4(v) or _HOSTNAME_RE.match(v):
        return v
    raise ValueError("must be a DNS hostname or IPv4 address (IPv6 not supported yet)")


def check_hostname(v: str) -> str:
    if not _HOSTNAME_RE.match(v):
        raise ValueError("must be a DNS hostname")
    return v


def check_host_header(v: str) -> str:
    host, _, port = v.partition(":")
    check_host(host)
    if port and not (port.isdigit() and 1 <= int(port) <= 65535):
        raise ValueError("port in Host header must be 1-65535")
    return v


def check_path(v: str) -> str:
    if not _PATH_RE.match(v):
        raise ValueError("must start with '/' and contain only visible ASCII characters (max 255)")
    return v


def check_status_codes(v: list[int]) -> list[int]:
    if not v:
        raise ValueError("at least one status code is required")
    if len(set(v)) != len(v):
        raise ValueError("status codes must be unique")
    return sorted(v)


ServerName = Annotated[str, AfterValidator(check_server_name)]
DisplayName = Annotated[str, AfterValidator(check_display_name)]
Host = Annotated[str, AfterValidator(check_host)]
Hostname = Annotated[str, AfterValidator(check_hostname)]
HostHeader = Annotated[str, AfterValidator(check_host_header)]
HealthPath = Annotated[str, AfterValidator(check_path)]
StatusCode = Annotated[int, Field(strict=True, ge=100, le=599)]
StatusCodes = Annotated[list[StatusCode], Field(max_length=20), AfterValidator(check_status_codes)]
Description = Annotated[str, Field(max_length=500)]
Reason = Annotated[Optional[str], Field(None, max_length=500, description="Why this change is made (audited).")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    error: str = Field(description="Machine-readable error code.")
    detail: Any = Field(description="Human-readable explanation or validation errors.")


AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing bearer token."},
    403: {"model": ErrorResponse, "description": "Invalid bearer token."},
}
