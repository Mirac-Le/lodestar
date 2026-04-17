"""Shared fixtures."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from lodestar.db import Repository, connect, init_schema


@pytest.fixture
def db_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db_path = tmp_path / "lodestar_test.db"
    conn = connect(db_path)
    init_schema(conn, embedding_dim=4)
    yield conn
    conn.close()


@pytest.fixture
def repo(db_conn: sqlite3.Connection) -> Repository:
    return Repository(db_conn)
