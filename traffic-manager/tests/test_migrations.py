"""Traffic Shift Management: gradual/scheduled migrations, safety gates, controls, emergency shift.

The migration engine and the metrics collector share a fake clock, so stage
intervals and observation windows are deterministic. Traffic is fed into the
collector exactly as HAProxy's log stream would deliver it.
"""

import threading

import pytest

from conftest import AUTH, cycle, seed

HYB = "apigee_hybrid_pool"


def start(c, **kw):
    body = {"source": "APIGEE_OPDK", "destination": "APIGEE_HYBRID", "target_destination_percent": 100,
            "step_percent": 10, "interval_seconds": 300, "min_requests": 50, "max_error_rate_percent": 1.0,
            "max_p95_ms": 500, "min_healthy_instances": 1, "auto_rollback": True, "require_approval": False}
    body.update(kw)
    return c.post("/api/v1/migrations", headers=AUTH, json=body)


def active(c):
    return c.get("/api/v1/migrations/active", headers=AUTH).json()


def tick(c):
    c.app.state.migrations.tick()


def observe(env, c, seconds=300, hybrid=100, opdk=100, errors=0, latency=20):
    """End the current observation window with the given traffic, then tick."""
    env.clock.advance(seconds)
    env.traffic("hybrid", hybrid, errors=errors, latency_ms=latency)
    env.traffic("opdk", opdk)
    tick(c)


# ------------------------------------------------------------- manual shift
def test_manual_shift_through_existing_api(env, client):
    seed(client)
    r = client.put("/api/v1/traffic/config", headers=AUTH, json={"opdk_weight": 80, "hybrid_weight": 20})
    assert r.status_code == 200 and env.fake.policy() == (80, 20)
    r = client.put("/api/v1/traffic/config", headers=AUTH, json={"opdk_weight": 80, "hybrid_weight": 30})
    assert r.status_code == 422                                       # total must be 100


# --------------------------------------------------------- gradual progression
def test_gradual_progression_100_to_0(env, client):
    seed(client)
    r = start(client)
    assert r.status_code == 201, r.text
    m = r.json()
    assert m["status"] == "RUNNING" and m["total_stages"] == 10
    assert env.fake.policy() == (90, 10)                              # stage 1 applied immediately
    seen = [env.fake.policy()]
    for _ in range(10):
        observe(env, client)
        seen.append(env.fake.policy())
    assert seen == [(90, 10), (80, 20), (70, 30), (60, 40), (50, 50), (40, 60), (30, 70), (20, 80),
                    (10, 90), (0, 100), (0, 100)]
    m = client.get(f"/api/v1/migrations/{m['id']}", headers=AUTH).json()
    assert m["status"] == "COMPLETED" and m["progress_percent"] == 100.0 and active(client) is None
    assert all(s["status"] == "PASSED" for s in m["stages"])
    assert all(s["actual_opdk"] == s["planned_opdk"] for s in m["stages"])   # read back from HAProxy
    st = m["stages"][4]
    assert st["metrics"]["APIGEE_HYBRID"]["requests"] == 100 and st["metrics"]["evaluated_pool"] == "APIGEE_HYBRID"
    assert {c["check"] for c in st["safety_checks"]["window"]} == {"min_requests", "max_error_rate_percent", "max_p95_ms"}
    assert {c["check"] for c in st["safety_checks"]["pre"]} == {"runtime_matches_migration", "destination_healthy_instances"}
    # each stage is a verified, versioned policy change made by the migration
    versions = client.get("/api/v1/traffic/policies", headers=AUTH).json()
    assert [v["updated_by"] for v in versions[-10:]] == [f"migration:{m['id']}"] * 10


def test_preview_does_not_change_anything(env, client):
    seed(client)
    r = client.post("/api/v1/migrations/preview", headers=AUTH, json={"target_destination_percent": 25,
                                                                         "step_percent": 10})
    assert [(s["opdk"], s["hybrid"]) for s in r.json()["stages"]] == [(100, 0), (90, 10), (80, 20), (75, 25)]
    assert env.fake.policy() == (100, 0) and active(client) is None


