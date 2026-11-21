"""
Live integration tests against the running Docker Compose stack.

    cd load-balancer            # run from the repository root; paths are relative to it
    docker compose up -d --build
    python3 -m venv .venv && .venv/bin/pip install pytest httpx
    .venv/bin/pytest traffic-manager/tests_integration -m integration -s -v

Requires: Docker CLI, the stack on localhost (:8080 data plane, :9000 API,
:8001-8004 simulators), TRAFFIC_API_TOKEN in the environment or in ./.env.
Runs on the host (Python 3.9+). It restarts and stops containers, so do not
run it against anything shared.
"""

import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]          # load-balancer/
sys.path.insert(0, str(ROOT / "traffic-generator"))
from test_distribution import request_detail, run_traffic_detailed  # noqa: E402

pytestmark = pytest.mark.integration

API = os.environ.get("TM_URL", "http://127.0.0.1:9000")
LB = os.environ.get("LB_URL", "http://localhost:8080/")
REQUESTS = int(os.environ.get("INTEGRATION_REQUESTS", "10000"))
Z = 3.29   # two-sided 99.9% confidence
SIM_PORT = {"opdk-router-1": 8001, "hybrid-ingress-1": 8002, "opdk-router-2": 8003, "hybrid-ingress-2": 8004}
REPORT = []


def _token():
    tok = os.environ.get("TRAFFIC_API_TOKEN")
    if not tok and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("TRAFFIC_API_TOKEN="):
                tok = line.split("=", 1)[1].strip()
    if not tok:
        pytest.skip("TRAFFIC_API_TOKEN not available")
    return tok


@pytest.fixture(scope="module")
def api():
    c = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {_token()}",
                                            "X-Actor": "integration-tests"}, timeout=30)
    wait_for(lambda: c.get("/livez").status_code == 200, 60, "API up")
    wait_for(lambda: all_ready(c), 60, "all instances READY")
    yield c
    c.put("/api/v1/traffic/config", json={"opdk_weight": 100, "hybrid_weight": 0, "reason": "test cleanup"})
    c.close()
    if REPORT:
        print("\n\n================ DISTRIBUTION REPORT ================")
        print(f"{'policy':>8} | {'OPDK exp':>8} {'OPDK obs':>8} | {'HYB exp':>8} {'HYB obs':>8} | tol(pp) | result")
        for r in REPORT:
            print(f"{r['policy']:>8} | {r['opdk_expected']:>8} {r['opdk_observed']:>8} | "
                  f"{r['hybrid_expected']:>8} {r['hybrid_observed']:>8} | {r['tolerance_pp']:>7} | {r['result']}")


# ------------------------------------------------------------------ helpers
def wait_for(cond, timeout, what):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if cond():
                return
        except (httpx.HTTPError, ValueError, KeyError, subprocess.CalledProcessError):
            pass
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for: {what}")


def haproxy(cmd):
    return subprocess.run(["docker", "exec", "-i", "haproxy", "socat", "stdio",
                           "unix-connect:/var/lib/haproxy/admin.sock"],
                          input=cmd + "\n", capture_output=True, text=True, check=True).stdout


def runtime_servers(backend):
    out = {}
    for line in haproxy(f"show servers state {backend}").splitlines()[2:]:
        f = line.split()
        if len(f) > 8:
            out[f[3]] = {"addr": f[4], "op": int(f[5]), "admin": int(f[6]), "weight": int(f[7])}
    return out


def policy_runtime():
    s = runtime_servers("traffic_policy")
    return s["opdk"]["weight"], s["hybrid"]["weight"]


def started_at():
    return subprocess.run(["docker", "inspect", "-f", "{{.State.StartedAt}}", "haproxy"],
                          capture_output=True, text=True, check=True).stdout.strip()


def all_ready(c):
    insts = c.get("/api/v1/instances").json()
    return len(insts) >= 4 and all(i["runtime"] and i["runtime"]["admin_state"] == "READY" for i in insts)


def pools_released():
    sel = runtime_servers("traffic_policy")
    return all(s["admin"] == 0 for s in sel.values())


