"""Hybrid search tests — keyword-only path doesn't need a real embedder."""

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


def test_owner_isolation_keyword_path(repo: Repository) -> None:
    """两个 owner 各自的网络只看得到自己挂的人。"""
    richard = repo.ensure_owner(slug="richard", display_name="Richard")
    tommy = repo.ensure_owner(slug="tommy", display_name="Tommy")

    r_alice = repo.add_person(
        Person(name="R-Alice", tags=["AI", "investor"], skills=["deep learning"])
    )
    t_bob = repo.add_person(
        Person(name="T-Bob", tags=["AI", "investor"], skills=["ml"])
    )
    assert r_alice.id is not None and t_bob.id is not None
    repo.attach_person_to_owner(r_alice.id, richard.id)
    repo.attach_person_to_owner(t_bob.id, tommy.id)

    intent = GoalIntent(
        original="raise from AI investor",
        keywords=["AI", "investor"],
        roles=["investor"],
    )

    r_search = HybridSearch(repo=repo, embedder=None, owner_id=richard.id)
    r_cands = r_search.search(intent, top_k=10)
    r_pids = {c.person_id for c in r_cands}
    assert r_alice.id in r_pids
    assert t_bob.id not in r_pids

    t_search = HybridSearch(repo=repo, embedder=None, owner_id=tommy.id)
    t_cands = t_search.search(intent, top_k=10)
    t_pids = {c.person_id for c in t_cands}
    assert t_bob.id in t_pids
    assert r_alice.id not in t_pids


def test_owner_id_filters_vector_search(repo: Repository) -> None:
    """vector_search 也按 owner 过滤，不会捞到对方网络里的人。"""
    richard = repo.ensure_owner(slug="richard", display_name="Richard")
    tommy = repo.ensure_owner(slug="tommy", display_name="Tommy")

    r_alice = repo.add_person(Person(name="R-Alice"))
    t_bob = repo.add_person(Person(name="T-Bob"))
    assert r_alice.id is not None and t_bob.id is not None
    repo.attach_person_to_owner(r_alice.id, richard.id)
    repo.attach_person_to_owner(t_bob.id, tommy.id)

    repo.upsert_embedding(r_alice.id, [1.0, 0.0, 0.0, 0.0])
    repo.upsert_embedding(t_bob.id, [0.99, 0.01, 0.0, 0.0])

    query = [1.0, 0.0, 0.0, 0.0]

    all_hits = {pid for pid, _ in repo.vector_search(query, limit=5)}
    assert {r_alice.id, t_bob.id}.issubset(all_hits)

    r_hits = {pid for pid, _ in repo.vector_search(query, limit=5, owner_id=richard.id)}
    assert r_hits == {r_alice.id}

    t_hits = {pid for pid, _ in repo.vector_search(query, limit=5, owner_id=tommy.id)}
    assert t_hits == {t_bob.id}
