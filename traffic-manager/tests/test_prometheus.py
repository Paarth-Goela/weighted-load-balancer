"""Prometheus /metrics: auth, exposition format, and that values track real state."""

import re

import pytest

from app.services.metrics_collector import MetricsCollector
from app.settings import Settings, SettingsError
from conftest import AUTH, TOKEN, cycle, seed

METRICS_TOKEN = "metrics-scrape-token-0123456789"
SAMPLE = re.compile(r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(\{(?P<labels>.*)\})? (?P<value>\S+)$')
LABEL = re.compile(r'(?P<k>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<v>(?:[^"\\]|\\.)*)"')


def parse(text: str) -> dict:
    """Strict-ish parser for text format 0.0.4: returns {(name, frozenset(labels)): value}."""
    out, helps, types = {}, set(), {}
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("# HELP "):
            name = line.split()[2]
            assert name not in helps, f"duplicate HELP for {name}"
            helps.add(name)
            continue
        if line.startswith("# TYPE "):
            _, _, name, mtype = line.split()
            assert name not in types, f"duplicate TYPE for {name}"
            assert mtype in ("counter", "gauge", "histogram", "summary", "untyped")
            types[name] = mtype
            continue
        m = SAMPLE.match(line)
        assert m, f"bad sample line: {line!r}"
        labels = frozenset((lm["k"], lm["v"]) for lm in LABEL.finditer(m["labels"] or ""))
        base = re.sub(r"_(bucket|sum|count)$", "", m["name"])
        assert m["name"] in types or base in types, f"sample without TYPE: {m['name']}"
        key = (m["name"], labels)
        assert key not in out, f"duplicate sample {key}"
        out[key] = float(m["value"].replace("+Inf", "inf"))
    return out


def val(samples: dict, name: str, **labels) -> float:
    want = frozenset((k, str(v)) for k, v in labels.items())
    hits = [v for (n, lbl), v in samples.items() if n == "tm_" + name and want <= lbl]
    assert len(hits) == 1, f"{name}{labels}: {len(hits)} matches"
    return hits[0]


def scrape(client, token=TOKEN) -> dict:
    r = client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    return parse(r.text)


# ----------------------------------------------------------------- auth
def test_metrics_requires_token(env):
    c = env.client(metrics_token=METRICS_TOKEN)
    assert c.get("/metrics").status_code == 401
    assert c.get("/metrics", headers={"Authorization": "Bearer wrong-token-xxxxxxxxxxxx"}).status_code == 403
    assert c.get("/metrics", headers=AUTH).status_code == 200
    assert c.get("/metrics", headers={"Authorization": f"Bearer {METRICS_TOKEN}"}).status_code == 200


def test_metrics_token_is_read_only(env):
    c = env.client(metrics_token=METRICS_TOKEN)
    seed(c)
    h = {"Authorization": f"Bearer {METRICS_TOKEN}"}
    assert c.get("/api/v1/traffic/config", headers=h).status_code == 403
    assert c.put("/api/v1/traffic/config", json={"opdk_weight": 0, "hybrid_weight": 100}, headers=h).status_code == 403
    assert c.post("/api/v1/traffic/emergency", json={}, headers=h).status_code == 403
    assert env.fake.policy() == (100, 0)


def test_metrics_token_validation(tmp_path):
    with pytest.raises(SettingsError):
        Settings(api_token=TOKEN, metrics_token="short")
    with pytest.raises(SettingsError):
        Settings(api_token=TOKEN, metrics_token=TOKEN)          # must differ from the admin token
    assert Settings(api_token=TOKEN, metrics_token=METRICS_TOKEN).metrics_token == METRICS_TOKEN
    assert "metrics-scrape" not in repr(Settings(api_token=TOKEN, metrics_token=METRICS_TOKEN))


# -------------------------------------------------------------- content
def test_exposition_parses_and_covers_every_area(client):
    seed(client)
    s = scrape(client)
    names = {n for n, _ in s}
    for required in ("tm_build_info", "tm_haproxy_up", "tm_traffic_policy_percent", "tm_traffic_policy_in_sync",
                     "tm_pool_routable_instances", "tm_instance_healthy", "tm_control_loop_cycles_total",
                     "tm_reconcile_runs_total", "tm_migration_active", "tm_migrations", "tm_database_up",
                     "tm_haproxy_server_weight", "tm_metrics_stream_events_total", "tm_health_probes_total"):
        assert required in names, required


def test_policy_and_health_values_track_state(env, client):
    seed(client, policy=(70, 30))
    s = scrape(client)
    assert val(s, "traffic_policy_percent", platform="APIGEE_OPDK", source="configured") == 70
    assert val(s, "traffic_policy_percent", platform="APIGEE_HYBRID", source="runtime") == 30
    assert val(s, "traffic_policy_in_sync") == 1
    assert val(s, "haproxy_server_weight", backend="traffic_policy", server="hybrid") == 30
    assert val(s, "pool_routable_instances", platform="APIGEE_HYBRID") == 2
    assert val(s, "pool_status", platform="APIGEE_HYBRID", status="HEALTHY") == 1
    assert val(s, "instance_routable", instance="hybrid-ingress-1") == 1
    assert val(s, "traffic_policy_changes_total", event="TRAFFIC_POLICY_CHANGE", status="SUCCESS") >= 1

    # Instance fails readiness -> UNHEALTHY -> counted and not routable.
    env.prober.unhealthy.add("hybrid-ingress-1")
    cycle(client, 3)
    s = scrape(client)
    assert val(s, "instance_healthy", instance="hybrid-ingress-1") == 0
    assert val(s, "instance_routable", instance="hybrid-ingress-1") == 0
    assert val(s, "pool_routable_instances", platform="APIGEE_HYBRID") == 1
    assert val(s, "pool_status", platform="APIGEE_HYBRID", status="DEGRADED") == 1
    assert val(s, "health_transitions_total", to="UNHEALTHY") == 1
    assert val(s, "health_probes_total", result="failure") >= 3
    assert val(s, "haproxy_server_admin_state", backend="apigee_hybrid_pool", server="hybrid-ingress-1",
               state="MAINT") == 1

    # Out-of-band drift is visible.
    env.fake("set server traffic_policy/opdk weight 50")
    assert val(scrape(client), "traffic_policy_in_sync") == 0


def test_request_counters_and_histogram(env, client):
    seed(client)
    ts = env.clock.ts() - 1                     # inside the current 60 s window (frozen clock)
    ingest = lambda line: env.metrics.ingest(line, ts)  # noqa: E731
    ingest("M apigee_hybrid_pool hybrid-ingress-1 200 3 4")
    ingest("M apigee_hybrid_pool hybrid-ingress-1 200 40 41")
    ingest("M apigee_hybrid_pool hybrid-ingress-2 503 -1 5000")   # HAProxy-generated, no response
    ingest("M apigee_hybrid_pool hybrid-ingress-2 -1 -1 50")      # aborted, no status
    ingest("M apigee_opdk_pool opdk-router-1 404 2000 2001")     # 4xx is not an error
    s = scrape(client)
    assert val(s, "gateway_requests_total", instance="hybrid-ingress-1", code_class="2xx") == 2
    assert val(s, "gateway_requests_total", instance="hybrid-ingress-2", code_class="5xx") == 1
    assert val(s, "gateway_requests_total", instance="hybrid-ingress-2", code_class="none") == 1
    assert val(s, "gateway_requests_total", instance="opdk-router-1", code_class="4xx") == 1
    assert val(s, "gateway_errors_total", platform="APIGEE_HYBRID") == 2
    assert val(s, "gateway_errors_total", platform="APIGEE_OPDK") == 0
    h = "gateway_response_seconds"
    assert val(s, h + "_count", platform="APIGEE_HYBRID") == 2                 # Tr = -1 not observed
    assert val(s, h + "_sum", platform="APIGEE_HYBRID") == pytest.approx(0.043)
    assert val(s, h + "_bucket", platform="APIGEE_HYBRID", le="0.005") == 1
    assert val(s, h + "_bucket", platform="APIGEE_HYBRID", le="0.05") == 2
    assert val(s, h + "_bucket", platform="APIGEE_OPDK", le="1") == 0
    assert val(s, h + "_bucket", platform="APIGEE_OPDK", le="2.5") == 1
    assert val(s, h + "_bucket", platform="APIGEE_OPDK", le="+Inf") == 1
    # Buckets are cumulative and end at _count.
    for plat in ("APIGEE_OPDK", "APIGEE_HYBRID"):
        buckets = sorted(((float(dict(lbl)["le"].replace("+Inf", "inf")), v) for (n, lbl), v in s.items()
                          if n == "tm_" + h + "_bucket" and ("platform", plat) in lbl))
        values = [v for _, v in buckets]
        assert values == sorted(values) and values[-1] == val(s, h + "_count", platform=plat)
    # 60 s window gauges
    assert val(s, "gateway_window_requests", platform="APIGEE_HYBRID") == 4
    assert val(s, "gateway_window_error_ratio", platform="APIGEE_HYBRID") == 0.5
    assert val(s, "metrics_stream_events_total") == 5


def test_counters_survive_window_pruning():
    now = [1000.0]
    c = MetricsCollector(None, retention_seconds=10, clock=lambda: now[0])
    c.ingest("M apigee_opdk_pool o1 500 5 6")
    now[0] = 2000.0
    c.ingest("M apigee_opdk_pool o1 200 5 6")
    assert c.totals()["errors"]["apigee_opdk_pool"] == 1
    assert sum(c.totals()["requests"].values()) == 2


def test_migration_metrics(env, client):
    seed(client)
    r = client.post("/api/v1/migrations", headers=AUTH, json={
        "source": "APIGEE_OPDK", "destination": "APIGEE_HYBRID", "target_destination_percent": 100,
        "step_percent": 25, "interval_seconds": 60})
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    client.app.state.migrations.tick()
    s = scrape(client)
    assert val(s, "migration_active") == 1
    assert val(s, "migration_active_status", status="RUNNING") == 1
    assert val(s, "migration_active_info", id=mid) == 1
    assert val(s, "migration_target_opdk_percent") == 0
    assert val(s, "migration_current_opdk_percent") == 75
    assert val(s, "migration_stages_planned") == 4
    assert val(s, "migrations", status="RUNNING") == 1
    assert client.post(f"/api/v1/migrations/{mid}/cancel", json={"reason": "test"}, headers=AUTH).status_code == 200
    s = scrape(client)
    assert val(s, "migration_active") == 0
    assert val(s, "migrations", status="CANCELLED") == 1
    assert not any(n == "tm_migration_active_info" for n, _ in s)


def test_haproxy_unreachable_still_scrapes(env, client):
    seed(client)
    env.fake.reachable = False
    s = scrape(client)
    assert val(s, "haproxy_up") == 0
    assert not any(n == "tm_traffic_policy_in_sync" for n, _ in s)   # unknown -> absent, not 0
    assert val(s, "pool_routable_instances", platform="APIGEE_OPDK") == 0
    assert val(s, "database_up") == 1


def test_label_values_are_escaped():
    from app.services.prometheus import Exposition
    x = Exposition()
    x.add("t", "gauge", "help", 1, {"name": 'a"b\\c\nd'})
    assert 'tm_t{name="a\\"b\\\\c\\nd"} 1' in x.render()
    parse(x.render())
