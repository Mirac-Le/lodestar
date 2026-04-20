"""CRUD for the whole domain. One class, many short methods.

The repository owns a live sqlite3 connection. All write methods commit
eagerly via context managers so callers never see partial state.
"""

from __future__ import annotations

import sqlite3
import struct
from collections.abc import Iterable, Sequence
from typing import Any

from lodestar.models import Frequency, Owner, Person, Relationship


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


# Edge provenance hierarchy used by `Repository.add_relationship`.
# Higher value = more authoritative; never gets overwritten by a write
# of lower value. Equal-value writes always win (idempotent re-runs).
_SOURCE_PRIORITY: dict[str, int] = {
    "manual": 2,
    "colleague_inferred": 1,
    "ai_inferred": 0,
}


class Repository:
    """Data access layer."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------- Owners
    def list_owners(self) -> list[Owner]:
        rows = self.conn.execute(
            "SELECT * FROM owner ORDER BY position, id"
        ).fetchall()
        return [self._hydrate_owner(r) for r in rows]

    def get_owner_by_slug(self, slug: str) -> Owner | None:
        row = self.conn.execute(
            "SELECT * FROM owner WHERE slug = ?", (slug,)
        ).fetchone()
        return self._hydrate_owner(row) if row else None

    def get_owner(self, owner_id: int) -> Owner | None:
        row = self.conn.execute(
            "SELECT * FROM owner WHERE id = ?", (owner_id,)
        ).fetchone()
        return self._hydrate_owner(row) if row else None

    def ensure_owner(
        self,
        slug: str,
        display_name: str,
        bio: str | None = None,
        accent_color: str | None = None,
    ) -> Owner:
        """Create the owner (and its me-person row) if not already present."""
        existing = self.get_owner_by_slug(slug)
        if existing is not None:
            return existing
        # Each owner needs its own me-person; we always create a fresh one
        # rather than reusing an existing person, even if the display name
        # matches an existing contact, to keep `me` topologically distinct.
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO person (name, bio, is_me) VALUES (?, ?, 1)",
                (display_name, bio),
            )
            me_pid = cur.lastrowid
            assert me_pid is not None
            position = self.conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM owner"
            ).fetchone()["p"]
            self.conn.execute(
                "INSERT INTO owner (slug, display_name, me_person_id, "
                "accent_color, position) VALUES (?, ?, ?, ?, ?)",
                (slug, display_name, me_pid, accent_color, position),
            )
            owner_id = self.conn.execute(
                "SELECT id FROM owner WHERE slug = ?", (slug,)
            ).fetchone()["id"]
            self.conn.execute(
                "INSERT OR IGNORE INTO person_owner (person_id, owner_id) "
                "VALUES (?, ?)",
                (me_pid, owner_id),
            )
        result = self.get_owner_by_slug(slug)
        assert result is not None
        return result

    def _hydrate_owner(self, row: sqlite3.Row) -> Owner:
        return Owner(
            id=int(row["id"]),
            slug=row["slug"],
            display_name=row["display_name"],
            me_person_id=int(row["me_person_id"]),
            accent_color=_row_get(row, "accent_color", None),
            position=int(_row_get(row, "position", 0)),
            web_password_hash=_row_get(row, "web_password_hash", None),
        )

    def set_owner_web_password(self, owner_id: int, plain: str | None) -> None:
        """Set or clear per-owner web UI password (stored as hash)."""
        with self.conn:
            if plain is None or plain == "":
                self.conn.execute(
                    "UPDATE owner SET web_password_hash = NULL WHERE id = ?",
                    (owner_id,),
                )
            else:
                from lodestar.web.owner_unlock import hash_web_password

                h = hash_web_password(plain)
                self.conn.execute(
                    "UPDATE owner SET web_password_hash = ? WHERE id = ?",
                    (h, owner_id),
                )

    # ------------------------------------------------------------------ Me
    def get_me(self, owner_id: int | None = None) -> Person | None:
        """Return the `me` person.

        If `owner_id` is provided, returns that owner's me row. If not,
        falls back to the first owner (so legacy single-owner code paths
        keep working for ad-hoc CLI usage).
        """
        if owner_id is not None:
            owner = self.get_owner(owner_id)
            return self.get_person(owner.me_person_id) if owner else None
        owners = self.list_owners()
        if owners:
            return self.get_person(owners[0].me_person_id)
        # Last-resort fallback for databases predating the owner table.
        row = self.conn.execute(
            "SELECT * FROM person WHERE is_me = 1 LIMIT 1"
        ).fetchone()
        return self._hydrate_person(row) if row else None

    def ensure_me(self, name: str, bio: str | None = None) -> Person:
        """Convenience for legacy single-owner workflows.

        Creates a default owner (slug='me') if none exists; otherwise
        returns its me row.
        """
        owner = self.get_owner_by_slug("me") or self.ensure_owner(
            slug="me", display_name=name, bio=bio,
        )
        result = self.get_person(owner.me_person_id)
        assert result is not None
        return result

    # -------------------------------------------------- person↔owner glue
    def attach_person_to_owner(self, person_id: int, owner_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO person_owner (person_id, owner_id) "
                "VALUES (?, ?)",
                (person_id, owner_id),
            )

    def list_owner_person_ids(self, owner_id: int) -> set[int]:
        rows = self.conn.execute(
            "SELECT person_id FROM person_owner WHERE owner_id = ?",
            (owner_id,),
        ).fetchall()
        return {int(r["person_id"]) for r in rows}

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
        """Lookup is intentionally global (owner-agnostic) so that a
        contact shared between two owners merges to a single row by name.

        We also explicitly skip rows where `is_me=1` so an owner whose
        display name happens to match a real contact's name does not
        accidentally get aliased onto that contact's node.
        """
        row = self.conn.execute(
            "SELECT * FROM person WHERE name = ? AND is_me = 0 LIMIT 1",
            (name,),
        ).fetchone()
        return self._hydrate_person(row) if row else None

    def list_people(self, owner_id: int | None = None) -> list[Person]:
        if owner_id is None:
            rows = self.conn.execute(
                "SELECT * FROM person WHERE is_me = 0 ORDER BY name"
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT p.* FROM person p
                JOIN person_owner po ON po.person_id = p.id
                WHERE p.is_me = 0 AND po.owner_id = ?
                ORDER BY p.name
                """,
                (owner_id,),
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

    # ---------------------------------------------------------- Companies
    def list_owner_companies(
        self, owner_id: int
    ) -> list[tuple[int, str, int]]:
        """Return `(company_id, name, headcount)` for every company that
        has at least one person attached under this owner.

        Used by `lodestar normalize-companies` to decide which alias rows
        to merge. We scope to one owner so two owners don't accidentally
        leak each other's roster — same company name in both owners is
        the same `company` row, but headcount is per-owner here.
        """
        rows = self.conn.execute(
            """
            SELECT c.id AS id, c.name AS name, COUNT(*) AS n
            FROM company c
            JOIN person_company pc ON pc.company_id = c.id
            JOIN person_owner   po ON po.person_id = pc.person_id
            WHERE po.owner_id = ?
            GROUP BY c.id, c.name
            ORDER BY n DESC, c.name
            """,
            (owner_id,),
        ).fetchall()
        return [(int(r["id"]), str(r["name"]), int(r["n"])) for r in rows]

    def merge_companies(
        self,
        canonical: str,
        aliases: Sequence[str],
        *,
        owner_id: int | None = None,
    ) -> tuple[int, int]:
        """Merge `aliases` into `canonical` at the `company` table level.

        Concretely: re-point every `person_company.company_id` that points
        to an alias row to the canonical row instead, then delete the now
        empty alias rows. Idempotent: aliases that no longer exist are
        skipped. If `canonical` itself doesn't exist yet but at least one
        alias does, we **rename** the alias to canonical instead of
        creating a fresh row, to preserve any existing FK references.

        `owner_id` is informational only — `company` is a global lookup
        table not scoped per owner, and merging it affects every owner
        that shares the alias. We accept the param to make caller intent
        explicit; the actual rewrite spans all owners atomically.

        Returns `(reassigned_links, deleted_alias_rows)`.
        """
        del owner_id  # caller-intent marker; merge is global by design
        canonical_clean = canonical.strip()
        if not canonical_clean:
            raise ValueError("canonical company name must be non-empty")
        alias_clean = [a.strip() for a in aliases if a and a.strip() and a.strip() != canonical_clean]
        if not alias_clean:
            return (0, 0)

        with self.conn:
            row = self.conn.execute(
                "SELECT id FROM company WHERE name = ?", (canonical_clean,)
            ).fetchone()
            if row is None:
                # No canonical row yet — recycle the first existing alias
                # by renaming it. This avoids breaking any other table
                # that holds a FK to that company id.
                first_alias_row = None
                for a in alias_clean:
                    r = self.conn.execute(
                        "SELECT id FROM company WHERE name = ?", (a,)
                    ).fetchone()
                    if r is not None:
                        first_alias_row = r
                        first_alias_name = a
                        break
                if first_alias_row is None:
                    return (0, 0)
                self.conn.execute(
                    "UPDATE company SET name = ? WHERE id = ?",
                    (canonical_clean, int(first_alias_row["id"])),
                )
                canonical_id = int(first_alias_row["id"])
                # Don't try to also merge the just-renamed row into itself.
                alias_clean = [a for a in alias_clean if a != first_alias_name]
            else:
                canonical_id = int(row["id"])

            reassigned = 0
            deleted = 0
            for a in alias_clean:
                r = self.conn.execute(
                    "SELECT id FROM company WHERE name = ?", (a,)
                ).fetchone()
                if r is None:
                    continue
                alias_id = int(r["id"])
                # Re-point person_company links. Use INSERT-OR-IGNORE +
                # DELETE so a person already linked to BOTH companies
                # collapses to a single link instead of crashing the PK.
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO person_company
                        (person_id, company_id, role, since, is_current)
                    SELECT person_id, ?, role, since, is_current
                    FROM person_company
                    WHERE company_id = ?
                    """,
                    (canonical_id, alias_id),
                )
                cur = self.conn.execute(
                    "DELETE FROM person_company WHERE company_id = ?",
                    (alias_id,),
                )
                reassigned += cur.rowcount or 0
                self.conn.execute(
                    "DELETE FROM company WHERE id = ?", (alias_id,)
                )
                deleted += 1

        return (reassigned, deleted)

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
    def add_relationship(
        self, rel: Relationship, owner_id: int | None = None
    ) -> Relationship:
        """Upsert an edge.

        Provenance hierarchy: `manual` > `colleague_inferred` > `ai_inferred`.
        A lower-priority source never overwrites a higher-priority one;
        same-priority writes always win (re-running an inference pass
        refreshes its own rows). This is what lets us safely re-run
        `infer-colleagues` after `enrich` has populated more companies,
        without trampling rows the user already curated by hand.
        """
        existing = self.conn.execute(
            "SELECT source FROM relationship WHERE source_id = ? AND target_id = ?",
            (rel.source_id, rel.target_id),
        ).fetchone()
        if existing is not None:
            existing_prio = _SOURCE_PRIORITY.get(existing["source"], 0)
            incoming_prio = _SOURCE_PRIORITY.get(rel.source, 0)
            if incoming_prio < existing_prio:
                return self._fetch_relationship(rel.source_id, rel.target_id)

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO relationship
                    (source_id, target_id, owner_id, strength, context,
                     frequency, last_contact, introduced_by_id, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, target_id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    strength = excluded.strength,
                    context = excluded.context,
                    frequency = excluded.frequency,
                    last_contact = excluded.last_contact,
                    introduced_by_id = excluded.introduced_by_id,
                    source = excluded.source
                """,
                (
                    rel.source_id,
                    rel.target_id,
                    owner_id,
                    rel.strength,
                    rel.context,
                    rel.frequency.value,
                    rel.last_contact.isoformat() if rel.last_contact else None,
                    rel.introduced_by_id,
                    rel.source,
                ),
            )
        return self._fetch_relationship(rel.source_id, rel.target_id)

    def _fetch_relationship(self, source_id: int, target_id: int) -> Relationship:
        row = self.conn.execute(
            "SELECT * FROM relationship WHERE source_id = ? AND target_id = ?",
            (source_id, target_id),
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
            source=_row_get(row, "source", "manual"),
        )

    def delete_ai_inferred_relationships(self, owner_id: int) -> int:
        """Wipe all `ai_inferred` edges for a given owner. Used by `enrich`
        before re-extracting so stale AI guesses don't pile up."""
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM relationship WHERE owner_id = ? AND source = 'ai_inferred'",
                (owner_id,),
            )
            return cur.rowcount or 0

    def list_relationships(
        self, owner_id: int | None = None
    ) -> list[Relationship]:
        """Return edges visible from the given owner's perspective.

        Filtering rules when `owner_id` is provided:
          - keep an edge if both endpoints are persons curated by this
            owner (peer↔peer and me↔contact alike);
          - drop edges to *another* owner's `me` node, so Richard never
            sees Tommy's me-edges and vice versa.

        When `owner_id` is None, returns every relationship in the DB
        unfiltered.
        """
        if owner_id is None:
            rows = self.conn.execute("SELECT * FROM relationship").fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT r.* FROM relationship r
                JOIN person_owner po_s
                    ON po_s.person_id = r.source_id AND po_s.owner_id = ?
                JOIN person_owner po_t
                    ON po_t.person_id = r.target_id AND po_t.owner_id = ?
                """,
                (owner_id, owner_id),
            ).fetchall()
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
                source=_row_get(r, "source", "manual"),
            )
            for r in rows
        ]

    # ---------------------------------------------------- Keyword matching
    def keyword_candidates(
        self, terms: Iterable[str], owner_id: int | None = None
    ) -> dict[int, int]:
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
        owner_join = (
            "JOIN person_owner po ON po.person_id = p.id AND po.owner_id = ? "
            if owner_id is not None
            else ""
        )
        for term in terms:
            like = f"%{term}%"
            sql = f"""
                SELECT DISTINCT p.id AS pid FROM person p
                {owner_join}
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
            params: list[Any] = []
            if owner_id is not None:
                params.append(owner_id)
            params.extend([like] * 7)
            for row in self.conn.execute(sql, tuple(params)).fetchall():
                pid = int(row["pid"])
                scores[pid] = scores.get(pid, 0) + 1
        return scores
