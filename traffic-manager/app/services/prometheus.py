"""
Prometheus exposition (text format 0.0.4) for the traffic-manager.

Pull model: every scrape reads current state; nothing is pushed and nothing
runs in the request path. Sources:

  * data plane   per-request HAProxy log stream (MetricsCollector): request,
                 error and latency-histogram counters per pool and instance.
  * policy       SQLite policy + HAProxy runtime weights (configured / runtime /
                 effective percent, in-sync flag, version, change counters).
  * health       per-instance monitor state, pool routability, probe and
                 transition counters.
  * HAProxy      reachability, uptime, per-server state/weight/sessions,
                 restarts detected by the reconciler.
  * control loop cycle/failure counters, last cycle duration and timestamp.
  * migrations   active migration status/stage/allocation, totals by status,
                 stage results, scheduler state.

Written by hand (no client library) because the format is small and the
service has no other use for one. Label values are escaped per the spec.
"""

from __future__ import annotations

import time
from typing import Iterable, Optional

from app.models.common import POLICY_BACKEND, POOL_BACKEND, PlatformType
from app.models.migration import ACTIVE_STATUSES
from app.services import audit_service as A
from app.services.haproxy_client import RuntimeAPIError
from app.services.metrics_collector import BACKEND_TO_TYPE, LATENCY_BUCKETS

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
PREFIX = "tm_"
MIGRATION_STATUSES = ("SCHEDULED", "RUNNING", "WAITING_APPROVAL", "PAUSED",
                      "COMPLETED", "FAILED", "CANCELLED", "ROLLED_BACK")
STAGE_STATUSES = ("PENDING", "APPLIED", "PASSED", "FAILED", "CANCELLED")
ADMIN_STATES = ("READY", "MAINT", "DRAIN")


def _esc(v: object) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(v: float) -> str:
    if v == float("inf"):
        return "+Inf"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer() and abs(v) < 1e15):
        return str(int(v))
    return repr(float(v))


class Exposition:
    """Collects families; each family emits HELP/TYPE once, then its samples."""

    def __init__(self) -> None:
        self._families: dict[str, tuple[str, str, list[str]]] = {}

    def add(self, name: str, mtype: str, help_: str, value: Optional[float],
            labels: Optional[dict] = None, suffix: str = "") -> None:
        full = PREFIX + name
        fam = self._families.setdefault(full, (mtype, help_, []))
        if value is None:
            return
        lbl = ""
        if labels:
            lbl = "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items()) + "}"
        fam[2].append(f"{full}{suffix}{lbl} {_fmt(value)}")

    def render(self) -> str:
        out: list[str] = []
        for name, (mtype, help_, samples) in self._families.items():
            out.append(f"# HELP {name} {help_}")
            out.append(f"# TYPE {name} {mtype}")
            out.extend(samples)
        return "\n".join(out) + "\n"


def _platform(backend: str) -> str:
    t = BACKEND_TO_TYPE.get(backend)
    return t.value if t else "unknown"


def collect(state) -> str:
    """Render all metrics from app.state."""
    t0 = time.monotonic()
    x = Exposition()
    x.add("build_info", "gauge", "Traffic-manager build information.", 1,
          {"version": state.app_version})

    _data_plane(x, state.metrics)
    snapshot = state.gateways.runtime_snapshot()
    _haproxy(x, state, snapshot)
    _policy(x, state)
    _health(x, state, snapshot)
    _control(x, state)
    _migrations(x, state)

    db_ok = state.db.ping()
    x.add("database_up", "gauge", "1 if the SQLite database answers a query.", db_ok)
    x.add("scrape_duration_seconds", "gauge", "Time taken to build this exposition.", time.monotonic() - t0)
    return x.render()


