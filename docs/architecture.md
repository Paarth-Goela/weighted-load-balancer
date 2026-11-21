# Architecture — Milestone 3: Apigee OPDK / Hybrid Gateway Traffic Management

An HAProxy Layer 7 traffic layer in front of two Apigee runtime environments,
plus a FastAPI control plane. The control plane registers gateways, pools and
instances, monitors instance readiness, and shifts traffic between OPDK and
Hybrid **without restarting HAProxy**.

> **Scope boundary.** This system shifts *traffic*. It does not migrate API
> proxies, deploy anything to Apigee, or verify that OPDK and Hybrid behave
> identically. Those need separate validation.

## 1. Components

```
                    API consumers
                         |
                         v  :8080
 +--------------------------------------------------------------+
 | HAProxy (data plane)                                          |
 |                                                               |
 |  frontend http_frontend                                       |
 |        |                                                      |
 |  backend traffic_policy  (balance roundrobin)   <-- POOL weights = traffic policy
 |     server opdk   abns@apigee_opdk_pool   weight 90 check     |
 |     server hybrid abns@apigee_hybrid_pool weight 10 check     |
 |        |                         |                            |
 |  frontend apigee_opdk_entry    frontend apigee_hybrid_entry   |
 |   monitor-uri /__pool_ready     monitor-uri /__pool_ready     |
 |   (fails if nbsrv == 0)         (fails if nbsrv == 0)         |
 |        |                         |                            |
 |  backend apigee_opdk_pool      backend apigee_hybrid_pool  <-- INSTANCE weights
 |   opdk-router-1  (dynamic)      hybrid-ingress-1 (dynamic)    |
 |   opdk-router-2  (dynamic)      hybrid-ingress-2 (dynamic)    |
 +---------|-----------------------------|-----------------------+
           v                             v
   OPDK routers / MPs             Hybrid ingress / runtime
   (simulated locally)            (simulated locally)

 Runtime API: /var/lib/haproxy/admin.sock  (Unix socket, mode 600, uid 99,
              shared read-only with traffic-manager via a named volume)
                         ^
                         |  add/del server, set weight/state/addr, show ...
 +--------------------------------------------------------------+
 | traffic-manager (control plane, FastAPI, 127.0.0.1:9000)      |
 |   API (bearer token) -> Gateway / Traffic managers            |
 |   Control loop: Health monitor -> Reconciler                  |
 |   SQLite (named volume tm-data): source of truth + audit log  |
 +--------------------------------------------------------------+
```

The control plane is **never in the request path**. If it stops, HAProxy
keeps routing with its last runtime state.

## 2. Two-tier routing: pool weights vs instance weights

| Concept | Where | Meaning |
|---|---|---|
| **Pool weight** (traffic policy) | `traffic_policy/opdk`, `traffic_policy/hybrid` | Share of *all* requests sent to each platform. `opdk_weight + hybrid_weight = 100`. |
| **Instance weight** (`lb_weight`) | servers inside `apigee_*_pool` | How a pool splits *its own* share between its instances. |

They are independent. With policy 90:10 and two equal OPDK routers, each router
gets 45%. If one router fails, the remaining router gets the full 90%, and
Hybrid stays at 10%. A single flat server list would shift the OPDK/Hybrid
split whenever an instance failed. Two tiers prevent that.

Each pool server in `traffic_policy` is an in-process link (abstract Unix
socket) to that pool's frontend. Its health check calls `/__pool_ready`,
which fails when the pool has no usable server (`nbsrv == 0`). **A pool with
no usable instance therefore drops out of the split automatically**, and the
other pool takes all traffic if its weight is > 0. A pool at weight 0 never
receives traffic, even if the other pool is down (requests get 503).

## 3. Control-plane modules (`traffic-manager/app`)

