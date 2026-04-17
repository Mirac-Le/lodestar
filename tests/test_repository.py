"""Basic CRUD and hybrid-lookup tests against an ephemeral DB."""

from __future__ import annotations

from lodestar.db import Repository
from lodestar.models import Frequency, Person, Relationship


def test_ensure_me_is_idempotent(repo: Repository) -> None:
    me1 = repo.ensure_me(name="Alice", bio="builder")
    me2 = repo.ensure_me(name="Someone Else")
    assert me1.id == me2.id
    assert me2.name == "Alice"


def test_add_person_roundtrip(repo: Repository) -> None:
    repo.ensure_me(name="Me")
    bob = repo.add_person(
        Person(
            name="Bob Liu",
            bio="ex-Google staff engineer working on search ranking",
            tags=["tech", "investor"],
            skills=["ML", "ranking"],
            companies=["Google"],
            cities=["Mountain View"],
        )
    )
    assert bob.id is not None
    fetched = repo.get_person(bob.id)
    assert fetched is not None
    assert fetched.tags == ["tech", "investor"]
    assert "ML" in fetched.skills
    assert "Google" in fetched.companies


def test_add_relationship_and_list(repo: Repository) -> None:
    me = repo.ensure_me(name="Me")
    bob = repo.add_person(Person(name="Bob"))
    assert me.id is not None and bob.id is not None
    repo.add_relationship(
        Relationship(
            source_id=me.id,
            target_id=bob.id,
            strength=4,
            context="college roommate",
            frequency=Frequency.QUARTERLY,
        )
    )
    rels = repo.list_relationships()
    assert len(rels) == 1
    assert rels[0].context == "college roommate"
    assert rels[0].strength == 4


def test_relationship_is_upsert(repo: Repository) -> None:
    me = repo.ensure_me(name="Me")
    bob = repo.add_person(Person(name="Bob"))
    assert me.id is not None and bob.id is not None
    for strength in (2, 3, 5):
        repo.add_relationship(
            Relationship(source_id=me.id, target_id=bob.id, strength=strength)
        )
    rels = repo.list_relationships()
    assert len(rels) == 1
    assert rels[0].strength == 5


def test_keyword_candidates_by_attribute(repo: Repository) -> None:
    repo.ensure_me(name="Me")
    repo.add_person(Person(name="Alice", tags=["investor"], skills=["AI"]))
    repo.add_person(Person(name="Bob", tags=["designer"]))

    scores = repo.keyword_candidates(["investor"])
    assert len(scores) == 1
    pid = next(iter(scores))
    assert repo.get_person(pid).name == "Alice"  # type: ignore[union-attr]


def test_vector_search_returns_nearest(repo: Repository) -> None:
    repo.ensure_me(name="Me")
    alice = repo.add_person(Person(name="Alice"))
    bob = repo.add_person(Person(name="Bob"))
    assert alice.id is not None and bob.id is not None

    repo.upsert_embedding(alice.id, [1.0, 0.0, 0.0, 0.0])
    repo.upsert_embedding(bob.id, [0.0, 1.0, 0.0, 0.0])

    hits = repo.vector_search([0.99, 0.0, 0.0, 0.0], limit=2)
    assert hits[0][0] == alice.id
