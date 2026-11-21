# Milestone 2 — Weighted Traffic Distribution

> **Updated by Milestone 3.** The HAProxy backend `http_backends`
> (`backend-a`/`backend-b`) is now the two-tier `traffic_policy` backend
> (`opdk` = A, `hybrid` = B) in front of the `apigee_opdk_pool` and
> `apigee_hybrid_pool` backends. `set_weights.py` and `test_distribution.py`
> use the new names by default. The Runtime API socket path is unchanged.
> Weight changes are now normally made through the Traffic Management API
> (see `architecture.md`, `api-reference.md`, `local-simulation.md`).

Extends the Milestone 1 HAProxy Layer 7 load balancer with:

- static weighted routing (weights in `haproxy.cfg`)
- **runtime** weight changes through the HAProxy Runtime API, with no restart or reload
- a traffic generator that measures the actual split HAProxy produces

HAProxy makes every routing decision. Python only sets weights and analyses results.

---

## 1. What changed

| File | Change |
|---|---|
| `haproxy/haproxy.cfg` | Added the admin `stats socket`, the L7 health check, and `weight` on each server. The frontend, ports and `balance roundrobin` are unchanged. |
| `runtime/haproxy_runtime.py` | **New.** Runtime API client: validation, reading and writing weights, ordered apply, rollback. |
| `runtime/set_weights.py` | **New.** CLI for changing weights live. |
| `runtime/test_haproxy_runtime.py` | **New.** Offline unit tests: validation, ordering, rollback. |
| `traffic-generator/test_distribution.py` | **New.** Distribution test. |
| `traffic-generator/run_milestone2_tests.sh` | **New.** Runs Tests 1–7 end to end. |
| `docker-compose.yml`, `backend-a/`, `backend-b/` | **Unchanged.** |

The boot-time weights are **50:50**. Under `roundrobin` this gives the same strict
A, B, A, B alternation as Milestone 1, so the existing behaviour is preserved.
The other configurations (100:0, 90:10, …) are applied at runtime.

No new dependencies: the scripts use only the Python standard library (3.9+), and
the HAProxy image already ships `socat`.

---

## 2. How HAProxy weighted routing works

```
backend http_backends
    balance roundrobin
    server backend-a backend-a:8000 check ... weight 80
    server backend-b backend-b:8000 check ... weight 20
```

- **`balance roundrobin`** is HAProxy's *dynamic* weighted round-robin. Each server
  is picked in proportion to its weight, and the weights can be changed at runtime.
  `static-rr` also honours weights but **cannot** change them at runtime (only 0%
  or 100%), so it is not suitable here. `leastconn` and the hash algorithms also use
  weights, but they don't produce a predictable percentage split.
- Selection uses a smooth, deterministic weighted round-robin, not random sampling.
  80:20 gives the pattern "4 × A, 1 × B" spread evenly over each cycle of 5. The
  observed split therefore matches the configured weights almost exactly, often to
  ±1 request in 10,000, instead of drifting statistically.
- Selection happens **per HTTP request** (`mode http`). The test opens a new TCP
  connection for every request (`Connection: close`), so each request is an
  independent load-balancing decision and keep-alive connections can't skew the
  result.

### Server weights vs. percentage-based distribution

A **weight** is a relative integer (0–256) attached to one server. HAProxy never
stores percentages. A server's share of traffic is its weight divided by the sum of
the weights of all *eligible* (UP, not in maintenance) servers:

```
share(A) = weight(A) / (weight(A) + weight(B))      80 / (80 + 20) = 80 %
```

Consequences:

- **Only the ratio matters.** `80:20`, `8:2` and `4:1` all route 80%/20%. This
  project uses weights that add up to 100, so weight = percentage, which makes them
  easy to read.
- **Shares are recomputed over healthy servers.** If B (weight 20) fails its health
  check, A receives 100% even though its weight is still 80. A percentage is a result
  of weight plus health; a weight is not a guarantee of a percentage.
