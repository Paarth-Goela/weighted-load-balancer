"""Bearer-token authentication for every /api/v1 endpoint.

POC limitation: a single shared token from TRAFFIC_API_TOKEN. There are no
per-user identities, scopes, rotation or expiry, and the HTTP listener has
no TLS (keep it on localhost or behind TLS). `updated_by` records the client
address and an optional, unverified X-Actor header.
"""

from __future__ import annotations

import hmac
import re
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="AdminBearer",
    description="Administrative token from the TRAFFIC_API_TOKEN environment variable.",
)
_ACTOR_RE = re.compile(r"[^A-Za-z0-9@._ -]")


def require_admin(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> None:
    """401 when no bearer token is presented, 403 when it is wrong."""
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    expected = request.app.state.settings.api_token
    if not hmac.compare_digest(credentials.credentials.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid token")


def require_metrics_reader(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> None:
    """GET /metrics: the admin token or the read-only METRICS_TOKEN (401/403 as above)."""
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    st = request.app.state.settings
    presented = credentials.credentials.encode()
    ok = hmac.compare_digest(presented, st.api_token.encode())
    if st.metrics_token:
        ok = hmac.compare_digest(presented, st.metrics_token.encode()) or ok
    if not ok:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid token")


def actor(request: Request) -> str:
    """Audit 'source': client address plus optional (unverified) X-Actor."""
    who = f"api:{request.client.host}" if request.client else "api"
    claimed = request.headers.get("x-actor")
    if claimed:
        who += f" (actor={_ACTOR_RE.sub('', claimed)[:64]})"
    return who