# --------------------------------------------------------------- data plane
def _data_plane(x: Exposition, collector) -> None:
    tot = collector.totals()
    x.add("gateway_requests_total", "counter",
          "Requests proxied to a gateway instance, from HAProxy's per-request log stream. "
          "code_class is 1xx..5xx, or none when no status was returned.", None)
    for (backend, server, cls), n in sorted(tot["requests"].items()):
        x.add("gateway_requests_total", "counter", "", n,
              {"platform": _platform(backend), "pool_backend": backend, "instance": server, "code_class": cls})
    x.add("gateway_errors_total", "counter",
          "Requests that failed: HTTP 5xx (incl. HAProxy-generated 502/503/504) or no response.", None)
    for backend, n in sorted(tot["errors"].items()):
        x.add("gateway_errors_total", "counter", "", n, {"platform": _platform(backend)})

    name = "gateway_response_seconds"
    x.add(name, "histogram", "Time for the gateway instance to return response headers (HAProxy Tr).", None)
    for backend in sorted(tot["hist"]):
        plat = _platform(backend)
        cum = 0
        for bound, n in zip(LATENCY_BUCKETS, tot["hist"][backend]):
            cum += n
            x.add(name, "histogram", "", cum, {"platform": plat, "le": _fmt(bound)}, suffix="_bucket")
        x.add(name, "histogram", "", tot["count"][backend], {"platform": plat, "le": "+Inf"}, suffix="_bucket")
        x.add(name, "histogram", "", tot["sum"][backend], {"platform": plat}, suffix="_sum")
        x.add(name, "histogram", "", tot["count"][backend], {"platform": plat}, suffix="_count")

    # Pre-computed 60 s window (exact nearest-rank p95, same numbers the console
    # and the migration engine use). Absent when the window had no requests.
    recent = collector.recent(60.0)
    x.add("gateway_window_requests", "gauge", "Requests per pool in the last 60 s.", None)
    x.add("gateway_window_error_ratio", "gauge", "Error ratio (0..1) per pool in the last 60 s.", None)
    x.add("gateway_window_p95_seconds", "gauge", "Exact p95 response time per pool in the last 60 s.", None)
    for plat, w in recent.items():
        x.add("gateway_window_requests", "gauge", "", w["requests"], {"platform": plat})
        if w["requests"]:
            x.add("gateway_window_error_ratio", "gauge", "", w["errors"] / w["requests"], {"platform": plat})
        if w["p95_ms"] is not None:
            x.add("gateway_window_p95_seconds", "gauge", "", w["p95_ms"] / 1000.0, {"platform": plat})

    x.add("metrics_stream_listening", "gauge", "1 if the HAProxy log-stream socket is bound.", collector.listening)
    x.add("metrics_stream_events_total", "counter", "Log-stream lines accepted.", tot["received"])
    x.add("metrics_stream_malformed_total", "counter", "Log-stream lines rejected as malformed.", tot["malformed"])
    x.add("metrics_stream_last_event_timestamp_seconds", "gauge", "Unix time of the last accepted line.",
          tot["last_event_at"])


# ------------------------------------------------------------------ HAProxy
def _haproxy(x: Exposition, state, snapshot: Optional[dict]) -> None:
    info = None
    try:
        info = state.client.info()
    except RuntimeAPIError:
        pass
    x.add("haproxy_up", "gauge", "1 if the HAProxy Runtime API answered.", info is not None)
    if info:
        x.add("haproxy_info", "gauge", "HAProxy version.", 1, {"version": info.get("Version", "")})
        up = info.get("Uptime_sec", "")
        x.add("haproxy_uptime_seconds", "gauge", "HAProxy process uptime.", int(up) if up.isdigit() else None)
    x.add("haproxy_restarts_detected_total", "counter",
          "HAProxy restarts detected by the reconciler (Start_time_sec changed).",
          state.reconciler.restarts_detected_total)
    if snapshot is None:
        return
    stats = snapshot.get("stats") or {}
    x.add("haproxy_server_up", "gauge", "1 if HAProxy reports the server operationally UP.", None)
    x.add("haproxy_server_admin_state", "gauge", "Administrative state (one-hot: READY, MAINT, DRAIN).", None)
    x.add("haproxy_server_weight", "gauge", "Current server weight (traffic_policy servers = pool split).", None)
    x.add("haproxy_server_current_sessions", "gauge", "Current sessions on the server.", None)
    x.add("haproxy_server_sessions_total", "counter", "Sessions since HAProxy start (resets on restart).", None)
    for backend, servers in sorted(snapshot["servers"].items()):
        for name, srv in sorted(servers.items()):
            lbl = {"backend": backend, "server": name}
            x.add("haproxy_server_up", "gauge", "", srv.op_state == 2, lbl)
            for st in ADMIN_STATES:
                x.add("haproxy_server_admin_state", "gauge", "", srv.admin == st, {**lbl, "state": st})
            x.add("haproxy_server_weight", "gauge", "", srv.weight, lbl)
            row = stats.get((backend, name))
            if row:
                for key, metric in (("scur", "haproxy_server_current_sessions"),
                                    ("stot", "haproxy_server_sessions_total")):
                    v = row.get(key, "")
                    if v.isdigit():
                        x.add(metric, "gauge" if key == "scur" else "counter", "", int(v), lbl)


