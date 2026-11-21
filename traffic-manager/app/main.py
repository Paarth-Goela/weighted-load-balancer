"""
Traffic Management API: application factory.

    uvicorn --factory app.main:create_app --host 0.0.0.0 --port 9000 --workers 1

Control plane only. API consumers -> HAProxy :8080 -> gateway pools. This
service never sits in the request path. It talks to HAProxy through the
private Runtime API socket.

Run a single worker: the control lock is per process.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import gateways, health, instances, pools, traffic
from app.bootstrap import run_bootstrap
from app.database.connection import Database
from app.errors import ServiceError
from app.security import actor
from app.services import audit_service as A
from app.services.audit_service import AuditService
from app.services.control import ControlLock, Resolver
from app.services.gateway_manager import GatewayManager
from app.services.haproxy_client import HAProxyClient, unix_socket_transport
from app.services.health_monitor import HealthMonitor, Prober
from app.services.reconciler import Reconciler
from app.services.traffic_manager import TrafficManager
from app.settings import Settings

log = logging.getLogger("traffic_manager")

DESCRIPTION = """
Control plane for an HAProxy Layer 7 traffic layer in front of **Apigee OPDK** and
**Apigee Hybrid** gateway pools. It registers gateways, pools and instances, monitors
instance readiness, and shifts traffic between the pools without restarting HAProxy.

* **Auth:** every `/api/v1/*` endpoint needs `Authorization: Bearer <TRAFFIC_API_TOKEN>`.
  A missing token returns **401**; a wrong token returns **403**. Only `/livez` and these docs are public.
* **Traffic policy:** `opdk_weight` + `hybrid_weight` = 100 (integers). This pool-level split is
  separate from instance weights, which only spread a pool's share across its own instances.
* **Safety:** changes are serialized, checked for pool eligibility, applied in a safe order,
  read back from HAProxy, rolled back on failure, versioned in SQLite and audited.
* **Health:** readiness probes with failure/recovery thresholds. Unhealthy instances are put in
  MAINT; recovered ones rejoin automatically.

Local simulators stand in for OPDK routers and Hybrid ingress. **They are not Apigee.**
This service shifts traffic. It does not migrate API proxies or verify that OPDK and
Hybrid behave identically.
"""


class ControlLoop:
    """Background thread: probe -> transitions -> reconcile, every interval (or when woken)."""

    def __init__(self, monitor: HealthMonitor, interval: float) -> None:
        self.monitor = monitor
        self.interval = interval
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.monitor.run_cycle()
                except Exception:
                    log.exception("control loop cycle failed")
                self._wake.wait(self.interval)
                self._wake.clear()

        self._thread = threading.Thread(target=loop, name="control-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)


def create_app(
    settings: Optional[Settings] = None,
    transport: Optional[Callable[[str], str]] = None,
    resolver: Optional[Resolver] = None,
    prober: Optional[Prober] = None,
) -> FastAPI:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = settings or Settings.from_env()
    resolver = resolver or Resolver()

    db = Database(settings.database_path)
    client = HAProxyClient(
        transport or unix_socket_transport(settings.haproxy_socket, settings.socket_timeout),
        ca_file=settings.haproxy_ca_file,
        check_inter=settings.native_check_inter,
    )
    audit = AuditService(db)
    lock = ControlLock(settings.lock_timeout)
    loop_ref: dict = {}
    gw = GatewayManager(db, client, audit, lock, resolver, settings.drain_timeout,
                        on_change=lambda: loop_ref["loop"].wake() if loop_ref.get("loop") else None)
    tm = TrafficManager(db, client, audit, lock, gw)
    rec = Reconciler(client, gw, tm, audit, lock, resolver,
                     settle_seconds=settings.pool_release_settle_seconds,
                     release_timeout=settings.pool_release_timeout_seconds)
    monitor = HealthMonitor(
        db, gw, rec, audit, lock,
        prober or Prober(settings.health_timeout, settings.probe_ca_file, resolver),
        failure_threshold=settings.failure_threshold,
        recovery_threshold=settings.recovery_threshold,
        recovery_hold_seconds=settings.recovery_hold_seconds,
    )
    control_loop = ControlLoop(monitor, settings.health_interval) if settings.run_background else None
    loop_ref["loop"] = control_loop

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        applied = db.migrate()
        log.info("database migrations applied: %s (schema %s)", applied or "none", db.schema_version())
        app.state.bootstrap_result = run_bootstrap(settings.bootstrap_file, gw, tm, audit)
        log.info("bootstrap: %s", app.state.bootstrap_result)
        log.info("startup reconcile: %s", rec.reconcile())  # restore runtime state from SQLite
        if control_loop:
            control_loop.start()
        yield
        if control_loop:
            control_loop.stop()
        db.close()

    app = FastAPI(title="Apigee OPDK / Hybrid Traffic Management API", version="3.0.0",
                  description=DESCRIPTION, lifespan=lifespan)
    st = app.state
    st.settings, st.db, st.client, st.audit, st.lock = settings, db, client, audit, lock
    st.gateways, st.traffic, st.reconciler, st.monitor = gw, tm, rec, monitor
    st.control_loop, st.bootstrap_result = control_loop, None

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError):
        headers = {"Retry-After": "2"} if exc.status_code == 503 else None
        return JSONResponse(status_code=exc.status_code, headers=headers,
                            content={"status": "error", "error": exc.code, "detail": exc.detail})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        codes = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}
        return JSONResponse(status_code=exc.status_code, headers=getattr(exc, "headers", None),
                            content={"status": "error", "error": codes.get(exc.status_code, "http_error"),
                                     "detail": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        # Authentication runs before body validation, so a 422 here comes from an
        # authenticated caller. Record rejected traffic-policy attempts.
        if request.method == "PUT" and request.url.path == "/api/v1/traffic/config":
            body = getattr(exc, "body", None)
            requested = ({k: body.get(k) for k in ("opdk_weight", "hybrid_weight")}
                         if isinstance(body, dict) else {"body": repr(body)[:200]})
            audit.record(A.TRAFFIC_POLICY_CHANGE, "REJECTED", actor(request), entity_type="traffic_policy",
                         new=requested, error="; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errors))
        return JSONResponse(status_code=422, content=jsonable_encoder(
            {"status": "error", "error": "validation_error", "detail": errors}))

    app.include_router(health.public)
    app.include_router(health.router)
    app.include_router(gateways.router)
    app.include_router(pools.router)
    app.include_router(instances.router)
    app.include_router(traffic.router)
    return app