def test_starting_allocation_and_reverse_direction(env, client):
    seed(client)
    r = start(client, start_destination_percent=50, target_destination_percent=50)
    assert r.status_code == 201 and [s["planned_opdk"] for s in r.json()["stages"]] == [50]
    observe(env, client)
    assert active(client) is None
    r = start(client, source="APIGEE_HYBRID", destination="APIGEE_OPDK", step_percent=25)   # back to OPDK
    assert [s["planned_opdk"] for s in r.json()["stages"]] == [75, 100]


def test_validation(env, client):
    seed(client)
    for bad in ({"source": "APIGEE_OPDK", "destination": "APIGEE_OPDK"}, {"step_percent": 0},
                {"interval_seconds": 0}, {"max_error_rate_percent": 101}, {"target_destination_percent": 150},
                {"min_healthy_instances": 0}, {"step_percent": "10"}, {"unknown": 1}):
        assert start(client, **bad).status_code == 422, bad
    r = start(client, target_destination_percent=0)                  # already there
    assert r.status_code == 422 and active(client) is None


# ---------------------------------------------------------- pause / resume
def test_pause_and_resume(env, client):
    seed(client)
    mid = start(client).json()["id"]
    assert client.post(f"/api/v1/migrations/{mid}/pause", headers=AUTH, json={"reason": "change freeze"}).status_code == 200
    observe(env, client)                                              # time passes while paused
    assert env.fake.policy() == (90, 10) and active(client)["status"] == "PAUSED"
    r = client.post(f"/api/v1/migrations/{mid}/resume", headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "RUNNING"
    env.traffic("hybrid", 500)          # traffic before the fresh window must not count
    observe(env, client)
    assert env.fake.policy() == (80, 20)
    assert client.post(f"/api/v1/migrations/{mid}/resume", headers=AUTH).status_code == 409   # not paused


def test_resume_rejects_policy_drift_and_unhealthy_destination(env, client):
    seed(client)
    mid = start(client).json()["id"]
    client.post(f"/api/v1/migrations/{mid}/pause", headers=AUTH)
    env.fake("set server traffic_policy/opdk weight 50")              # out-of-band change
    env.fake("set server traffic_policy/hybrid weight 50")
    r = client.post(f"/api/v1/migrations/{mid}/resume", headers=AUTH)
    assert r.status_code == 409 and "differs" in r.json()["detail"]
    env.fake("set server traffic_policy/opdk weight 90")
    env.fake("set server traffic_policy/hybrid weight 10")
    env.prober.unhealthy |= {"hybrid-ingress-1", "hybrid-ingress-2"}
    cycle(client, 3)
    r = client.post(f"/api/v1/migrations/{mid}/resume", headers=AUTH)
    assert r.status_code == 409 and "healthy" in r.json()["detail"]


def test_manual_policy_change_pauses_migration(env, client):
    seed(client)
    mid = start(client).json()["id"]
    client.put("/api/v1/traffic/config", headers=AUTH, json={"opdk_weight": 70, "hybrid_weight": 30})
    m = client.get(f"/api/v1/migrations/{mid}", headers=AUTH).json()
    assert m["status"] == "PAUSED" and "changed manually" in m["status_reason"]
    observe(env, client)
    assert env.fake.policy() == (70, 30)                              # migration never overwrites it


def test_out_of_band_runtime_change_detected_before_next_stage(env, client):
    seed(client)
    start(client)
    env.fake("set server traffic_policy/opdk weight 85")              # e.g. Milestone 2 CLI
    env.fake("set server traffic_policy/hybrid weight 15")
    observe(env, client)
    m = active(client)
    assert m["status"] == "PAUSED" and "outside the migration" in m["status_reason"]
    assert env.fake.policy() == (85, 15)


# ------------------------------------------------------------- cancellation
def test_cancel(env, client):
    seed(client)
    mid = start(client).json()["id"]
    observe(env, client)                                              # now 80:20
    r = client.post(f"/api/v1/migrations/{mid}/cancel", headers=AUTH, json={"reason": "postponed"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert env.fake.policy() == (80, 20)                              # traffic left as is
    assert all(s["status"] in ("PASSED", "APPLIED", "CANCELLED") for s in r.json()["stages"])
    assert active(client) is None
    assert start(client).status_code == 201                           # a new one may start


# ---------------------------------------------------------- safety gates
def test_error_rate_violation_triggers_auto_rollback(env, client):
    seed(client)
    mid = start(client).json()["id"]
    observe(env, client)                                              # stage 1 passes -> 80:20 applied
    observe(env, client, hybrid=200, errors=10)                       # 5% errors > 1%
    m = client.get(f"/api/v1/migrations/{mid}", headers=AUTH).json()
    assert m["status"] == "ROLLED_BACK" and "max_error_rate_percent" in m["status_reason"]
    assert env.fake.policy() == (90, 10)                              # last known-good
    failed = [s for s in m["stages"] if s["status"] == "FAILED"]
    assert len(failed) == 1 and failed[0]["seq"] == 2
    assert failed[0]["metrics"]["APIGEE_HYBRID"]["error_rate_percent"] == 5.0
    hist = client.get("/api/v1/traffic/history", headers=AUTH).json()["entries"]
    assert hist[-1]["event_type"] == "TRAFFIC_ROLLBACK" and "automatic rollback" in hist[-1]["reason"]


def test_latency_violation_without_auto_rollback_pauses(env, client):
    seed(client)
    mid = start(client, auto_rollback=False).json()["id"]
    observe(env, client, latency=900)                                 # p95 900 > 500
    m = client.get(f"/api/v1/migrations/{mid}", headers=AUTH).json()
    assert m["status"] == "PAUSED" and "max_p95_ms" in m["status_reason"]
    assert env.fake.policy() == (90, 10)                              # stays; operator decides
    r = client.post(f"/api/v1/migrations/{mid}/rollback", headers=AUTH, json={"to": "last_good"})
    assert r.status_code == 200 and r.json()["status"] == "ROLLED_BACK"
    assert env.fake.policy() == (100, 0)


def test_insufficient_requests_pauses(env, client):
    seed(client)
    start(client, min_requests=1000)
    observe(env, client, hybrid=10)
    m = active(client)
    assert m["status"] == "PAUSED" and "insufficient traffic" in m["status_reason"]
    assert env.fake.policy() == (90, 10)


def test_destination_failure_during_observation_rolls_back(env, client):
    seed(client)
    mid = start(client, min_healthy_instances=2).json()["id"]
    env.prober.unhealthy.add("hybrid-ingress-2")
    cycle(client, 3)                                                  # 1 of 2 hybrid instances left
    tick(client)                                                      # checked every tick, not only at window end
    m = client.get(f"/api/v1/migrations/{mid}", headers=AUTH).json()
    assert m["status"] == "ROLLED_BACK" and "healthy instance" in m["status_reason"]
    assert env.fake.policy() == (100, 0)


def test_destination_unhealthy_before_first_stage(env, client):
    seed(client)
    env.prober.unhealthy |= {"hybrid-ingress-1", "hybrid-ingress-2"}
    cycle(client, 3)
    r = start(client)
    assert r.status_code == 201 and r.json()["status"] == "PAUSED"
    assert "pre-check" in r.json()["status_reason"]
    assert env.fake.policy() == (100, 0)                              # nothing applied


def test_manual_approval_between_stages(env, client):
    seed(client)
    mid = start(client, require_approval=True).json()["id"]
    observe(env, client)
    assert active(client)["status"] == "WAITING_APPROVAL" and env.fake.policy() == (90, 10)
    observe(env, client)
    assert env.fake.policy() == (90, 10)                              # waits indefinitely
    r = client.post(f"/api/v1/migrations/{mid}/approve", headers=AUTH)
    assert r.status_code == 200 and env.fake.policy() == (80, 20)


# ------------------------------------------------------- duplicate prevention
def test_duplicate_migrations_rejected(env, client):
    seed(client)
    assert start(client).status_code == 201
    r = start(client)
    assert r.status_code == 409 and "already" in r.json()["detail"]


def test_concurrent_start_requests_create_one_migration(env, client):
    seed(client)
    codes, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        codes.append(start(client).status_code)

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [201] + [409] * 7
    assert len(client.get("/api/v1/migrations", headers=AUTH).json()) == 1


def test_database_enforces_single_active_migration(env, client):
    seed(client)
    start(client)
    import sqlite3
    db = client.app.state.db
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE traffic_migrations SET active_slot = 1")   # fine (same row)
        db.execute("INSERT INTO traffic_migrations (name, mode, source_platform, destination_platform, initial_opdk,"
                   " target_opdk, current_opdk, last_good_opdk, interval_seconds, max_error_rate_percent, max_p95_ms,"
                   " status, active_slot, created_by, created_at, updated_at) VALUES ('x','GRADUAL','APIGEE_OPDK',"
                   "'APIGEE_HYBRID',100,0,100,100,1,1,1,'RUNNING',1,'t','t','t')")


# -------------------------------------------------------------- scheduled
def scheduled(env, c, offsets=(60, 120, 180), weights=((90, 10), (75, 25), (50, 50)), **kw):
    from datetime import timedelta
    stages = [{"at": (env.clock.now() + timedelta(seconds=o)).isoformat(), "opdk_weight": w[0], "hybrid_weight": w[1]}
              for o, w in zip(offsets, weights)]
    body = {"stages": stages, "final_observation_seconds": 60, "min_requests": 10}
    body.update(kw)
    return c.post("/api/v1/migrations/scheduled", headers=AUTH, json=body)


def test_scheduled_shift(env, client):
    seed(client)
    r = scheduled(env, client)
    assert r.status_code == 201 and r.json()["status"] == "SCHEDULED"
    mid = r.json()["id"]
    env.clock.advance(30)
    tick(client)
    assert env.fake.policy() == (100, 0)                              # not yet
    env.clock.advance(30)
    tick(client)
    assert env.fake.policy() == (90, 10)                              # stage 1 at t+60
    # cancel stage 2 before it executes
    r = client.delete(f"/api/v1/migrations/{mid}/stages/2", headers=AUTH)
    assert r.status_code == 200 and r.json()["stages"][1]["status"] == "CANCELLED"
    assert client.delete(f"/api/v1/migrations/{mid}/stages/1", headers=AUTH).status_code == 409   # already applied
    observe(env, client, seconds=120, hybrid=50)                      # t+180: stage 1 evaluated, stage 3 applied
    assert env.fake.policy() == (50, 50)
    observe(env, client, seconds=60, hybrid=50)                       # final observation
    m = client.get(f"/api/v1/migrations/{mid}", headers=AUTH).json()
    assert m["status"] == "COMPLETED" and [s["status"] for s in m["stages"]] == ["PASSED", "CANCELLED", "PASSED"]


def test_scheduled_validation_and_cancel_before_execution(env, client):
    seed(client)
    assert scheduled(env, client, offsets=(-10, 60), weights=((90, 10), (80, 20))).status_code == 422   # past
    assert scheduled(env, client, offsets=(120, 60), weights=((90, 10), (80, 20))).status_code == 422   # unordered
    r = client.post("/api/v1/migrations/scheduled", headers=AUTH, json={"stages": [
        {"at": "2030-01-01T09:00:00", "opdk_weight": 90, "hybrid_weight": 10}]})
    assert r.status_code == 422                                       # timezone required
    mid = scheduled(env, client).json()["id"]
    r = client.post(f"/api/v1/migrations/{mid}/cancel", headers=AUTH)
    assert r.json()["status"] == "CANCELLED" and all(s["status"] == "CANCELLED" for s in r.json()["stages"])
    env.clock.advance(600)
    tick(client)
    assert env.fake.policy() == (100, 0)


def test_missed_scheduled_stage_is_not_executed_late(env, client):
    seed(client)
    mid = scheduled(env, client).json()["id"]
    env.clock.advance(300)                                            # service was down past stages 1-3
    tick(client)
    m = client.get(f"/api/v1/migrations/{mid}", headers=AUTH).json()
    assert m["status"] == "PAUSED" and "missed" in m["status_reason"]
    assert env.fake.policy() == (100, 0)
    r = client.post(f"/api/v1/migrations/{mid}/resume", headers=AUTH)
    assert r.status_code == 200
    tick(client)
    m = client.get(f"/api/v1/migrations/{mid}", headers=AUTH).json()
    assert all(s["status"] == "CANCELLED" for s in m["stages"]) and m["status"] == "COMPLETED"
    assert env.fake.policy() == (100, 0)


# ------------------------------------------------------------- restart
def test_service_restart_during_migration(env):
    c1 = env.client()
    seed(c1)
    mid = start(c1).json()["id"]
    observe(env, c1)                                                  # 80:20 under observation
    env.close()                                                       # process stops mid-window
    c2 = env.client()                                                 # restart on the same SQLite DB
    m = c2.get(f"/api/v1/migrations/{mid}", headers=AUTH).json()
    assert m["status"] == "PAUSED" and "restarted" in m["status_reason"]
    observe(env, c2)
    assert env.fake.policy() == (80, 20)                              # not resumed automatically
    r = c2.post(f"/api/v1/migrations/{mid}/resume", headers=AUTH)     # verifies policy + health
    assert r.status_code == 200
    observe(env, c2)
    assert env.fake.policy() == (70, 30)
    assert start(c2).status_code == 409                               # still the only active one


# ------------------------------------------------------------- emergency
def test_emergency_shift(env, client):
    seed(client)
    mid = start(client).json()["id"]
    base = {"opdk_weight": 100, "hybrid_weight": 0, "reason": "hybrid incident"}
    assert client.post("/api/v1/traffic/emergency", headers=AUTH, json=base).status_code == 422   # no confirm
    assert client.post("/api/v1/traffic/emergency", headers=AUTH,
                       json={**base, "confirm": "yes"}).status_code == 422
    r = client.post("/api/v1/traffic/emergency", headers=AUTH, json={**base, "confirm": "EMERGENCY SHIFT"})
    assert r.status_code == 200 and env.fake.policy() == (100, 0)
    assert r.json()["cancelled_migration"] == mid
    m = client.get(f"/api/v1/migrations/{mid}", headers=AUTH).json()
    assert m["status"] == "CANCELLED" and "emergency" in m["status_reason"]
    last = client.get("/api/v1/traffic/history", headers=AUTH).json()["entries"][-1]
    assert last["event_type"] == "TRAFFIC_EMERGENCY_SHIFT" and "EMERGENCY: hybrid incident" in last["reason"]


def test_emergency_shift_validates_destination(env, client):
    seed(client)
    env.prober.unhealthy |= {"hybrid-ingress-1", "hybrid-ingress-2"}
    cycle(client, 3)
    r = client.post("/api/v1/traffic/emergency", headers=AUTH, json={
        "opdk_weight": 0, "hybrid_weight": 100, "reason": "test", "confirm": "EMERGENCY SHIFT"})
    assert r.status_code == 409 and r.json()["error"] == "pool_not_eligible"
    assert env.fake.policy() == (100, 0)


# --------------------------------------------------------------- history
def test_migration_audit_history(env, client):
    seed(client)
    mid = start(client).json()["id"]
    observe(env, client)
    client.post(f"/api/v1/migrations/{mid}/pause", headers=AUTH, json={"reason": "freeze"})
    client.post(f"/api/v1/migrations/{mid}/resume", headers=AUTH)
    client.post(f"/api/v1/migrations/{mid}/rollback", headers=AUTH, json={"to": "initial", "reason": "abort"})
    h = client.get(f"/api/v1/migrations/{mid}/history", headers=AUTH).json()["entries"]
    events = [e["event_type"] for e in h]
    assert events[0] == "MIGRATION_CREATED"
    for ev in ("MIGRATION_STAGE", "MIGRATION_PAUSED", "MIGRATION_RESUMED", "MIGRATION_ROLLBACK"):
        assert ev in events
    assert h[-1]["source"].startswith("api:") and env.fake.policy() == (100, 0)


def test_rollback_after_completion_restores_initial(env, client):
    seed(client)
    mid = start(client, target_destination_percent=20).json()["id"]
    observe(env, client)
    observe(env, client)
    assert client.get(f"/api/v1/migrations/{mid}", headers=AUTH).json()["status"] == "COMPLETED"
    r = client.post(f"/api/v1/migrations/{mid}/rollback", headers=AUTH)
    assert r.json()["status"] == "ROLLED_BACK" and env.fake.policy() == (100, 0)
