"""HTTP-level tests for /api/relationships/* endpoints.

We mock `LLMClient.chat_json` so the parse endpoint runs end-to-end
against the real anonymizer + repository, but never touches the network.
The list / apply / patch / delete endpoints don't touch the LLM at all,
so they exercise pure DB + DTO logic.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lodestar.config import Settings
from lodestar.db import Repository, connect, init_schema
from lodestar.enrich.client import LLMCallResult
from lodestar.models import Frequency, Person, Relationship
from lodestar.web import create_app


# ---------- helpers ---------------------------------------------------------
class _FakeLLMClient:
    """Drop-in replacement for `enrich.LLMClient` with canned responses."""

    next_response: dict[str, Any] = {"edges": [], "unknown_mentions": []}
    last_user: str | None = None

    def __init__(self, *_: Any, **__: Any) -> None:
        # The real one validates env vars in __init__; we explicitly do
        # nothing so tests don't need LODESTAR_LLM_API_KEY.
        pass

    def chat_json(self, *, system: str, user: str, temperature: float = 0.1) -> LLMCallResult:
        type(self).last_user = user
        return LLMCallResult(data=type(self).next_response, raw="{}")


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[
    tuple[TestClient, dict[str, int]]
]:
    db: Path = tmp_path / "rel_web.db"
    test_settings = Settings(
        db_path=db,
        embedding_dim=8,
        llm_api_key="x",
        embedding_api_key="x",
    )
    monkeypatch.setattr("lodestar.config.get_settings", lambda: test_settings)
    monkeypatch.setattr("lodestar.web.app.get_settings", lambda: test_settings)

    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    r = repo.ensure_owner(slug="r", display_name="Richard")
    t = repo.ensure_owner(slug="t", display_name="Tommy")
    assert r.id is not None and t.id is not None

    pids: dict[str, int] = {"me_r": r.me_person_id, "me_t": t.me_person_id}
    for slug_prefix, owner_id, names in [
        ("r", r.id, ["RAlice", "RBob"]),
        ("t", t.id, ["TCarol", "TDan"]),
    ]:
        for n in names:
            p = repo.add_person(Person(name=n))
            assert p.id is not None
            repo.attach_person_to_owner(p.id, owner_id)
            pids[n] = p.id
    # Seed: one me-edge in R, one peer-edge in T (manual + ai_inferred).
    repo.add_relationship(
        Relationship(
            source_id=r.me_person_id,
            target_id=pids["RAlice"],
            strength=4,
            frequency=Frequency.MONTHLY,
            context="老同事",
            source="manual",
        ),
        owner_id=r.id,
    )
    repo.add_relationship(
        Relationship(
            source_id=pids["TCarol"],
            target_id=pids["TDan"],
            strength=2,
            frequency=Frequency.YEARLY,
            source="ai_inferred",
        ),
        owner_id=t.id,
    )
    conn.close()

    # Patch the LLMClient and RelationshipParser entry points used inside
    # `parse_relationships`. We import the module symbol *at module top*
    # because the endpoint does `from lodestar.enrich import ...` at call
    # time — patching the source symbol makes the in-function import see
    # the fake.
    monkeypatch.setattr("lodestar.enrich.LLMClient", _FakeLLMClient)

    client = TestClient(create_app())
    try:
        yield client, pids
    finally:
        # Reset class-level state so tests don't bleed.
        _FakeLLMClient.next_response = {"edges": [], "unknown_mentions": []}
        _FakeLLMClient.last_user = None


# ---------- /api/relationships (GET) ---------------------------------------
def test_list_filters_by_owner(setup: tuple[TestClient, dict[str, int]]) -> None:
    client, _ = setup
    rs = client.get("/api/relationships?owner=r").json()
    rt = client.get("/api/relationships?owner=t").json()
    assert rs["total"] == 1
    assert rt["total"] == 1
    assert rs["items"][0]["b_name"] == "RAlice"
    assert rt["items"][0]["a_name"] == "TCarol"
    assert rt["items"][0]["source"] == "ai_inferred"


def test_list_filters_by_min_strength_and_source(
    setup: tuple[TestClient, dict[str, int]],
) -> None:
    client, _ = setup
    # min_strength=3 should drop T's strength=2 row.
    r = client.get("/api/relationships?owner=t&min_strength=3").json()
    assert r["total"] == 0
    # source=manual should only see R's me-edge.
    r = client.get("/api/relationships?owner=r&source=manual").json()
    assert r["total"] == 1
    r = client.get("/api/relationships?owner=r&source=ai_inferred").json()
    assert r["total"] == 0


def test_list_include_me_false_drops_me_edges(
    setup: tuple[TestClient, dict[str, int]],
) -> None:
    client, _ = setup
    r = client.get("/api/relationships?owner=r&include_me=false").json()
    assert r["total"] == 0  # R only has a me-edge


# ---------- /api/relationships/parse ---------------------------------------
def test_parse_returns_proposals_with_existing_edge_context(
    setup: tuple[TestClient, dict[str, int]],
) -> None:
    client, pids = setup
    # Stage one edge between RAlice (P001) and RBob (P002).
    _FakeLLMClient.next_response = {
        "edges": [
            {
                "a": "P001",
                "b": "P002",
                "strength": 3,
                "context": "饭局认识",
                "frequency": "yearly",
            }
        ],
        "unknown_mentions": ["Mike"],
    }
    r = client.post(
        "/api/relationships/parse?owner=r",
        json={"text": "RAlice 和 RBob 是饭局认识的，Mike 也在场。"},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["proposals"]) == 1
    p = data["proposals"][0]
    assert {p["a_id"], p["b_id"]} == {pids["RAlice"], pids["RBob"]}
    assert p["existing_edge"] is None  # not yet in DB
    assert data["unknown_mentions"] == ["Mike"]
    # context_for is keyed by person id and contains existing edges
    # touching that person — RAlice has the manual me-edge, so we expect
    # it to surface here.
    assert str(pids["RAlice"]) in data["context_for"]
    assert any(
        e["b_name"] == "RAlice" or e["a_name"] == "RAlice"
        for e in data["context_for"][str(pids["RAlice"])]
    )


def test_parse_owner_isolation(setup: tuple[TestClient, dict[str, int]]) -> None:
    """If we ask R to parse but the LLM returns a token (P001) that maps
    to a *different* contact in T's namespace, R must drop it because R
    has no P002 etc."""
    client, _ = setup
    _FakeLLMClient.next_response = {
        "edges": [{"a": "P003", "b": "P004", "strength": 5}],
        "unknown_mentions": [],
    }
    r = client.post("/api/relationships/parse?owner=r", json={"text": "x"}).json()
    assert r["proposals"] == []


# ---------- /api/relationships/apply --------------------------------------
def test_apply_writes_manual_edge_and_returns_dto(
    setup: tuple[TestClient, dict[str, int]],
) -> None:
    client, pids = setup
    body = {
        "edges": [
            {
                "a_id": pids["RAlice"],
                "b_id": pids["RBob"],
                "strength": 3,
                "context": "校友",
                "frequency": "yearly",
            }
        ]
    }
    r = client.post("/api/relationships/apply?owner=r", json=body).json()
    assert r["applied"] == 1
    assert r["skipped"] == 0
    assert len(r["items"]) == 1
    assert r["items"][0]["source"] == "manual"

    # Now visible on list endpoint.
    listed = client.get("/api/relationships?owner=r&include_me=false").json()
    assert listed["total"] == 1
    assert listed["items"][0]["context"] == "校友"


def test_apply_rejects_cross_owner_endpoints(
    setup: tuple[TestClient, dict[str, int]],
) -> None:
    """Even if the client crafts a request mixing R's id with T's id,
    the endpoint must skip it (owner-scoped pid check)."""
    client, pids = setup
    body = {
        "edges": [
            {
                "a_id": pids["RAlice"],
                "b_id": pids["TCarol"],  # not in R's roster
                "strength": 3,
            }
        ]
    }
    r = client.post("/api/relationships/apply?owner=r", json=body).json()
    assert r["applied"] == 0
    assert r["skipped"] == 1


# ---------- /api/relationships/{id} PATCH/DELETE --------------------------
def test_patch_promotes_to_manual_and_overrides_strength(
    setup: tuple[TestClient, dict[str, int]],
) -> None:
    client, _ = setup
    listed = client.get("/api/relationships?owner=t").json()
    rel_id = listed["items"][0]["id"]
    assert listed["items"][0]["source"] == "ai_inferred"

    r = client.patch(
        f"/api/relationships/{rel_id}?owner=t",
        json={"strength": 5, "context": "改成铁磁"},
    )
    assert r.status_code == 200
    updated = r.json()
    assert updated["strength"] == 5
    assert updated["source"] == "manual"
    assert updated["context"] == "改成铁磁"


def test_patch_404_when_edge_belongs_to_other_owner(
    setup: tuple[TestClient, dict[str, int]],
) -> None:
    client, _ = setup
    t_rel_id = client.get("/api/relationships?owner=t").json()["items"][0]["id"]
    r = client.patch(
        f"/api/relationships/{t_rel_id}?owner=r",
        json={"strength": 5},
    )
    assert r.status_code == 404


def test_delete_removes_edge(setup: tuple[TestClient, dict[str, int]]) -> None:
    client, _ = setup
    listed = client.get("/api/relationships?owner=t").json()
    rel_id = listed["items"][0]["id"]
    r = client.delete(f"/api/relationships/{rel_id}?owner=t")
    assert r.status_code == 200
    again = client.get("/api/relationships?owner=t").json()
    assert again["total"] == 0
