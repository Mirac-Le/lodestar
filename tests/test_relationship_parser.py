"""Unit tests for `lodestar.enrich.relationship_parser.RelationshipParser`.

The parser orchestrates anonymizer + LLM + deanonymizer. The LLM is the
only thing we mock — everything else exercises the real Anonymizer /
Repository code paths so a regression in token assignment, P000-skipping,
or deanonymization immediately surfaces here.

Note: 自从切到一人一库后，parser 不再需要 ``owner_id`` 形参，整个 repo
就是一个 owner 的命名空间；isolation 由数据库文件本身保证（见
``test_mount_unlock``）。
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


@dataclass
class _FakeLLMClient:
    """Records the last `chat_json` call and returns canned JSON."""

    responses: list[dict[str, Any]]
    last_system: str | None = None
    last_user: str | None = None

    def chat_json(
        self, *, system: str, user: str, temperature: float = 0.1
    ) -> LLMCallResult:
        self.last_system = system
        self.last_user = user
        if not self.responses:
            raise AssertionError("FakeLLMClient ran out of canned responses.")
        data = self.responses.pop(0)
        return LLMCallResult(data=data, raw="{}")


@pytest.fixture
def parser_setup(
    tmp_path: Path,
) -> Iterator[tuple[Repository, dict[str, int], _FakeLLMClient]]:
    """Build me + three peers, return ids + fake LLM client."""
    db: Path = tmp_path / "rel.db"
    conn = connect(db)
    init_schema(conn, embedding_dim=4)
    repo = Repository(conn)
    me = repo.ensure_me("Me")
    assert me.id is not None

    pids: dict[str, int] = {"me": me.id}
    for n in ("Alice", "Bob", "Carol"):
        p = repo.add_person(Person(name=n, companies=["Acme"]))
        assert p.id is not None
        pids[n] = p.id

    fake = _FakeLLMClient(responses=[])
    try:
        yield repo, pids, fake
    finally:
        conn.close()


def test_parse_returns_proposals_and_anonymizes_prompt(
    parser_setup: tuple[Repository, dict[str, int], _FakeLLMClient],
) -> None:
    repo, pids, fake = parser_setup
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
    parser = RelationshipParser(repo, client=fake)
    result = parser.parse("Alice 和 Bob 是中金的同事，每月聚一次。Acme 公司事")

    assert result.error is None
    assert len(result.proposals) == 1
    e = result.proposals[0]
    assert {e.a_id, e.b_id} == {pids["Alice"], pids["Bob"]}
    assert e.strength == 4
    assert e.frequency == "monthly"
    assert e.context == "中金同事"

    assert fake.last_user is not None
    payload = json.loads(fake.last_user)
    body = payload["text"]
    assert "Alice" not in body
    assert "Bob" not in body
    assert "Acme" not in body
    assert "P001" in body or "P002" in body


def test_parse_skips_p000_and_unknown_tokens(
    parser_setup: tuple[Repository, dict[str, int], _FakeLLMClient],
) -> None:
    repo, _pids, fake = parser_setup
    fake.responses.append(
        {
            "edges": [
                {"a": "P000", "b": "P001", "strength": 3},
                {"a": "P001", "b": "P999", "strength": 3},
                {"a": "P002", "b": "P002", "strength": 3},
                {"a": "P002", "b": "P003", "strength": 2},
            ],
            "unknown_mentions": ["Mike", "  ", "Mike"],
        }
    )
    parser = RelationshipParser(repo, client=fake)
    result = parser.parse("anything")

    assert len(result.proposals) == 1
    assert result.unknown_mentions == ["Mike"]


def test_parse_empty_text_returns_error(
    parser_setup: tuple[Repository, dict[str, int], _FakeLLMClient],
) -> None:
    repo, _pids, fake = parser_setup
    parser = RelationshipParser(repo, client=fake)
    result = parser.parse("   ")
    assert result.proposals == []
    assert result.error is not None
    assert fake.last_user is None


def test_parse_dedupes_unordered_pair(
    parser_setup: tuple[Repository, dict[str, int], _FakeLLMClient],
) -> None:
    repo, _pids, fake = parser_setup
    fake.responses.append(
        {
            "edges": [
                {"a": "P001", "b": "P002", "strength": 3},
                {"a": "P002", "b": "P001", "strength": 5},
            ],
            "unknown_mentions": [],
        }
    )
    parser = RelationshipParser(repo, client=fake)
    result = parser.parse("...")
    assert len(result.proposals) == 1


def test_parse_deanonymizes_rationale_and_context(
    parser_setup: tuple[Repository, dict[str, int], _FakeLLMClient],
) -> None:
    """Real-world LLMs quote the *anonymized* prompt back inside `rationale`
    (and sometimes `context`). We must reverse-substitute Pxxx/Cxxx tokens
    before exposing those strings to the UI.
    """
    repo, _pids, fake = parser_setup
    fake.responses.append(
        {
            "edges": [
                {
                    "a": "P001",
                    "b": "P002",
                    "strength": 4,
                    "frequency": "monthly",
                    "context": "P001 和 P002 在 C001 一起工作",
                    "rationale": "原文：『P001 和 P002 是 C001 同事』",
                }
            ],
            "unknown_mentions": [],
        }
    )
    parser = RelationshipParser(repo, client=fake)
    result = parser.parse("Alice 和 Bob 是 Acme 同事")

    assert len(result.proposals) == 1
    e = result.proposals[0]
    assert e.context == "Alice 和 Bob 在 Acme 一起工作"
    assert e.rationale == "原文：『Alice 和 Bob 是 Acme 同事』"
    assert "P001" not in (e.rationale or "")
    assert "C001" not in (e.context or "")


def test_parse_handles_no_contacts(tmp_path: Path) -> None:
    db: Path = tmp_path / "empty.db"
    conn = connect(db)
    init_schema(conn, embedding_dim=4)
    repo = Repository(conn)
    repo.ensure_me("Me")  # me only, zero peers
    fake = _FakeLLMClient(responses=[])
    parser = RelationshipParser(repo, client=fake)
    result = parser.parse("Alice 和 Bob 是同事")
    assert result.error is not None
    assert "联系人" in result.error
    assert fake.last_user is None
    conn.close()
