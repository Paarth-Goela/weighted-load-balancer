-- Traffic Shift Management: persistent migration (gradual / scheduled) state.
-- Allocations are stored as the OPDK percentage; Hybrid = 100 - opdk.

CREATE TABLE traffic_migrations (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    name                    TEXT NOT NULL,
    mode                    TEXT NOT NULL CHECK (mode IN ('GRADUAL', 'SCHEDULED')),
    source_platform         TEXT NOT NULL CHECK (source_platform IN ('APIGEE_OPDK', 'APIGEE_HYBRID')),
    destination_platform    TEXT NOT NULL CHECK (destination_platform IN ('APIGEE_OPDK', 'APIGEE_HYBRID')),
    initial_opdk            INTEGER NOT NULL CHECK (initial_opdk BETWEEN 0 AND 100),
    target_opdk             INTEGER NOT NULL CHECK (target_opdk BETWEEN 0 AND 100),
    current_opdk            INTEGER NOT NULL CHECK (current_opdk BETWEEN 0 AND 100),
    last_good_opdk          INTEGER NOT NULL CHECK (last_good_opdk BETWEEN 0 AND 100),
    step_percent            INTEGER,                 -- GRADUAL only
    interval_seconds        INTEGER NOT NULL,        -- observation window per stage
    min_requests            INTEGER NOT NULL DEFAULT 0,
    max_error_rate_percent  REAL NOT NULL,
    max_p95_ms              INTEGER NOT NULL,
    min_healthy_instances   INTEGER NOT NULL DEFAULT 1,
    auto_rollback           INTEGER NOT NULL DEFAULT 1 CHECK (auto_rollback IN (0, 1)),
    require_approval        INTEGER NOT NULL DEFAULT 0 CHECK (require_approval IN (0, 1)),
    status                  TEXT NOT NULL CHECK (status IN ('SCHEDULED', 'RUNNING', 'WAITING_APPROVAL', 'PAUSED',
                                                            'COMPLETED', 'FAILED', 'CANCELLED', 'ROLLED_BACK')),
    status_reason           TEXT,
    current_stage           INTEGER NOT NULL DEFAULT 0,   -- seq of the last applied stage (0 = none)
    next_action_at          TEXT,
    -- 1 while the migration is active (SCHEDULED/RUNNING/WAITING_APPROVAL/PAUSED), NULL when finished.
    -- UNIQUE => at most one active migration, enforced by the database itself.
    active_slot             INTEGER UNIQUE CHECK (active_slot IS NULL OR active_slot = 1),
    created_by              TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    finished_at             TEXT,
    CHECK (source_platform <> destination_platform)
);

CREATE TABLE traffic_migration_stages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    migration_id    INTEGER NOT NULL REFERENCES traffic_migrations(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    planned_opdk    INTEGER NOT NULL CHECK (planned_opdk BETWEEN 0 AND 100),
    scheduled_at    TEXT,                           -- SCHEDULED mode
    status          TEXT NOT NULL CHECK (status IN ('PENDING', 'APPLIED', 'PASSED', 'FAILED', 'CANCELLED')),
    applied_at      TEXT,
    observe_from    TEXT,                           -- start of the current observation window
    completed_at    TEXT,
    actual_opdk     INTEGER,                        -- read back from HAProxy after applying
    policy_version  INTEGER REFERENCES traffic_policies(id),
    safety_checks   TEXT,                           -- JSON
    metrics         TEXT,                           -- JSON
    reason          TEXT,
    UNIQUE (migration_id, seq)
);
CREATE INDEX traffic_migration_stages_mig_idx ON traffic_migration_stages(migration_id, seq);
