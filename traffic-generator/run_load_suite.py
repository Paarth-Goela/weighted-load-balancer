"""
Sustained-load and failure test suite for the OPDK / Hybrid traffic layer
(LOCAL SIMULATION). Standard library only; run from the repository root:

    python3 traffic-generator/run_load_suite.py                     # all scenarios (~12 min)
    python3 traffic-generator/run_load_suite.py -s sustained -s instance_crash
    python3 traffic-generator/run_load_suite.py --set sustained.duration_seconds=300 --report-dir reports/load

Every scenario drives real HTTP load through HAProxy :8080 while it changes
the policy (through the management API) or injects a fault, then checks
measured values against thresholds (load_thresholds.json, override with
--set). Results:

    <report-dir>/<UTC timestamp>/summary.json     machine-readable: every check, measured value, limit
    <report-dir>/<UTC timestamp>/junit.xml        one testcase per scenario (for CI)
    <report-dir>/<UTC timestamp>/<scenario>.timeline.csv   per-second requests/errors/p95/pool/instance

Exit code: 0 all scenarios passed, 1 a check failed, 2 a scenario could not run.

The suite records the active traffic policy at start and restores it (and
clears every injected fault, restarts any killed container) at the end, also
on Ctrl+C. It refuses to start while a migration is active.

Simulators are not Apigee: these numbers describe this local stack, not real
gateway capacity.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import (POOL_INSTANCES, ApiError, Faults, TrafficManagerAPI, metric_sum,  # noqa: E402
                     wait_until)
from loadgen import LoadRun, Phase, RunResult, is_error, ramp, spike, sustained  # noqa: E402

HERE = Path(__file__).resolve().parent
OPDK, HYBRID = "APIGEE_OPDK", "APIGEE_HYBRID"
POOLS = (OPDK, HYBRID)


# ================================================================== results
class Scenario:
    def __init__(self, name: str, description: str, cfg: dict) -> None:
        self.name, self.description, self.cfg = name, description, cfg
        self.checks: List[dict] = []
        self.measured: Dict[str, object] = {}
        self.notes: List[str] = []
        self.load: Optional[dict] = None
        self.markers: List[dict] = []
        self.error: Optional[str] = None
        self.started = time.time()
        self.seconds = 0.0

    def check(self, name: str, measured, op: str, limit, unit: str = "") -> bool:
        if measured is None:
            ok = False
        elif op == "<=":
            ok = measured <= limit
        elif op == ">=":
            ok = measured >= limit
        elif op == "==":
            ok = measured == limit
        elif op == "between":
            ok = limit[0] <= measured <= limit[1]
        else:
            raise ValueError(op)
        self.checks.append({"check": name, "measured": measured, "op": op, "limit": limit, "unit": unit,
                            "passed": ok})
        return ok

    @property
    def verdict(self) -> str:
        if self.error:
            return "ERROR"
        return "PASS" if self.checks and all(c["passed"] for c in self.checks) else "FAIL"

    def to_json(self) -> dict:
        return {"scenario": self.name, "description": self.description, "verdict": self.verdict,
                "seconds": round(self.seconds, 1), "config": self.cfg, "checks": self.checks,
                "measured": self.measured, "markers": self.markers, "notes": self.notes,
                "load": self.load, "error": self.error}


# ================================================================== helpers
class Ctx:
    def __init__(self, api: TrafficManagerAPI, faults: Faults, out: Path, host: str, port: int) -> None:
        self.api, self.faults, self.out, self.host, self.port = api, faults, out, host, port

    def load(self, phases: List[Phase]) -> LoadRun:
        return LoadRun(phases, self.host, self.port)

    def baseline(self, opdk: int) -> None:
        """Clean state: no faults, no migration, policy applied, all 4 instances routable."""
        problems = self.faults.restore_all()
        if problems:
            raise RuntimeError("could not reset simulators: " + "; ".join(problems))
        self.api.cancel_active("load-test baseline")
        wait_until(lambda: all(g["health"]["routable_instances"] == g["health"]["total_instances"] > 0
                               for g in self.api.gateways().values()), 60, "all instances routable")
        if self.api.policy() != (opdk, 100 - opdk):
            self.api.set_policy(opdk, "load-test baseline")
        wait_until(lambda: self.api.config()["in_sync"], 15, "policy in sync")

    def save(self, sc: Scenario, run: RunResult, suffix: str = "") -> None:
        run.write_timeline_csv(str(self.out / f"{sc.name}{suffix}.timeline.csv"))


def at(run: LoadRun, t: float) -> None:
    """Sleep until the load run's clock reaches t seconds."""
    while run.now() < t:
        time.sleep(min(0.05, max(0.0, t - run.now())))


