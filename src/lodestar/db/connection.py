"""Connection helpers. Owns the sqlite-vec extension loading dance.

一人一库后，``connect()`` / ``init_schema()`` 都只面对**一个 db 文件**。
``serve --mount`` 会 per-mount 各调一次。
"""

from __future__ import annotations

import secrets
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
    """Create all tables if they don't exist. Idempotent.

    一次性 schema：v4 之前 owner / person_owner / relationship.owner_id
    那套残留数据**不**做迁移（旧库请走 ``cp .bak`` 备份 + 按 quickstart
    重跑的路线，参见 README）。本函数只在干净 db 上 CREATE，不做 ALTER。

    每次启动还会确保 ``meta.unlock_secret`` 存在 —— per-db HMAC 签名 key，
    db 出生时随机生成，cp 走的人把自己的 secret 一起带走，互不相干。
    """
    with conn:
        for stmt in DDL_STATEMENTS:
            conn.execute(stmt)
        conn.execute(vec_ddl(embedding_dim))
        _ensure_unlock_secret(conn)


def _ensure_unlock_secret(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'unlock_secret'"
    ).fetchone()
    if row is not None and row["value"]:
        return
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('unlock_secret', ?)",
        (secrets.token_hex(32),),
    )
