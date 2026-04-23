"""End-to-end test for POST /api/feedback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lodestar.config import Settings
from lodestar.db import Repository, connect, init_schema
from lodestar.models import Frequency, Person, Relationship
from lodestar.web import create_app

MOUNT_SLUG = "me"
PFX = f"/r/{MOUNT_SLUG}"
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8A"
    "AAIIAQBoQzN3AAAAAElFTkSuQmCC"
)


def _bootstrap_mount(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ts = Settings(db_path=db, embedding_dim=8,
                  llm_api_key="x", embedding_api_key="x")
    monkeypatch.setattr("lodestar.config.get_settings", lambda: ts)
    monkeypatch.setattr("lodestar.web.app.get_settings", lambda: ts)
    monkeypatch.setenv(
        "LODESTAR_MOUNTS_JSON",
        json.dumps([{"slug": MOUNT_SLUG, "db_path": str(db)}]),
    )


@pytest.fixture
def client_with_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, int, Path]:
    """Returns (client, alice_id, feedback_dir)."""
    db = tmp_path / "fb.db"
    _bootstrap_mount(db, monkeypatch)
    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    me = repo.ensure_me("我")
    alice = repo.add_person(Person(name="Alice", bio="研究员"))
    assert me.id and alice.id
    repo.add_relationship(Relationship(
        source_id=me.id, target_id=alice.id, strength=3,
        frequency=Frequency.MONTHLY,
    ))
    conn.close()
    feedback_dir = tmp_path / "feedback-out"
    feedback_dir.mkdir()
    monkeypatch.setenv("LODESTAR_FEEDBACK_DIR", str(feedback_dir))
    return TestClient(create_app()), alice.id, feedback_dir


def _valid_bug_body(alice_id: int) -> dict:
    return {
        "type": "bug",
        "form": {
            "title": "搜索漏人的情况这里描述",
            "involved_person_ids": [alice_id],
            "want_to_do": "找人",
            "did": "1. 输入\n2. 回车",
            "actual": "没出来",
            "expected": "应该出来",
            "history": "recent",
        },
        "submitter": "王磊",
        "severity": "daily",
        "auto_capture": {
            "mount_slug": "me",
            "view_mode": "intent",
            "search_active": True,
            "query": "研究员",
            "detail_person_id": None,
            "active_path_key": None,
            "direct_overrides": [],
            "indirect_targets": [alice_id],
            "contacted_targets": [],
            "api_trace": [],
            "error_buffer": [],
            "frontend_version": "20260423-x",
            "user_agent": "Mozilla/5.0",
            "viewport": "1920x1080",
        },
        "screenshots": [{
            "filename": "scr1.png",
            "content_type": "image/png",
            "data_base64": _TINY_PNG_B64,
        }],
    }


def test_submit_bug_returns_ticket_id_and_writes_md(
    client_with_data: tuple[TestClient, int, Path],
) -> None:
    client, alice_id, feedback_dir = client_with_data
    r = client.post(f"{PFX}/api/feedback", json=_valid_bug_body(alice_id))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ticket_id"].startswith("FB-")
    assert data["ticket_id"].endswith("-0001")
    md_path = feedback_dir / "me" / f"{data['ticket_id']}.md"
    assert md_path.exists()
    md = md_path.read_text(encoding="utf-8")
    assert "Alice" in md
    assert "@assistant" in md


def test_submit_bug_without_screenshot_rejected(
    client_with_data: tuple[TestClient, int, Path],
) -> None:
    client, alice_id, _ = client_with_data
    body = _valid_bug_body(alice_id)
    body["screenshots"] = []
    r = client.post(f"{PFX}/api/feedback", json=body)
    assert r.status_code == 422


def test_submit_feature_without_when_then_rejected(
    client_with_data: tuple[TestClient, int, Path],
) -> None:
    client, alice_id, _ = client_with_data
    body = {
        "type": "feature",
        "form": {
            "title": "按城市筛选联系人列表",
            "involved_person_ids": [alice_id],
            "user_story": "我想按城市筛选",
            "acceptance": ["- 城市下拉里有上海"],
        },
        "submitter": "王磊",
        "severity": "nice",
        "auto_capture": _valid_bug_body(alice_id)["auto_capture"],
        "screenshots": [],
    }
    r = client.post(f"{PFX}/api/feedback", json=body)
    assert r.status_code == 422


def test_ticket_id_increments_same_day(
    client_with_data: tuple[TestClient, int, Path],
) -> None:
    client, alice_id, _ = client_with_data
    r1 = client.post(f"{PFX}/api/feedback", json=_valid_bug_body(alice_id))
    r2 = client.post(f"{PFX}/api/feedback", json=_valid_bug_body(alice_id))
    assert r1.json()["ticket_id"] != r2.json()["ticket_id"]
    assert r2.json()["ticket_id"].endswith("-0002")


def test_submit_feature_end_to_end(
    client_with_data: tuple[TestClient, int, Path],
) -> None:
    client, alice_id, feedback_dir = client_with_data
    body = {
        "type": "feature",
        "form": {
            "title": "按城市筛选联系人列表这里",
            "involved_person_ids": [alice_id],
            "user_story": "当我搜索没结果的时候，我希望能按城市再过滤一遍",
            "acceptance": [
                "- 城市下拉里有上海、北京",
                "- 选中后列表实时过滤",
            ],
            "workaround": "现在只能手动翻",
        },
        "submitter": "王磊",
        "severity": "nice",
        "auto_capture": {
            "mount_slug": "me", "view_mode": "ambient",
            "search_active": False, "query": None,
            "detail_person_id": None, "active_path_key": None,
            "direct_overrides": [], "indirect_targets": [],
            "contacted_targets": [], "api_trace": [],
            "error_buffer": [],
            "frontend_version": "20260424-feedback",
            "user_agent": "Mozilla/5.0", "viewport": "1920x1080",
        },
        "screenshots": [],
    }
    r = client.post(f"{PFX}/api/feedback", json=body)
    assert r.status_code == 200, r.text
    ticket = r.json()["ticket_id"]
    md = (feedback_dir / "me" / f"{ticket}.md").read_text(encoding="utf-8")
    assert "用户故事" in md
    assert "当我搜索没结果的时候" in md
    assert "验收标准" in md
    assert "城市下拉" in md
