"""Unit tests for `lodestar.enrich.relationship_parser.RelationshipParser`.

The parser orchestrates anonymizer + LLM + deanonymizer. The LLM is the
only thing we mock — everything else exercises the real Anonymizer /
Repository code paths so a regression in token assignment, owner scoping,
or P000-skipping immediately surfaces here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lodestar.db import Repository, connect, init_schema
from lodestar.enrich.client import LLMCallResult
from lodestar.enrich.relationship_parser import RelationshipParser
from lodestar.models import Person


# ---------- fixtures --------------------------------------------------------
@dataclass
class _FakeLLMClient:
    """Records the last `chat_json` call and returns canned JSON.

    `responses` is a list popped FIFO so multi-step tests can stage
    several calls. The captured `last_user` lets a test assert the
    anonymized prompt was actually built (contains tokens, not real
    names).
    """

    responses: list[dict[str, Any]]
    last_system: str | None = None
    last_user: str | None = None

    def chat_json(self, *, system: str, user: str, temperature: float = 0.1) -> LLMCallResult:
        self.last_system = system
        self.last_user = user
        if not self.responses:
            raise AssertionError("FakeLLMClient ran out of canned responses.")
        data = self.responses.pop(0)
        return LLMCallResult(data=data, raw="{}")


@pytest.fixture
def parser_setup(tmp_path: Path) -> Iterator[tuple[Repository, int, dict[str, int], _FakeLLMClient]]:
    """Build an owner with three peers + me, return ids + fake LLM client.

    Both the parser tests and the existing `db_conn` fixture aren't
    compatible (parser needs an owner_id), so we set up our own DB here.
    """
    db: Path = tmp_path / "rel.db"
    conn = connect(db)
    init_schema(conn, embedding_dim=4)
    repo = Repository(conn)
    owner = repo.ensure_owner(slug="r", display_name="Richard")
    assert owner.id is not None

    pids: dict[str, int] = {}
    for n in ("Alice", "Bob", "Carol"):
        p = repo.add_person(Person(name=n, companies=["Acme"]))
        assert p.id is not None
        repo.attach_person_to_owner(p.id, owner.id)
        pids[n] = p.id
    pids["me"] = owner.me_person_id

    fake = _FakeLLMClient(responses=[])
    try:
        yield repo, owner.id, pids, fake
    finally:
        conn.close()


# ---------- tests -----------------------------------------------------------
def test_parse_returns_proposals_and_anonymizes_prompt(
    parser_setup: tuple[Repository, int, dict[str, int], _FakeLLMClient],
) -> None:
    repo, owner_id, pids, fake = parser_setup
    # The fake LLM "returns" a single A↔B edge in token space; the parser
    # must map P-tokens back to the right person ids.
    fake.responses.append(
        {
            "edges": [
                {
                    "a": "P001",
                    "b": "P002",
                    "strength": 4,
                    "context": "中金同事",
                    "frequency": "monthly",
                    "rationale": "原文：『Alice 和 Bob 是同事』",
                }
            ],
            "unknown_mentions": [],
        }
    )
    parser = RelationshipParser(repo, owner_id=owner_id, client=fake)
    result = parser.parse("Alice 和 Bob 是中金的同事，每月聚一次。Acme 公司事")

    assert result.error is None
    assert len(result.proposals) == 1
    e = result.proposals[0]
    assert {e.a_id, e.b_id} == {pids["Alice"], pids["Bob"]}
    assert e.strength == 4
    assert e.frequency == "monthly"
    assert e.context == "中金同事"

    # The anonymized text body should contain Pxxx tokens, never real
    # names. We check the `text` field of the JSON payload separately
    # because `roster[*].hint` is allowed to keep a 6-char name preview
    # by design (see `_build_roster` docstring).
    assert fake.last_user is not None
    payload = json.loads(fake.last_user)
    body = payload["text"]
    assert "Alice" not in body
    assert "Bob" not in body
    assert "Acme" not in body
    assert "P001" in body or "P002" in body


def test_parse_skips_p000_and_unknown_tokens(
    parser_setup: tuple[Repository, int, dict[str, int], _FakeLLMClient],
) -> None:
    repo, owner_id, _pids, fake = parser_setup
    fake.responses.append(
        {
            "edges": [
                # Should be dropped: touches me.
                {"a": "P000", "b": "P001", "strength": 3},
                # Should be dropped: P999 was never issued.
                {"a": "P001", "b": "P999", "strength": 3},
                # Should be dropped: same endpoint twice.
                {"a": "P002", "b": "P002", "strength": 3},
                # Should be kept.
                {"a": "P002", "b": "P003", "strength": 2},
            ],
            "unknown_mentions": ["Mike", "  ", "Mike"],  # de-dup + trim
        }
    )
    parser = RelationshipParser(repo, owner_id=owner_id, client=fake)
    result = parser.parse("anything")

    assert len(result.proposals) == 1
    assert result.unknown_mentions == ["Mike"]


def test_parse_empty_text_returns_error(
    parser_setup: tuple[Repository, int, dict[str, int], _FakeLLMClient],
) -> None:
    repo, owner_id, _pids, fake = parser_setup
    parser = RelationshipParser(repo, owner_id=owner_id, client=fake)
    result = parser.parse("   ")
    assert result.proposals == []
    assert result.error is not None
    assert fake.last_user is None  # never called


def test_parse_dedupes_unordered_pair(
    parser_setup: tuple[Repository, int, dict[str, int], _FakeLLMClient],
) -> None:
    repo, owner_id, _pids, fake = parser_setup
    fake.responses.append(
        {
            "edges": [
                {"a": "P001", "b": "P002", "strength": 3},
                {"a": "P002", "b": "P001", "strength": 5},  # same undirected pair
            ],
            "unknown_mentions": [],
        }
    )
    parser = RelationshipParser(repo, owner_id=owner_id, client=fake)
    result = parser.parse("...")
    assert len(result.proposals) == 1


def test_parse_owner_isolation(tmp_path: Path) -> None:
    """A parser scoped to owner R must not see / propose edges between
    owner T's contacts even when the LLM tries to."""
    db: Path = tmp_path / "iso.db"
    conn = connect(db)
    init_schema(conn, embedding_dim=4)
    repo = Repository(conn)
    r = repo.ensure_owner(slug="r", display_name="Richard")
    t = repo.ensure_owner(slug="t", display_name="Tommy")
    assert r.id is not None and t.id is not None

    r_alice = repo.add_person(Person(name="RAlice"))
    t_bob = repo.add_person(Person(name="TBob"))
    assert r_alice.id is not None and t_bob.id is not None
    repo.attach_person_to_owner(r_alice.id, r.id)
    repo.attach_person_to_owner(t_bob.id, t.id)

    # P001 in R's namespace = RAlice; P001 in T's namespace = TBob.
    # If the LLM returns an edge "P001-P002" while we're scoped to R,
    # the parser must drop it because R has no P002.
    fake = _FakeLLMClient(responses=[
        {"edges": [{"a": "P001", "b": "P002", "strength": 3}], "unknown_mentions": []},
    ])
    parser = RelationshipParser(repo, owner_id=r.id, client=fake)
    result = parser.parse("...")
    assert result.proposals == []  # no second peer in R's namespace
    conn.close()


