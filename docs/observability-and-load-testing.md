# Observability and load/failure testing

This covers the Prometheus metrics endpoint, the alert rules, the optional
Prometheus service, and the sustained-load and failure test suite.

Everything here runs against the **local simulation**. The simulators are not
Apigee, so the numbers describe this stack on one machine, not real gateway
capacity. Real limits must come from staging measurements.

## 1. Prometheus metrics (`GET /metrics`)

The traffic-manager serves Prometheus text format 0.0.4 at `/metrics`. Each
scrape reads current state; nothing is pushed, and nothing is in the request
path.

**Auth:** a bearer token is required. Two tokens are accepted:

- `METRICS_TOKEN` (optional, recommended for Prometheus). It is read-only: it
  works on `/metrics` and returns 403 everywhere else.
- `TRAFFIC_API_TOKEN` (the admin token).

`METRICS_TOKEN` must be at least 16 characters and must differ from the admin
token. Otherwise the service refuses to start.

```bash
curl -s -H "Authorization: Bearer $METRICS_TOKEN" http://127.0.0.1:9000/metrics | grep -v '^#' | head
```

| Area | Metrics (all prefixed `tm_`) | Source |
|---|---|---|
| Data plane | `gateway_requests_total{platform,pool_backend,instance,code_class}`, `gateway_errors_total{platform}`, `gateway_response_seconds` histogram, `gateway_window_requests` / `gateway_window_error_ratio` / `gateway_window_p95_seconds` (last 60 s, exact p95) | HAProxy per-request log stream |
| Policy | `traffic_policy_percent{platform,source=configured\|runtime\|effective}`, `traffic_policy_in_sync`, `traffic_policy_version`, `traffic_policy_changes_total{event,status}` | SQLite policy, HAProxy weights, audit log |
| Health | `pool_routable_instances`, `pool_instances{state}`, `pool_up`, `pool_status{status}` (one-hot), `instance_healthy` / `instance_enabled` / `instance_routable` / `instance_consecutive_failures`, `health_probes_total{result}`, `health_transitions_total{to}` | Health monitor and runtime state |
| HAProxy | `haproxy_up`, `haproxy_info{version}`, `haproxy_uptime_seconds`, `haproxy_restarts_detected_total`, `haproxy_server_up` / `admin_state` / `weight` / `current_sessions` / `sessions_total{backend,server}` | Runtime API |
| Control loop | `control_loop_running`, `control_loop_cycles_total`, `control_loop_failures_total`, `control_loop_last_cycle_duration_seconds`, `control_loop_last_cycle_timestamp_seconds`, `reconcile_runs_total` / `actions_total` / `errors_total` / `haproxy_unreachable_total`, `pool_hold_active` | Control loop and reconciler counters |
| Migrations | `migration_active`, `migration_active_status{status}`, `migration_active_info{id,name,mode,source,destination}`, `migration_current_opdk_percent`, `migration_target_opdk_percent`, `migration_last_good_opdk_percent`, `migration_current_stage`, `migration_stages_planned`, `migration_progress_percent`, `migrations{status}`, `migration_stages{status}`, `migration_scheduler_running`, `migration_tick_errors_total` | Migration engine and SQLite |
| Service | `build_info{version}`, `database_up`, `scrape_duration_seconds`, `metrics_stream_listening` / `events_total` / `malformed_total` / `last_event_timestamp_seconds` | |

What counts as an error, and how values behave:

- **Error** means HTTP 5xx (including 502/503/504 that HAProxy generates itself) or no response. 4xx is not an error. `code_class="none"` means no HTTP status was sent.
- **Process-lifetime counters** reset when the traffic-manager restarts, which Prometheus handles with `rate()`. These are the request and error counters, the histogram, and the probe, cycle and reconcile counters.
- **Counts read from SQLite** survive restarts: `migrations`, `migration_stages` and `traffic_policy_changes_total`.
- **Unknown values are left out** rather than reported as 0. For example, `traffic_policy_in_sync` is absent while HAProxy is unreachable.

## 2. Alert rules and Prometheus

The rules are in `observability/prometheus/alerts.yml`. There are 15 rules in
three groups:

| Group | Alerts |
|---|---|
| Availability | `TrafficManagerScrapeFailing`, `HAProxyRuntimeUnreachable`, `ControlLoopStalled`, `TrafficPolicyOutOfSync`, `HAProxyRestartDetected`, `MetricsStreamDown` |
| Gateway pools | `GatewayPoolHighErrorRate` (over 1%, only when the pool serves at least 1 req/s), `GatewayPoolHighLatencyP95` (over 1 s), `GatewayInstanceUnhealthy`, `GatewayPoolDegraded`, `GatewayPoolDownWithTrafficAssigned` |
| Migrations | `MigrationSafetyViolation`, `MigrationRolledBackOrFailed`, `MigrationPausedTooLong`, `EmergencyTrafficShift` |

**Tuning:** each threshold is a literal next to its rule; edit the file to
tune it. The values suit the local simulation and are starting points, not
SLOs.

**Not included yet:** routing alerts to on-call. That needs Alertmanager,
which is Phase 2 work.

**Rule tests:** `alerts_test.yml` holds promtool unit tests with synthetic
series. They check that each alert fires, or stays quiet, as intended.

```bash
docker run --rm --entrypoint promtool -v "$PWD/observability/prometheus:/cfg:ro" -w /cfg \
  prom/prometheus:v3.5.0 check rules alerts.yml
docker run --rm --entrypoint promtool -v "$PWD/observability/prometheus:/cfg:ro" -w /cfg \
  prom/prometheus:v3.5.0 test rules alerts_test.yml
```

**Running Prometheus** (opt-in Compose profile, free image). Prometheus keeps
metric history in the `prometheus-data` volume, 15 days by default
(`PROMETHEUS_RETENTION`).

```bash
# once
echo "METRICS_TOKEN=$(openssl rand -hex 32)" >> .env
# also recreates traffic-manager with the token
docker compose --profile observability up -d
open http://127.0.0.1:9090/alerts
```

The token reaches Prometheus as a Compose secret
(`/run/secrets/metrics_token`). It is never written into `prometheus.yml`.

## 3. Load generator (`traffic-generator/loadgen.py`)

The load generator uses only the standard library and runs on Python 3.9 or
later. A run is a list of phases; each phase sets a duration, a concurrency,
and optionally a total requests-per-second target.

| Profile | Shape |
|---|---|
| `sustained` | constant concurrency; closed loop, or paced with `--rps` |
| `ramp` | concurrency rises linearly from `--start` to `--end` |
| `spike` | base → peak → base |
| `steps` | fixed concurrency levels (`--levels 2,8,16,32`) |

```bash
python3 traffic-generator/loadgen.py --profile sustained --duration 30 --concurrency 8 --json out.json --csv out.csv
```

**How responses are attributed:**

- **Pool and instance** come from the `X-Gateway-Pool` / `X-Gateway-Instance` headers. HAProxy adds them only when `EXPOSE_ROUTING_HEADERS=1`, which Compose sets for the local simulation. Keep it at `0` wherever real clients connect.
- **Errors generated by HAProxy itself** also carry these headers, so they are attributed too.
- **No response:** a request that got no HTTP response (connection refused, reset or timeout) is recorded with status 0.

## 4. Load and failure suite (`traffic-generator/run_load_suite.py`)

```bash
python3 traffic-generator/run_load_suite.py --list
# all 12 scenarios, about 12 minutes
python3 traffic-generator/run_load_suite.py
python3 traffic-generator/run_load_suite.py -s sustained -s instance_crash
python3 traffic-generator/run_load_suite.py --set sustained.duration_seconds=300
```

Each scenario does the following:

1. Resets to a clean baseline: no faults, no migration, all instances routable, and the scenario's policy applied through the API.
2. Drives real load through `:8080`.
3. Changes the policy through the API, or injects a fault, at fixed times on the run's clock.
4. Checks the measured values against `traffic-generator/load_thresholds.json`.