def prom_counts(api: TrafficManagerAPI) -> Dict[str, float]:
    s = api.metrics()
    out = {}
    for p in POOLS:
        out[p + "_requests"] = metric_sum(s, "tm_gateway_requests_total", platform=p)
        out[p + "_errors"] = metric_sum(s, "tm_gateway_errors_total", platform=p)
    return out


def metrics_agreement(sc: Scenario, api: TrafficManagerAPI, before: dict, after: dict,
                      run: RunResult, limit_pct: float) -> None:
    """Client-observed counts vs. the traffic-manager's Prometheus counters (same requests)."""
    deadline = time.time() + 5.0
    while True:
        worst = 0.0
        measured = {}
        for p in POOLS:
            client_req = sum(1 for s in run.samples if s.pool == p)
            client_err = sum(1 for s in run.samples if s.pool == p and is_error(s.status))
            prom_req = after[p + "_requests"] - before[p + "_requests"]
            prom_err = after[p + "_errors"] - before[p + "_errors"]
            measured[f"{p}_client_vs_prometheus"] = {
                "client_requests": client_req,
                "prometheus_requests": prom_req,
                "client_errors": client_err,
                "prometheus_errors": prom_err,
            }
            if client_req:
                worst = max(worst, 100.0 * abs(prom_req - client_req) / client_req)
            if client_err:
                worst = max(worst, 100.0 * abs(prom_err - client_err) / client_err)
        sc.measured.update(measured)
        if worst <= limit_pct or time.time() >= deadline:
            break
        time.sleep(0.25)
        after = prom_counts(api)


def share_deviation(summary: dict, opdk: int) -> Optional[float]:
    got = summary["share_percent"][OPDK]
    return None if got is None else round(abs(got - opdk), 3)


def last_error_after(run: RunResult, t: float, end: float = float("inf"), pool: Optional[str] = None
                     ) -> Optional[float]:
    errs = [s.t for s in run.samples if t <= s.t < end and is_error(s.status) and (pool is None or s.pool == pool)]
    return max(errs) if errs else None


def first_served_after(run: RunResult, instance: str, t: float) -> Optional[float]:
    return next((s.t for s in run.samples if s.t >= t and s.instance == instance and not is_error(s.status)), None)


def last_served_before(run: RunResult, instance: str, t0: float, t1: float) -> Optional[float]:
    hits = [s.t for s in run.samples if t0 <= s.t < t1 and s.instance == instance]
    return max(hits) if hits else None


def pool_errors(run: RunResult, pool: str, start: float = 0.0, end: float = float("inf")) -> int:
    return sum(1 for s in run.samples if start <= s.t < end and s.pool == pool and is_error(s.status))


def watch(fn: Callable[[], None], interval: float, stop: threading.Event) -> threading.Thread:
    def loop():
        while not stop.is_set():
            try:
                fn()
            except Exception:                       # noqa: BLE001 - observation only
                pass
            stop.wait(interval)
    th = threading.Thread(target=loop, daemon=True)
    th.start()
    return th


def stack_reachable(api: str, host: str, port: int) -> bool:
    """Management API /livez answers and HAProxy accepts TCP connections."""
    import socket
    import urllib.request
    try:
        with urllib.request.urlopen(api.rstrip("/") + "/livez", timeout=3):
            pass
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


