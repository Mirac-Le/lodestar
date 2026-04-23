"""Smoke tests for the FastAPI web app (one-db-per-mount)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lodestar.config import Settings
from lodestar.db import Repository, connect, init_schema
from lodestar.models import Frequency, Person, Relationship
from lodestar.web import create_app

# All API paths in this file are scoped to this prefix because the SPA
# now lives under `/r/<slug>/`. We test through the mount router instead
# of poking endpoints with `?owner=` query strings (which no longer exist).
MOUNT_SLUG = "me"
PFX = f"/r/{MOUNT_SLUG}"


def _bootstrap_mount(
    db: Path, monkeypatch: pytest.MonkeyPatch, *, embedding_dim: int = 8
) -> None:
    """Wire LODESTAR_MOUNTS_JSON + a test-only Settings to point at ``db``.

    Has to run BEFORE create_app() — the env var is read at boot to seed
    the mount registry, and Settings has to match so init_schema picks the
    right embedding_dim everywhere.
    """
    test_settings = Settings(
        db_path=db, embedding_dim=embedding_dim,
        llm_api_key="x", embedding_api_key="x",
    )
    monkeypatch.setattr("lodestar.config.get_settings", lambda: test_settings)
    monkeypatch.setattr("lodestar.web.app.get_settings", lambda: test_settings)
    monkeypatch.setenv(
        "LODESTAR_MOUNTS_JSON",
        json.dumps([{"slug": MOUNT_SLUG, "db_path": str(db)}]),
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    db = tmp_path / "web.db"
    _bootstrap_mount(db, monkeypatch)

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

    yield TestClient(create_app())


def test_graph_endpoint(client: TestClient) -> None:
    r = client.get(f"{PFX}/api/graph")
    assert r.status_code == 200
    data = r.json()
    assert data["me_id"] >= 1
    assert len(data["nodes"]) == 3
    assert len(data["edges"]) == 2
    me_node = next(n for n in data["nodes"] if n["is_me"])
    assert me_node["industry"] == "我"
    assert data["mount_slug"] == MOUNT_SLUG


def test_person_detail(client: TestClient) -> None:
    graph = client.get(f"{PFX}/api/graph").json()
    alice_id = next(n["id"] for n in graph["nodes"] if n["label"] == "Alice")
    r = client.get(f"{PFX}/api/people/{alice_id}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["name"] == "Alice"
    assert detail["industry"] == "投资金融"
    assert "因子" in detail["skills"]
    assert any(rel["name"] == "我" for rel in detail["related"])


def test_search_keyword_only(client: TestClient) -> None:
    r = client.post(f"{PFX}/api/search", json={"goal": "私募", "no_llm": True})
    assert r.status_code == 200
    data = r.json()
    assert data["goal"] == "私募"
    names = [p["target_name"] for p in data["results"]]
    assert "Alice" in names
    # SearchResponse only exposes the two-bucket UI shape (indirect /
    # contacted) plus the wishlist sidebar. Anything else (the historical
    # direct / weak / targets fields) was removed when we collapsed the
    # path-list into "需引荐 / 已联系".
    assert "indirect" in data and "contacted" in data
    assert "wishlist" in data
    assert "direct" not in data and "weak" not in data and "targets" not in data


def test_wishlist_flag_is_decoupled_from_path_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wishlist contact should be allowed to also be a 1-hop direct contact:
    is_wishlist is curation, path_kind is topology, and they must not collapse
    into one another."""
    db = tmp_path / "wish.db"
    _bootstrap_mount(db, monkeypatch)

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
    r = client.post(f"{PFX}/api/search", json={"goal": "私募", "no_llm": True})
    assert r.status_code == 200
    data = r.json()
    star_row = next(p for p in data["results"] if p["target_name"] == "Star")
    assert star_row["path_kind"] == "direct"
    assert star_row["is_wishlist"] is True
    assert any(p["target_name"] == "Star" for p in data["wishlist"])


