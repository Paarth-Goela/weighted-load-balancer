# Local Simulation, Running and Testing

No real Apigee OPDK or Apigee Hybrid environment is needed locally. Four small
Python services **simulate gateway runtime endpoints**.

> **The simulators are not Apigee.** They return JSON naming the simulated
> platform and instance, and expose a readiness endpoint you can switch off.
> They do not implement proxies, policies, authentication, analytics, or
> anything else Apigee does. Results from them prove that the traffic layer
> works, not that Apigee does.

| Container | Simulates | Pool | Host port |
|---|---|---|---|
| `backend-a` (alias `opdk-router-1`) | OPDK Router | OPDK Pool | 8001 |
| `opdk-router-2` | OPDK Router | OPDK Pool | 127.0.0.1:8003 |
| `backend-b` (alias `hybrid-ingress-1`) | Hybrid ingress | Hybrid Pool | 8002 |
| `hybrid-ingress-2` | Hybrid ingress | Hybrid Pool | 127.0.0.1:8004 |

Response example (also served on the readiness path `/healthz`):

```json
{"message":"Request successfully processed","backend":"Backend A","version":"1.0",
 "platform":"APIGEE_OPDK","instance":"opdk-router-1","role":"OPDK Router (simulated)",
 "simulated":true,"hostname":"…","path":"/"}
```

`backend` is kept so the Milestone 1 and 2 tools keep working.

**Fault injection (simulation only).** The readiness path returns 503 while
normal paths keep returning 200. This shows that an open TCP port is not
readiness. A second switch makes normal API calls fail or slow down *while
readiness stays green*. That shows a passing health check is not proof that
the gateway serves API traffic correctly, and it is how the Traffic Shift
safety gates are tested.

```bash
# opdk-router-2 not ready
curl -s -X POST localhost:8003/__sim/health -d '{"ready": false}'
curl -s -X POST localhost:8003/__sim/health -d '{"ready": true}'
# hybrid-ingress-1
curl -s -X POST localhost:8002/__sim/faults -d '{"error_rate": 0.3, "latency_ms": 250}'
curl -s -X POST localhost:8002/__sim/faults -d '{"error_rate": 0, "latency_ms": 0}'
```

**Operations Console:** http://localhost:9000/console/ (sign in with the token
from `.env`). See [traffic-shift.md](traffic-shift.md).

## Run

```bash
cd load-balancer
cp .env.example .env && sed -i '' "s/^TRAFFIC_API_TOKEN=.*/TRAFFIC_API_TOKEN=$(openssl rand -hex 32)/" .env
docker compose up -d --build
# traffic-manager becomes (healthy)
docker compose ps
```

On first start, the traffic-manager imports `config/bootstrap.simulation.json`
(platforms, pools, the four instances, policy 100:0). It adds the instances to
HAProxy in MAINT and enables them after two successful probes, about 4 s later.

```bash
set -a; source .env; set +a; H="Authorization: Bearer $TRAFFIC_API_TOKEN"
curl -s localhost:9000/api/v1/traffic/config -H "$H" | python3 -m json.tool
curl -s -X PUT localhost:9000/api/v1/traffic/config -H "$H" -H 'Content-Type: application/json' \
     -d '{"opdk_weight":90,"hybrid_weight":10,"reason":"canary"}'
python3 traffic-generator/test_distribution.py --a 90 --b 10 --requests 10000
curl -s -X POST localhost:9000/api/v1/traffic/rollback -H "$H"
```

## Using real endpoints instead of simulators

* Register through the API (`POST /api/v1/instances`, see api-reference.md),
  or
* write your own bootstrap JSON (same shape as `config/bootstrap.simulation.json`)
  and set `BOOTSTRAP_FILE` before the **first** start (it is only imported
  into an empty database), and
* set `OPDK_NATIVE_CHECK_PATH` / `HYBRID_NATIVE_CHECK_PATH`,
  `HAPROXY_CA_FILE` and `PROBE_CA_FILE` for your environment.

Choose readiness endpoints that reflect real serving ability for your OPDK
and Hybrid environments (see architecture.md §5). Don't put credentials, keys
or certificates in the bootstrap file or the repository. Mount CA bundles as
files and pass their paths through the environment.

To start over from scratch: `docker compose down -v` (deletes the SQLite volume).

## Tests

### Unit tests: 150 tests, Python 3.12, in Docker, no stack needed

```bash
docker build -f traffic-manager/Dockerfile --target test -t traffic-manager-test .
docker run --rm traffic-manager-test
```

These use an in-memory fake of the HAProxy Runtime API (including dynamic
servers and per-pool readiness), a fake resolver and a fake prober. Real
HTTP/HTTPS probes run against local servers with a throwaway self-signed
certificate.