# ================================================================ scenarios
def s_baseline_compare(ctx: Ctx, sc: Scenario) -> None:
    """Identical closed-loop load against 100:0, then 0:100; compare throughput and latency."""
    c = sc.cfg
    results = {}
    for pool, opdk in ((OPDK, 100), (HYBRID, 0)):
        ctx.baseline(opdk)
        run = ctx.load(sustained(c["duration_seconds"], c["concurrency"])).run()
        ctx.save(sc, run, f".{pool.lower()}")
        s = run.summary()["overall"]
        results[pool] = s
        sc.check(f"{pool} error rate", s["error_rate_percent"], "<=", c["max_error_rate_percent"], "%")
        sc.check(f"{pool} p95 latency", s["latency_ms"]["p95"], "<=", c["max_p95_ms"], "ms")
    o, h = results[OPDK], results[HYBRID]
    sc.measured["comparison"] = {
        p: {"throughput_rps": results[p]["throughput_rps"], "error_rate_percent": results[p]["error_rate_percent"],
            **{k: results[p]["latency_ms"][k] for k in ("p50", "p95", "p99", "max")}} for p in POOLS}
    ratio = round(h["latency_ms"]["p95"] / o["latency_ms"]["p95"], 3) if o["latency_ms"]["p95"] else None
    tput = round(h["throughput_rps"] / o["throughput_rps"], 3) if o["throughput_rps"] else None
    sc.measured["hybrid_vs_opdk"] = {"p95_ratio": ratio, "throughput_ratio": tput}
    sc.check("Hybrid/OPDK p95 ratio", ratio, "<=", c["max_p95_ratio"])
    sc.check("Hybrid/OPDK throughput ratio", tput, ">=", c["min_throughput_ratio"])
    sc.load = {"opdk_only": o, "hybrid_only": h}


def s_sustained(ctx: Ctx, sc: Scenario) -> None:
    """Constant concurrency at a fixed split; latency, throughput, split accuracy, metrics accuracy."""
    c = sc.cfg
    ctx.baseline(c["opdk_percent"])
    before = prom_counts(ctx.api)
    run = ctx.load(sustained(c["duration_seconds"], c["concurrency"])).run()
    time.sleep(1.0)
    after = prom_counts(ctx.api)
    ctx.save(sc, run)
    s = run.summary()
    sc.load = s
    o = s["overall"]
    sc.check("error rate", o["error_rate_percent"], "<=", c["max_error_rate_percent"], "%")
    sc.check("p95 latency", o["latency_ms"]["p95"], "<=", c["max_p95_ms"], "ms")
    sc.check("throughput", o["throughput_rps"], ">=", c["min_throughput_rps"], "req/s")
    sc.check("OPDK share deviation from policy", share_deviation(s, c["opdk_percent"]), "<=",
             c["max_share_deviation_pp"], "pp")
    metrics_agreement(sc, ctx.api, before, after, run, c["max_metrics_disagreement_percent"])


def s_ramp(ctx: Ctx, sc: Scenario) -> None:
    """Concurrency rises step by step from start to end; errors and p95 must stay within limits."""
    c = sc.cfg
    ctx.baseline(c["opdk_percent"])
    run = ctx.load(ramp(c["duration_seconds"], c["start_concurrency"], c["end_concurrency"],
                        steps=8)).run()
    ctx.save(sc, run)
    sc.load = run.summary()
    sc.measured["per_step"] = [{"concurrency": p["concurrency"], "throughput_rps": p["throughput_rps"],
                                "error_rate_percent": p["error_rate_percent"], "p95_ms": p["latency_ms"]["p95"]}
                               for p in run.phase_summaries()]
    o = sc.load["overall"]
    sc.check("error rate", o["error_rate_percent"], "<=", c["max_error_rate_percent"], "%")
    sc.check("p95 latency", o["latency_ms"]["p95"], "<=", c["max_p95_ms"], "ms")


def s_spike(ctx: Ctx, sc: Scenario) -> None:
    """Base load, sudden spike, base load again; p95 during and after the spike."""
    c = sc.cfg
    ctx.baseline(c["opdk_percent"])
    d = c["phase_seconds"]
    run = ctx.load(spike(c["base_concurrency"], c["peak_concurrency"], d, d, d)).run()
    ctx.save(sc, run)
    sc.load = run.summary()
    phases = run.phase_summaries()
    sc.measured["phases"] = [{"phase": p["phase"], "concurrency": p["concurrency"],
                              "throughput_rps": p["throughput_rps"], "p95_ms": p["latency_ms"]["p95"],
                              "error_rate_percent": p["error_rate_percent"]} for p in phases]
    sc.check("error rate", sc.load["overall"]["error_rate_percent"], "<=", c["max_error_rate_percent"], "%")
    sc.check("p95 during spike", phases[1]["latency_ms"]["p95"], "<=", c["max_spike_p95_ms"], "ms")
    sc.check("p95 after spike (recovery)", phases[2]["latency_ms"]["p95"], "<=", c["max_after_spike_p95_ms"], "ms")