def set_policy(c, o, h, **kw):
    return c.put("/api/v1/traffic/config", json={"opdk_weight": o, "hybrid_weight": h, **kw})


def sim_ready(name, ready):
    r = httpx.post(f"http://127.0.0.1:{SIM_PORT[name]}/__sim/health", json={"ready": ready}, timeout=5)
    assert r.status_code == 200


def instance(c, name):
    return next(i for i in c.get("/api/v1/instances").json() if i["name"] == name)


def tolerance_pp(p, n):
    return 0.0 if p in (0.0, 1.0) else round(100 * Z * math.sqrt(p * (1 - p) / n), 2)


# ------------------------------------------------------------------- tests
def test_bootstrapped_gateways(api):
    plats = {p["platform_type"] for p in api.get("/api/v1/gateways").json()}
    assert plats == {"APIGEE_OPDK", "APIGEE_HYBRID"}
    pools = api.get("/api/v1/pools").json()
    assert {p["haproxy_backend"] for p in pools} == {"apigee_opdk_pool", "apigee_hybrid_pool"}
    assert all(p["health"]["routable_instances"] == 2 for p in pools)
    for b in ("apigee_opdk_pool", "apigee_hybrid_pool"):
        assert all(s["admin"] == 0 and s["op"] == 2 for s in runtime_servers(b).values())


@pytest.mark.parametrize("opdk,hybrid", [(100, 0), (90, 10), (75, 25), (50, 50), (25, 75), (0, 100)])
def test_weighted_distribution_10k(api, opdk, hybrid):
    t0 = started_at()
    r = set_policy(api, opdk, hybrid, reason=f"integration {opdk}:{hybrid}")
    assert r.status_code == 200, r.text
    assert policy_runtime() == (opdk, hybrid)                       # verified in HAProxy itself
    counts, per_inst, _ = run_traffic_detailed(LB, REQUESTS, 20, 5.0)
    n_o, n_h = counts.get("Backend A", 0), counts.get("Backend B", 0)
    errors = REQUESTS - n_o - n_h
    p = opdk / 100.0
    tol = tolerance_pp(p, REQUESTS)
    obs_o, obs_h = 100.0 * n_o / REQUESTS, 100.0 * n_h / REQUESTS
    ok = errors == 0 and abs(obs_o - opdk) <= tol and abs(obs_h - hybrid) <= tol
    REPORT.append({"policy": f"{opdk}:{hybrid}", "opdk_expected": int(REQUESTS * p), "opdk_observed": n_o,
                   "hybrid_expected": REQUESTS - int(REQUESTS * p), "hybrid_observed": n_h,
                   "tolerance_pp": tol, "result": "PASS" if ok else "FAIL"})
    print(f"\n{opdk}:{hybrid}  OPDK expected {int(REQUESTS * p)} observed {n_o} ({obs_o:.2f}%) | "
          f"Hybrid expected {REQUESTS - int(REQUESTS * p)} observed {n_h} ({obs_h:.2f}%) | "
          f"tolerance ±{tol}pp | per instance {dict(per_inst)}")
    assert errors == 0, counts
    assert ok, counts
    # instance-level balancing: each pool's share is split evenly between its two instances
    for pool_label, share in (("Backend A", n_o), ("Backend B", n_h)):
        insts = {k: v for k, v in per_inst.items() if k.startswith(pool_label)}
        if share:
            assert len(insts) == 2 and abs(max(insts.values()) - min(insts.values())) <= 0.02 * share + 2
    assert started_at() == t0                                       # HAProxy never restarted


