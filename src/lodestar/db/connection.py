"""Connection helpers. Owns the sqlite-vec extension loading dance."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

from lodestar.db.schema import DDL_STATEMENTS, vec_ddl


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with sqlite-vec loaded and sensible pragmas."""
    # FastAPI sync routes run inside a thread pool while dependency setup for
    # generators may run on the asyncio event-loop thread, so the connection
    # can be created on a different thread than Repository queries. SQLite
    # disallows that unless check_same_thread=False; WAL + one conn per request
    # keeps this safe for our access pattern.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection, embedding_dim: int) -> None:
    """Create all tables if they don't exist, then run lightweight column-add
    migrations for existing databases. Idempotent."""
    with conn:
        for stmt in DDL_STATEMENTS:
            conn.execute(stmt)
        conn.execute(vec_ddl(embedding_dim))
        _migrate_in_place(conn)


def _migrate_in_place(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema.

    SQLite has no `ADD COLUMN IF NOT EXISTS`, so we inspect `PRAGMA
    table_info` and only ALTER when the column is missing. Each migration
    must be safe to re-run on already-upgraded databases.
    """
    existing_person_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(person)")
    }
    if "is_wishlist" not in existing_person_cols:
        conn.execute(
            "ALTER TABLE person ADD COLUMN is_wishlist INTEGER NOT NULL DEFAULT 0"
        )

    existing_rel_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(relationship)")
    }
    if "owner_id" not in existing_rel_cols:
        conn.execute(
            "ALTER TABLE relationship ADD COLUMN owner_id INTEGER "
            "REFERENCES owner(id) ON DELETE SET NULL"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_rel_owner ON relationship(owner_id)")
    if "source" not in existing_rel_cols:
        # SQLite ALTER ADD COLUMN cannot add a column with a non-constant
        # default if it includes a CHECK constraint, so we add the column
        # with a constant default and skip the CHECK on legacy DBs.
        conn.execute(
            "ALTER TABLE relationship ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
        )
