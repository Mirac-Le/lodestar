"""Shortest-path ranking tests."""

from __future__ import annotations

from lodestar.db import Repository
from lodestar.models import Person, Relationship
from lodestar.search import Candidate, PathFinder


def _connect(repo: Repository, a: int, b: int, strength: int, context: str = "") -> None:
    repo.add_relationship(
        Relationship(source_id=a, target_id=b, strength=strength, context=context)
    )


def test_prefers_stronger_shorter_path(repo: Repository) -> None:
    me = repo.ensure_me(name="Me")
    alice = repo.add_person(Person(name="Alice"))
    bob = repo.add_person(Person(name="Bob"))
    assert me.id and alice.id and bob.id

    _connect(repo, me.id, alice.id, strength=5, context="sister")
    _connect(repo, alice.id, bob.id, strength=4, context="colleague")

    finder = PathFinder(repo=repo, max_hops=3)
    results = finder.rank([Candidate(person_id=bob.id, score=0.9)])

    assert len(results) == 1
    r = results[0]
    assert r.target.name == "Bob"
    names = [s.name for s in r.path]
    assert names == ["Me", "Alice", "Bob"]
    assert r.path_strength == 9.0


def test_unreachable_beyond_max_hops(repo: Repository) -> None:
    me = repo.ensure_me(name="Me")
    alice = repo.add_person(Person(name="Alice"))
    bob = repo.add_person(Person(name="Bob"))
    assert me.id and alice.id and bob.id

    _connect(repo, me.id, alice.id, strength=3)

    finder = PathFinder(repo=repo, max_hops=3)
    results = finder.rank([Candidate(person_id=bob.id, score=0.5)])
    assert len(results) == 1
    assert results[0].path_strength == 0.0
    assert results[0].path[0].name == "Bob"


def test_path_kind_is_purely_topological(repo: Repository) -> None:
    """`path_kind` must reflect graph topology only — `is_wishlist` is a
    curation flag carried on the Person and must NOT change the bucket."""
    me = repo.ensure_me(name="Me")
    direct_friend = repo.add_person(Person(name="Direct", is_wishlist=True))
    weak_friend = repo.add_person(Person(name="Weak"))
    far = repo.add_person(Person(name="Far", is_wishlist=True))
    bridge = repo.add_person(Person(name="Bridge"))
    assert me.id and direct_friend.id and weak_friend.id and far.id and bridge.id

    _connect(repo, me.id, direct_friend.id, strength=4)
    _connect(repo, me.id, weak_friend.id, strength=1)
    _connect(repo, me.id, bridge.id, strength=3)
    _connect(repo, bridge.id, far.id, strength=3)

    finder = PathFinder(repo=repo, max_hops=3)
    results = {
        r.target.name: r
        for r in finder.rank([
            Candidate(person_id=direct_friend.id, score=0.7),
            Candidate(person_id=weak_friend.id, score=0.7),
            Candidate(person_id=far.id, score=0.7),
        ])
    }
    assert results["Direct"].path_kind == "direct"
    assert results["Direct"].target.is_wishlist is True
    assert results["Weak"].path_kind == "weak"
    assert results["Far"].path_kind == "indirect"
    assert results["Far"].target.is_wishlist is True


def test_wishlist_no_longer_overrides_relevance(repo: Repository) -> None:
    """Regression: previously a wishlist (no Me-edge) candidate got a softer
    hop penalty so it could outrank a directly-connected candidate with the
    same relevance. Now ranking is fair across kinds."""
    me = repo.ensure_me(name="Me")
    direct = repo.add_person(Person(name="Direct"))
    distant = repo.add_person(Person(name="Distant", is_wishlist=True))
    bridge = repo.add_person(Person(name="Bridge"))
    assert me.id and direct.id and distant.id and bridge.id

    _connect(repo, me.id, direct.id, strength=4)
    _connect(repo, me.id, bridge.id, strength=3)
    _connect(repo, bridge.id, distant.id, strength=3)

    finder = PathFinder(repo=repo, max_hops=3)
    ranked = finder.rank([
        Candidate(person_id=direct.id, score=0.7),
        Candidate(person_id=distant.id, score=0.7),
    ])
    assert [r.target.name for r in ranked][0] == "Direct"
    direct_row = next(r for r in ranked if r.target.name == "Direct")
    distant_row = next(r for r in ranked if r.target.name == "Distant")
    assert direct_row.combined_score > distant_row.combined_score


def test_direct_connection_wins(repo: Repository) -> None:
    me = repo.ensure_me(name="Me")
    alice = repo.add_person(Person(name="Alice"))
    bob = repo.add_person(Person(name="Bob"))
    carol = repo.add_person(Person(name="Carol"))
    assert me.id and alice.id and bob.id and carol.id

    _connect(repo, me.id, alice.id, strength=5)
    _connect(repo, alice.id, carol.id, strength=4)
    _connect(repo, me.id, bob.id, strength=5)
    _connect(repo, bob.id, carol.id, strength=3)

    finder = PathFinder(repo=repo, max_hops=3)
    results = finder.rank([Candidate(person_id=carol.id, score=1.0)])
    path_names = [s.name for s in results[0].path]
    assert path_names[0] == "Me"
    assert path_names[-1] == "Carol"
    assert len(path_names) == 3