def test_health_failure_removal_and_recovery(api):
    assert set_policy(api, 50, 50).status_code == 200
    sim_ready("opdk-router-2", False)          # TCP still accepted, readiness returns 503
    try:
        wait_for(lambda: instance(api, "opdk-router-2")["current_health_status"] == "UNHEALTHY", 30, "UNHEALTHY")
        wait_for(lambda: runtime_servers("apigee_opdk_pool")["opdk-router-2"]["admin"] & 1, 15, "MAINT")
        counts, per_inst, _ = run_traffic_detailed(LB, 2000, 20, 5.0)
        assert not any("opdk-router-2" in k for k in per_inst), per_inst       # removed from routing
        assert abs(counts["Backend A"] - 1000) <= 2                            # pool share unchanged
        cfg = api.get("/api/v1/health/gateways").json()["gateways"]
        assert next(g for g in cfg if g["platform"]["platform_type"] == "APIGEE_OPDK")["status"] == "DEGRADED"
    finally:
        sim_ready("opdk-router-2", True)
    wait_for(lambda: instance(api, "opdk-router-2")["runtime"]["admin_state"] == "READY", 30, "recovered")
    _, per_inst, _ = run_traffic_detailed(LB, 400, 10, 5.0)
    assert any("opdk-router-2" in k for k in per_inst)
    t = api.get("/api/v1/health/instances").json()["instances"]
    trans = next(i for i in t if i["name"] == "opdk-router-2")["recent_transitions"]
    assert [x["to"] for x in trans[-2:]] == ["UNHEALTHY", "HEALTHY"]


def test_pool_not_eligible_when_all_instances_down(api):
    assert set_policy(api, 100, 0).status_code == 200
    for n in ("hybrid-ingress-1", "hybrid-ingress-2"):
        sim_ready(n, False)
    try:
        wait_for(lambda: all(instance(api, n)["current_health_status"] == "UNHEALTHY"
                             for n in ("hybrid-ingress-1", "hybrid-ingress-2")), 30, "hybrid down")
        r = set_policy(api, 90, 10)
        assert r.status_code == 409 and r.json()["error"] == "pool_not_eligible"
        assert policy_runtime() == (100, 0)
    finally:
        for n in ("hybrid-ingress-1", "hybrid-ingress-2"):
            sim_ready(n, True)
    wait_for(lambda: all_ready(api), 30, "hybrid recovered")


def test_register_and_deregister_instance(api):
    pool = next(p for p in api.get("/api/v1/pools").json() if p["platform_type"] == "APIGEE_OPDK")
    r = api.post("/api/v1/instances", json={"name": "opdk-router-3", "pool_id": pool["id"],
                                            "host": "opdk-router-2", "port": 8000, "reason": "scale out"})
    assert r.status_code == 201, r.text
    assert runtime_servers("apigee_opdk_pool")["opdk-router-3"]["admin"] & 1          # starts MAINT
    wait_for(lambda: runtime_servers("apigee_opdk_pool")["opdk-router-3"]["admin"] == 0, 30, "READY")
    iid = r.json()["id"]
    r = api.delete(f"/api/v1/instances/{iid}", params={"reason": "scale in"})
    assert r.status_code == 200, r.text
    assert "opdk-router-3" not in runtime_servers("apigee_opdk_pool")
    time.sleep(3)                                                     # a full control-loop cycle
    assert "opdk-router-3" not in runtime_servers("apigee_opdk_pool")


def test_invalid_and_unauthorized(api):
    assert set_policy(api, 80, 20).status_code == 200
    for body in ({"opdk_weight": 60, "hybrid_weight": 50}, {"opdk_weight": 0, "hybrid_weight": 0},
                 {"opdk_weight": -10, "hybrid_weight": 110}, {"opdk_weight": "80", "hybrid_weight": "20"},
                 {"opdk_weight": 80}):
        assert api.put("/api/v1/traffic/config", json=body).status_code == 422
    anon = httpx.put(f"{API}/api/v1/traffic/config", json={"opdk_weight": 0, "hybrid_weight": 100})
    bad = httpx.put(f"{API}/api/v1/traffic/config", json={"opdk_weight": 0, "hybrid_weight": 100},
                    headers={"Authorization": "Bearer not-the-token-123456"})
    assert (anon.status_code, bad.status_code) == (401, 403)
    assert policy_runtime() == (80, 20)


