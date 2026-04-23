"""Feedback table schema + repository smoke tests."""

from __future__ import annotations

from pathlib import Path

from lodestar.db import connect, init_schema


def test_feedback_table_exists(tmp_path: Path) -> None:
    db = tmp_path / "fb.db"
    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(feedback)").fetchall()
    }
    expected = {
        "id", "ticket_id", "type", "status", "title", "submitter",
        "severity", "payload_json", "md_path", "created_at",
        "closed_at", "closed_by", "related_pr",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_feedback_ticket_id_unique(tmp_path: Path) -> None:
    db = tmp_path / "fb.db"
    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    conn.execute(
        "INSERT INTO feedback (ticket_id, type, title, submitter, payload_json)"
        " VALUES (?,?,?,?,?)",
        ("FB-20260423-0001", "bug", "t", "s", "{}"),
    )
    import sqlite3

    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO feedback (ticket_id, type, title, submitter, payload_json)"
            " VALUES (?,?,?,?,?)",
            ("FB-20260423-0001", "feature", "t2", "s2", "{}"),
        )
