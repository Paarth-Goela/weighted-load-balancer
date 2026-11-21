-- Milestone 3: gateway platforms, pools, instances, traffic policies, audit.
-- Timestamps: ISO-8601 UTC strings.

CREATE TABLE gateway_platforms (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    -- One platform per type: each type maps to exactly one HAProxy pool backend.
    platform_type   TEXT NOT NULL UNIQUE CHECK (platform_type IN ('APIGEE_OPDK', 'APIGEE_HYBRID')),
    description     TEXT NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE gateway_pools (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    platform_id     INTEGER NOT NULL UNIQUE REFERENCES gateway_platforms(id) ON DELETE RESTRICT,
    description     TEXT NOT NULL DEFAULT '',
    -- Mirror of the active traffic policy (pool-level share, 0-100).
    traffic_weight  INTEGER NOT NULL DEFAULT 0 CHECK (traffic_weight BETWEEN 0 AND 100),
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE gateway_instances (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    name                    TEXT NOT NULL UNIQUE,      -- also the HAProxy server name
    pool_id                 INTEGER NOT NULL REFERENCES gateway_pools(id) ON DELETE RESTRICT,
    host                    TEXT NOT NULL,
    port                    INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    protocol                TEXT NOT NULL CHECK (protocol IN ('HTTP', 'HTTPS')),
    health_check_path       TEXT NOT NULL,
    expected_status_codes   TEXT NOT NULL,             -- JSON array of ints
    host_header             TEXT,
    tls_verify              INTEGER NOT NULL DEFAULT 1 CHECK (tls_verify IN (0, 1)),
    tls_server_name         TEXT,
    -- Instance-level load-balancing weight inside its pool (NOT the pool share).
    lb_weight               INTEGER NOT NULL DEFAULT 100 CHECK (lb_weight BETWEEN 1 AND 256),
    enabled                 INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    current_health_status   TEXT NOT NULL DEFAULT 'UNKNOWN'
                            CHECK (current_health_status IN ('UNKNOWN', 'HEALTHY', 'UNHEALTHY')),
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    consecutive_successes   INTEGER NOT NULL DEFAULT 0,
    last_health_check       TEXT,
    last_health_error       TEXT,
    last_status_change      TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);
CREATE INDEX gateway_instances_pool_idx ON gateway_instances(pool_id);

-- Every successfully applied policy is a new row (id = version).
-- Exactly one row is enabled (active).
CREATE TABLE traffic_policies (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    opdk_weight         INTEGER NOT NULL CHECK (opdk_weight BETWEEN 0 AND 100),
    hybrid_weight       INTEGER NOT NULL CHECK (hybrid_weight BETWEEN 0 AND 100),
    enabled             INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    reason              TEXT,
    updated_by          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    previous_policy_id  INTEGER REFERENCES traffic_policies(id),
    CHECK (opdk_weight + hybrid_weight = 100)
);
CREATE UNIQUE INDEX traffic_policies_one_active ON traffic_policies(enabled) WHERE enabled = 1;

CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('SUCCESS', 'FAILED', 'REJECTED', 'INFO')),
    source          TEXT NOT NULL,
    reason          TEXT,
    entity_type     TEXT,
    entity_id       INTEGER,
    entity_name     TEXT,
    previous_state  TEXT,      -- JSON
    new_state       TEXT,      -- JSON
    error           TEXT
);
CREATE INDEX audit_log_event_idx ON audit_log(event_type, id);
CREATE INDEX audit_log_entity_idx ON audit_log(entity_type, entity_id, id);