- **Weight 0 = no new traffic.** The server stays configured and health-checked
  (handy for a canary that is deployed but not yet live). Requests already in flight
  are not cut off.
- **Resolution.** Weights are integers up to 256, so the finest split between two
  servers is 1:255 (about 0.4%). With a 100-point scale the step is 1%.
- **0:0 is invalid.** Without any eligible server, HAProxy would return 503. The
  tools reject it.

---

## 3. Runtime API (admin socket)

```
global
    stats socket /var/lib/haproxy/admin.sock mode 600 level admin
    stats timeout 10s
```

### How the socket is protected

- It is a **Unix socket only**. It has no TCP listener and no Docker `ports:` entry,
  so it can't be reached from the network, including from the backend containers on
  the same bridge network.
- With `mode 600`, only the `haproxy` user inside the container can open it.
- The host reaches it only through `docker exec haproxy socat ...`. Anyone who can
  change weights therefore already has Docker daemon access, which on that host is
  equivalent to root.

### Raw commands (what the scripts send)

```bash
SOCK="docker exec -i haproxy socat stdio unix-connect:/var/lib/haproxy/admin.sock"
echo "show servers state http_backends"           | $SOCK
echo "get weight http_backends/backend-a"         | $SOCK   # -> "80 (initial 50)"
echo "set server http_backends/backend-a weight 80" | $SOCK # empty reply = success
echo "show info"                                  | $SOCK   # Pid, Uptime, ...
```

### `runtime/set_weights.py`

```bash
python3 runtime/set_weights.py --show              # current weights, health, share
python3 runtime/set_weights.py --a 90 --b 10
python3 runtime/set_weights.py --scenario canary-2 # 80:20
python3 runtime/set_weights.py --list-scenarios
```

| Scenario | A | B |
|---|---:|---:|
| `baseline` | 100 | 0 |
| `canary-1` | 90 | 10 |
| `canary-2` | 80 | 20 |
| `canary-3` | 75 | 25 |
| `equal` | 50 | 50 |
| `final` | 0 | 100 |

Exit codes: `0` = applied, `1` = HAProxy or Docker error (previous weights kept),
`2` = invalid input (nothing sent).

### Safety guarantees in `set_weights()`

1. **Validation before contact.** Each weight must be a plain integer from 0 to 256,
   and A + B must be greater than 0. Negatives, `abc`, `1.5`, `50%`, empty values,
   a missing `--a`/`--b` and `0:0` are rejected locally. Nothing is sent to HAProxy.
2. **Preflight.** The current weights are read and both servers must exist. If this
   step fails, nothing has been changed.
3. **Ordered apply.** Increases are applied before decreases, so the total never
   passes through 0. For example, 100:0 → 0:100 goes through 100:100, never 0:0,
   which would cause 503s.
4. **Read-back verification** after applying.
5. **Rollback.** If any step fails, servers that were already changed are set back to
   their previous weights and the error says so.

The Runtime API has no multi-command transaction, so for one 2-step change there
are a few milliseconds where one server has its new weight and the other still has
its old one. Point 3 ensures that intermediate state is always a valid one with
traffic flowing.

---

## 4. Running the tests

Prerequisites (run from `load-balancer/`):

```bash
docker compose up -d --build
```

If you are upgrading a stack that was already running from Milestone 1, run
`docker compose up -d --force-recreate haproxy` once. This is the only time HAProxy
needs a restart: it loads the new config with the admin socket. Every weight change
after that is done live.

### Run everything

```bash
./traffic-generator/run_milestone2_tests.sh              # 10,000 requests per test
REQUESTS=2000 TOLERANCE=1 ./traffic-generator/run_milestone2_tests.sh
```

The script runs the Milestone 1 endpoint pre-check, the unit tests and Tests 1–7,
prints a summary (`SUMMARY: 9 passed, 0 failed`), and leaves HAProxy at 50:50.

### Individual tests

`test_distribution.py` options:

