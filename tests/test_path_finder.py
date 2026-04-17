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
