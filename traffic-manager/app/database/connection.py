"""
SQLite access layer + migration runner (standard library only).

* One connection shared by all threads, serialized by an RLock.
  The service runs a single Uvicorn worker.
* WAL journal, foreign keys ON, synchronous=FULL for durability.
* `transaction()` uses BEGIN IMMEDIATE. Nested calls join the outer
  transaction, so services can compose writes (e.g. change + audit record)
  atomically.
* Migrations: numbered *.sql files in ./migrations, applied once each,
  in order, inside a transaction, and tracked in `schema_migrations`.
* All timestamps are stored as ISO-8601 UTC strings.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = FULL")
            self._conn.execute("PRAGMA busy_timeout = 5000")

    # ------------------------------------------------------------------ core
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self._conn
                finally:
                    self._depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self._conn
            except BaseException:
                self._depth = 0
                self._conn.execute("ROLLBACK")
                raise
            self._depth = 0
            self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.transaction() as conn:
            return conn.execute(sql, params)

    def ping(self) -> bool:
        try:
            return self.query_one("SELECT 1") is not None
        except sqlite3.Error:
            return False

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ migrations
    def migrate(self, directory: Path = MIGRATIONS_DIR) -> list[str]:
        """Apply pending migrations. Returns the versions applied now."""
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version TEXT PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            done = {r["version"] for r in self._conn.execute("SELECT version FROM schema_migrations")}
            applied: list[str] = []
            for f in sorted(directory.glob("*.sql")):
                m = _MIGRATION_RE.match(f.name)
                if not m or m.group(1) in done:
                    continue
                version = m.group(1)
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    # Execute statement by statement so the whole file stays in
                    # one transaction (executescript would COMMIT implicitly).
                    for stmt in _split_sql(f.read_text(encoding="utf-8")):
                        self._conn.execute(stmt)
                    self._conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?,?,?)",
                        (version, f.name, utcnow()),
                    )
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                applied.append(version)
            return applied

    def schema_version(self) -> Optional[str]:
        row = self.query_one("SELECT max(version) AS v FROM schema_migrations")
        return row["v"] if row else None


def _split_sql(script: str) -> list[str]:
    """Split a migration into statements (no semicolons inside literals/triggers)."""
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None
