"""Per-owner web UI password (tab lock)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lodestar.config import Settings
from lodestar.db import Repository, connect, init_schema
from lodestar.models import Person, Relationship
from lodestar.web import create_app


@pytest.fixture
def locked_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db = tmp_path / "lock.db"
    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    r_owner = repo.ensure_owner(slug="richard", display_name="Richard")
    t_owner = repo.ensure_owner(slug="tommy", display_name="Tommy")
    assert r_owner.id is not None and t_owner.id is not None
    me_r = repo.get_me(owner_id=r_owner.id)
    assert me_r is not None and me_r.id is not None
    p = repo.add_person(Person(name="Alice"))
    repo.attach_person_to_owner(p.id, r_owner.id)
    repo.add_relationship(
        Relationship(source_id=me_r.id, target_id=p.id, strength=3),
        owner_id=r_owner.id,
    )
    repo.set_owner_web_password(r_owner.id, "secret123")
    conn.close()

    settings = Settings(
        db_path=db,
        embedding_dim=8,
        llm_api_key="x",
        embedding_api_key="x",
        owner_unlock_secret="unit-test-hmac-secret",
    )

    def fake_settings() -> Settings:
        return settings

    monkeypatch.setattr("lodestar.config.get_settings", fake_settings)
    monkeypatch.setattr("lodestar.web.app.get_settings", fake_settings)
    monkeypatch.setattr("lodestar.web.owner_unlock.get_settings", fake_settings)

    return TestClient(create_app())


def test_list_owners_shows_web_locked(locked_client: TestClient) -> None:
    r = locked_client.get("/api/owners")
    assert r.status_code == 200
    owners = r.json()["owners"]
    rich = next(x for x in owners if x["slug"] == "richard")
    tommy = next(x for x in owners if x["slug"] == "tommy")
    assert rich["web_locked"] is True
    assert tommy["web_locked"] is False


def test_graph_requires_unlock(locked_client: TestClient) -> None:
    r = locked_client.get("/api/graph?owner=richard")
    assert r.status_code == 401
    d = r.json()["detail"]
    assert d["code"] == "owner_locked"


def test_unlock_and_graph(locked_client: TestClient) -> None:
    bad = locked_client.post(
        "/api/owners/unlock",
        json={"slug": "richard", "password": "wrong"},
    )
    assert bad.status_code == 401

    ok = locked_client.post(
        "/api/owners/unlock",
        json={"slug": "richard", "password": "secret123"},
    )
    assert ok.status_code == 200
    token = ok.json()["token"]

    r = locked_client.get(
        "/api/graph?owner=richard",
        headers={"X-Owner-Unlock": token},
    )
    assert r.status_code == 200


def test_unlocked_owner_unaffected(locked_client: TestClient) -> None:
    r = locked_client.get("/api/graph?owner=tommy")
    assert r.status_code == 200