| Module | Responsibility |
|---|---|
| `services/haproxy_client.py` | Runtime API client. Reuses the Milestone 2 `haproxy_runtime` module for policy weights (validated, increases-first, verified, rolled back). Adds dynamic servers, state, weight and address changes, read-back verification, malformed-response detection, and refuses `;`/newline in commands. |
| `services/gateway_manager.py` | Platforms, pools, instances: SQLite plus HAProxy registration, update and safe deregistration. |
| `services/traffic_manager.py` | Traffic policy transactions, versions, rollback, history. |
| `services/health_monitor.py` | HTTP/HTTPS readiness probes and the health state machine. |
| `services/reconciler.py` | Makes HAProxy match SQLite. Restores state after restarts. Pool hold/release. |
| `services/audit_service.py` | Audit log (SQLite). |
| `services/control.py` | Shared control lock; DNS resolution. |
| `database/` | SQLite connection, migration runner, numbered SQL migrations. |
| `bootstrap.py` | First-start import from `BOOTSTRAP_FILE`. |

### Concurrency
Every HAProxy-mutating operation (API changes, health transitions,
reconciliation) takes one re-entrant **control lock**, with a bounded wait
(`LOCK_TIMEOUT_SECONDS`, then 503 `update_in_progress`). Runtime API commands
from two requests can never interleave. Run **one Uvicorn worker**: the lock
is per process.

## 4. Traffic policy transaction

```
PUT /api/v1/traffic/config {opdk_weight, hybrid_weight}
 1. schema validation: strict ints, 0..100, sum == 100   -> 422 (audited REJECTED)
 2. acquire control lock                                   -> 503 if busy
 3. eligibility: each pool with weight > 0 is enabled and has >= 1 routable
    instance (enabled + HEALTHY + READY/UP in HAProxy)     -> 409 pool_not_eligible
 4. preflight: read current runtime weights               -> 503 if unreachable
 5. apply: increases first (the total never reaches 0)
 6. read back and verify; on failure restore previous      -> 502 (rolled back)
                                    rollback also failed   -> 500 inconsistent_state
                                                              (control loop repairs)
 7. one SQLite transaction: deactivate old version, insert new active version,
    mirror pool.traffic_weight, audit SUCCESS. If the DB fails, compensate
    the runtime                                            -> 500 persistence_failed
```

HAProxy's Runtime API has **no multi-server transaction**. Atomicity is
emulated with ordering, read-back verification and compensation.

**Rollback** (`POST /traffic/rollback`) re-applies an earlier version as a
*new* version, so history is never rewritten. By default it restores the
version active before the current one, so rolling back twice returns to where
you started. `to_version` selects any version. It goes through the same
transaction, including the eligibility check.

## 5. Instance lifecycle and health

```
register ──> HAProxy: add server (MAINT), enable health      DB: UNKNOWN
                 │
     health monitor: recovery_threshold consecutive successes
                 v
             HEALTHY ──reconciler──> set server state ready  (receives traffic)
                 │
     failure_threshold consecutive failures
                 v
            UNHEALTHY ──reconciler──> set server state maint (removed from routing)
                 │
     recovery_threshold successes (+ optional recovery_hold_seconds)
                 v
             HEALTHY ──> ready (rejoins)

deregister: drain -> wait for sessions (DRAIN_TIMEOUT_SECONDS) -> maint
            -> shutdown sessions -> del server -> verify gone -> DB delete
```

* Probes run outside the lock, in parallel. Results are applied under the
  lock. A result is discarded if the instance was reconfigured during the
  probe.
* Every transition is timestamped and audited (`HEALTH_TRANSITION`), and so
  is every routing change the reconciler makes (`INSTANCE_RUNTIME_SYNC`).
* An instance is routable only if it is **enabled**, **HEALTHY** in the
  monitor, **and** UP in HAProxy's own native check (the safety net).
* Safety: disabling, reconfiguring or deregistering the last routable
  instance of a pool that carries traffic returns 409 unless `force=true`.
* Changing host/port/protocol/TLS replaces the HAProxy server. It goes back
  to UNKNOWN and must pass probes again.

**What a probe proves.** A successful TCP connection, or an HTTP 200 from a
generic health URL, does **not** prove that API proxies are deployed, that
authentication policies work, that client-facing certificates are valid, or
that backends are reachable. For real OPDK/Hybrid deployments, point
`health_check_path` at an environment-specific readiness endpoint (for
example, a dedicated health-check proxy that exercises the critical path) and
set `expected_status_codes` to match.

## 6. Persistence and reconciliation

