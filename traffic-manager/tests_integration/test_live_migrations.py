"""
Live Traffic Shift tests against the running stack (real HAProxy, real metric stream).

    cd load-balancer            # run from the repository root
    docker compose up -d --build
    .venv/bin/pytest traffic-manager/tests_integration/test_live_migrations.py -m integration -s -v

Background load runs through :8080 during each test so observation windows
contain real requests. Error/latency faults are injected into the Hybrid
simulators through their /__sim/faults switch while their readiness checks
keep passing.
"""

import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "traffic-generator"))
from test_distribution import request_detail  # noqa: E402

pytestmark = pytest.mark.integration
API = os.environ.get("TM_URL", "http://127.0.0.1:9000")
HYBRID_SIMS = (8002, 8004)


def _token():
    tok = os.environ.get("TRAFFIC_API_TOKEN")
    if not tok and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("TRAFFIC_API_TOKEN="):
                tok = line.split("=", 1)[1].strip()
    if not tok:
        pytest.skip("TRAFFIC_API_TOKEN not available")
    return tok


def wait_for(cond, timeout, what):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = cond()
            if last:
                return last
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for: {what} (last={last})")


def sim_faults(error_rate=0.0, latency_ms=0):
    for port in HYBRID_SIMS:
        httpx.post(f"http://127.0.0.1:{port}/__sim/faults",
                   json={"error_rate": error_rate, "latency_ms": latency_ms}, timeout=5).raise_for_status()


def haproxy_policy():
    out = subprocess.run(["docker", "exec", "-i", "haproxy", "socat", "stdio",
                          "unix-connect:/var/lib/haproxy/admin.sock"], input="show servers state traffic_policy\n",
                         capture_output=True, text=True, check=True).stdout
    w = {f[3]: int(f[7]) for f in (ln.split() for ln in out.splitlines()[2:]) if len(f) > 8}
    return w["opdk"], w["hybrid"]


@contextmanager
def load(threads=6):
    stop = threading.Event()
    counts = {}

    def run():
        while not stop.is_set():
            label = request_detail("localhost", 8080, "/", 3.0)[0]
            counts[label] = counts.get(label, 0) + 1

    ts = [threading.Thread(target=run, daemon=True) for _ in range(threads)]
    [t.start() for t in ts]
    try:
        yield counts
    finally:
        stop.set()
        [t.join() for t in ts]


@pytest.fixture(scope="module")
def api():
    c = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {_token()}", "X-Actor": "live-migration-tests"},
                     timeout=30)
    wait_for(lambda: c.get("/livez").status_code == 200, 60, "API up")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def clean(api):
    """Every test starts at 100:0, no active migration, no injected faults."""
    def reset():
        sim_faults()
        a = api.get("/api/v1/migrations/active").json()
        if a:
            api.post(f"/api/v1/migrations/{a['id']}/cancel", json={"reason": "test cleanup"})
        api.put("/api/v1/traffic/config", json={"opdk_weight": 100, "hybrid_weight": 0, "reason": "test baseline"})
        wait_for(lambda: all(i["runtime"]["admin_state"] == "READY" for i in api.get("/api/v1/instances").json()),
                 60, "instances ready")
    reset()
    yield
    reset()


def gradual(api, **kw):
    body = {"source": "APIGEE_OPDK", "destination": "APIGEE_HYBRID", "target_destination_percent": 100,
            "step_percent": 50, "interval_seconds": 4, "min_requests": 20, "max_error_rate_percent": 1.0,
            "max_p95_ms": 200, "min_healthy_instances": 2, "auto_rollback": True, "require_approval": False}
    body.update(kw)
    return api.post("/api/v1/migrations", json=body)


def status_of(api, mid, *wanted):
    m = api.get(f"/api/v1/migrations/{mid}").json()
    return m if m["status"] in wanted else None


# ------------------------------------------------------------------- tests
def test_console_served(api):
    r = httpx.get(f"{API}/console/")
    assert r.status_code == 200 and "Gateway Operations Console" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    o = api.get("/api/v1/traffic-shift/overview").json()
    assert o["metrics"]["collector"]["listening"] is True


def test_live_gradual_migration_completes(api):
    with load() as counts:
        r = gradual(api)
        assert r.status_code == 201, r.text
        mid = r.json()["id"]
        assert haproxy_policy() == (50, 50)                                   # stage 1 applied at once
        m = wait_for(lambda: status_of(api, mid, "COMPLETED", "PAUSED", "ROLLED_BACK", "FAILED"), 40, "finish")
    assert m["status"] == "COMPLETED", m["status_reason"]
    assert haproxy_policy() == (0, 100)
    for st in m["stages"]:
        hyb = st["metrics"]["APIGEE_HYBRID"]
        assert st["status"] == "PASSED" and hyb["requests"] >= 20 and hyb["error_rate_percent"] == 0.0
    print(f"\ngradual 100:0 -> 50:50 -> 0:100 completed; stage metrics: "
          f"{[(s['seq'], s['metrics']['APIGEE_HYBRID']['requests'], s['metrics']['APIGEE_HYBRID']['p95_ms']) for s in m['stages']]}; "
          f"client saw {counts}")


