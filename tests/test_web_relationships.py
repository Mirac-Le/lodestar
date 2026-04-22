"""HTTP-level tests for /api/relationships/* endpoints.

We mock `LLMClient.chat_json` so the parse endpoint runs end-to-end
against the real anonymizer + repository, but never touches the network.
The list / apply / patch / delete endpoints don't touch the LLM at all.

With one database per user, "isolation" is not an endpoint query parameter
but the mount path prefix. Each owner has a separate database, so edges
across owners are not reachable; these tests only assert
list/parse/apply/patch/delete behavior for a *single* mount. Cross-mount
isolation is covered by ``test_mount_unlock``.
"""

from __future__ import annotations

import json
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

MOUNT = "r"
PFX = f"/r/{MOUNT}"


class _FakeLLMClient:
    """Drop-in replacement for `enrich.LLMClient` with canned responses."""

    next_response: dict[str, Any] = {"edges": [], "unknown_mentions": []}
    last_user: str | None = None

    def __init__(self, *_: Any, **__: Any) -> None:
        # Real client validates env vars in __init__; tests must not.
        pass

    def chat_json(self, *, system: str, user: str, temperature: float = 0.1) -> LLMCallResult:
        type(self).last_user = user
        return LLMCallResult(data=type(self).next_response, raw="{}")


@pytest.fixture
def setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, dict[str, int]]]:
    db: Path = tmp_path / "rel_web.db"
    test_settings = Settings(
        db_path=db,
        embedding_dim=8,
        llm_api_key="x",
        embedding_api_key="x",
    )
    monkeypatch.setattr("lodestar.config.get_settings", lambda: test_settings)
    monkeypatch.setattr("lodestar.web.app.get_settings", lambda: test_settings)
    monkeypatch.setenv(
        "LODESTAR_MOUNTS_JSON",
        json.dumps([{"slug": MOUNT, "db_path": str(db)}]),
    )

    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    me = repo.ensure_me("Richard")
    assert me.id is not None
    pids: dict[str, int] = {"me": me.id}
    for n in ("Alice", "Bob", "Carol", "Dan"):
        p = repo.add_person(Person(name=n))
        assert p.id is not None
        pids[n] = p.id

    # One me-edge (Richard ↔ Alice, manual) + one peer-edge (Carol ↔ Dan,
    # ai_inferred) for the list / patch / delete tests below.
    repo.add_relationship(
        Relationship(
            source_id=me.id,
            target_id=pids["Alice"],
            strength=4,
            frequency=Frequency.MONTHLY,
            context="老同事",
            source="manual",
        ),
    )
    repo.add_relationship(
        Relationship(
            source_id=pids["Carol"],
            target_id=pids["Dan"],
            strength=2,
            frequency=Frequency.YEARLY,
            source="ai_inferred",
        ),
    )
    conn.close()

    # Patch the LLMClient symbol the endpoint imports lazily (`from
    # lodestar.enrich import LLMClient` inside the route).
    monkeypatch.setattr("lodestar.enrich.LLMClient", _FakeLLMClient)

    client = TestClient(create_app())
    try:
        yield client, pids
    finally:
        _FakeLLMClient.next_response = {"edges": [], "unknown_mentions": []}
        _FakeLLMClient.last_user = None


# ---------- /api/relationships (GET) ---------------------------------------
def test_list_returns_seeded_edges(
    setup: tuple[TestClient, dict[str, int]],
) -> None:
    client, _ = setup
    r = client.get(f"{PFX}/api/relationships").json()
    # 2 edges: 1 me-edge + 1 peer-edge
    assert r["total"] == 2
    sources = {item["source"] for item in r["items"]}
    assert sources == {"manual", "ai_inferred"}


def test_list_filters_by_min_strength_and_source(
    setup: tuple[TestClient, dict[str, int]],
) -> None:
    client, _ = setup
    r = client.get(f"{PFX}/api/relationships?min_strength=3").json()
    assert r["total"] == 1
    assert r["items"][0]["source"] == "manual"

    r = client.get(f"{PFX}/api/relationships?source=ai_inferred").json()
    assert r["total"] == 1
    assert r["items"][0]["source"] == "ai_inferred"


def test_list_include_me_false_drops_me_edges(
    setup: tuple[TestClient, dict[str, int]],
) -> None:
    client, _ = setup
    r = client.get(f"{PFX}/api/relationships?include_me=false").json()
    # me-edge dropped; only peer edge remains.
    assert r["total"] == 1
    item = r["items"][0]
    assert not item["a_is_me"] and not item["b_is_me"]