def s_instance_unready(ctx: Ctx, sc: Scenario) -> None:
    """Readiness fails on one Hybrid instance: graceful removal, no client errors, automatic rejoin."""
    c = sc.cfg
    inst = POOL_INSTANCES[HYBRID][0]
    ctx.baseline(c["opdk_percent"])
    states: List[tuple] = []
    run = ctx.load(sustained(c["duration_seconds"], c["concurrency"])).start()
    stop = threading.Event()
    watch(lambda: states.append((run.now(), ctx.api.instances()[inst]["health_status"])), 0.5, stop)
    at(run, c["fault_at"])
    t_fault = run.mark("readiness_fail", instance=inst)
    ctx.faults.set_ready([inst], False)
    at(run, c["restore_at"])
    t_restore = run.mark("readiness_restore", instance=inst)
    ctx.faults.set_ready([inst], True)
    res = run.wait()
    stop.set()
    ctx.save(sc, res)
    sc.load, sc.markers = res.summary(), res.markers
    last = last_served_before(res, inst, t_fault, t_restore)
    removal = round(last - t_fault, 2) if last is not None else 0.0
    back = first_served_after(res, inst, t_restore)
    unhealthy_at = next((t for t, st in states if t >= t_fault and st == "UNHEALTHY"), None)
    sc.measured.update(removal_seconds=removal, recovery_seconds=None if back is None else round(back - t_restore, 2),
                       monitor_marked_unhealthy_after=None if unhealthy_at is None else round(unhealthy_at - t_fault, 2))
    sc.check("client errors", sc.load["overall"]["errors"], "<=", c["max_errors"])
    sc.check("monitor marked instance UNHEALTHY", unhealthy_at is not None, "==", True)
    sc.check("time until instance stopped receiving traffic", removal, "<=", c["max_removal_seconds"], "s")
    sc.check("time until instance served again", sc.measured["recovery_seconds"], "<=",
             c["max_recovery_seconds"], "s")


def _crash(ctx: Ctx, sc: Scenario, victims: tuple, unaffected: str) -> RunResult:
    c = sc.cfg
    ctx.baseline(c["opdk_percent"])
    run = ctx.load(sustained(c["duration_seconds"], c["concurrency"])).start()
    states: List[tuple] = []
    stop = threading.Event()
    watch(lambda: states.append((run.now(), {n: i["health_status"] for n, i in ctx.api.instances().items()})),
          0.5, stop)
    at(run, c["fault_at"])
    t_kill = run.mark("docker_kill", instances=list(victims))
    ctx.faults.crash(victims)
    at(run, c["restore_at"])
    t_start = run.mark("docker_start", instances=list(victims))
    ctx.faults.start(victims)
    run.mark("containers_up")
    res = run.wait()
    stop.set()
    ctx.save(sc, res)
    sc.load, sc.markers = res.summary(), res.markers

    last_err = last_error_after(res, t_kill, t_start)
    failover = round(last_err - t_kill, 2) if last_err is not None else 0.0
    errs = sum(1 for s in res.samples if t_kill <= s.t < t_start and is_error(s.status))
    backs = [first_served_after(res, v, t_start) for v in victims]
    recovery = None if any(b is None for b in backs) else round(max(backs) - t_start, 2)
    unhealthy = {v: next((round(t - t_kill, 2) for t, st in states if t >= t_kill and st.get(v) == "UNHEALTHY"),
                         None) for v in victims}
    sc.measured.update(failover_seconds=failover, errors_during_fault=errs, recovery_seconds=recovery,
                       monitor_marked_unhealthy_after=unhealthy,
                       error_status_codes={k: v for k, v in sc.load["overall"]["status_codes"].items()
                                           if k == "0" or int(k) >= 500})
    sc.check("overall error rate", sc.load["overall"]["error_rate_percent"], "<=", c["max_error_rate_percent"], "%")
    sc.check("failover time (fault -> last client error)", failover, "<=", c["max_failover_seconds"], "s")
    sc.check(f"errors on unaffected pool {unaffected}", pool_errors(res, unaffected), "<=",
             c["max_unaffected_pool_errors"])
    sc.check("monitor marked crashed instance(s) UNHEALTHY", all(v is not None for v in unhealthy.values()),
             "==", True)
    sc.check("recovery time (restart -> serving again)", recovery, "<=", c["max_recovery_seconds"], "s")
    return res