| Scenario | What it does | Main checks |
|---|---|---|
| `baseline_compare` | Identical load at 100:0, then at 0:100 | Error rate and p95 for each pool; Hybrid/OPDK p95 and throughput ratios |
| `sustained` | 60 s at 50:50 | Error rate, p95, throughput, split accuracy, Prometheus counters vs client counts |
| `ramp` | Concurrency 1 → 32 | Error rate, p95 |
| `spike` | 4 → 64 → 4 | p95 during and after the spike |
| `instance_unready` | One Hybrid instance's readiness fails, then recovers | No client errors; removal time; rejoin time |
| `instance_crash` | `docker kill` one Hybrid instance, then restart it | Failover time; no errors on OPDK; monitor marks the instance UNHEALTHY; recovery time |
| `pool_outage` | Kill both Hybrid instances | All traffic moves to OPDK; failover and recovery times; split restored |
| `connection_drop` | Hybrid instances reset 10% of connections; readiness stays green | Error rate detected and attributed to Hybrid; OPDK clean; Prometheus agrees with client counts |
| `migration_error_rollback` | Gradual migration while Hybrid returns 30% 5xx | Migration is ROLLED_BACK; time to rollback; policy back at 100:0; no Hybrid traffic afterwards |
| `migration_latency_pause` | Gradual migration with auto-rollback off while Hybrid is slow | Migration PAUSES with a p95 reason; operator rollback restores 100:0 |
| `migration_complete` | Full staged rollout under load | COMPLETED; time taken; error rate; ~100% on Hybrid at the end |
| `haproxy_restart` | `docker restart haproxy` under load | Outage length; policy in sync; split restored |

**Fault types** the simulators accept on `POST /__sim/faults`:
- `error_rate`
- `latency_ms`
- `latency_jitter_ms`
- `drop_rate` (a TCP reset with no response)

Plus `POST /__sim/health` (readiness), and `docker kill`/`start`, and a
HAProxy restart. **Limitation:** `drop_rate` is an application-level stand-in
for packet loss. True network-level loss would need `tc netem` and is not
simulated.

**Reports** are written to `reports/load/<UTC timestamp>/`, which is
git-ignored:
- `summary.json`: every check, measured value and limit, plus markers and per-pool load summaries.
- `junit.xml`: one testcase per scenario.
- `<scenario>.timeline.csv`: per-second requests, errors and p95, by pool and instance.

**Exit codes:** 0 means everything passed, 1 means a check failed, and 2 means
a scenario could not run.

**Safety:**
- The suite refuses to start while a migration is active.
- It records the starting policy and restores it, clears every fault and restarts killed containers at the end, also on Ctrl+C.
- If the stack disappears mid-run, it stops instead of failing every remaining scenario.

## 5. What has been verified

Unit tests and checks:
- **Traffic-manager unit tests:** 150 passed (11 new, in `tests/test_prometheus.py`).
- **Load-generator unit tests:** `traffic-generator/test_loadgen.py`, 10 passed.
- **promtool:** 15 rules valid; all rule tests pass.

Live checks:
- **`/metrics` against the running stack:** returns 401 without a token. Its request counters matched a 29,599-request load test to within one request.
- **Shakedown run** (3 scenarios): all passed.

| Scenario | Result |
|---|---|
| sustained (20 s) | 0 errors; 4,076 req/s; p95 3.6 ms; split exact; Prometheus within 0.19% of client counts |
| connection_drop | 9.96% Hybrid errors detected at a 10% drop rate; 0 OPDK errors; Prometheus within 0.10% |
| instance_crash | 0.0038% errors overall; last error 0.28 s after the kill; 0 OPDK errors; monitor marked it UNHEALTHY; serving again 4.67 s after restart |

**Full-suite run (interrupted):** the stack was stopped manually partway through.
- **Passed before the stop:** `baseline_compare`, `sustained` (60 s, 7,066 req/s), `ramp` and `spike` (p95 7.6 ms at 64 concurrent).
  - `baseline_compare`: Hybrid/OPDK p95 ratio 1.04, throughput ratio 0.97.
- **`instance_unready`:** zero client errors during the fault and the rejoin. It was marked FAIL only because of 326 "no response" errors at 40–41 s, when the stack was taken down.
  - The instance stopped receiving traffic 4.8 s after its readiness failed.
  - It served again 3.9 s after readiness recovered.
- **Not run:** `pool_outage`, `migration_error_rollback`, `migration_latency_pause`, `migration_complete` and `haproxy_restart` have not completed against this build yet. The Prometheus container itself has not been started yet either; only its rules were validated, with promtool.