def test_concurrent_updates(api):
    targets = [(100 - 10 * i, 10 * i) for i in range(10)]
    codes = []
    barrier = threading.Barrier(len(targets))

    def go(o, h):
        barrier.wait()
        codes.append(set_policy(api, o, h).status_code)

    threads = [threading.Thread(target=go, args=t) for t in targets]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes.count(200) == len(targets), codes
    cfg = api.get("/api/v1/traffic/config").json()
    assert cfg["in_sync"] is True
    assert policy_runtime() == (cfg["policy"]["opdk_weight"], cfg["policy"]["hybrid_weight"])
    last = [e for e in api.get("/api/v1/traffic/history").json()["entries"] if e["status"] == "SUCCESS"][-1]
    assert last["new_state"]["version"] == cfg["policy"]["id"]


def test_rollback(api):
    set_policy(api, 90, 10)
    set_policy(api, 75, 25)
    r = api.post("/api/v1/traffic/rollback", json={"reason": "integration rollback"})
    assert r.status_code == 200, r.text
    assert policy_runtime() == (90, 10)


def test_persistence_after_management_api_restart(api):
    assert set_policy(api, 75, 25).status_code == 200
    haproxy("set server traffic_policy/opdk weight 50")       # drift while the API restarts
    haproxy("set server traffic_policy/hybrid weight 50")
    subprocess.run(["docker", "restart", "traffic-manager"], check=True, capture_output=True)
    wait_for(lambda: api.get("/livez").status_code == 200, 60, "API back")
    assert policy_runtime() == (75, 25)                        # restored from SQLite at startup
    assert len(api.get("/api/v1/instances").json()) == 4
    assert api.get("/api/v1/traffic/config").json()["in_sync"] is True


def test_haproxy_restart_recovery(api):
    """Restart HAProxy under continuous load. The saved policy and instances must be
    restored, and the 10% pool must never receive more than its share while
    pools come back (the reconciler holds both pool links and releases them together)."""
    assert set_policy(api, 90, 10).status_code == 200
    seen, stop = [], threading.Event()

    def sample():
        while not stop.is_set():
            seen.append(request_detail("localhost", 8080, "/", 3.0)[0])

    workers = [threading.Thread(target=sample) for _ in range(4)]
    [w.start() for w in workers]
    try:
        time.sleep(1)
        subprocess.run(["docker", "restart", "haproxy"], check=True, capture_output=True)
        wait_for(lambda: policy_runtime() == (90, 10), 30, "policy re-applied")
        wait_for(lambda: all_ready(api), 30, "instances re-added and READY")
        wait_for(pools_released, 30, "pool links released")
        time.sleep(2)
    finally:
        stop.set()
        [w.join() for w in workers]
    served = [x for x in seen if x in ("Backend A", "Backend B")]
    hybrid_share = served.count("Backend B") / len(served)
    errors = len(seen) - len(served)
    print(f"\nduring restart: {len(served)} served, {errors} failed (outage window), "
          f"hybrid share of served = {100 * hybrid_share:.2f}%")
    assert hybrid_share <= 0.12, f"canary pool took {100 * hybrid_share:.1f}% during recovery"
    counts, _, _ = run_traffic_detailed(LB, 2000, 20, 5.0)
    opdk_observed = 100 * counts.get("Backend A", 0) / 2000
    hybrid_observed = 100 * counts.get("Backend B", 0) / 2000
    assert abs(opdk_observed - 90) <= 2.0 and abs(hybrid_observed - 10) <= 2.0, counts
    entries = api.get("/api/v1/audit?limit=200").json()["entries"]
    assert any(e["source"] == "haproxy-restart-recovery" for e in entries)
    assert any(e["event_type"] == "POOL_ROUTING_HOLD" and "released" in (e["reason"] or "") for e in entries)


def test_runtime_api_unreachable(api):
    subprocess.run(["docker", "stop", "haproxy"], check=True, capture_output=True)
    try:
        r = set_policy(api, 50, 50)
        assert r.status_code == 503, r.text
        assert api.get("/api/v1/traffic/config").json()["policy"]["opdk_weight"] == 90   # not persisted
        assert api.get("/api/v1/health").json()["status"] == "degraded"
    finally:
        subprocess.run(["docker", "start", "haproxy"], check=True, capture_output=True)
    wait_for(lambda: policy_runtime() == (90, 10), 30, "recovered after HAProxy start")
    wait_for(lambda: all_ready(api), 30, "instances READY")
    wait_for(pools_released, 30, "pool links released")