def test_indirect_carries_direct_me_strength_when_weak_direct_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the algorithm picks an indirect path for a target that I also
    know directly (just via a weak edge that got penalised by
    ``weak_me_floor``), the response must expose that weak strength on the
    indirect row so the UI can offer a "use direct instead" fallback.
    Contacted-bucket rows must NOT carry the field — it's reserved for
    indirect, where it actually means something to surface.

    Topology:
        Me --5-- Alice --5-- Bob
        Me --3-- Bob   (weak; weak_me_floor=4 by default → penalised)

    Expected: Bob falls into indirect with direct_me_strength=3;
              Alice (1-hop, strength 5) lands in contacted with
              direct_me_strength=None.
    """
    db = tmp_path / "weakdirect.db"
    _bootstrap_mount(db, monkeypatch)

    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    me = repo.ensure_me("我")
    alice = repo.add_person(Person(name="Alice", bio="量化研究员", tags=["私募"]))
    bob = repo.add_person(Person(name="Bob", bio="销售总监", tags=["私募", "销售"]))
    assert me.id and alice.id and bob.id
    repo.add_relationship(Relationship(
        source_id=me.id, target_id=alice.id, strength=5,
        frequency=Frequency.MONTHLY,
    ))
    repo.add_relationship(Relationship(
        source_id=alice.id, target_id=bob.id, strength=5,
        frequency=Frequency.MONTHLY,
    ))
    repo.add_relationship(Relationship(
        source_id=me.id, target_id=bob.id, strength=3,
        frequency=Frequency.YEARLY,
    ))
    conn.close()

    client = TestClient(create_app())
    r = client.post(f"{PFX}/api/search", json={"goal": "私募", "no_llm": True})
    assert r.status_code == 200
    data = r.json()

    bob_indirect = next(
        (p for p in data["indirect"] if p["target_name"] == "Bob"), None,
    )
    assert bob_indirect is not None, (
        "Bob should be in indirect bucket because weak_me_floor=4 penalises "
        "the strength=3 direct edge"
    )
    assert bob_indirect["path_kind"] == "indirect"
    assert bob_indirect["direct_me_strength"] == 3, (
        "indirect rows that ALSO have a weak direct me-edge must surface that "
        "edge's strength so the UI can render the 'use direct' fallback"
    )

    for row in data["contacted"]:
        assert row.get("direct_me_strength") is None, (
            f"contacted rows must not carry direct_me_strength "
            f"(target={row['target_name']}, value={row.get('direct_me_strength')})"
        )


def test_two_person_path(client: TestClient) -> None:
    graph = client.get(f"{PFX}/api/graph").json()
    me_id = graph["me_id"]
    bob_id = next(n["id"] for n in graph["nodes"] if n["label"] == "Bob")
    r = client.post(f"{PFX}/api/path", json={
        "source_id": me_id, "target_id": bob_id, "max_paths": 3,
    })
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert len(paths) >= 1
    assert paths[0]["path"][-1]["name"] == "Bob"


def test_introductions(client: TestClient) -> None:
    r = client.get(f"{PFX}/api/introductions")
    assert r.status_code == 200
    suggestions = r.json()["suggestions"]
    pairs = [(s["seeker_name"], s["provider_name"]) for s in suggestions]
    assert ("Alice", "Bob") in pairs or ("Bob", "Alice") in pairs


def test_stats(client: TestClient) -> None:
    r = client.get(f"{PFX}/api/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["total_contacts"] == 2
    assert data["total_relationships"] == 2
    assert "投资金融" in data["by_industry"]


def test_create_and_delete(client: TestClient) -> None:
    r = client.post(f"{PFX}/api/people", json={
        "name": "Carol", "bio": "VC",
        "tags": ["创业老板"], "strength_to_me": 2,
        "embed": False,
    })
    assert r.status_code == 200
    new_id = r.json()["id"]
    r2 = client.get(f"{PFX}/api/people/{new_id}")
    assert r2.json()["name"] == "Carol"
    r3 = client.delete(f"{PFX}/api/people/{new_id}")
    assert r3.status_code == 200
    assert client.get(f"{PFX}/api/people/{new_id}").status_code == 404


# ---------------------------------------------------------------------
# 联系人档案 inline 编辑：姓名 / bio KV / tags / 可信度
# ---------------------------------------------------------------------


def _alice_id(client: TestClient) -> int:
    graph = client.get(f"{PFX}/api/graph").json()
    return next(n["id"] for n in graph["nodes"] if n["label"] == "Alice")


def test_person_dto_exposes_me_edge_id(client: TestClient) -> None:
    """档案面板调可信度需要 me_edge_id；fixture 里 Me→Alice 有边、
    Me→Bob 没直接边，DTO 必须正确区分。"""
    graph = client.get(f"{PFX}/api/graph").json()
    alice_id = next(n["id"] for n in graph["nodes"] if n["label"] == "Alice")
    bob_id = next(n["id"] for n in graph["nodes"] if n["label"] == "Bob")
    me_id = graph["me_id"]

    alice = client.get(f"{PFX}/api/people/{alice_id}").json()
    assert alice["me_edge_id"] is not None  # has Me edge
    assert alice["strength_to_me"] == 4

    bob = client.get(f"{PFX}/api/people/{bob_id}").json()
    assert bob["me_edge_id"] is None  # no Me edge

    me = client.get(f"{PFX}/api/people/{me_id}").json()
    assert me["me_edge_id"] is None  # 自己永远没有"自己→自己"的边


def test_update_person_rename(client: TestClient) -> None:
    pid = _alice_id(client)
    r = client.patch(
        f"{PFX}/api/people/{pid}",
        json={"name": "Alice 王", "embed": False},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Alice 王"
    # round-trip
    assert client.get(f"{PFX}/api/people/{pid}").json()["name"] == "Alice 王"


def test_update_person_rename_rejects_blank(client: TestClient) -> None:
    pid = _alice_id(client)
    r = client.patch(
        f"{PFX}/api/people/{pid}",
        json={"name": "   ", "embed": False},
    )
    # min_length=1 通过（"   " 长度 3），但 strip 后变空，由后端 422 拦截
    assert r.status_code == 422


def test_update_person_bio_kv_roundtrip(client: TestClient) -> None:
    """前端把 KV 编辑器的草稿用 `bioFromPairs` 拼成 "k：v · k：v"
    再 PATCH bio，后端只是当字符串存。验证存回去能再被 bioPairs 解析。"""
    pid = _alice_id(client)
    new_bio = "行业：私募基金 · 职务：基金经理 · 城市：上海 · 合作价值：4/5"
    r = client.patch(
        f"{PFX}/api/people/{pid}",
        json={"bio": new_bio, "embed": False},
    )
    assert r.status_code == 200
    assert r.json()["bio"] == new_bio


def test_update_person_tags_add_and_remove(client: TestClient) -> None:
    pid = _alice_id(client)
    # Add a tag
    r = client.patch(
        f"{PFX}/api/people/{pid}",
        json={"tags": ["私募基金", "新增标签"], "embed": False},
    )
    assert r.status_code == 200
    assert set(r.json()["tags"]) == {"私募基金", "新增标签"}
    # Remove all tags
    r2 = client.patch(
        f"{PFX}/api/people/{pid}",
        json={"tags": [], "embed": False},
    )
    assert r2.status_code == 200
    assert r2.json()["tags"] == []


def test_update_relationship_strength_via_me_edge(client: TestClient) -> None:
    """档案面板可信度直接走 PATCH /api/relationships/{me_edge_id}。"""
    pid = _alice_id(client)
    detail = client.get(f"{PFX}/api/people/{pid}").json()
    rid = detail["me_edge_id"]
    assert rid is not None

    for v in (1, 5, 3):
        r = client.patch(f"{PFX}/api/relationships/{rid}", json={"strength": v})
        assert r.status_code == 200, r.text
        assert r.json()["strength"] == v
        # Person DTO should reflect the new strength_to_me
        d2 = client.get(f"{PFX}/api/people/{pid}").json()
        assert d2["strength_to_me"] == v


def test_update_relationship_strength_rejects_zero(client: TestClient) -> None:
    """RelationshipUpdateRequest 限定 strength ∈ [1, 5]，0 必须被 422。
    这条门挡住前端 UI 误把"已联系"打回 wishlist。"""
    pid = _alice_id(client)
    rid = client.get(f"{PFX}/api/people/{pid}").json()["me_edge_id"]
    r = client.patch(f"{PFX}/api/relationships/{rid}", json={"strength": 0})
    assert r.status_code == 422


def test_mounts_list_is_public(client: TestClient) -> None:
    """Root-level /api/mounts is the only endpoint the SPA can hit
    before unlocking — it must not require auth and must surface the
    locked flag so the frontend can pre-render the lock icon."""
    r = client.get("/api/mounts")
    assert r.status_code == 200
    payload = r.json()
    assert payload["default_slug"] == MOUNT_SLUG
    [m] = payload["mounts"]
    assert m["slug"] == MOUNT_SLUG
    assert m["locked"] is False  # default fixture sets no password


def test_root_redirects_when_single_mount(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    # Single-mount setups skip the picker page entirely.
    assert r.status_code in (302, 307)
    assert r.headers["location"] == f"/r/{MOUNT_SLUG}/"


def test_mount_index_serves_spa(client: TestClient) -> None:
    r = client.get(f"{PFX}/")
    assert r.status_code == 200
    assert "lodestar" in r.text.lower()
    assert "cytoscape" in r.text.lower()
