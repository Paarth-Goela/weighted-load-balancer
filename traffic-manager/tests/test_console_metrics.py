"""Metrics collector, dashboard endpoints, and GUI-to-API integration (console contract)."""

import os
import re
import socket
import time
from pathlib import Path

from app.models.common import PlatformType
from app.services.metrics_collector import MetricsCollector, p95
from conftest import AUTH, seed

CONSOLE = Path(__file__).resolve().parents[1] / "app" / "console"
if not CONSOLE.exists():
    CONSOLE = Path("/app/app/console")


# ---------------------------------------------------------- metrics engine
def test_p95_nearest_rank():
    assert p95([]) is None
    assert p95([5]) == 5
    assert p95(list(range(1, 101))) == 95
    assert p95(list(range(1, 21))) == 19


def test_collector_parsing_windows_and_errors():
    now = [1000.0]
    c = MetricsCollector(None, retention_seconds=60, clock=lambda: now[0])
    assert c.ingest("M apigee_hybrid_pool h1 200 10 12", 995)
    assert c.ingest("M apigee_hybrid_pool h2 503 40 41", 996)
    assert c.ingest("M apigee_hybrid_pool h1 -1 -1 3001", 997)          # no response at all
    assert c.ingest("M apigee_opdk_pool o1 200 5 6", 990)
    for bad in ("garbage", "M unknown_pool x 200 1 1", "M apigee_opdk_pool o1 abc 1 1", "M apigee_opdk_pool o1 200"):
        assert not c.ingest(bad)
    assert c.malformed == 4
    w = c.window(PlatformType.APIGEE_HYBRID, 994, 1000)
    assert w["requests"] == 3 and w["errors"] == 2 and w["error_rate_percent"] == 66.667
    assert w["p95_ms"] == 40 and w["per_server"] == {"h1": 2, "h2": 1}
    assert c.window(PlatformType.APIGEE_HYBRID, 996.5, 1000)["requests"] == 1   # window boundary
    assert c.window(PlatformType.APIGEE_OPDK, 994, 1000)["requests"] == 0
    now[0] = 1100.0
    c.ingest("M apigee_opdk_pool o1 200 5 6")                             # prunes > 60s old
    assert c.window(PlatformType.APIGEE_HYBRID, 0, 2000)["requests"] == 0


def test_collector_series_buckets():
    now = [1000.0]
    c = MetricsCollector(None, clock=lambda: now[0])
    for i in range(10):
        c.ingest("M apigee_opdk_pool o 200 10 11", 975 + i)
    series = c.series(seconds=60, bucket=10)
    assert sum(b["APIGEE_OPDK"]["requests"] for b in series) == 10
    assert all(set(b) == {"t", "APIGEE_OPDK", "APIGEE_HYBRID"} for b in series)


def test_collector_receives_haproxy_datagrams(tmp_path):
    path = str(tmp_path / "metrics.sock")
    c = MetricsCollector(path)
    c.start()
    try:
        assert c.listening and oct(os.stat(path).st_mode & 0o777) == "0o600"
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(b"M apigee_opdk_pool opdk-router-1 200 3 4\n", path)
            s.sendto(b"M apigee_hybrid_pool hybrid-ingress-1 500 7 9\n", path)
        deadline = time.time() + 3
        while c.received < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert c.received == 2
        assert c.recent(60)["APIGEE_HYBRID"]["errors"] == 1
    finally:
        c.stop()


def test_missing_socket_directory_degrades_gracefully(tmp_path):
    c = MetricsCollector(str(tmp_path / "missing" / "m.sock"))
    c.start()
    assert not c.listening and "unavailable" in c.status()["error"]


# --------------------------------------------------------- dashboard APIs
def test_overview_uses_live_values(env, client):
    seed(client, policy=(90, 10))
    env.traffic("hybrid", 40, errors=2, latency_ms=30)
    env.traffic("opdk", 60)
    o = client.get("/api/v1/traffic-shift/overview", headers=AUTH).json()
    assert o["traffic"]["runtime"] == {"opdk_weight": 90, "hybrid_weight": 10}
    assert o["target_allocation"]["source"] == "active policy" and o["migration"] is None
    assert o["last_successful_shift"]["new_state"]["hybrid_weight"] == 10
    assert {g["platform_type"]: g["health"]["status"] for g in o["gateways"]} == {
        "APIGEE_OPDK": "HEALTHY", "APIGEE_HYBRID": "HEALTHY"}
    hyb = o["metrics"]["pools"]["APIGEE_HYBRID"]
    assert hyb["requests"] == 40 and hyb["error_rate_percent"] == 5.0 and hyb["p95_ms"] == 30
    # values follow the live system, not fixtures
    env.fake("set server traffic_policy/opdk weight 70")
    env.fake("set server traffic_policy/hybrid weight 30")
    o2 = client.get("/api/v1/traffic-shift/overview", headers=AUTH).json()
    assert o2["traffic"]["runtime"]["hybrid_weight"] == 30 and o2["traffic"]["in_sync"] is False


