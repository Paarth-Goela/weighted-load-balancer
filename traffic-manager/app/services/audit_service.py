"""Audit log in SQLite. Records changes, failures, rejections and health transitions.

Records never contain credentials or the HAProxy socket path.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional

from app.database.connection import Database, utcnow

log = logging.getLogger(__name__)

MAX_TEXT = 500

# Event types
GATEWAY_REGISTERED = "GATEWAY_REGISTERED"
POOL_CREATED = "POOL_CREATED"
INSTANCE_REGISTERED = "INSTANCE_REGISTERED"
INSTANCE_UPDATED = "INSTANCE_UPDATED"
INSTANCE_DEREGISTERED = "INSTANCE_DEREGISTERED"
INSTANCE_RUNTIME_SYNC = "INSTANCE_RUNTIME_SYNC"
HEALTH_TRANSITION = "HEALTH_TRANSITION"
TRAFFIC_POLICY_CHANGE = "TRAFFIC_POLICY_CHANGE"
TRAFFIC_ROLLBACK = "TRAFFIC_ROLLBACK"
TRAFFIC_POLICY_RESTORE = "TRAFFIC_POLICY_RESTORE"
BOOTSTRAP = "BOOTSTRAP"
POOL_ROUTING_HOLD = "POOL_ROUTING_HOLD"

TRAFFIC_EVENTS = (TRAFFIC_POLICY_CHANGE, TRAFFIC_ROLLBACK, TRAFFIC_POLICY_RESTORE)


def _clip(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = str(v)
    return v if len(v) <= MAX_TEXT else v[:MAX_TEXT] + "..."


def _dump(v: Any) -> Optional[str]:
    return None if v is None else json.dumps(v, default=str)


class AuditService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        event_type: str,
        status: str,
        source: str,
        *,
        reason: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[int] = None,
        entity_name: Optional[str] = None,
        previous: Any = None,
        new: Any = None,
        error: Optional[str] = None,
    ) -> int:
        """Insert an audit record (joins the caller's transaction if any)."""
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO audit_log (timestamp, event_type, status, source, reason, entity_type,"
                " entity_id, entity_name, previous_state, new_state, error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (utcnow(), event_type, status, _clip(source) or "unknown", _clip(reason), entity_type,
                 entity_id, entity_name, _dump(previous), _dump(new), _clip(error)),
            )
            audit_id = cur.lastrowid
        log.info("audit %s %s %s %s", event_type, status, entity_name or "", error or "")
        return audit_id

    def list(
        self,
        *,
        event_types: Optional[Iterable[str]] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        """Most recent `limit` entries after skipping `offset` newest, returned oldest first."""
        where, params = [], []
        if event_types:
            types = list(event_types)
            where.append(f"event_type IN ({','.join('?' * len(types))})")
            params += types
        if entity_type:
            where.append("entity_type = ?")
            params.append(entity_type)
        if entity_id is not None:
            where.append("entity_id = ?")
            params.append(entity_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = self.db.query_one(f"SELECT count(*) AS n FROM audit_log {clause}", tuple(params))["n"]
        rows = self.db.query(
            f"SELECT * FROM audit_log {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )
        entries = []
        for r in reversed(rows):
            d = dict(r)
            for k in ("previous_state", "new_state"):
                d[k] = json.loads(d[k]) if d[k] else None
            entries.append(d)
        return total, entries
