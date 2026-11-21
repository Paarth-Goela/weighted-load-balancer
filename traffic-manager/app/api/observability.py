"""Prometheus scrape endpoint.

GET /metrics returns the text exposition format. It needs a bearer token:
either the read-only METRICS_TOKEN (recommended for Prometheus; it cannot
call any other endpoint) or the admin token.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app.models.common import AUTH_RESPONSES
from app.security import require_metrics_reader
from app.services import prometheus

router = APIRouter(tags=["observability"], responses=AUTH_RESPONSES)


@router.get("/metrics", summary="Prometheus metrics", dependencies=[Depends(require_metrics_reader)],
            response_class=Response,
            responses={200: {"content": {prometheus.CONTENT_TYPE.split(";")[0]: {}},
                             "description": "Prometheus text exposition format 0.0.4"}})
def metrics(request: Request) -> Response:
    return Response(prometheus.collect(request.app.state), media_type=prometheus.CONTENT_TYPE)
