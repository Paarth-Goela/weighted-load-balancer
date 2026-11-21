"""Spec tests 9-11: health-check failures, automatic removal, recovery. Plus real HTTP/HTTPS probes."""

import http.server
import shutil
import ssl
import subprocess
import threading

import pytest

from app.services.health_monitor import Prober
from conftest import AUTH, FakeResolver, cycle, seed

OPDK = "apigee_opdk_pool"


def transitions(client, inst_id):
    return client.get(f"/api/v1/audit?event_type=HEALTH_TRANSITION&entity_id={inst_id}", headers=AUTH).json()["entries"]


def test_9_health_check_failures_are_counted(env, client):
    ids = seed(client)
    env.prober.unhealthy.add("opdk-router-2")
    cycle(client, 2)  # below failure_threshold (3)
    inst = client.get(f"/api/v1/instances/{ids['opdk-router-2']}", headers=AUTH).json()
    assert inst["consecutive_failures"] == 2 and inst["current_health_status"] == "HEALTHY"
    assert inst["last_health_error"] == "unexpected HTTP status 503"
    assert env.fake.admin(OPDK, "opdk-router-2") == "READY"     # not removed yet (no flapping)


def test_10_unhealthy_instance_removed_from_routing(env, client):
    ids = seed(client)
    env.prober.unhealthy.add("opdk-router-2")
    cycle(client, 3)
    inst = client.get(f"/api/v1/instances/{ids['opdk-router-2']}", headers=AUTH).json()
    assert inst["current_health_status"] == "UNHEALTHY"
    assert env.fake.admin(OPDK, "opdk-router-2") == "MAINT"     # removed from routing
    assert env.fake.admin(OPDK, "opdk-router-1") == "READY"     # pool keeps serving
    t = transitions(client, ids["opdk-router-2"])
    assert t[-1]["previous_state"]["health"] == "HEALTHY" and t[-1]["new_state"]["health"] == "UNHEALTHY"
    assert t[-1]["timestamp"]
    gw = client.get("/api/v1/health/gateways", headers=AUTH).json()["gateways"]
    opdk = next(g for g in gw if g["platform"]["platform_type"] == "APIGEE_OPDK")
    assert opdk["status"] == "DEGRADED" and opdk["health"]["routable_instances"] == 1


def test_whole_pool_unhealthy_drops_out(env, client):
    seed(client, policy=(90, 10))
    env.prober.unhealthy |= {"hybrid-ingress-1", "hybrid-ingress-2"}
    cycle(client, 3)
    cfg = client.get("/api/v1/traffic/config", headers=AUTH).json()
    eff = {p["platform_type"]: p for p in cfg["pools"]}
    assert eff["APIGEE_HYBRID"]["pool_up"] is False and eff["APIGEE_HYBRID"]["effective_percent"] == 0.0
    assert eff["APIGEE_OPDK"]["effective_percent"] == 100.0
    assert cfg["policy"]["hybrid_weight"] == 10                # policy itself unchanged


def test_11_recovery_of_healthy_instance(env, client):
    ids = seed(client)
    env.prober.unhealthy.add("opdk-router-2")
    cycle(client, 3)
    assert env.fake.admin(OPDK, "opdk-router-2") == "MAINT"
    env.prober.unhealthy.clear()
    cycle(client, 1)
    assert env.fake.admin(OPDK, "opdk-router-2") == "MAINT"     # needs recovery_threshold=2
    cycle(client, 1)
    assert env.fake.admin(OPDK, "opdk-router-2") == "READY"
    t = transitions(client, ids["opdk-router-2"])
    assert [e["new_state"]["health"] for e in t] == ["HEALTHY", "UNHEALTHY", "HEALTHY"]


def test_recovery_hold_down(env):
    c = env.client(recovery_hold_seconds=3600)
    ids = seed(c)
    env.prober.unhealthy.add("opdk-router-2")
    cycle(c, 3)
    env.prober.unhealthy.clear()
    cycle(c, 5)
    inst = c.get(f"/api/v1/instances/{ids['opdk-router-2']}", headers=AUTH).json()
    assert inst["current_health_status"] == "UNHEALTHY"        # held down despite successes
    assert env.fake.admin(OPDK, "opdk-router-2") == "MAINT"


def test_new_instance_never_enabled_while_failing(env, client):
    env.prober.unhealthy.add("opdk-router-1")
    ids = seed(client, healthy=False, policy=None)
    cycle(client, 5)
    assert env.fake.admin(OPDK, "opdk-router-1") == "MAINT"
    inst = client.get(f"/api/v1/instances/{ids['opdk-router-1']}", headers=AUTH).json()
    assert inst["current_health_status"] == "UNHEALTHY"


def test_haproxy_native_check_down_makes_instance_not_routable(env, client):
    seed(client)
    env.fake.native_down.add("opdk-router-1")
    cfg = client.get("/api/v1/instances", headers=AUTH).json()
    r1 = next(i for i in cfg if i["name"] == "opdk-router-1")
    assert r1["runtime"]["operational_state"] == "DOWN" and r1["runtime"]["receiving_traffic"] is False


# --------------------------------------------------------------- real probes
class _Handler(http.server.BaseHTTPRequestHandler):
    seen_hosts: list = []

    def do_GET(self):
        _Handler.seen_hosts.append(self.headers.get("Host"))
        code = 204 if self.path == "/ready" else 503
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def http_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _inst(port, **kw):
    base = dict(name="x", host="127.0.0.1", port=port, protocol="HTTP", health_check_path="/ready",
                expected_status_codes=[204], host_header=None, tls_verify=True, tls_server_name=None)
    base.update(kw)
    return base


def test_http_probe_path_status_and_host_header(http_server):
    port = http_server.server_address[1]
    p = Prober(timeout=2, resolver=FakeResolver())
    assert p.probe(_inst(port, host_header="api.example.test")).ok
    assert _Handler.seen_hosts[-1] == "api.example.test"
    bad = p.probe(_inst(port, health_check_path="/other"))
    assert not bad.ok and bad.status_code == 503
    assert not p.probe(_inst(port, expected_status_codes=[200])).ok
    refused = p.probe(_inst(1))
    assert not refused.ok and "ConnectionRefused" in refused.error   # TCP failure


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not available")
def test_https_probe_tls_verify_and_sni(tmp_path):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(key), "-out", str(crt), "-subj", "/CN=gw.example.test",
                    "-addext", "subjectAltName=DNS:gw.example.test"], check=True, capture_output=True)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    seen_sni = []
    ctx.sni_callback = lambda sock, name, c: seen_sni.append(name)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        p = Prober(timeout=3, ca_file=str(crt), resolver=FakeResolver())
        ok = p.probe(_inst(port, protocol="HTTPS", tls_server_name="gw.example.test"))
        assert ok.ok, ok.error
        assert seen_sni[-1] == "gw.example.test"
        wrong = p.probe(_inst(port, protocol="HTTPS", tls_server_name="other.example.test"))
        assert not wrong.ok and "certificate" in wrong.error.lower()   # SNI/hostname mismatch
        untrusted = Prober(timeout=3, resolver=FakeResolver()).probe(
            _inst(port, protocol="HTTPS", tls_server_name="gw.example.test"))
        assert not untrusted.ok                                        # unknown CA
        insecure = Prober(timeout=3, resolver=FakeResolver()).probe(
            _inst(port, protocol="HTTPS", tls_server_name="gw.example.test", tls_verify=False))
        assert insecure.ok                                             # explicit opt-out
    finally:
        srv.shutdown()
