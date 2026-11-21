"""Spec test 12: instance deregistration (and PATCH updates)."""

from conftest import AUTH, cycle, seed

OPDK = "apigee_opdk_pool"


def test_12_deregistration_removes_instance_from_routing(env, client):
    ids = seed(client)
    r = client.delete(f"/api/v1/instances/{ids['opdk-router-2']}?reason=decommission", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["removed_from_haproxy"] is True
    assert env.fake.server(OPDK, "opdk-router-2") is None              # gone from HAProxy
    assert client.get(f"/api/v1/instances/{ids['opdk-router-2']}", headers=AUTH).status_code == 404
    # sequence: drain -> maint -> shutdown sessions -> delete
    cmds = [c for c in env.fake.commands if "opdk-router-2" in c and not c.startswith("show")]
    order = [c.split()[0] + ":" + c.split()[-1] for c in cmds[-4:]]
    assert order == ["set:drain", "set:maint", "shutdown:apigee_opdk_pool/opdk-router-2",
                     "del:apigee_opdk_pool/opdk-router-2"]
    # reconciler never re-adds a deregistered instance
    cycle(client, 2)
    assert env.fake.server(OPDK, "opdk-router-2") is None
    audit = client.get("/api/v1/audit?event_type=INSTANCE_DEREGISTERED", headers=AUTH).json()["entries"]
    assert audit[-1]["status"] == "SUCCESS" and audit[-1]["reason"] == "decommission"


def test_deregistering_last_routable_instance_requires_force(env, client):
    ids = seed(client)                                                  # OPDK has 100%
    assert client.delete(f"/api/v1/instances/{ids['opdk-router-2']}", headers=AUTH).status_code == 200
    r = client.delete(f"/api/v1/instances/{ids['opdk-router-1']}", headers=AUTH)
    assert r.status_code == 409 and "no routable instance" in r.json()["detail"]
    assert env.fake.admin(OPDK, "opdk-router-1") == "READY"
    # instances in a 0% pool can be removed freely
    assert client.delete(f"/api/v1/instances/{ids['hybrid-ingress-1']}", headers=AUTH).status_code == 200
    assert client.delete(f"/api/v1/instances/{ids['opdk-router-1']}?force=true", headers=AUTH).status_code == 200


def test_patch_disable_enable_weight(env, client):
    ids = seed(client)
    i2 = ids["opdk-router-2"]
    r = client.patch(f"/api/v1/instances/{i2}", headers=AUTH, json={"enabled": False, "reason": "maintenance"})
    assert r.status_code == 200 and env.fake.admin(OPDK, "opdk-router-2") == "MAINT"
    cycle(client, 2)
    assert env.fake.admin(OPDK, "opdk-router-2") == "MAINT"            # disabled stays out even if healthy
    client.patch(f"/api/v1/instances/{i2}", headers=AUTH, json={"enabled": True})
    assert env.fake.admin(OPDK, "opdk-router-2") == "READY"
    client.patch(f"/api/v1/instances/{i2}", headers=AUTH, json={"lb_weight": 50})
    assert env.fake.server(OPDK, "opdk-router-2").weight == 50
    cfg = client.get("/api/v1/traffic/config", headers=AUTH).json()
    assert cfg["policy"]["opdk_weight"] == 100                         # pool weight untouched


def test_patch_connection_change_requires_fresh_health(env, client):
    ids = seed(client)
    i2 = ids["opdk-router-2"]
    env.resolver.overrides["new-router-2"] = "172.30.9.9"
    r = client.patch(f"/api/v1/instances/{i2}", headers=AUTH, json={"host": "new-router-2", "port": 9000})
    assert r.status_code == 200, r.text
    assert r.json()["current_health_status"] == "UNKNOWN"
    srv = env.fake.server(OPDK, "opdk-router-2")
    assert (srv.addr, srv.port, env.fake.admin(OPDK, "opdk-router-2")) == ("172.30.9.9", 9000, "MAINT")
    cycle(client, 2)
    assert env.fake.admin(OPDK, "opdk-router-2") == "READY"


def test_patch_invalid_merged_config_rejected(env, client):
    ids = seed(client)
    r = client.patch(f"/api/v1/instances/{ids['opdk-router-1']}", headers=AUTH,
                     json={"tls_server_name": "x.example.test"})          # protocol still HTTP
    assert r.status_code == 422
    r = client.patch(f"/api/v1/instances/{ids['opdk-router-1']}", headers=AUTH, json={"pool_id": 2})
    assert r.status_code == 422                                          # moving pools not allowed


def test_address_drift_is_repaired(env, client):
    seed(client)
    env.resolver.overrides["opdk-router-1"] = "172.30.200.1"            # container got a new IP
    cycle(client, 1)
    assert env.fake.server(OPDK, "opdk-router-1").addr == "172.30.200.1"


def test_orphan_server_removed(env, client):
    seed(client)
    env.fake("add server apigee_opdk_pool/rogue 10.9.9.9:80 weight 100")
    env.fake("set server apigee_opdk_pool/rogue state ready")
    cycle(client, 1)
    assert env.fake.server(OPDK, "rogue") is None