def s_instance_crash(ctx: Ctx, sc: Scenario) -> None:
    """Hard kill of one Hybrid instance under load; the other keeps serving the Hybrid share."""
    _crash(ctx, sc, (POOL_INSTANCES[HYBRID][0],), OPDK)


def s_pool_outage(ctx: Ctx, sc: Scenario) -> None:
    """Every Hybrid instance dies: the whole share fails over to OPDK, then returns."""
    c = sc.cfg
    res = _crash(ctx, sc, POOL_INSTANCES[HYBRID], OPDK)
    t_kill = next(m["t"] for m in res.markers if m["event"] == "docker_kill")
    t_start = next(m["t"] for m in res.markers if m["event"] == "docker_start")
    settled = t_kill + (sc.measured["failover_seconds"] or 0) + 1
    during = res.summary(settled, t_start)
    sc.measured["during_outage"] = {"opdk_share_percent": during["share_percent"][OPDK],
                                    "requests": during["overall"]["requests"],
                                    "errors": during["overall"]["errors"]}
    sc.check("share on OPDK while Hybrid is down", during["share_percent"][OPDK], ">=", 100.0, "%")
    tail = res.summary(res.duration - 10, res.duration)
    sc.check("split restored after recovery (last 10 s)", share_deviation(tail, c["opdk_percent"]), "<=",
             c["max_share_deviation_pp"], "pp")


def s_connection_drop(ctx: Ctx, sc: Scenario) -> None:
    """Hybrid instances reset a fraction of connections; errors must be detected and attributed."""
    c = sc.cfg
    ctx.baseline(c["opdk_percent"])
    before = prom_counts(ctx.api)
    run = ctx.load(sustained(c["duration_seconds"], c["concurrency"])).start()
    at(run, c["fault_at"])
    t0 = run.mark("drop_on", drop_rate=c["drop_rate"])
    ctx.faults.set_faults(POOL_INSTANCES[HYBRID], drop_rate=c["drop_rate"])
    at(run, c["restore_at"])
    health = {n: i["health_status"] for n, i in ctx.api.instances().items()}
    t1 = run.mark("drop_off")
    ctx.faults.clear_faults(POOL_INSTANCES[HYBRID])
    res = run.wait()
    time.sleep(1.0)
    after = prom_counts(ctx.api)
    ctx.save(sc, res)
    sc.load, sc.markers = res.summary(), res.markers
    win = res.summary(t0, t1)["pools"][HYBRID]
    sc.measured.update(hybrid_error_rate_during_fault=win["error_rate_percent"],
                       hybrid_status_codes_during_fault=win["status_codes"],
                       instance_health_during_fault=health)
    sc.notes.append("Readiness stays green while connections are dropped: health checks alone do not "
                    "prove the gateway serves API traffic correctly.")
    sc.check("Hybrid error rate detected during fault", win["error_rate_percent"], "between",
             [c["min_detected_error_rate_percent"], c["max_detected_error_rate_percent"]], "%")
    sc.check("errors on unaffected pool APIGEE_OPDK", pool_errors(res, OPDK), "<=", c["max_unaffected_pool_errors"])
    metrics_agreement(sc, ctx.api, before, after, res, c["max_metrics_disagreement_percent"])


def _migration(ctx: Ctx, sc: Scenario, *, faults: dict, auto_rollback: bool, until: tuple, timeout: float,
               max_p95_ms: int = 1000) -> tuple:
    c = sc.cfg
    ctx.baseline(100)
    run = ctx.load(sustained(timeout + 30, c["concurrency"])).start()
    time.sleep(2)
    body = {"source": OPDK, "destination": HYBRID, "target_destination_percent": 100,
            "step_percent": c["step_percent"], "interval_seconds": c["interval_seconds"],
            "min_requests": c["min_requests"], "max_error_rate_percent": 1.0, "max_p95_ms": max_p95_ms,
            "min_healthy_instances": 1, "auto_rollback": auto_rollback, "require_approval": False,
            "name": f"load-suite {sc.name}", "reason": "load/failure suite"}
    m = ctx.api.call("POST", "/api/v1/migrations", body)
    t_create = run.mark("migration_created", id=m["id"])
    if faults:
        ctx.faults.set_faults(POOL_INSTANCES[HYBRID], **faults)
        run.mark("faults_on", **faults)
    final = wait_until(lambda: (lambda x: x if x["status"] in until + ("FAILED", "COMPLETED", "ROLLED_BACK",
                                                                      "CANCELLED", "PAUSED") else None)(
        ctx.api.migration(m["id"])), timeout, f"migration {m['id']} to reach {until}", interval=0.5)
    t_end = run.mark("migration_" + final["status"].lower(), reason=final.get("status_reason"))
    if faults:
        ctx.faults.clear_faults(POOL_INSTANCES[HYBRID])
    return run, m, final, t_create, t_end