| Spec test | Where |
|---|---|
| 1 Register OPDK / 2 Register Hybrid | `test_gateways.py::test_1_*`, `test_2_*` |
| 3 Create pools / 4 Multiple instances | `test_gateways.py::test_3_*`, `test_4_*` |
| 5 Update policy / 6 Invalid percentages / 7 Both zero / 8 No restart | `test_traffic.py::test_5_*` … `test_8_*` |
| 9 Health failures / 10 Removal / 11 Recovery | `test_health.py::test_9_*` … `test_11_*` |
| 12 Deregistration | `test_instances.py::test_12_*` |
| 13 Persistence after API restart | `test_persistence.py::test_13_*` (+ HAProxy restart, pool hold) |
| 14 Failed Runtime API updates | `test_failures.py::test_14_*` (+ mid-update failure, connection loss, DB failure, malformed replies) |
| 15 Rollback | `test_traffic.py::test_15_*` |
| 16 Unauthorized requests | `test_security.py::test_16_*` (every endpoint) |
| 17 Concurrent updates | `test_traffic.py::test_17_*` |

### Live integration tests: 16 tests, against the running stack

These restart and stop containers. Use only a local stack. Run every command
**from the `load-balancer/` repository folder**; the paths are relative to it.

```bash
# quote it: the parent folder name has a space
cd "/path/to/load balancer/load-balancer"
python3 -m venv .venv && .venv/bin/pip install pytest httpx
.venv/bin/pytest traffic-manager/tests_integration -m integration -s -v
INTEGRATION_REQUESTS=20000 .venv/bin/pytest traffic-manager/tests_integration -m integration -s
# Traffic Shift only
.venv/bin/pytest traffic-manager/tests_integration/test_live_migrations.py -m integration -s -v
```

`test_live_migrations.py` (8 tests) runs background load through `:8080` and
covers: the console being served, a gradual migration to completion,
error-rate and latency violations injected while health stays green, duplicate
starts and manual-change pausing, a management-service restart
mid-migration, a scheduled shift, and an emergency shift.

Each distribution test sends 10,000 requests through `:8080`, each on a **new
TCP connection** (`Connection: close`), so every request is an independent
HAProxy decision. Tolerance is ±3.29·√(p(1−p)/n), the 99.9% two-sided interval
for an independent random split. It is 0 for 100:0 and 0:100 (checked
exactly).

Result of the verified run:

| Policy | OPDK expected | OPDK observed | Hybrid expected | Hybrid observed | Tolerance | Result |
|---|---:|---:|---:|---:|---:|---|
| 100:0 | 10000 | 10000 | 0 | 0 | exact | PASS |
| 90:10 | 9000 | 9001 | 1000 | 999 | ±0.99 pp | PASS |
| 75:25 | 7500 | 7501 | 2500 | 2499 | ±1.42 pp | PASS |
| 50:50 | 5000 | 5000 | 5000 | 5000 | ±1.65 pp | PASS |
| 25:75 | 2500 | 2499 | 7500 | 7501 | ±1.42 pp | PASS |
| 0:100 | 0 | 0 | 10000 | 10000 | exact | PASS |

**Why the deviations are at most ±1 request.** HAProxy's `roundrobin` is a
*deterministic* smooth weighted round-robin, not random sampling. Over 10,000
requests it produces the exact ratio, apart from where in the cycle the test
happened to start (±1). The statistical tolerance is a ceiling for a random
splitter; this system stays far inside it. Each pool's share is also split
evenly between its two instances (checked in the same test).

The live suite also covers:
- unhealthy instance removal and recovery;
- refusing to shift traffic onto a pool with no healthy instances;
- registering and deregistering an instance;
- invalid and unauthorized requests;
- concurrent updates;
- rollback;
- restarting the management API (state restored from SQLite);
- restarting HAProxy under load (policy and instances restored, and the canary never exceeded its share);
- the Runtime API becoming unreachable (503, nothing persisted, recovery after restart).

### Load and failure suite, Prometheus metrics and alerts

See [observability-and-load-testing.md](observability-and-load-testing.md).

```bash
# 12 scenarios, ~12 min, stack must be up
python3 traffic-generator/run_load_suite.py
# load-generator unit tests, no stack needed
.venv/bin/pytest traffic-generator/test_loadgen.py -q
```

### Milestone 2 suite (still passes)

```bash
./traffic-generator/run_milestone2_tests.sh
```

`set_weights.py` now targets the `traffic_policy` backend (`opdk` = A,
`hybrid` = B). Changes made with it bypass SQLite and show up as
`in_sync: false` in `GET /api/v1/traffic/config`. Re-apply a policy through
the API afterwards.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `traffic-manager` exits immediately | `TRAFFIC_API_TOKEN` is missing or shorter than 16 characters (`docker logs traffic-manager`). |
| `:8080` returns 503 right after `up`/restart | Instances are being re-added and probed (about 5 s). Check `GET /api/v1/health/instances`. |
| `pool_not_eligible` (409) | The target pool has no enabled, HEALTHY, UP instance. Check instance health and simulator readiness. |
| `haproxy_unavailable` (503) | HAProxy is down, or the socket volume is not mounted. Check `docker compose ps` and `docker exec traffic-manager ls -l /run/haproxy-runtime`. |
| `in_sync: false` | Weights were changed out-of-band (e.g. with `set_weights.py`). PUT the desired policy. |
| Instance stuck UNKNOWN | Its readiness path or expected codes are wrong. See `last_health_error` in `/api/v1/health/instances`. |
| Bootstrap ignored | The DB already had platforms. Run `docker compose down -v` to start over. |
