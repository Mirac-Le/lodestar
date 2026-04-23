"""反查涉及联系人的 db snapshot（Person + 1 跳邻居 + Me-edge），带脱敏。"""

from __future__ import annotations

from pathlib import Path

import pytest

from lodestar.db import Repository, connect, init_schema
from lodestar.models import Frequency, Person, Relationship
from lodestar.web.feedback_snapshot import build_snapshot


@pytest.fixture
def repo_with_graph(tmp_path: Path) -> Repository:
    conn = connect(tmp_path / "snap.db")
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    me = repo.ensure_me("我")
    alice = repo.add_person(Person(name="Alice", bio="电话 13812348888"))
    bob = repo.add_person(Person(name="Bob", bio="普通简介"))
    assert me.id and alice.id and bob.id
    repo.add_relationship(Relationship(
        source_id=me.id, target_id=alice.id, strength=4,
        frequency=Frequency.MONTHLY,
    ))
    repo.add_relationship(Relationship(
        source_id=alice.id, target_id=bob.id, strength=3,
        frequency=Frequency.QUARTERLY,
    ))
    return repo


def test_snapshot_includes_each_involved_person(repo_with_graph: Repository) -> None:
    alice_id = next(
        p.id for p in repo_with_graph.list_people() if p.name == "Alice"
    )
    snap = build_snapshot(repo_with_graph, [alice_id])
    assert len(snap) == 1
    assert snap[0]["person"]["name"] == "Alice"


def test_snapshot_scrubs_pii_in_bio(repo_with_graph: Repository) -> None:
    alice_id = next(
        p.id for p in repo_with_graph.list_people() if p.name == "Alice"
    )
    snap = build_snapshot(repo_with_graph, [alice_id])
    assert "13812348888" not in snap[0]["person"]["bio"]
    assert "138****8888" in snap[0]["person"]["bio"]


def test_snapshot_includes_me_edge_and_neighbors(
    repo_with_graph: Repository,
) -> None:
    alice_id = next(
        p.id for p in repo_with_graph.list_people() if p.name == "Alice"
    )
    snap = build_snapshot(repo_with_graph, [alice_id])
    entry = snap[0]
    assert entry["me_edge"] is not None
    assert entry["me_edge"]["strength"] == 4
    neighbor_names = {n["name"] for n in entry["neighbors"]}
    assert "Bob" in neighbor_names
