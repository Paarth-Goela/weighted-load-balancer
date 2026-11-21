"""Spec tests 5-8, 15, 17: policy updates, validation, no restart, rollback, concurrency."""

import threading

import pytest

from conftest import AUTH, seed


def put(client, o, h, **extra):
    return client.put("/api/v1/traffic/config", headers=AUTH, json={"opdk_weight": o, "hybrid_weight": h, **extra})


def test_5_update_traffic_policy(env, client):
    seed(client)                                  # active policy 100:0
    r = put(client, 90, 10, reason="canary week 1", name="canary-10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["previous"] == {"opdk_weight": 100, "hybrid_weight": 0}
    assert body["runtime"] == {"opdk_weight": 90, "hybrid_weight": 10, "source": "haproxy-runtime"}
    assert body["policy"]["opdk_weight"] == 90 and body["policy"]["name"] == "canary-10"
    assert body["in_sync"] is True and body["verified"] is True
    assert env.fake.policy() == (90, 10)
    # pool mirror + history
    pools = {p["platform_type"]: p for p in client.get("/api/v1/pools", headers=AUTH).json()}
    assert pools["APIGEE_OPDK"]["traffic_weight"] == 90 and pools["APIGEE_HYBRID"]["traffic_weight"] == 10
    last = client.get("/api/v1/traffic/history", headers=AUTH).json()["entries"][-1]
    assert last["status"] == "SUCCESS" and last["reason"] == "canary week 1"
    assert last["previous_state"]["opdk_weight"] == 100 and last["new_state"]["hybrid_weight"] == 10
    # GET reads live runtime
    cfg = client.get("/api/v1/traffic/config", headers=AUTH).json()
    assert cfg["runtime"]["hybrid_weight"] == 10
    eff = {p["platform_type"]: p["effective_percent"] for p in cfg["pools"]}
    assert eff == {"APIGEE_OPDK": 90.0, "APIGEE_HYBRID": 10.0}


@pytest.mark.parametrize("payload", [
    {"opdk_weight": 60, "hybrid_weight": 50},           # sum != 100
    {"opdk_weight": 50, "hybrid_weight": 49},
    {"opdk_weight": -10, "hybrid_weight": 110},         # out of range
    {"opdk_weight": 101, "hybrid_weight": -1},
    {"opdk_weight": "90", "hybrid_weight": "10"},       # not integers
    {"opdk_weight": 90.0, "hybrid_weight": 10.0},
    {"opdk_weight": 90.5, "hybrid_weight": 9.5},
    {"opdk_weight": True, "hybrid_weight": 0},
    {"opdk_weight": None, "hybrid_weight": 100},
    {"opdk_weight": 90},                                # missing
    {},
    {"opdk_weight": 90, "hybrid_weight": 10, "extra": 1},
])
def test_6_invalid_traffic_percentages(env, client, payload):
    seed(client)
    before = env.fake.policy()
    r = client.put("/api/v1/traffic/config", headers=AUTH, json=payload)
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "validation_error"
    assert env.fake.policy() == before
    last = client.get("/api/v1/traffic/history", headers=AUTH).json()["entries"][-1]
    assert last["status"] == "REJECTED"


def test_7_both_weights_zero(env, client):
    seed(client)
    r = put(client, 0, 0)
    assert r.status_code == 422
    assert "both pools cannot have zero weight" in str(r.json()["detail"])
    assert env.fake.policy() == (100, 0)


def test_8_update_without_restarting_haproxy(env, client):
    seed(client)
    start = env.fake.start_time
    for o, h in ((90, 10), (75, 25), (50, 50), (25, 75), (0, 100), (100, 0)):
        assert put(client, o, h).status_code == 200
        assert env.fake.policy() == (o, h)
    assert env.fake.start_time == start                    # same HAProxy process
    # every intermediate state kept total > 0 (increase-first ordering)
    assert all(a + b > 0 for a, b in env.fake.policy_history)
    # only runtime weight commands were used, never a reload
    assert not [c for c in env.fake.commands if "reload" in c]


def test_pool_without_routable_instance_is_not_eligible(env, client):
    seed(client)
    env.prober.unhealthy |= {"hybrid-ingress-1", "hybrid-ingress-2"}
    from conftest import cycle
    cycle(client, 3)
    r = put(client, 90, 10)
    assert r.status_code == 409 and r.json()["error"] == "pool_not_eligible"
    assert env.fake.policy() == (100, 0)
    assert client.get("/api/v1/traffic/history", headers=AUTH).json()["entries"][-1]["status"] == "REJECTED"


def test_15_rollback(env, client):
    seed(client)                                   # v1 100:0
    put(client, 90, 10)                            # v2
    put(client, 75, 25)                            # v3
    r = client.post("/api/v1/traffic/rollback", headers=AUTH, json={"reason": "hybrid errors"})
    assert r.status_code == 200, r.text
    assert env.fake.policy() == (90, 10)
    assert r.json()["policy"]["name"] == "rollback-to-v2"
    # rolling back again returns to where we were (toggle)
    client.post("/api/v1/traffic/rollback", headers=AUTH)
    assert env.fake.policy() == (75, 25)
    # explicit version
    assert client.post("/api/v1/traffic/rollback", headers=AUTH, json={"to_version": 1}).status_code == 200
    assert env.fake.policy() == (100, 0)
    assert client.post("/api/v1/traffic/rollback", headers=AUTH, json={"to_version": 999}).status_code == 404
    hist = client.get("/api/v1/traffic/history", headers=AUTH).json()["entries"]
    assert [e["event_type"] for e in hist[-3:]] == ["TRAFFIC_ROLLBACK"] * 3
    versions = client.get("/api/v1/traffic/policies", headers=AUTH).json()
    assert len(versions) == 6 and sum(v["enabled"] for v in versions) == 1


def test_rollback_without_history(env, client):
    seed(client)
    r = client.post("/api/v1/traffic/rollback", headers=AUTH)
    assert r.status_code == 409


def test_rollback_respects_eligibility(env, client):
    seed(client)
    put(client, 50, 50)
    put(client, 100, 0)
    env.prober.unhealthy |= {"hybrid-ingress-1", "hybrid-ingress-2"}
    from conftest import cycle
    cycle(client, 3)
    r = client.post("/api/v1/traffic/rollback", headers=AUTH)   # back to 50:50 -> hybrid not eligible
    assert r.status_code == 409
    assert env.fake.policy() == (100, 0)


def test_17_concurrent_policy_updates(env, client):
    seed(client)
    targets = [(100 - i * 5, i * 5) for i in range(1, 20)]
    results = []
    barrier = threading.Barrier(len(targets))

    def worker(o, h):
        barrier.wait()
        results.append(put(client, o, h).status_code)

    threads = [threading.Thread(target=worker, args=t) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(200) == len(targets), results

    versions = client.get("/api/v1/traffic/policies", headers=AUTH).json()
    active = [v for v in versions if v["enabled"]]
    assert len(active) == 1
    # final runtime == the last completed update == active policy
    assert env.fake.policy() == (active[0]["opdk_weight"], active[0]["hybrid_weight"])
    # serialization proof: each version's predecessor is the version before it,
    # and each audit 'previous' equals the prior update's 'new'
    for prev, cur in zip(versions, versions[1:]):
        assert cur["previous_policy_id"] == prev["id"]
    hist = [e for e in client.get("/api/v1/traffic/history?limit=100", headers=AUTH).json()["entries"]
            if e["status"] == "SUCCESS"]
    for a, b in zip(hist, hist[1:]):
        assert b["previous_state"]["opdk_weight"] == a["new_state"]["opdk_weight"]
    assert all(a + b > 0 for a, b in env.fake.policy_history)


def test_lock_contention_returns_503(env):
    c = env.client(lock_timeout=0.2)
    seed(c)
    lock = c.app.state.lock
    held = threading.Event()
    release = threading.Event()

    def holder():
        with lock.hold():
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    try:
        r = put(c, 90, 10)
        assert r.status_code == 503 and r.json()["error"] == "update_in_progress"
        assert r.headers.get("retry-after") == "2"
    finally:
        release.set()
        t.join()
    assert env.fake.policy() == (100, 0)