SQLite (`/app/data/traffic-manager.db`, WAL, `synchronous=FULL`) is the source
of truth for platforms, pools, instances, policy versions and the audit log.
Timestamps are ISO-8601 UTC. Schema changes are numbered SQL migrations,
applied once each inside a transaction and tracked in `schema_migrations`.

The **control loop** (every `HEALTH_CHECK_INTERVAL_SECONDS`) probes, applies
transitions, then reconciles:

| Situation | Reconciler action |
|---|---|
| Management API starts | Re-apply the active policy if runtime differs. Re-add missing instances. Any instance HAProxy has in MAINT needs a fresh probe before it is enabled. |
| HAProxy restarted (`Start_time_sec` changed) | Hold both pool links in MAINT. Re-add all instances in MAINT. Restore the policy. Enable instances after a fresh successful probe. Release both pool links together once every weighted pool is ready (see below). |
| Instance missing from HAProxy | Re-add in MAINT. |
| Server in HAProxy but not registered | Drain and delete it (a deregistered instance never comes back). |
| Container IP changed | Re-resolve the hostname and `set server addr`. |
| Instance weight drift | `set server weight`. |
| Out-of-band *policy* weight change while HAProxy runs | **Not reverted**; reported as `in_sync: false`. |

**Pool hold and release after an HAProxy restart.** The two pool links have
independent 1-second readiness checks. Without a hold, whichever pool became
ready first would briefly take 100% of traffic, so a 10% Hybrid canary could
take everything for a second. The reconciler holds both links in MAINT and
releases them back-to-back. Release happens once every pool with weight > 0
either has a routable instance (and has been ready longer than one check
interval) or has nothing that could recover, or after
`POOL_RELEASE_TIMEOUT_SECONDS`. Verified live: during an HAProxy restart under
load at 90:10, Hybrid received 9.98% of served requests.

## 7. Security

* **Runtime API:** Unix socket only, `mode 600`, owned by uid 99. It is shared
  only with the traffic-manager container (which runs as uid 99), on a
  read-only mount. No TCP listener, no published port, unreachable from the
  simulator containers.
* **Command safety:** every name, host and SNI value is validated against
  strict allow-lists before it reaches a command. The client also refuses any
  command containing `;`, CR or LF.
* **Management API:** bound to `127.0.0.1:9000`. All `/api/v1/*` endpoints
  require `Authorization: Bearer $TRAFFIC_API_TOKEN` (401 if missing, 403 if
  wrong; constant-time comparison). Only `/livez` (returns `{"status":"ok"}`)
  and `/docs` are public. The service refuses to start without a token of at
  least 16 characters.
* **Secrets:** environment only (`.env`, git-ignored). Error responses and
  audit records never include the token or the socket path.
* **POC limitations:** a single shared token (no per-user identity, scopes,
  rotation or expiry); `X-Actor` is recorded but unverified; no TLS on :9000.
  Keep it on localhost or put it behind a TLS proxy.

## 8. Known limitations

1. **HAProxy restart outage window.** Instances are dynamic servers, which
   HAProxy loses on restart. Until the control plane re-adds them and they
   pass a fresh probe, `:8080` returns 503. Measured at 4.8–5.1 s (3 restarts) with a
   2 s loop interval, including the pool hold. If the management service is down, the outage lasts until
   it returns. A later milestone could render a static bootstrap config or use
   `server-state-file`.
2. **No DNS resolvers for dynamic servers (HAProxy 3.0).** Hostnames are
   resolved by the control plane to IPv4 and refreshed every cycle. IPv6
   instances are not supported yet.
3. **Host header:** client Host headers pass through unchanged. Per-instance
   `host_header` applies to health probes only, because HAProxy cannot rewrite
   Host per server.
4. **One platform per type and one pool per platform.** This matches the two
   HAProxy pool backends. Adding another gateway type (Kong, NGINX, ...) means
   adding a `PlatformType`, a pool backend in `haproxy.cfg`, and a
   generalized policy schema.
5. **Single process.** The control lock is in-process: run one worker and
   one replica.
6. **Native HAProxy check path is per pool** (`OPDK_NATIVE_CHECK_PATH`,
   `HYBRID_NATIVE_CHECK_PATH`). Per-instance paths, Host headers and SNI are
   enforced by the management monitor.
7. **Audit log growth:** there is no retention policy yet.