# ------------------------------------------------------------------- policy
def _policy(x: Exposition, state) -> None:
    cfg = state.traffic.get_config()
    pol, rt = cfg["policy"], cfg["runtime"]
    x.add("traffic_policy_percent", "gauge",
          "Pool share in percent. source=configured (SQLite policy), runtime (HAProxy weight), "
          "effective (share after excluding DOWN pools).", None)
    for p in cfg["pools"]:
        plat = p["platform_type"]
        key = "opdk_weight" if plat == PlatformType.APIGEE_OPDK.value else "hybrid_weight"
        if pol:
            x.add("traffic_policy_percent", "gauge", "", pol[key], {"platform": plat, "source": "configured"})
        if rt:
            x.add("traffic_policy_percent", "gauge", "", rt[key], {"platform": plat, "source": "runtime"})
        if p.get("effective_percent") is not None:
            x.add("traffic_policy_percent", "gauge", "", p["effective_percent"],
                  {"platform": plat, "source": "effective"})
    x.add("traffic_policy_in_sync", "gauge",
          "1 if HAProxy runtime weights equal the active policy (absent if unknown).",
          None if cfg["in_sync"] is None else cfg["in_sync"])
    x.add("traffic_policy_version", "gauge", "Active traffic policy version (row id).",
          pol["id"] if pol else None)
    rows = state.db.query(
        "SELECT event_type, status, COUNT(*) AS n FROM audit_log WHERE event_type IN ({}) "
        "GROUP BY event_type, status ORDER BY event_type, status".format(",".join("?" * len(A.TRAFFIC_EVENTS))),
        tuple(A.TRAFFIC_EVENTS))
    x.add("traffic_policy_changes_total", "counter",
          "Audited traffic-policy events (change, rollback, restore, emergency) by outcome.", None)
    for r in rows:
        x.add("traffic_policy_changes_total", "counter", "", r["n"],
              {"event": r["event_type"], "status": r["status"]})


# ------------------------------------------------------------------- health
def _health(x: Exposition, state, snapshot: Optional[dict]) -> None:
    gw = state.gateways
    x.add("pool_routable_instances", "gauge", "Instances that are enabled, HEALTHY, READY and UP.", None)
    x.add("pool_instances", "gauge", "Enabled instances per pool by monitor health state.", None)
    x.add("pool_up", "gauge", "1 if HAProxy's pool-level check reports the pool UP.", None)
    x.add("pool_status", "gauge", "Pool status (one-hot: HEALTHY, DEGRADED, DOWN, DISABLED).", None)
    for ptype in PlatformType:
        pool = gw.pool_by_type(ptype)
        if not pool:
            continue
        h = gw.pool_health(pool, snapshot)
        lbl = {"platform": ptype.value}
        x.add("pool_routable_instances", "gauge", "", h["routable_instances"], lbl)
        for st, key in (("HEALTHY", "healthy_instances"), ("UNHEALTHY", "unhealthy_instances"),
                        ("UNKNOWN", "unknown_instances")):
            x.add("pool_instances", "gauge", "", h[key], {**lbl, "state": st})
        if h["haproxy_pool_up"] is not None:
            x.add("pool_up", "gauge", "", h["haproxy_pool_up"], lbl)
        for st in ("HEALTHY", "DEGRADED", "DOWN", "DISABLED"):
            x.add("pool_status", "gauge", "", h["status"] == st, {**lbl, "status": st})

    x.add("instance_healthy", "gauge", "1 if the health monitor reports the instance HEALTHY.", None)
    x.add("instance_enabled", "gauge", "1 if the instance is administratively enabled.", None)
    x.add("instance_routable", "gauge", "1 if the instance can receive traffic now.", None)
    x.add("instance_consecutive_failures", "gauge", "Consecutive failed readiness probes.", None)
    for inst in gw.all_instance_rows():
        backend = gw.backend_of(inst)
        srv = snapshot["servers"].get(backend, {}).get(inst["name"]) if snapshot else None
        lbl = {"platform": inst["platform_type"], "instance": inst["name"]}
        x.add("instance_healthy", "gauge", "", inst["current_health_status"] == "HEALTHY", lbl)
        x.add("instance_enabled", "gauge", "", bool(inst["enabled"]), lbl)
        x.add("instance_routable", "gauge", "", gw.is_routable(inst, srv), lbl)
        x.add("instance_consecutive_failures", "gauge", "", inst["consecutive_failures"], lbl)

    mon = state.monitor
    x.add("health_probes_total", "counter", "Readiness probes by result.", None)
    for res, n in sorted(mon.probes_total.items()):
        x.add("health_probes_total", "counter", "", n, {"result": res})
    x.add("health_transitions_total", "counter", "Instance health transitions by new state.", None)
    for to, n in sorted(mon.transitions_total.items()):
        x.add("health_transitions_total", "counter", "", n, {"to": to})


