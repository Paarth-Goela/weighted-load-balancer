"""Spec test 13: persistence across management-API restarts; HAProxy restart recovery;
migrations; bootstrap."""

import sqlite3

from app.database.connection import Database
from conftest import AUTH, INSTANCES, cycle, seed, write_json

OPDK, HYB = "apigee_opdk_pool", "apigee_hybrid_pool"


def test_13_configuration_survives_management_api_restart(env):
    c1 = env.client()
    ids = seed(c1)
    c1.put("/api/v1/traffic/config", headers=AUTH, json={"opdk_weight": 75, "hybrid_weight": 25})
    env.close()                                     # stop the API process

    env.fake("set server traffic_policy/opdk weight 50")   # out-of-band drift while API is down
    env.fake("set server traffic_policy/hybrid weight 50")
    c2 = env.client()                               # start again on the same SQLite file
    assert len(c2.get("/api/v1/gateways", headers=AUTH).json()) == 2
    assert len(c2.get("/api/v1/instances", headers=AUTH).json()) == 4
    cfg = c2.get("/api/v1/traffic/config", headers=AUTH).json()
    assert cfg["policy"]["opdk_weight"] == 75
    assert env.fake.policy() == (75, 25) and cfg["in_sync"] is True   # reconciled at startup
    assert env.fake.admin(OPDK, "opdk-router-1") == "READY"           # healthy instances untouched
    restore = c2.get("/api/v1/traffic/history", headers=AUTH).json()["entries"][-1]
    assert restore["event_type"] == "TRAFFIC_POLICY_RESTORE" and restore["source"] == "startup-reconcile"
    assert c2.get(f"/api/v1/instances/{ids['opdk-router-1']}", headers=AUTH).json()["current_health_status"] == "HEALTHY"


def test_haproxy_restart_recovery(env, client):
    seed(client, policy=(90, 10))
    env.prober.unhealthy.add("hybrid-ingress-2")
    cycle(client, 3)                                   # hybrid-ingress-2 UNHEALTHY
    env.fake.restart()                                 # dynamic servers + weights lost
    assert env.fake.policy() == (100, 0) and not env.fake.backends[OPDK]

    client.app.state.reconciler.reconcile()            # control loop detects new Start_time_sec
    assert env.fake.policy() == (90, 10)                # policy restored
    assert env.fake.admin("traffic_policy", "opdk") == "MAINT"     # pools held until ready
    assert env.fake.admin("traffic_policy", "hybrid") == "MAINT"
    for name in INSTANCES:                               # all re-added, all MAINT first
        backend = OPDK if name.startswith("opdk") else HYB
        assert env.fake.admin(backend, name) == "MAINT"
    cycle(client, 1)                                     # fresh probe -> READY for healthy ones only
    assert env.fake.admin(OPDK, "opdk-router-1") == "READY"
    assert env.fake.admin(HYB, "hybrid-ingress-1") == "READY"
    assert env.fake.admin(HYB, "hybrid-ingress-2") == "MAINT"   # unhealthy NOT re-enabled
    assert env.fake.admin("traffic_policy", "opdk") == "READY"     # released together
    assert env.fake.admin("traffic_policy", "hybrid") == "READY"
    holds = client.get("/api/v1/audit?event_type=POOL_ROUTING_HOLD", headers=AUTH).json()["entries"]
    assert [h["reason"].split(":")[0].split(" after")[0] for h in holds][-2:] == [
        "HAProxy (re)started without its instances", "pool links released together"]
    src = [e["source"] for e in client.get("/api/v1/traffic/history", headers=AUTH).json()["entries"]]
    assert src[-1] == "haproxy-restart-recovery"


def test_unhealthy_and_disabled_not_reenabled_after_api_restart(env):
    c1 = env.client()
    ids = seed(c1)
    env.prober.unhealthy.add("opdk-router-2")
    cycle(c1, 3)
    c1.patch(f"/api/v1/instances/{ids['hybrid-ingress-2']}", headers=AUTH, json={"enabled": False})
    env.close()
    env.fake("set server apigee_opdk_pool/opdk-router-2 state ready")   # someone flips them back
    env.fake("set server apigee_hybrid_pool/hybrid-ingress-2 state ready")
    env.client()
    assert env.fake.admin(OPDK, "opdk-router-2") == "MAINT"
    assert env.fake.admin(HYB, "hybrid-ingress-2") == "MAINT"


