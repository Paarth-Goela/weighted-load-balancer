"""Spec test 14: failed Runtime API updates (plus malformed responses and persistence failure)."""

import sqlite3

import pytest

from app.services.haproxy_client import HAProxyClient, RuntimeAPIError
from conftest import AUTH, FakeHAProxy, seed


def put(client, o, h):
    return client.put("/api/v1/traffic/config", headers=AUTH, json={"opdk_weight": o, "hybrid_weight": h})


def test_14_runtime_api_unreachable(env, client):
    seed(client)
    env.fake.reachable = False
    r = put(client, 90, 10)
    assert r.status_code == 503 and r.json()["error"] == "haproxy_unavailable"
    assert "no changes were applied" in r.json()["detail"]
    assert "sock" not in r.text                                   # socket path never exposed
    env.fake.reachable = True
    assert env.fake.policy() == (100, 0)
    cfg = client.get("/api/v1/traffic/config", headers=AUTH).json()
    assert cfg["policy"]["opdk_weight"] == 100                    # not falsely persisted
    assert client.get("/api/v1/traffic/history", headers=AUTH).json()["entries"][-1]["status"] == "FAILED"


def test_unreachable_reported_by_reads_and_health(env, client):
    seed(client)
    env.fake.reachable = False
    cfg = client.get("/api/v1/traffic/config", headers=AUTH).json()
    assert cfg["runtime"] is None and cfg["haproxy_error"]
    h = client.get("/api/v1/health", headers=AUTH).json()
    assert h["status"] == "degraded" and h["haproxy"]["reachable"] is False
    pool_id = client.get("/api/v1/pools", headers=AUTH).json()[0]["id"]
    r = client.post("/api/v1/instances", headers=AUTH, json={"name": "n", "pool_id": pool_id, "host": "n",
                                                              "port": 8000})
    assert r.status_code == 503
    env.fake.reachable = True
    assert len(client.get("/api/v1/instances", headers=AUTH).json()) == 4   # registration rolled back


def test_haproxy_rejects_midway_previous_weights_restored(env, client):
    seed(client)                                                  # 100:0
    # increases go first: hybrid 0->10 succeeds, then opdk 100->90 is rejected
    env.fake.fail_once["set server traffic_policy/opdk weight"] = "Simulated HAProxy failure.\n"
    r = put(client, 90, 10)
    assert r.status_code == 502 and r.json()["error"] == "haproxy_update_failed"
    assert "restored" in r.json()["detail"]
    assert env.fake.policy() == (100, 0)                           # rolled back
    assert client.get("/api/v1/traffic/config", headers=AUTH).json()["policy"]["opdk_weight"] == 100
    last = client.get("/api/v1/traffic/history", headers=AUTH).json()["entries"][-1]
    assert last["status"] == "FAILED" and "rolled_back=True" in last["error"]


def test_connection_lost_midway_then_repaired(env, client):
    seed(client)
    env.fake.disconnect_on = "set server traffic_policy/opdk weight"  # dies after hybrid was raised
    r = put(client, 90, 10)
    assert r.status_code == 500 and r.json()["error"] == "inconsistent_state"
    env.fake.disconnect_on = None
    env.fake.reachable = True
    assert env.fake.policy() == (100, 10)                          # partial state left behind...
    client.app.state.reconciler.reconcile()
    assert env.fake.policy() == (100, 0)                           # ...repaired to the active policy


def test_persistence_failure_compensates_runtime(env, client, monkeypatch):
    seed(client)
    db = client.app.state.db
    real = db.transaction

    monkeypatch.setattr(db, "transaction", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    monkeypatch.setattr(client.app.state.audit, "record", lambda *a, **k: 0)
    r = put(client, 90, 10)
    monkeypatch.setattr(db, "transaction", real)
    assert r.status_code == 500 and r.json()["error"] == "persistence_failed"
    assert env.fake.policy() == (100, 0)


@pytest.mark.parametrize("reply", ["", "garbage\n", "Permission denied.\n", "1\nno header line\n"])
def test_malformed_responses_raise(reply):
    client = HAProxyClient(lambda cmd: reply)
    with pytest.raises(RuntimeAPIError):
        client.servers("apigee_opdk_pool")
    with pytest.raises(RuntimeAPIError):
        client.policy_weights()


def test_command_injection_guard():
    client = HAProxyClient(FakeHAProxy())
    for cmd in ("show info; shutdown frontend x", "show info\nshutdown", "a\rb"):
        with pytest.raises(ValueError):
            client.command(cmd)
    with pytest.raises(ValueError):
        client.set_state("apigee_opdk_pool", "x;y", "ready")
