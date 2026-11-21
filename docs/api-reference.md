# Traffic Management API — Reference

Base URL: `http://localhost:9000`. Interactive docs: `http://localhost:9000/docs`.

All `/api/v1/*` endpoints require:

```
Authorization: Bearer $TRAFFIC_API_TOKEN
X-Actor: alice@example.com        # optional, recorded in audit (unverified)
```

```bash
set -a; source .env; set +a
H="Authorization: Bearer $TRAFFIC_API_TOKEN"
```

## Errors

Every error has the shape `{"status":"error","error":"<code>","detail":...}`.

| HTTP | `error` | Meaning |
|---|---|---|
| 401 | `unauthorized` | No bearer token (`WWW-Authenticate: Bearer`) |
| 403 | `forbidden` | Wrong token |
| 404 | `not_found` | Unknown id |
| 409 | `conflict` | Duplicate, or would leave a carrying pool with no routable instance (retry with `force=true`), or nothing to roll back to |
| 409 | `pool_not_eligible` | A pool given weight > 0 has no routable instance |
| 422 | `validation_error` / `invalid_request` | Bad input (details list), unresolvable host |
| 502 | `haproxy_update_failed` | HAProxy rejected the change; previous state restored |
| 503 | `haproxy_unavailable` | Runtime API unreachable; nothing changed (`Retry-After`) |
| 503 | `update_in_progress` | Control lock busy; retry (`Retry-After`) |
| 500 | `inconsistent_state` | Change and rollback both failed; the control loop repairs from SQLite |
| 500 | `persistence_failed` | DB write failed; runtime change rolled back |

## Gateway platforms

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/gateways` | List |
| POST | `/api/v1/gateways` | `{"name","platform_type":"APIGEE_OPDK"\|"APIGEE_HYBRID","description","enabled"}`, 201. One per type. |
| GET | `/api/v1/gateways/{id}` | Details |

## Gateway pools

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/pools` | List, each with a `health` summary |
| POST | `/api/v1/pools` | `{"name","platform_id","description","enabled"}`, 201. One per platform. Maps to `apigee_opdk_pool` / `apigee_hybrid_pool`. |
| GET | `/api/v1/pools/{id}` | Configuration and health summary: `total/enabled/healthy/unhealthy/unknown/routable_instances`, `haproxy_pool_up`, `status` (HEALTHY \| DEGRADED \| DOWN \| DISABLED) |

`traffic_weight` is read-only. It mirrors the active traffic policy.

## Gateway instances

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/instances` | Register, 201. Added to HAProxy in MAINT; routable after `HEALTH_RECOVERY_THRESHOLD` successful probes. |
| GET | `/api/v1/instances?pool_id=` | List, with `runtime` (HAProxy admin/operational state, address, sessions, `receiving_traffic`) |
| GET | `/api/v1/instances/{id}` | Details |
| PATCH | `/api/v1/instances/{id}?force=` | Partial update. Connection fields replace the server and reset health. `enabled:false` sets MAINT. |
| DELETE | `/api/v1/instances/{id}?force=&reason=` | Drain, maint, delete, verify. No traffic afterwards. |

Instance fields:

| Field | Type / default | Notes |
|---|---|---|
| `name` | `^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$` | Unique; also the HAProxy server name |
| `pool_id` | int | Cannot be changed later |
| `host` | DNS name or IPv4 | Resolved to IPv4 by the control plane |
| `port` | 1–65535 | |
| `protocol` | `HTTP` \| `HTTPS` (`HTTP`) | HTTPS: `ssl verify required ca-file $HAPROXY_CA_FILE` |
| `health_check_path` | `/healthz` | Readiness endpoint |
| `expected_status_codes` | `[200]` | 1–20 unique codes 100–599 |
| `host_header` | null | Host header for probes |
| `tls_verify` | `true` | `false` means `verify none` (explicit opt-out) |
| `tls_server_name` | null | SNI and certificate name (HTTPS only; defaults to `host`) |
| `lb_weight` | 100 (1–256) | Instance weight inside the pool, not the pool share |
| `enabled` | true | |
| `reason` | optional | Audited |

Example:

```bash
curl -s -X POST localhost:9000/api/v1/instances -H "$H" -H 'Content-Type: application/json' -d '{
  "name": "hybrid-ingress-eu-1", "pool_id": 2, "host": "ingress.hybrid.internal", "port": 443,
  "protocol": "HTTPS", "tls_server_name": "api.example.com",
  "health_check_path": "/health/ready", "expected_status_codes": [200], "host_header": "api.example.com"}'