def test_migrations_are_idempotent(tmp_path):
    db = Database(tmp_path / "m.db")
    assert db.migrate() == ["0001"]
    assert db.migrate() == []
    assert db.schema_version() == "0001"
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"gateway_platforms", "gateway_pools", "gateway_instances", "traffic_policies",
            "audit_log", "schema_migrations"} <= tables
    # DB-level constraints back up API validation
    db.execute("INSERT INTO gateway_platforms (name, platform_type, created_at, updated_at) VALUES ('a','APIGEE_OPDK','t','t')")
    for bad in ("INSERT INTO traffic_policies (name, opdk_weight, hybrid_weight, updated_by, updated_at) VALUES ('x',60,50,'t','t')",
                "INSERT INTO gateway_platforms (name, platform_type, created_at, updated_at) VALUES ('b','KONG','t','t')"):
        try:
            db.execute(bad)
            raise AssertionError("constraint not enforced")
        except sqlite3.IntegrityError:
            pass


BOOT = {
    "platforms": [{"name": "Apigee OPDK (simulated)", "platform_type": "APIGEE_OPDK"},
                  {"name": "Apigee Hybrid (simulated)", "platform_type": "APIGEE_HYBRID"}],
    "pools": [{"name": "OPDK Pool", "platform_type": "APIGEE_OPDK"},
              {"name": "Hybrid Pool", "platform_type": "APIGEE_HYBRID"}],
    "instances": [{"name": n, "pool": "OPDK Pool" if n.startswith("opdk") else "Hybrid Pool", "host": n,
                   "port": 8000} for n in INSTANCES],
    "initial_policy": {"opdk_weight": 100, "hybrid_weight": 0},
}


def test_bootstrap_file(env):
    path = write_json(env.tmp / "boot.json", BOOT)
    c = env.client(bootstrap_file=path)
    assert c.app.state.bootstrap_result == "applied"
    assert len(c.get("/api/v1/instances", headers=AUTH).json()) == 4
    assert env.fake.admin(OPDK, "opdk-router-1") == "MAINT"          # added by startup reconcile
    cycle(c, 2)
    assert env.fake.admin(OPDK, "opdk-router-1") == "READY"
    env.close()
    c2 = env.client(bootstrap_file=path)                             # second start: not re-imported
    assert c2.app.state.bootstrap_result.startswith("skipped")


def test_invalid_bootstrap_imports_nothing(env):
    bad = dict(BOOT, initial_policy={"opdk_weight": 60, "hybrid_weight": 60})
    c = env.client(bootstrap_file=write_json(env.tmp / "bad.json", bad))
    assert c.app.state.bootstrap_result.startswith("failed")
    assert c.get("/api/v1/gateways", headers=AUTH).json() == []
    entries = c.get("/api/v1/audit?event_type=BOOTSTRAP", headers=AUTH).json()["entries"]
    assert entries[-1]["status"] == "FAILED"


def test_pool_hold_waits_for_all_weighted_pools(env, client):
    seed(client, policy=(90, 10))
    env.fake.restart()
    env.prober.unhealthy |= {"opdk-router-1", "opdk-router-2"}      # OPDK not back yet
    rec = client.app.state.reconciler
    rec.reconcile()
    cycle(client, 1)
    # Hybrid is ready, OPDK instances could still recover -> keep holding
    assert env.fake.admin(HYB, "hybrid-ingress-1") == "READY"
    assert env.fake.admin("traffic_policy", "hybrid") == "MAINT"
    env.prober.unhealthy.clear()
    cycle(client, 1)
    assert env.fake.admin("traffic_policy", "opdk") == "READY"
    assert env.fake.admin("traffic_policy", "hybrid") == "READY"


def test_pool_hold_releases_when_pool_cannot_recover(env, client):
    seed(client, policy=(90, 10))
    env.prober.unhealthy |= {"opdk-router-1", "opdk-router-2"}
    cycle(client, 3)                                                # OPDK UNHEALTHY before restart
    env.fake.restart()
    cycle(client, 1)                                                # re-added, awaiting fresh probe
    assert env.fake.admin("traffic_policy", "hybrid") == "MAINT"
    cycle(client, 1)
    # OPDK cannot recover soon -> release; Hybrid serves (pool-level failover)
    assert env.fake.admin("traffic_policy", "hybrid") == "READY"


def test_pool_hold_resumes_after_api_restart_mid_hold(env):
    c1 = env.client()
    seed(c1, policy=(90, 10))
    env.fake.restart()
    c1.app.state.reconciler.reconcile()                             # hold engaged
    env.close()
    c2 = env.client()                                               # API restarts mid-hold
    assert c2.app.state.reconciler.pool_hold_since is not None
    assert env.fake.admin(OPDK, "opdk-router-1") == "MAINT"           # not re-enabled without a probe
    cycle(c2, 1)
    assert env.fake.admin("traffic_policy", "opdk") == "READY"