- `--a/--b` or `--scenario`: the expected weights. If you give neither, the live
  HAProxy weights are used.
- `--apply`: set those weights through the Runtime API first.
- `--requests N` (default 1000)
- `--tolerance PP`: percentage points, default 2. 0% and 100% are always checked
  **exactly**.
- `--concurrency N` (default 10)
- `--url`

Exit code: `0` PASS, `1` FAIL, `2` invalid input.

| Test | Command |
|---|---|
| 1 — 90:10 | `python3 traffic-generator/test_distribution.py --a 90 --b 10 --requests 10000 --apply` |
| 2 — 80:20 | `python3 traffic-generator/test_distribution.py --a 80 --b 20 --requests 10000 --apply` |
| 75:25 | `python3 traffic-generator/test_distribution.py --scenario canary-3 --requests 10000 --apply` |
| 3 — 50:50 | `python3 traffic-generator/test_distribution.py --a 50 --b 50 --requests 10000 --apply` |
| 4 — 100:0 | `python3 traffic-generator/test_distribution.py --a 100 --b 0 --requests 10000 --apply` |
| 5 — 0:100 | `python3 traffic-generator/test_distribution.py --a 0 --b 100 --requests 10000 --apply` |

**Test 6: dynamic configuration (no restart)**

```bash
docker inspect -f '{{.State.StartedAt}}' haproxy        # note the value
python3 runtime/set_weights.py --a 90 --b 10
python3 traffic-generator/test_distribution.py --a 90 --b 10 --requests 10000
python3 runtime/set_weights.py --a 80 --b 20
python3 traffic-generator/test_distribution.py --a 80 --b 20 --requests 10000
python3 runtime/set_weights.py --a 50 --b 50
python3 traffic-generator/test_distribution.py --a 50 --b 50 --requests 10000
docker inspect -f '{{.State.StartedAt}}' haproxy        # identical -> no restart
```

The runner script also compares the HAProxy worker `Pid` from `show info` before and
after, which shows there was no reload either.

**Test 7: invalid configurations.** Each command must exit non-zero, and
`--show` must report the same weights afterwards.

```bash
python3 runtime/set_weights.py --a 80 --b 20     # known starting point
python3 runtime/set_weights.py --a -10 --b 20    # negative
python3 runtime/set_weights.py --a 80 --b -1     # negative
python3 runtime/set_weights.py --a 0 --b 0       # zero total
python3 runtime/set_weights.py --a abc --b 20    # not an integer
python3 runtime/set_weights.py --a 1.5 --b 20    # not an integer
python3 runtime/set_weights.py --a 300 --b 20    # above HAProxy max (256)
python3 runtime/set_weights.py --a 80            # missing --b
python3 runtime/set_weights.py --b 20            # missing --a
python3 runtime/set_weights.py                   # no arguments
python3 runtime/set_weights.py --a 50 --b 50 --server-b backend-x  # unknown server
python3 runtime/set_weights.py --show            # still 80:20
```

Rollback after a failure part-way through an update can't be triggered safely
against a live HAProxy, so it is covered by the unit tests:

```bash
python3 runtime/test_haproxy_runtime.py -v
```

---

## 5. Expected output

Results from the verified run on HAProxy 3.0.31 with 10,000 requests per test.
A count of ±1 comes from wherever the round-robin cycle happened to be when the
test started.

| Test | Weights | Backend A | Backend B | Result |
|---|---|---:|---:|---|
| 1 | 90:10 | 9001 (90.01%) | 999 (9.99%) | PASS |
| 2 | 80:20 | 8001 (80.01%) | 1999 (19.99%) | PASS |
| — | 75:25 | 7500 (75.00%) | 2500 (25.00%) | PASS |
| 3 | 50:50 | 5000 (50.00%) | 5000 (50.00%) | PASS |
| 4 | 100:0 | 10000 (100%) | 0 | PASS |
| 5 | 0:100 | 0 | 10000 (100%) | PASS |
| 6 | 90:10 → 80:20 → 50:50 live | as above | as above | PASS (same StartedAt, same Pid) |
| 7 | invalid inputs | — | — | PASS (all rejected, 80:20 preserved) |