# ------------------------------------------------------------- control loop
def _control(x: Exposition, state) -> None:
    loop, mon, rec = state.control_loop, state.monitor, state.reconciler
    x.add("control_loop_running", "gauge", "1 if the probe/reconcile thread is alive.",
          bool(loop and loop.running))
    x.add("control_loop_cycles_total", "counter", "Completed probe/reconcile cycles.", mon.cycles_total)
    x.add("control_loop_failures_total", "counter", "Cycles that raised an exception.",
          loop.failures_total if loop else 0)
    x.add("control_loop_last_cycle_duration_seconds", "gauge", "Duration of the last cycle.",
          mon.last_cycle_seconds)
    x.add("control_loop_last_cycle_timestamp_seconds", "gauge", "Unix time the last cycle finished.",
          mon.last_cycle_end)
    x.add("reconcile_runs_total", "counter", "Reconciler runs.", rec.runs_total)
    x.add("reconcile_actions_total", "counter", "Corrective runtime actions taken by the reconciler.",
          rec.actions_total)
    x.add("reconcile_errors_total", "counter", "Reconciler Runtime API errors.", rec.errors_total)
    x.add("reconcile_haproxy_unreachable_total", "counter", "Reconciler runs that could not reach HAProxy.",
          rec.unreachable_total)
    x.add("pool_hold_active", "gauge", "1 while both pools are held after an HAProxy restart.",
          rec.pool_hold_since is not None)


# --------------------------------------------------------------- migrations
def _migrations(x: Exposition, state) -> None:
    mgr = state.migrations
    m = mgr.active()
    x.add("migration_active", "gauge", "1 if a migration is SCHEDULED, RUNNING, WAITING_APPROVAL or PAUSED.",
          m is not None)
    x.add("migration_active_status", "gauge", "Status of the active migration (one-hot).", None)
    for st in ACTIVE_STATUSES:
        x.add("migration_active_status", "gauge", "", bool(m and m["status"] == st), {"status": st})
    if m:
        lbl = {"id": m["id"], "mode": m["mode"], "source": m["source_platform"],
               "destination": m["destination_platform"]}
        x.add("migration_active_info", "gauge", "Active migration identity.", 1, {**lbl, "name": m["name"]})
        x.add("migration_current_opdk_percent", "gauge", "Allocation (OPDK %) the migration last applied.",
              m["current_opdk"])
        x.add("migration_target_opdk_percent", "gauge", "Migration target allocation (OPDK %).", m["target_opdk"])
        x.add("migration_last_good_opdk_percent", "gauge", "Last allocation that passed its checks (OPDK %).",
              m["last_good_opdk"])
        x.add("migration_current_stage", "gauge", "Sequence number of the last applied stage.", m["current_stage"])
        x.add("migration_stages_planned", "gauge", "Non-cancelled stages in the active migration.",
              m["total_stages"])
        x.add("migration_progress_percent", "gauge", "Progress of the active migration (last good vs target).",
              m["progress_percent"])
    db = state.db
    counts = {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) AS n FROM traffic_migrations GROUP BY status")}
    x.add("migrations", "gauge", "Migrations stored, by status.", None)
    for st in MIGRATION_STATUSES:
        x.add("migrations", "gauge", "", counts.get(st, 0), {"status": st})
    stages = {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) AS n FROM traffic_migration_stages GROUP BY status")}
    x.add("migration_stages", "gauge", "Migration stages stored, by status (FAILED = safety violation "
          "or failed apply).", None)
    for st in STAGE_STATUSES:
        x.add("migration_stages", "gauge", "", stages.get(st, 0), {"status": st})
    sched = state.migration_scheduler
    x.add("migration_scheduler_running", "gauge", "1 if the migration scheduler thread is alive.",
          bool(sched and sched.running))
    x.add("migration_tick_errors_total", "counter", "Scheduler ticks that raised an exception.",
          mgr.tick_errors_total)


def iter_lines(text: str) -> Iterable[str]:   # small helper for tests
    return (ln for ln in text.splitlines() if ln and not ln.startswith("#"))
