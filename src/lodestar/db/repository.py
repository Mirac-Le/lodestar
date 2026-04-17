"""CRUD for the whole domain. One class, many short methods.

The repository owns a live sqlite3 connection. All write methods commit
eagerly via context managers so callers never see partial state.
"""

from __future__ import annotations

import sqlite3
import struct
from collections.abc import Iterable, Sequence
from typing import Any

from lodestar.models import Frequency, Person, Relationship


def _pack_vector(vec: Sequence[float]) -> bytes:
    """sqlite-vec expects little-endian float32 bytes."""
    return struct.pack(f"{len(vec)}f", *vec)


def _row_get(row: sqlite3.Row, key: str, default: Any) -> Any:
    """sqlite3.Row has no .get(); emulate it for forward-compatible columns
    that may be absent on databases that pre-date a migration."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


class Repository:
    """Data access layer."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ Me
    def get_me(self) -> Person | None:
        row = self.conn.execute("SELECT * FROM person WHERE is_me = 1").fetchone()
        return self._hydrate_person(row) if row else None

    def ensure_me(self, name: str, bio: str | None = None) -> Person:
        existing = self.get_me()
        if existing is not None:
            return existing
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO person (name, bio, is_me) VALUES (?, ?, 1)",
                (name, bio),
            )
        pid = cur.lastrowid
        assert pid is not None
        result = self.get_person(pid)
        assert result is not None
        return result

    # -------------------------------------------------------------- Person
    def add_person(self, person: Person) -> Person:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO person (name, bio, notes, is_me, is_wishlist) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    person.name, person.bio, person.notes,
                    int(person.is_me), int(person.is_wishlist),
                ),
            )
            pid = cur.lastrowid
            assert pid is not None
            self._apply_attributes(pid, person)
        result = self.get_person(pid)
        assert result is not None
        return result

    def update_person(self, person: Person) -> Person:
        assert person.id is not None, "Cannot update a person without an id"
        with self.conn:
            self.conn.execute(
                "UPDATE person SET name = ?, bio = ?, notes = ?, is_wishlist = ? "
                "WHERE id = ?",
                (
                    person.name, person.bio, person.notes,
                    int(person.is_wishlist), person.id,
                ),
            )
            for tbl in (
                "person_tag",
                "person_skill",
                "person_company",
                "person_city",
                "person_need",
            ):
                self.conn.execute(f"DELETE FROM {tbl} WHERE person_id = ?", (person.id,))
            self._apply_attributes(person.id, person)
        result = self.get_person(person.id)
        assert result is not None
        return result

    def delete_person(self, person_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM vec_person_bio WHERE person_id = ?", (person_id,))
            self.conn.execute("DELETE FROM person WHERE id = ?", (person_id,))

    def get_person(self, person_id: int) -> Person | None:
        row = self.conn.execute("SELECT * FROM person WHERE id = ?", (person_id,)).fetchone()
        return self._hydrate_person(row) if row else None

    def find_person_by_name(self, name: str) -> Person | None:
        row = self.conn.execute(
            "SELECT * FROM person WHERE name = ? LIMIT 1", (name,)
        ).fetchone()
        return self._hydrate_person(row) if row else None

    def list_people(self) -> list[Person]:
        rows = self.conn.execute(
            "SELECT * FROM person WHERE is_me = 0 ORDER BY name"
        ).fetchall()
        return [self._hydrate_person(r) for r in rows]

    def _hydrate_person(self, row: sqlite3.Row) -> Person:
        pid = row["id"]
        tags = [r["name"] for r in self.conn.execute(
            "SELECT t.name FROM tag t JOIN person_tag pt ON pt.tag_id = t.id WHERE pt.person_id = ?",
            (pid,),
        )]
        skills = [r["name"] for r in self.conn.execute(
            "SELECT s.name FROM skill s JOIN person_skill ps ON ps.skill_id = s.id WHERE ps.person_id = ?",
            (pid,),
        )]
        companies = [r["name"] for r in self.conn.execute(
            "SELECT c.name FROM company c JOIN person_company pc ON pc.company_id = c.id WHERE pc.person_id = ?",
            (pid,),
        )]
        cities = [r["name"] for r in self.conn.execute(
            "SELECT ci.name FROM city ci JOIN person_city pci ON pci.city_id = ci.id WHERE pci.person_id = ?",
            (pid,),
        )]
        needs = [r["name"] for r in self.conn.execute(
            "SELECT n.name FROM need n JOIN person_need pn ON pn.need_id = n.id WHERE pn.person_id = ?",
            (pid,),
        )]
        return Person(
            id=pid,
            name=row["name"],
            bio=row["bio"],
            notes=row["notes"],
            is_me=bool(row["is_me"]),
            is_wishlist=bool(_row_get(row, "is_wishlist", 0)),
            tags=tags,
            skills=skills,
            companies=companies,
            cities=cities,
            needs=needs,
        )

    def set_wishlist(self, person_id: int, *, on: bool) -> None:
        """Toggle the is_wishlist curation flag without disturbing other fields."""
        with self.conn:
            self.conn.execute(
                "UPDATE person SET is_wishlist = ? WHERE id = ?",
                (1 if on else 0, person_id),
            )

    def _apply_attributes(self, person_id: int, p: Person) -> None:
        def upsert_lookup(table: str, name: str) -> int:
            self.conn.execute(f"INSERT OR IGNORE INTO {table} (name) VALUES (?)", (name,))
            row = self.conn.execute(
                f"SELECT id FROM {table} WHERE name = ?", (name,)
            ).fetchone()
            return int(row["id"])

        for name in p.tags:
            tid = upsert_lookup("tag", name)
            self.conn.execute(
                "INSERT OR IGNORE INTO person_tag (person_id, tag_id) VALUES (?, ?)",
                (person_id, tid),
            )
        for name in p.skills:
            sid = upsert_lookup("skill", name)
            self.conn.execute(
                "INSERT OR IGNORE INTO person_skill (person_id, skill_id) VALUES (?, ?)",
                (person_id, sid),
            )
        for name in p.companies:
            cid = upsert_lookup("company", name)
            self.conn.execute(
                "INSERT OR IGNORE INTO person_company (person_id, company_id) VALUES (?, ?)",
                (person_id, cid),
            )
        for name in p.cities:
            ci = upsert_lookup("city", name)
            self.conn.execute(
                "INSERT OR IGNORE INTO person_city (person_id, city_id) VALUES (?, ?)",
                (person_id, ci),
            )
        for name in p.needs:
            nid = upsert_lookup("need", name)
            self.conn.execute(
                "INSERT OR IGNORE INTO person_need (person_id, need_id) VALUES (?, ?)",
                (person_id, nid),
            )

    # ---------------------------------------------------------- Embeddings
    def upsert_embedding(self, person_id: int, vector: Sequence[float]) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM vec_person_bio WHERE person_id = ?", (person_id,)
            )
            self.conn.execute(
                "INSERT INTO vec_person_bio (person_id, embedding) VALUES (?, ?)",
                (person_id, _pack_vector(vector)),
            )

    def vector_search(
        self, query_vec: Sequence[float], limit: int = 20
    ) -> list[tuple[int, float]]:
        """Return [(person_id, distance)] for nearest neighbors. Lower distance = closer."""
        rows = self.conn.execute(
            """
            SELECT person_id, distance
            FROM vec_person_bio
            WHERE embedding MATCH ?
              AND k = ?
            ORDER BY distance
            """,
            (_pack_vector(query_vec), limit),
        ).fetchall()
        return [(int(r["person_id"]), float(r["distance"])) for r in rows]

    # ------------------------------------------------------- Relationships
    def add_relationship(self, rel: Relationship) -> Relationship:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO relationship
                    (source_id, target_id, strength, context, frequency,
                     last_contact, introduced_by_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, target_id) DO UPDATE SET
                    strength = excluded.strength,
                    context = excluded.context,
                    frequency = excluded.frequency,
                    last_contact = excluded.last_contact,
                    introduced_by_id = excluded.introduced_by_id
                """,
                (
                    rel.source_id,
                    rel.target_id,
                    rel.strength,
                    rel.context,
                    rel.frequency.value,
                    rel.last_contact.isoformat() if rel.last_contact else None,
                    rel.introduced_by_id,
                ),
            )
        row = self.conn.execute(
            "SELECT * FROM relationship WHERE source_id = ? AND target_id = ?",
            (rel.source_id, rel.target_id),
        ).fetchone()
        assert row is not None
        return Relationship(
            id=row["id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            strength=row["strength"],
            context=row["context"],
            frequency=Frequency(row["frequency"]),
            last_contact=None,
            introduced_by_id=row["introduced_by_id"],
        )

    def list_relationships(self) -> list[Relationship]:
        rows = self.conn.execute("SELECT * FROM relationship").fetchall()
        return [
            Relationship(
                id=r["id"],
                source_id=r["source_id"],
                target_id=r["target_id"],
                strength=r["strength"],
                context=r["context"],
                frequency=Frequency(r["frequency"]),
                last_contact=None,
                introduced_by_id=r["introduced_by_id"],
            )
            for r in rows
        ]

    # ---------------------------------------------------- Keyword matching
    def keyword_candidates(self, terms: Iterable[str]) -> dict[int, int]:
        """Score people by how many distinct query terms hit any attribute.

        Returns {person_id: hit_count}. Case-insensitive LIKE match against
        name / bio / notes / tag / skill / company / city.

        NOTE: We deliberately do NOT match against `need` here. The `need`
        column records what a person *is seeking* — matching a helper-query
        term like "融资" against another person's need "机构融资" would
        surface peers with the same gap as the searcher, not actual helpers.
        See `brokerable_by_needs()` for the seeker↔provider match that
        *does* want the `need` column.
        """
        terms = [t.strip() for t in terms if t and t.strip()]
        if not terms:
            return {}

        scores: dict[int, int] = {}
        for term in terms:
            like = f"%{term}%"
            sql = """
                SELECT DISTINCT p.id AS pid FROM person p
                LEFT JOIN person_tag pt ON pt.person_id = p.id
                LEFT JOIN tag t         ON t.id = pt.tag_id
                LEFT JOIN person_skill ps ON ps.person_id = p.id
                LEFT JOIN skill s         ON s.id = ps.skill_id
                LEFT JOIN person_company pc ON pc.person_id = p.id
                LEFT JOIN company co        ON co.id = pc.company_id
                LEFT JOIN person_city pci   ON pci.person_id = p.id
                LEFT JOIN city ci           ON ci.id = pci.city_id
                WHERE p.is_me = 0
                  AND (
                    p.name  LIKE ? COLLATE NOCASE
                    OR p.bio   LIKE ? COLLATE NOCASE
                    OR p.notes LIKE ? COLLATE NOCASE
                    OR t.name  LIKE ? COLLATE NOCASE
                    OR s.name  LIKE ? COLLATE NOCASE
                    OR co.name LIKE ? COLLATE NOCASE
                    OR ci.name LIKE ? COLLATE NOCASE
                  )
            """
            args: tuple[Any, ...] = (like,) * 7
            for row in self.conn.execute(sql, args).fetchall():
                pid = int(row["pid"])
                scores[pid] = scores.get(pid, 0) + 1
        return scores