def test_parse_deanonymizes_rationale_and_context(
    parser_setup: tuple[Repository, int, dict[str, int], _FakeLLMClient],
) -> None:
    """Real-world LLMs quote the *anonymized* prompt back inside `rationale`
    (and sometimes `context`). We must reverse-substitute Pxxx/Cxxx tokens
    before exposing those strings to the UI — otherwise the user sees
    `原文：[P052和P014是同事]` instead of real names.
    """
    repo, owner_id, pids, fake = parser_setup
    fake.responses.append(
        {
            "edges": [
                {
                    "a": "P001",
                    "b": "P002",
                    "strength": 4,
                    "frequency": "monthly",
                    # Both fields contain anonymized tokens — the parser
                    # should deanonymize before returning.
                    "context": "P001 和 P002 在 C001 一起工作",
                    "rationale": "原文：『P001 和 P002 是 C001 同事』",
                }
            ],
            "unknown_mentions": [],
        }
    )
    parser = RelationshipParser(repo, owner_id=owner_id, client=fake)
    result = parser.parse("Alice 和 Bob 是 Acme 同事")

    assert len(result.proposals) == 1
    e = result.proposals[0]
    # Token names are gone; real names + company are visible.
    assert e.context == "Alice 和 Bob 在 Acme 一起工作"
    assert e.rationale == "原文：『Alice 和 Bob 是 Acme 同事』"
    assert "P001" not in (e.rationale or "")
    assert "C001" not in (e.context or "")


def test_parse_handles_no_contacts(tmp_path: Path) -> None:
    db: Path = tmp_path / "empty.db"
    conn = connect(db)
    init_schema(conn, embedding_dim=4)
    repo = Repository(conn)
    owner = repo.ensure_owner(slug="r", display_name="Richard")
    assert owner.id is not None
    fake = _FakeLLMClient(responses=[])
    parser = RelationshipParser(repo, owner_id=owner.id, client=fake)
    result = parser.parse("Alice 和 Bob 是同事")
    assert result.error is not None
    assert "联系人" in result.error
    assert fake.last_user is None
    conn.close()