# ---------- /api/relationships/parse ---------------------------------------
def test_parse_returns_proposals_with_existing_edge_context(
    setup: tuple[TestClient, dict[str, int]],
) -> None:
    client, pids = setup
    # P000 = me, P001..P004 = Alice..Dan in insert order. Stage Alice ↔ Bob.
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
        f"{PFX}/api/relationships/parse",
        json={"text": "Alice 和 Bob 是饭局认识的，Mike 也在场。"},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["proposals"]) == 1
    p = data["proposals"][0]
    assert {p["a_id"], p["b_id"]} == {pids["Alice"], pids["Bob"]}
    assert p["existing_edge"] is None
    assert data["unknown_mentions"] == ["Mike"]
    # Alice has the manual me-edge; it should surface in context_for[Alice].
    assert str(pids["Alice"]) in data["context_for"]
    assert any(
        e["b_name"] == "Alice" or e["a_name"] == "Alice"
        for e in data["context_for"][str(pids["Alice"])]
    )


def test_parse_drops_unknown_tokens(
    setup: tuple[TestClient, dict[str, int]],
) -> None:
    """LLM hallucinating a token outside the issued range must be silently
    dropped — not 500'd."""
    client, _ = setup
    _FakeLLMClient.next_response = {
        "edges": [{"a": "P001", "b": "P999", "strength": 5}],
        "unknown_mentions": [],
    }
    r = client.post(f"{PFX}/api/relationships/parse", json={"text": "x"}).json()
    assert r["proposals"] == []


# ---------- /api/relationships/apply --------------------------------------
def test_apply_writes_manual_edge_and_returns_dto(
    setup: tuple[TestClient, dict[str, int]],
) -> None:
    client, pids = setup
    body = {
        "edges": [
            {
                "a_id": pids["Alice"],
                "b_id": pids["Bob"],
                "strength": 3,
                "context": "校友",
                "frequency": "yearly",
            }
        ]
    }
    r = client.post(f"{PFX}/api/relationships/apply", json=body).json()
    assert r["applied"] == 1
    assert r["skipped"] == 0
    assert len(r["items"]) == 1
    assert r["items"][0]["source"] == "manual"

    listed = client.get(f"{PFX}/api/relationships?include_me=false").json()
    # Pre-seed peer edge (Carol↔Dan) + new Alice↔Bob = 2.
    assert listed["total"] == 2
    contexts = {item["context"] for item in listed["items"]}
    assert "校友" in contexts


def test_apply_skips_unknown_pid(
    setup: tuple[TestClient, dict[str, int]],
) -> None:
    """Endpoint must guard against client-supplied person ids that don't
    exist in this mount's db."""
    client, pids = setup
    body = {"edges": [{"a_id": pids["Alice"], "b_id": 9_999_999, "strength": 3}]}
    r = client.post(f"{PFX}/api/relationships/apply", json=body).json()
    assert r["applied"] == 0
    assert r["skipped"] == 1


# ---------- /api/relationships/{id} PATCH/DELETE --------------------------
def test_patch_promotes_to_manual_and_overrides_strength(
    setup: tuple[TestClient, dict[str, int]],
) -> None:
    client, _ = setup
    listed = client.get(f"{PFX}/api/relationships?source=ai_inferred").json()
    assert listed["total"] == 1
    rel_id = listed["items"][0]["id"]

    r = client.patch(
        f"{PFX}/api/relationships/{rel_id}",
        json={"strength": 5, "context": "改成铁磁"},
    )
    assert r.status_code == 200
    updated = r.json()
    assert updated["strength"] == 5
    assert updated["source"] == "manual"
    assert updated["context"] == "改成铁磁"


def test_patch_404_for_unknown_id(
    setup: tuple[TestClient, dict[str, int]],
) -> None:
    client, _ = setup
    r = client.patch(
        f"{PFX}/api/relationships/9999999",
        json={"strength": 5},
    )
    assert r.status_code == 404


def test_delete_removes_edge(setup: tuple[TestClient, dict[str, int]]) -> None:
    client, _ = setup
    listed = client.get(f"{PFX}/api/relationships?source=ai_inferred").json()
    rel_id = listed["items"][0]["id"]
    r = client.delete(f"{PFX}/api/relationships/{rel_id}")
    assert r.status_code == 200
    again = client.get(f"{PFX}/api/relationships?source=ai_inferred").json()
    assert again["total"] == 0
