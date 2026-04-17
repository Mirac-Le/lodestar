"""Smoke tests for the FastAPI web app."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lodestar.config import Settings
from lodestar.db import Repository, connect, init_schema
from lodestar.models import Frequency, Person, Relationship
from lodestar.web import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db = tmp_path / "web.db"

    test_settings = Settings(
        db_path=db, embedding_dim=8,
        llm_api_key="x", embedding_api_key="x",
    )
    monkeypatch.setattr("lodestar.config.get_settings", lambda: test_settings)
    monkeypatch.setattr("lodestar.web.app.get_settings", lambda: test_settings)

    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    me = repo.ensure_me("我", bio="主人")
    alice = repo.add_person(Person(
        name="Alice", bio="量化研究员",
        tags=["私募基金"], skills=["因子"], needs=["销售"],
    ))
    bob = repo.add_person(Person(
        name="Bob", bio="销售经理",
        tags=["销售渠道"], skills=["拓客"],
    ))
    assert me.id and alice.id and bob.id
    repo.add_relationship(Relationship(
        source_id=me.id, target_id=alice.id, strength=4,
        frequency=Frequency.MONTHLY, context="老同事",
    ))
    repo.add_relationship(Relationship(
        source_id=alice.id, target_id=bob.id, strength=3,
        frequency=Frequency.QUARTERLY,
    ))
    conn.close()

    app = create_app()
    return TestClient(app)


def test_graph_endpoint(client: TestClient) -> None:
    r = client.get("/api/graph")
    assert r.status_code == 200
    data = r.json()
    assert data["me_id"] >= 1
    assert len(data["nodes"]) == 3
    assert len(data["edges"]) == 2
    me_node = next(n for n in data["nodes"] if n["is_me"])
    assert me_node["industry"] == "我"


def test_person_detail(client: TestClient) -> None:
    graph = client.get("/api/graph").json()
    alice_id = next(n["id"] for n in graph["nodes"] if n["label"] == "Alice")
    r = client.get(f"/api/people/{alice_id}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["name"] == "Alice"
    assert detail["industry"] == "投资金融"
    assert "因子" in detail["skills"]
    assert any(rel["name"] == "我" for rel in detail["related"])


def test_search_keyword_only(client: TestClient) -> None:
    r = client.post("/api/search", json={"goal": "私募", "no_llm": True})
    assert r.status_code == 200
    data = r.json()
    assert data["goal"] == "私募"
    names = [p["target_name"] for p in data["results"]]
    assert "Alice" in names
    # New bucket layout: keep the indirect alias and surface the topology
    # buckets explicitly. `targets` is kept transitionally and must mirror
    # `indirect` exactly so older clients keep working.
    assert "indirect" in data and "direct" in data and "weak" in data
    assert "wishlist" in data
    assert data["targets"] == data["indirect"]


def test_wishlist_flag_is_decoupled_from_path_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wishlist contact should be allowed to also be a 1-hop direct contact:
    is_wishlist is curation, path_kind is topology, and they must not collapse
    into one another."""
    db = tmp_path / "wish.db"
    test_settings = Settings(
        db_path=db, embedding_dim=8,
        llm_api_key="x", embedding_api_key="x",
    )
    monkeypatch.setattr("lodestar.config.get_settings", lambda: test_settings)
    monkeypatch.setattr("lodestar.web.app.get_settings", lambda: test_settings)

    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    me = repo.ensure_me("我")
    star = repo.add_person(Person(
        name="Star", bio="量化研究员",
        tags=["私募"], is_wishlist=True,
    ))
    assert me.id and star.id
    repo.add_relationship(Relationship(
        source_id=me.id, target_id=star.id, strength=4,
    ))
    conn.close()

    client = TestClient(create_app())
    r = client.post("/api/search", json={"goal": "私募", "no_llm": True})
    assert r.status_code == 200
    data = r.json()
    star_row = next(p for p in data["results"] if p["target_name"] == "Star")
    assert star_row["path_kind"] == "direct"
    assert star_row["is_wishlist"] is True
    # Star also appears in the dedicated wishlist bucket.
    assert any(p["target_name"] == "Star" for p in data["wishlist"])


def test_two_person_path(client: TestClient) -> None:
    graph = client.get("/api/graph").json()
    me_id = graph["me_id"]
    bob_id = next(n["id"] for n in graph["nodes"] if n["label"] == "Bob")
    r = client.post("/api/path", json={
        "source_id": me_id, "target_id": bob_id, "max_paths": 3,
    })
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert len(paths) >= 1
    assert paths[0]["path"][-1]["name"] == "Bob"


def test_introductions(client: TestClient) -> None:
    r = client.get("/api/introductions")
    assert r.status_code == 200
    suggestions = r.json()["suggestions"]
    # Alice needs "客户资源" (customer resources); Bob has "销售渠道" (sales) tag
    pairs = [(s["seeker_name"], s["provider_name"]) for s in suggestions]
    assert ("Alice", "Bob") in pairs or ("Bob", "Alice") in pairs


def test_stats(client: TestClient) -> None:
    r = client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["total_contacts"] == 2
    assert data["total_relationships"] == 2
    assert "投资金融" in data["by_industry"]


def test_create_and_delete(client: TestClient) -> None:
    r = client.post("/api/people", json={
        "name": "Carol", "bio": "VC",
        "tags": ["创业老板"], "strength_to_me": 2,
        "embed": False,
    })
    assert r.status_code == 200
    new_id = r.json()["id"]
    r2 = client.get(f"/api/people/{new_id}")
    assert r2.json()["name"] == "Carol"
    r3 = client.delete(f"/api/people/{new_id}")
    assert r3.status_code == 200
    assert client.get(f"/api/people/{new_id}").status_code == 404


def test_index_serves(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "LODESTAR" in r.text
    assert "cytoscape" in r.text.lower()
