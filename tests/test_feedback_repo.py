"""Feedback table schema + repository smoke tests."""

from __future__ import annotations

from pathlib import Path

from lodestar.db import Repository, connect, init_schema


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


def test_next_ticket_id_increments_per_day(tmp_path: Path) -> None:
    db = tmp_path / "fb.db"
    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    t1 = repo.next_feedback_ticket_id(today="20260423")
    # 生成第一个 id 后必须写库，不然后续 call 都返回 0001。
    conn.execute(
        "INSERT INTO feedback (ticket_id, type, title, submitter, payload_json)"
        " VALUES (?,?,?,?,?)",
        (t1, "bug", "t", "s", "{}"),
    )
    t2 = repo.next_feedback_ticket_id(today="20260423")
    assert t1 == "FB-20260423-0001"
    assert t2 == "FB-20260423-0002"


def test_next_ticket_id_resets_on_new_day(tmp_path: Path) -> None:
    db = tmp_path / "fb.db"
    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    conn.execute(
        "INSERT INTO feedback (ticket_id, type, title, submitter, payload_json)"
        " VALUES ('FB-20260422-0001', 'bug', 't', 's', '{}')",
    )
    t = repo.next_feedback_ticket_id(today="20260423")
    assert t == "FB-20260423-0001"


def test_add_feedback_returns_row_with_id(tmp_path: Path) -> None:
    db = tmp_path / "fb.db"
    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    row_id = repo.add_feedback(
        ticket_id="FB-20260423-0001",
        type_="bug",
        title="x" * 15,
        submitter="王磊",
        severity="daily",
        payload_json='{"foo":"bar"}',
        md_path="docs/feedback/me/FB-20260423-0001.md",
    )
    assert isinstance(row_id, int) and row_id > 0
    row = conn.execute(
        "SELECT * FROM feedback WHERE id = ?", (row_id,)
    ).fetchone()
    assert row["status"] == "open"
    assert row["type"] == "bug"