def test_metrics_endpoints(env, client):
    seed(client)
    env.traffic("opdk", 10)
    r = client.get("/api/v1/metrics/traffic?window_seconds=60", headers=AUTH).json()
    assert r["pools"]["APIGEE_OPDK"]["requests"] == 10 and "collector" in r
    ts = client.get("/api/v1/metrics/timeseries?seconds=60&bucket_seconds=10", headers=AUTH).json()
    assert sum(b["APIGEE_OPDK"]["requests"] for b in ts["series"]) == 10


# ---------------------------------------------------- console (GUI <-> API)
def test_console_is_served_with_security_headers(client):
    r = client.get("/console/")
    assert r.status_code == 200 and "Gateway Operations Console" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/console/app.js").status_code == 200
    assert client.get("/console/styles.css").status_code == 200
    assert client.get("/", follow_redirects=False).headers["location"] == "/console/"


def _console_api_paths():
    js = (CONSOLE / "app.js").read_text()
    table = js[js.index("const API = {"):js.index("};", js.index("const API = {"))]
    actions = re.findall(r'"(\w+)"', js[js.index("const ACTIONS"):js.index(";", js.index("const ACTIONS"))])
    paths = set()
    for literal in re.findall(r'["`](/api/v1/[^"`]*)["`]', table):
        if "${action}" in literal:
            paths |= {literal.replace("${action}", a) for a in actions}
        else:
            paths.add(literal)
    return {re.sub(r"\$\{\w+\}", "{}", p) for p in paths}, actions


def test_every_console_endpoint_exists_in_the_api(client):
    """GUI-to-API contract: each endpoint the console calls is a real, authenticated route."""
    used, actions = _console_api_paths()
    assert set(actions) == {"pause", "resume", "approve", "cancel", "rollback"}
    spec = client.get("/openapi.json").json()
    served = {re.sub(r"\{\w+\}", "{}", p) for p in spec["paths"]}
    missing = used - served
    assert not missing, f"console calls endpoints the API does not serve: {missing}"
    assert len(used) >= 15
    for p in used:
        assert p.startswith("/api/v1/")          # all behind require_admin


def test_console_has_no_hardcoded_data_or_unsafe_dom():
    js = (CONSOLE / "app.js").read_text()
    html = (CONSOLE / "index.html").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "eval(" not in js
    # never addresses HAProxy, its socket, or the simulators: only same-origin /api/v1 calls
    assert ":8080" not in js and "admin.sock" not in js and "/__sim" not in js
    assert re.findall(r"fetch\(([^,)]+)", js) == ["path"]                         # one fetch, via api()
    assert "<script>" not in html and "style=" not in html                       # CSP-compatible
    assert "sessionStorage" in js and "localStorage" not in js                    # token not persisted


def test_console_drives_a_full_shift_through_the_api(env, client):
    """The same request sequence the console sends for manual, gradual and emergency shifts."""
    seed(client)
    h = {**AUTH, "X-Actor": "operations-console"}
    assert client.put("/api/v1/traffic/config", headers=h, json={"opdk_weight": 90, "hybrid_weight": 10,
                                                                  "reason": "console"}).status_code == 200
    plan = client.post("/api/v1/migrations/preview", headers=h, json={
        "source": "APIGEE_OPDK", "destination": "APIGEE_HYBRID", "target_destination_percent": 30,
        "step_percent": 10, "interval_seconds": 60, "min_requests": 1, "max_error_rate_percent": 1,
        "max_p95_ms": 1000, "min_healthy_instances": 1, "auto_rollback": True, "require_approval": False}).json()
    assert [s["hybrid"] for s in plan["stages"]] == [10, 20, 30]
    m = client.post("/api/v1/migrations", headers=h, json={
        "source": "APIGEE_OPDK", "destination": "APIGEE_HYBRID", "target_destination_percent": 30,
        "step_percent": 10, "interval_seconds": 60, "min_requests": 1, "max_error_rate_percent": 1,
        "max_p95_ms": 1000, "min_healthy_instances": 1, "auto_rollback": True, "require_approval": False}).json()
    o = client.get("/api/v1/traffic-shift/overview", headers=h).json()
    assert o["migration"]["id"] == m["id"] and o["traffic"]["runtime"]["hybrid_weight"] == 20
    assert client.post(f"/api/v1/migrations/{m['id']}/pause", headers=h, json={"reason": "pause from console"}).status_code == 200
    r = client.post("/api/v1/traffic/emergency", headers=h, json={
        "opdk_weight": 100, "hybrid_weight": 0, "reason": "console emergency", "confirm": "EMERGENCY SHIFT"})
    assert r.status_code == 200 and env.fake.policy() == (100, 0)
    hist = client.get(f"/api/v1/migrations/{m['id']}/history", headers=h).json()["entries"]
    assert any("actor=operations-console" in e["source"] for e in hist)