Sample output:

```
Applied weights via Runtime API: 90:10 -> 80:20

WEIGHTED ROUTING TEST

Target:         http://localhost:8080/
Weights:        A=80 B=20 (from command line)
Total requests: 10000  (concurrency 10, new TCP connection per request, 6.3s, 1580 req/s)
Tolerance:      +/-2 percentage points (0% / 100% checked exactly)

Backend A:
Expected: 80%
Observed: 80.01%
Requests: 8001
Deviation: +0.01 pp  [ok]

Backend B:
Expected: 20%
Observed: 19.99%
Requests: 1999
Deviation: -0.01 pp  [ok]

Distribution test: PASS
```

Weight change:

```
$ python3 runtime/set_weights.py --a 80 --b 20
Weights updated without restart: 90:10 -> 80:20
  backend-a: 80.00% of new requests
  backend-b: 20.00% of new requests
```

Rejected input:

```
$ python3 runtime/set_weights.py --a 0 --b 0
REJECTED: combined weight must be greater than zero (0:0 would leave no server eligible for traffic). No changes were made.
```

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `cannot reach HAProxy runtime socket ... No such file` | HAProxy is still running the Milestone 1 config. Run `docker compose up -d --force-recreate haproxy` once. |
| `cannot reach ... No such container: haproxy` | The stack is down, or the container name is different. Run `docker compose up -d`, or pass `--container` / set `HAPROXY_CONTAINER`. |
| `'docker' executable not found` | Docker CLI isn't on PATH, or Docker Desktop isn't running. |
| Observed split is far from expected, with a `WARNING: HAProxy is currently configured X:Y` | The test expected different weights from the live ones. Add `--apply`, or run `set_weights.py` first. |
| One backend gets 100% even though both weights are > 0 | The other backend is DOWN or in MAINT. Run `python3 runtime/set_weights.py --show` and look at `status=`. Check `docker compose ps` and `docker logs backend-b`. |
| `Failed / unidentified requests: HTTP 503` | No eligible server: both are down, or both weights are 0 (the tools prevent the latter). Check the health status as above. |
| `error: ConnectionRefusedError` / timeouts | Nothing is listening on :8080 (check `docker compose ps`), or the concurrency is too high for the machine. Use `--concurrency 4`. |
| Weights went back to 50:50 | Runtime changes live in memory. A container restart or recreate reloads the boot weights from `haproxy.cfg` (see Limitations). |
| `Backend is using a static LB algorithm` | Someone changed `balance` to `static-rr`. Use `roundrobin`. |
| Backend logs show `GET /health` every 3 s | Expected: these are HAProxy's L7 health checks. |

Useful commands:

```bash
docker compose ps
docker logs haproxy --tail 50
python3 runtime/set_weights.py --show
echo "show stat" | docker exec -i haproxy socat stdio unix-connect:/var/lib/haproxy/admin.sock | cut -d, -f1,2,18,19,35
```

---

## 7. Limitations and notes

- **Runtime weights are not persisted.** They last as long as the HAProxy process.
  After a container restart, the weights in `haproxy.cfg` (50:50) apply again. To
  make a configuration permanent, edit `weight` in `haproxy.cfg` as well. A later
  milestone could add `server-state-file` together with
  `show servers state > file` before reloads.
- **Two commands, not one transaction.** See section 3. The ordering and rollback
  keep every intermediate state valid.
- **Stickiness is not configured.** Each request is balanced on its own, which is
  what a measurable weighted test needs. For a real canary, users would usually be
  pinned with a cookie (`cookie SRV insert`). That is out of scope here.
- Everything above was verified against HAProxy **3.0.31** (`haproxy:3.0-alpine`).
  `set server <be>/<srv> weight` and `show servers state` have been available since
  HAProxy 1.6/1.7, so no version limitation applies.
