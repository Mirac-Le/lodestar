"""Hybrid search tests — keyword-only path doesn't need a real embedder.

Owner isolation has moved to the *file* level (one db per owner) so the
historical `test_owner_isolation_*` cases were dropped together with the
`owner_id` parameter on `HybridSearch` and `Repository`. Per-owner
separation is now exercised end-to-end in `test_mount_unlock.py`.
"""

from __future__ import annotations

from lodestar.db import Repository
from lodestar.models import GoalIntent, Person
from lodestar.search import HybridSearch


def test_keyword_path_finds_matching_person(repo: Repository) -> None:
    repo.ensure_me(name="Me")
    repo.add_person(Person(name="Alice", tags=["AI", "investor"], skills=["deep learning"]))
    repo.add_person(Person(name="Bob", tags=["design"]))

    search = HybridSearch(repo=repo, embedder=None)
    intent = GoalIntent(original="raise from AI investor", keywords=["AI", "investor"])
    candidates = search.search(intent, top_k=5)

    assert candidates
    top_pid = candidates[0].person_id
    top = repo.get_person(top_pid)
    assert top is not None
    assert top.name == "Alice"
    assert candidates[0].score == 1.0


def test_empty_intent_returns_nothing(repo: Repository) -> None:
    repo.ensure_me(name="Me")
    repo.add_person(Person(name="Alice"))
    search = HybridSearch(repo=repo, embedder=None)
    assert search.search(GoalIntent(original="")) == []