def s_migration_error_rollback(ctx: Ctx, sc: Scenario) -> None:
    """Gradual OPDK->Hybrid under load; Hybrid returns 5xx while readiness stays green -> auto rollback."""
    c = sc.cfg
    run, m, final, t0, t1 = _migration(ctx, sc, faults={"error_rate": c["error_rate"]}, auto_rollback=True,
                                       until=("ROLLED_BACK",), timeout=c["interval_seconds"] * 4)
    time.sleep(1)
    t_post = run.mark("post_rollback_window_start")
    time.sleep(8)
    run.stop()
    res = run.wait()
    ctx.save(sc, res)
    sc.load, sc.markers = res.summary(), res.markers
    post = res.summary(t_post, res.duration)
    sc.measured.update(final_status=final["status"], status_reason=final.get("status_reason"),
                       seconds_to_rollback=round(t1 - t0, 2), policy_after=list(ctx.api.policy()),
                       post_rollback=post["pools"][HYBRID]["requests"],
                       stage_metrics=[s.get("metrics") for s in final.get("stages", []) if s.get("metrics")])
    sc.check("migration status", final["status"], "==", "ROLLED_BACK")
    sc.check("time to automatic rollback", round(t1 - t0, 2), "<=", c["max_seconds_to_rollback"], "s")
    sc.check("policy restored to 100:0", list(ctx.api.policy()), "==", [100, 0])
    sc.check("Hybrid requests after rollback", post["pools"][HYBRID]["requests"], "<=",
             c["max_post_rollback_destination_requests"])


def s_migration_latency_pause(ctx: Ctx, sc: Scenario) -> None:
    """Hybrid gets slow; with auto-rollback off the migration must PAUSE on the p95 limit; operator rolls back."""
    c = sc.cfg
    run, m, final, t0, t1 = _migration(ctx, sc, faults={"latency_ms": c["latency_ms"]}, auto_rollback=False,
                                       until=("PAUSED",), timeout=c["interval_seconds"] * 4,
                                       max_p95_ms=c["max_p95_ms"])
    sc.measured.update(final_status=final["status"], status_reason=final.get("status_reason"),
                       seconds_to_pause=round(t1 - t0, 2), policy_when_paused=list(ctx.api.policy()))
    sc.check("migration status", final["status"], "==", "PAUSED")
    sc.check("pause reason mentions p95", "p95" in (final.get("status_reason") or ""), "==", True)
    sc.check("time to pause", round(t1 - t0, 2), "<=", c["max_seconds_to_pause"], "s")
    rb = ctx.api.call("POST", f"/api/v1/migrations/{m['id']}/rollback", {"to": "initial", "reason": "load suite"})
    run.mark("operator_rollback", status=rb["status"])
    sc.check("operator rollback restores 100:0", list(ctx.api.policy()), "==", [100, 0])
    time.sleep(3)
    run.stop()
    res = run.wait()
    ctx.save(sc, res)
    sc.load, sc.markers = res.summary(), res.markers


def s_migration_complete(ctx: Ctx, sc: Scenario) -> None:
    """Full staged rollout OPDK->Hybrid under sustained load with no faults."""
    c = sc.cfg
    run, m, final, t0, t1 = _migration(ctx, sc, faults={}, auto_rollback=True, until=("COMPLETED",),
                                       timeout=c["max_seconds_to_complete"] + 30)
    time.sleep(1)
    t_tail = run.mark("tail_start")
    time.sleep(5)
    run.stop()
    res = run.wait()
    ctx.save(sc, res)
    sc.load, sc.markers = res.summary(), res.markers
    tail = res.summary(t_tail, res.duration)
    sc.measured.update(final_status=final["status"], seconds_to_complete=round(t1 - t0, 2),
                       stages=[{"seq": s["seq"], "planned_hybrid": s["planned_hybrid"], "status": s["status"],
                                "metrics": s.get("metrics")} for s in final.get("stages", [])])
    sc.check("migration status", final["status"], "==", "COMPLETED")
    sc.check("time to complete", round(t1 - t0, 2), "<=", c["max_seconds_to_complete"], "s")
    sc.check("error rate during rollout", sc.load["overall"]["error_rate_percent"], "<=",
             c["max_error_rate_percent"], "%")
    sc.check("Hybrid share after completion", tail["share_percent"][HYBRID], ">=",
             c["min_final_destination_share_percent"], "%")