```

## Traffic

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/traffic/config` | Active `policy` (SQLite), `runtime` weights (read live from HAProxy), `in_sync`, per-pool `routable_instances`, `pool_up`, `effective_percent` |
| PUT | `/api/v1/traffic/config` | `{"opdk_weight":90,"hybrid_weight":10,"name"?,"reason"?}`. Integers, 0–100, sum = 100. |
| POST | `/api/v1/traffic/rollback` | Body optional: `{"to_version"?: int, "reason"?}` |
| GET | `/api/v1/traffic/history?limit=50&offset=0` | Policy changes, rollbacks, restores, including FAILED and REJECTED. Oldest first; `offset` skips the newest N. |
| GET | `/api/v1/traffic/policies` | All applied versions (`id` = version) |

```bash
curl -s -X PUT localhost:9000/api/v1/traffic/config -H "$H" -H 'Content-Type: application/json' \
     -d '{"opdk_weight":90,"hybrid_weight":10,"reason":"canary week 1"}'
```
```json
{"status":"success",
 "policy":{"id":2,"name":"opdk-90-hybrid-10","opdk_weight":90,"hybrid_weight":10,"enabled":true,
           "reason":"canary week 1","updated_by":"api:172.19.0.1","previous_policy_id":1, "...":"..."},
 "runtime":{"opdk_weight":90,"hybrid_weight":10,"source":"haproxy-runtime"},
 "in_sync":true,
 "pools":[{"platform_type":"APIGEE_OPDK","configured_percent":90,"routable_instances":2,"pool_up":true,"effective_percent":90.0,"...":"..."},
          {"platform_type":"APIGEE_HYBRID","configured_percent":10,"routable_instances":2,"pool_up":true,"effective_percent":10.0,"...":"..."}],
 "message":"OPDK/Hybrid 100:0 -> 90:10 applied without restart and verified (policy v2)",
 "previous":{"opdk_weight":100,"hybrid_weight":0},
 "verified":true}
```

## Traffic Shift (migrations, emergency, metrics)

See [traffic-shift.md](traffic-shift.md) for semantics and safety rules.

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/migrations/preview` | Computes gradual stages without starting anything |
| POST | `/api/v1/migrations` | Start a gradual migration (201). 409 if one is already active. |
| POST | `/api/v1/migrations/scheduled` | Create a scheduled migration: `stages[] {at (tz required), opdk_weight, hybrid_weight}` |
| GET | `/api/v1/migrations?limit=` | Recent migrations (newest first), with stages |
| GET | `/api/v1/migrations/active` | The active migration, or `null` |
| GET | `/api/v1/migrations/{id}` | One migration with stages, safety checks and metrics |
| GET | `/api/v1/migrations/{id}/history` | Its audit trail |
| POST | `/api/v1/migrations/{id}/pause` \| `resume` \| `approve` \| `cancel` | Body optional: `{"reason"}`. `resume` re-verifies the HAProxy policy and destination health (409 if not). |
| POST | `/api/v1/migrations/{id}/rollback` | `{"to": "last_good" \| "initial", "reason"?}` |
| DELETE | `/api/v1/migrations/{id}/stages/{seq}` | Cancel a PENDING stage before it runs |
| POST | `/api/v1/traffic/emergency` | `{"opdk_weight","hybrid_weight","reason","confirm":"EMERGENCY SHIFT"}`. Cancels any active migration; returns `cancelled_migration`. |
| GET | `/api/v1/metrics/traffic?window_seconds=60` | Per-pool requests, errors, error rate, p95, avg, rps, per-server counts, collector status |
| GET | `/api/v1/metrics/timeseries?seconds=600&bucket_seconds=10` | Per-pool buckets for charts |
| GET | `/api/v1/traffic-shift/overview` | Everything the dashboard shows, in one call |

Gradual body: `source`, `destination`, `start_destination_percent?`,
`target_destination_percent`, `step_percent`, `interval_seconds`, `min_requests`,
`max_error_rate_percent`, `max_p95_ms`, `min_healthy_instances`, `auto_rollback`,
`require_approval`, `name?`, `reason?`.

Console: `GET /console/` (static, public; all data comes from the endpoints
above with the operator's token). `GET /` redirects to it.

## Health and audit

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/livez` | **public** | `{"status":"ok"}` only; used by the container health check |
| GET | `/api/v1/health` | yes | `ok`/`degraded`; DB and schema version, HAProxy reachability/version/uptime, bootstrap result, last control-loop cycle |
| GET | `/api/v1/health/gateways` | yes | Per platform: pool health summary, status, configured vs effective %, HAProxy pool check |
| GET | `/api/v1/health/instances?transitions=5` | yes | Per instance: health, counters, last error, runtime state, `awaiting_fresh_probe`, recent transitions |
| GET | `/api/v1/audit?event_type=&entity_type=&entity_id=&limit=&offset=` | yes | All audit events |
| GET | `/metrics` | admin token **or** `METRICS_TOKEN` | Prometheus text format 0.0.4 (see [observability-and-load-testing.md](observability-and-load-testing.md)). `METRICS_TOKEN` is read-only: it is accepted here and nowhere else |

