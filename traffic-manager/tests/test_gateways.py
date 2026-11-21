"""Spec tests 1-4: register OPDK, register Hybrid, create pools, register multiple instances."""

from conftest import AUTH, INSTANCES, cycle, seed


def test_1_register_opdk(client):
    r = client.post("/api/v1/gateways", headers=AUTH, json={
        "name": "Apigee OPDK (simulated)", "platform_type": "APIGEE_OPDK", "description": "on-prem"})
    assert r.status_code == 201
    body = r.json()
    assert body["platform_type"] == "APIGEE_OPDK" and body["enabled"] is True
    assert client.get(f"/api/v1/gateways/{body['id']}", headers=AUTH).json()["name"] == "Apigee OPDK (simulated)"
    audit = client.get("/api/v1/audit?event_type=GATEWAY_REGISTERED", headers=AUTH).json()
    assert audit["entries"][-1]["status"] == "SUCCESS"


def test_2_register_hybrid_and_reject_duplicates(client):
    r = client.post("/api/v1/gateways", headers=AUTH, json={"name": "Hybrid", "platform_type": "APIGEE_HYBRID"})
    assert r.status_code == 201
    # one platform per type; unique names; unknown types rejected
    assert client.post("/api/v1/gateways", headers=AUTH,
                       json={"name": "Hybrid 2", "platform_type": "APIGEE_HYBRID"}).status_code == 409
    assert client.post("/api/v1/gateways", headers=AUTH,
                       json={"name": "Kong", "platform_type": "KONG"}).status_code == 422
    assert client.get("/api/v1/gateways/999", headers=AUTH).status_code == 404
    assert len(client.get("/api/v1/gateways", headers=AUTH).json()) == 1


def test_3_create_pools(client):
    o = client.post("/api/v1/gateways", headers=AUTH, json={"name": "OPDK", "platform_type": "APIGEE_OPDK"}).json()
    r = client.post("/api/v1/pools", headers=AUTH, json={"name": "OPDK Pool", "platform_id": o["id"]})
    assert r.status_code == 201
    pool = r.json()
    assert pool["haproxy_backend"] == "apigee_opdk_pool" and pool["platform_type"] == "APIGEE_OPDK"
    assert pool["health"]["status"] == "DOWN" and pool["health"]["total_instances"] == 0
    # one pool per platform; platform must exist
    assert client.post("/api/v1/pools", headers=AUTH, json={"name": "X", "platform_id": o["id"]}).status_code == 409
    assert client.post("/api/v1/pools", headers=AUTH, json={"name": "Y", "platform_id": 99}).status_code == 422
    assert client.get(f"/api/v1/pools/{pool['id']}", headers=AUTH).status_code == 200


def test_4_register_multiple_instances(env, client):
    ids = seed(client, healthy=False, policy=None)
    fake = env.fake
    # all four registered in the right HAProxy pool, in MAINT until healthy
    for name, key in INSTANCES.items():
        backend = f"apigee_{key}_pool"
        assert fake.admin(backend, name) == "MAINT"
    inst = client.get(f"/api/v1/instances/{ids['opdk-router-1']}", headers=AUTH).json()
    assert inst["current_health_status"] == "UNKNOWN" and inst["runtime"]["admin_state"] == "MAINT"

    cycle(client, 2)  # two consecutive successful probes -> HEALTHY -> READY
    for name, key in INSTANCES.items():
        assert fake.admin(f"apigee_{key}_pool", name) == "READY"
    pool = client.get(f"/api/v1/pools/{ids['opdk_pool']}", headers=AUTH).json()
    assert pool["health"]["routable_instances"] == 2 and pool["health"]["status"] == "HEALTHY"
    assert len(client.get(f"/api/v1/instances?pool_id={ids['hybrid_pool']}", headers=AUTH).json()) == 2


def test_instance_validation_blocks_runtime_command_injection(env, client):
    seed(client, healthy=False, policy=None)
    pool_id = client.get("/api/v1/pools", headers=AUTH).json()[0]["id"]
    bad = [
        {"name": "x;shutdown sessions", "host": "h"},
        {"name": "x\nset server", "host": "h"},
        {"name": "ok-1", "host": "evil host;del"},
        {"name": "ok-2", "host": "h", "health_check_path": "no-slash"},
        {"name": "ok-3", "host": "h", "health_check_path": "/a b"},
        {"name": "ok-4", "host": "h", "expected_status_codes": []},
        {"name": "ok-5", "host": "h", "expected_status_codes": [99]},
        {"name": "ok-6", "host": "h", "protocol": "HTTP", "tls_server_name": "x.test"},
        {"name": "ok-7", "host": "h", "port": 70000},
        {"name": "ok-8", "host": "h", "port": "8000"},
        {"name": "ok-9", "host": "h", "unknown_field": 1},
    ]
    before = list(env.fake.commands)
    for b in bad:
        r = client.post("/api/v1/instances", headers=AUTH, json={"pool_id": pool_id, "port": 8000, **b})
        assert r.status_code == 422, (b, r.text)
    assert not [c for c in env.fake.commands[len(before):] if c.startswith(("add", "set", "del"))]
    dup = client.post("/api/v1/instances", headers=AUTH,
                      json={"name": "opdk-router-1", "pool_id": pool_id, "host": "h", "port": 8000})
    assert dup.status_code == 409


def test_https_instance_gets_tls_and_sni(env, client):
    seed(client, healthy=False, policy=None)
    pool_id = client.get("/api/v1/pools", headers=AUTH).json()[1]["id"]
    r = client.post("/api/v1/instances", headers=AUTH, json={
        "name": "hybrid-ingress-tls", "pool_id": pool_id, "host": "ingress.example.test", "port": 443,
        "protocol": "HTTPS", "tls_server_name": "api.example.test", "health_check_path": "/healthz/ready",
        "expected_status_codes": [200, 204], "host_header": "api.example.test"})
    assert r.status_code == 201, r.text
    cmd = env.fake.server("apigee_hybrid_pool", "hybrid-ingress-tls").cmd
    assert "ssl verify required ca-file @system-ca" in cmd
    assert "sni str(api.example.test) check-sni api.example.test" in cmd


def test_unresolvable_host_rejected(env, client):
    seed(client, healthy=False, policy=None)
    env.resolver.unresolvable.add("ghost")
    pool_id = client.get("/api/v1/pools", headers=AUTH).json()[0]["id"]
    r = client.post("/api/v1/instances", headers=AUTH, json={"name": "g", "pool_id": pool_id, "host": "ghost",
                                                              "port": 8000})
    assert r.status_code == 422 and "resolve" in r.json()["detail"]
    assert env.fake.server("apigee_opdk_pool", "g") is None