def s_haproxy_restart(ctx: Ctx, sc: Scenario) -> None:
    """HAProxy restarts under load: dynamic servers and policy are rebuilt by the reconciler."""
    c = sc.cfg
    ctx.baseline(c["opdk_percent"])
    run = ctx.load(sustained(c["duration_seconds"], c["concurrency"])).start()
    at(run, c["fault_at"])
    t0 = run.mark("haproxy_restart")
    ctx.faults.restart_haproxy()
    run.mark("haproxy_restart_returned")
    res = run.wait()
    ctx.save(sc, res)
    sc.load, sc.markers = res.summary(), res.markers
    errs = [s.t for s in res.samples if s.t >= t0 and is_error(s.status)]
    outage = round(max(errs) - min(errs), 2) if errs else 0.0
    tail = res.summary(res.duration - 10, res.duration)
    cfg = ctx.api.config()
    sc.measured.update(outage_seconds=outage, failed_requests=len(errs), tail_share=tail["share_percent"],
                       policy_in_sync=cfg["in_sync"])
    sc.check("outage (first -> last failed request)", outage, "<=", c["max_outage_seconds"], "s")
    sc.check("policy in sync after restart", cfg["in_sync"], "==", True)
    sc.check("split restored (last 10 s)", share_deviation(tail, c["opdk_percent"]), "<=",
             c["max_share_deviation_pp"], "pp")


SCENARIOS: Dict[str, Callable[[Ctx, Scenario], None]] = {
    "baseline_compare": s_baseline_compare,
    "sustained": s_sustained,
    "ramp": s_ramp,
    "spike": s_spike,
    "instance_unready": s_instance_unready,
    "instance_crash": s_instance_crash,
    "pool_outage": s_pool_outage,
    "connection_drop": s_connection_drop,
    "migration_error_rollback": s_migration_error_rollback,
    "migration_latency_pause": s_migration_latency_pause,
    "migration_complete": s_migration_complete,
    "haproxy_restart": s_haproxy_restart,
}


# =================================================================== output
def junit(results: List[Scenario]) -> str:
    fails = sum(1 for r in results if r.verdict == "FAIL")
    errs = sum(1 for r in results if r.verdict == "ERROR")
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<testsuite name="load-failure-suite" tests="{len(results)}" failures="{fails}" errors="{errs}" '
           f'time="{sum(r.seconds for r in results):.1f}">']
    for r in results:
        out.append(f'  <testcase classname="load_suite" name={quoteattr(r.name)} time="{r.seconds:.1f}">')
        if r.verdict == "ERROR":
            out.append(f'    <error message={quoteattr((r.error or "")[:200])}>{escape(r.error or "")}</error>')
        elif r.verdict == "FAIL":
            failed = [c for c in r.checks if not c["passed"]]
            msg = "; ".join(f"{c['check']}: {c['measured']} not {c['op']} {c['limit']}" for c in failed)
            out.append(f'    <failure message={quoteattr(msg[:500])}>{escape(json.dumps(failed, indent=1))}</failure>')
        out.append(f'    <system-out>{escape(json.dumps(r.measured, default=str)[:20000])}</system-out>')
        out.append("  </testcase>")
    out.append("</testsuite>")
    return "\n".join(out) + "\n"


def fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def print_scenario(r: Scenario) -> None:
    print(f"\n== {r.name}: {r.verdict}  ({r.seconds:.0f}s)  {r.description}")
    if r.error:
        print(f"   ERROR: {r.error.splitlines()[-1]}")
    for c in r.checks:
        mark = "ok  " if c["passed"] else "FAIL"
        print(f"   [{mark}] {c['check']}: {fmt(c['measured'])}{c['unit']}  (limit {c['op']} {fmt(c['limit'])}{c['unit']})")