Audit event types also include `TRAFFIC_EMERGENCY_SHIFT` and `MIGRATION_*`
(see traffic-shift.md). Earlier types: `GATEWAY_REGISTERED`, `POOL_CREATED`, `INSTANCE_REGISTERED`,
`INSTANCE_UPDATED`, `INSTANCE_DEREGISTERED`, `INSTANCE_RUNTIME_SYNC`,
`HEALTH_TRANSITION`, `TRAFFIC_POLICY_CHANGE`, `TRAFFIC_ROLLBACK`,
`TRAFFIC_POLICY_RESTORE`, `POOL_ROUTING_HOLD`, `BOOTSTRAP`.
Statuses: `SUCCESS`, `FAILED`, `REJECTED`, `INFO`.

## Configuration (environment)

| Variable | Default | |
|---|---|---|
| `TRAFFIC_API_TOKEN` | — | **Required**, at least 16 characters |
| `HAPROXY_RUNTIME_SOCKET` | `/run/haproxy-runtime/admin.sock` | |
| `DATABASE_PATH` | `/app/data/traffic-manager.db` | |
| `BOOTSTRAP_FILE` | compose: `/app/bootstrap/bootstrap.simulation.json` | Imported only into an empty DB |
| `HEALTH_CHECK_INTERVAL_SECONDS` | 5 (compose: 2) | Control-loop period |
| `HEALTH_CHECK_TIMEOUT_SECONDS` | 2 | |
| `HEALTH_FAILURE_THRESHOLD` | 3 | Consecutive failures before UNHEALTHY |
| `HEALTH_RECOVERY_THRESHOLD` | 2 | Consecutive successes before HEALTHY |
| `HEALTH_RECOVERY_HOLD_SECONDS` | 0 | Minimum time UNHEALTHY before rejoining |
| `PROBE_CA_FILE` | — | Extra CA bundle for HTTPS probes |
| `HAPROXY_CA_FILE` | `@system-ca` | CA for HAProxy-to-instance TLS |
| `DRAIN_TIMEOUT_SECONDS` | 10 | Deregistration drain wait |
| `LOCK_TIMEOUT_SECONDS` | 10 | |
| `POOL_RELEASE_SETTLE_SECONDS` / `POOL_RELEASE_TIMEOUT_SECONDS` | 1.5 / 20 | Pool hold after an HAProxy restart |
| `OPDK_NATIVE_CHECK_PATH` / `HYBRID_NATIVE_CHECK_PATH` | `/healthz` | HAProxy's native per-pool check |
| `METRICS_SOCKET` | compose: `/run/haproxy-runtime/metrics.sock` | HAProxy per-request log stream (unset = metrics disabled) |
| `METRICS_RETENTION_SECONDS` | 900 | In-memory metric retention |
| `MIGRATION_TICK_SECONDS` | 1 | Migration engine tick |
| `SCHEDULE_GRACE_SECONDS` | 60 | A scheduled stage later than this is not run; the migration pauses |
| `METRICS_TOKEN` | — | Optional read-only bearer token for `GET /metrics` only (≥16 chars, must differ from `TRAFFIC_API_TOKEN`) |
| `EXPOSE_ROUTING_HEADERS` (HAProxy) | compose: `1` | Adds `X-Gateway-Pool` / `X-Gateway-Instance` to responses for the load suite. Local simulation only; set `0` wherever real clients connect |