def test_live_error_rate_violation_rolls_back_although_health_passes(api):
    sim_faults(error_rate=0.3)                    # Hybrid returns 5xx on API paths, readiness stays green
    with load():
        mid = gradual(api, step_percent=10).json()["id"]
        m = wait_for(lambda: status_of(api, mid, "ROLLED_BACK", "FAILED", "PAUSED", "COMPLETED"), 40, "violation")
    assert m["status"] == "ROLLED_BACK", m["status_reason"]
    assert "max_error_rate_percent" in m["status_reason"]
    assert haproxy_policy() == (100, 0)                                       # last known-good = initial
    health = {i["name"]: i["health_status"] for i in api.get("/api/v1/health/instances").json()["instances"]}
    assert health["hybrid-ingress-1"] == health["hybrid-ingress-2"] == "HEALTHY"   # health != compatibility
    failed = [s for s in m["stages"] if s["status"] == "FAILED"][0]
    print(f"\nerror-rate violation: hybrid error rate {failed['metrics']['APIGEE_HYBRID']['error_rate_percent']}% "
          f"over {failed['metrics']['APIGEE_HYBRID']['requests']} requests -> rolled back to 100:0")


def test_live_latency_violation_pauses_then_operator_rolls_back(api):
    sim_faults(latency_ms=300)
    with load():
        mid = gradual(api, step_percent=10, auto_rollback=False).json()["id"]
        m = wait_for(lambda: status_of(api, mid, "PAUSED", "ROLLED_BACK", "COMPLETED"), 40, "pause")
    assert m["status"] == "PAUSED" and "max_p95_ms" in m["status_reason"]
    assert haproxy_policy() == (90, 10)                                       # held, operator decides
    r = api.post(f"/api/v1/migrations/{mid}/rollback", json={"to": "last_good"})
    assert r.status_code == 200 and r.json()["status"] == "ROLLED_BACK" and haproxy_policy() == (100, 0)


def test_live_duplicate_and_manual_change(api):
    with load():
        mid = gradual(api, interval_seconds=60).json()["id"]
        assert gradual(api).status_code == 409
        api.put("/api/v1/traffic/config", json={"opdk_weight": 80, "hybrid_weight": 20})
        m = api.get(f"/api/v1/migrations/{mid}").json()
    assert m["status"] == "PAUSED" and "manually" in m["status_reason"] and haproxy_policy() == (80, 20)


def test_live_restart_during_migration(api):
    with load():
        mid = gradual(api, interval_seconds=60).json()["id"]
        subprocess.run(["docker", "restart", "traffic-manager"], check=True, capture_output=True)
        wait_for(lambda: api.get("/livez").status_code == 200, 60, "API back")
        m = api.get(f"/api/v1/migrations/{mid}").json()
        assert m["status"] == "PAUSED" and "restarted" in m["status_reason"]
        assert haproxy_policy() == (50, 50)                                   # unchanged by the restart
        r = api.post(f"/api/v1/migrations/{mid}/resume")
        assert r.status_code == 200 and r.json()["status"] == "RUNNING"


def test_live_scheduled_shift(api):
    now = datetime.now(timezone.utc)
    stages = [{"at": (now + timedelta(seconds=s)).isoformat(), "opdk_weight": o, "hybrid_weight": 100 - o}
              for s, o in ((3, 90), (7, 75))]
    with load():
        r = api.post("/api/v1/migrations/scheduled", json={
            "stages": stages, "final_observation_seconds": 4, "min_requests": 5, "max_error_rate_percent": 1,
            "max_p95_ms": 200, "min_healthy_instances": 1})
        assert r.status_code == 201 and r.json()["status"] == "SCHEDULED"
        assert haproxy_policy() == (100, 0)
        m = wait_for(lambda: status_of(api, r.json()["id"], "COMPLETED", "PAUSED", "ROLLED_BACK"), 40, "schedule")
    assert m["status"] == "COMPLETED", m["status_reason"]
    assert haproxy_policy() == (75, 25)


def test_live_emergency_shift(api):
    with load():
        mid = gradual(api, interval_seconds=60).json()["id"]
        r = api.post("/api/v1/traffic/emergency", json={"opdk_weight": 100, "hybrid_weight": 0,
                                                        "reason": "live drill", "confirm": "EMERGENCY SHIFT"})
    assert r.status_code == 200 and r.json()["cancelled_migration"] == mid and haproxy_policy() == (100, 0)
    last = api.get("/api/v1/traffic/history").json()["entries"][-1]
    assert last["event_type"] == "TRAFFIC_EMERGENCY_SHIFT"