def parse_set(values: List[str], cfg: dict) -> None:
    for item in values:
        key, _, raw = item.partition("=")
        scen, _, field = key.partition(".")
        if scen not in cfg or not field or not raw:
            raise SystemExit(f"--set {item!r}: expected <scenario>.<key>=<value> with a known scenario")
        try:
            cfg[scen][field] = json.loads(raw)
        except json.JSONDecodeError:
            cfg[scen][field] = raw


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Sustained-load and failure test suite (local simulation).")
    ap.add_argument("-s", "--scenario", action="append", choices=sorted(SCENARIOS),
                    help="run only these (repeatable); default: all")
    ap.add_argument("--thresholds", default=str(HERE / "load_thresholds.json"))
    ap.add_argument("--set", action="append", default=[], metavar="SCENARIO.KEY=VALUE")
    ap.add_argument("--report-dir", default="reports/load")
    ap.add_argument("--api", default="http://127.0.0.1:9000")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = ap.parse_args(argv)

    if args.list:
        for n, fn in SCENARIOS.items():
            print(f"{n:26} {(fn.__doc__ or '').strip()}")
        return 0

    cfg = {k: v for k, v in json.loads(Path(args.thresholds).read_text()).items() if not k.startswith("_")}
    parse_set(args.set, cfg)
    names = args.scenario or list(SCENARIOS)

    api = TrafficManagerAPI(args.api)
    faults = Faults()
    try:
        if api.active_migration():
            print("A migration is active; finish or cancel it before running the suite.", file=sys.stderr)
            return 2
        initial = api.policy()
    except (ApiError, OSError) as exc:
        print(f"traffic-manager API not reachable at {args.api}: {exc}", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.report_dir) / stamp
    out.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(api, faults, out, args.host, args.port)
    print(f"Load/failure suite: {len(names)} scenario(s); starting policy {initial[0]}:{initial[1]} "
          f"(restored at the end); reports -> {out}")

    results: List[Scenario] = []
    aborted: List[str] = []
    try:
        for name in names:
            if not stack_reachable(args.api, args.host, args.port):
                print(f"\nStack not reachable (API {args.api} / HAProxy {args.host}:{args.port}); "
                      f"stopping. Not run: {', '.join(names[len(results):])}")
                aborted.extend(names[len(results):])
                break
            sc = Scenario(name, (SCENARIOS[name].__doc__ or "").strip(), dict(cfg.get(name, {})))
            t0 = time.monotonic()
            try:
                SCENARIOS[name](ctx, sc)
            except Exception as exc:                 # noqa: BLE001
                sc.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            sc.seconds = time.monotonic() - t0
            results.append(sc)
            print_scenario(sc)
            problems = faults.restore_all()
            if problems:
                print("   cleanup problems: " + "; ".join(problems))
    except KeyboardInterrupt:
        print("\ninterrupted - restoring state")
    finally:
        problems = faults.restore_all()
        try:
            api.cancel_active("load-suite finished")
            wait_until(lambda: all(g["health"]["routable_instances"] > 0 for g in api.gateways().values()),
                       60, "pools routable")
            if api.policy() != initial:
                api.set_policy(initial[0], "load-suite: restore starting policy")
            restored = api.policy() == initial
        except Exception as exc:                     # noqa: BLE001
            restored = False
            problems.append(f"restore policy: {exc}")
        summary = {
            "suite": "load-failure-suite", "started_at": stamp, "target": f"http://{args.host}:{args.port}",
            "simulation": True,
            "verdict": "PASS" if results and not aborted and all(r.verdict == "PASS" for r in results) else "FAIL",
            "not_run": aborted,
            "counts": {v: sum(1 for r in results if r.verdict == v) for v in ("PASS", "FAIL", "ERROR")},
            "starting_policy": list(initial), "policy_restored": restored, "cleanup_problems": problems,
            "scenarios": [r.to_json() for r in results],
        }
        (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        (out / "junit.xml").write_text(junit(results))
        print(f"\nSUITE {summary['verdict']}: {summary['counts']}  policy restored to "
              f"{initial[0]}:{initial[1]}: {restored}")
        if problems:
            print("cleanup problems: " + "; ".join(problems))
        print(f"reports: {out}/summary.json, junit.xml, *.timeline.csv")
    if aborted or any(r.verdict == "ERROR" for r in results):
        return 2
    return 0 if summary["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
