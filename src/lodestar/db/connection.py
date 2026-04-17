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
    """Create all tables if they don't exist. Idempotent."""
    with conn:
        for stmt in DDL_STATEMENTS:
            conn.execute(stmt)
        conn.execute(vec_ddl(embedding_dim))
