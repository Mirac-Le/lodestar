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


# Edge provenance hierarchy used by `Repository.add_relationship`.
# Higher value = more authoritative; never gets overwritten by a write
# of lower value. Equal-value writes always win (idempotent re-runs).
_SOURCE_PRIORITY: dict[str, int] = {
    "manual": 2,
    "colleague_inferred": 1,
    "ai_inferred": 0,
}


class Repository:
    """Data access layer.

    一人一库（v4）：``Repository`` 只面对**一个** db handle，没有 owner
    维度的方法。多人靠 web 层 ``--mount`` 把多个 db 挂到同一进程的不同
    URL 前缀，**不在数据层混合**。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # --------------------------------------------------------- Meta KV
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self.conn:
            if value is None:
                self.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                self.conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    # ----------- Typed accessors over meta KV (used by web/CLI) ------
    @property
    def display_name(self) -> str | None:
        return self.get_meta("display_name")

    @display_name.setter
    def display_name(self, value: str | None) -> None:
        self.set_meta("display_name", value)

    @property
    def accent_color(self) -> str | None:
        return self.get_meta("accent_color")

    @accent_color.setter
    def accent_color(self, value: str | None) -> None:
        self.set_meta("accent_color", value)

    @property
    def unlock_secret(self) -> str:
        """HMAC signing key for this db's web unlock tokens.

        Guaranteed to exist because `init_schema()` populates it on first
        open. Cp'ing the db file copies the secret too — that's
        intentional: file-level access is the only ACL we trust.
        """
        v = self.get_meta("unlock_secret")
        assert v, "init_schema must populate unlock_secret"
        return v

    @property
    def web_password_hash(self) -> str | None:
        """Salted PBKDF2 hash, hex-encoded; None means 'no password set'."""
        return self.get_meta("web_password_hash")

    @property
    def web_password_salt(self) -> str | None:
        return self.get_meta("web_password_salt")

    def set_web_password(self, plain: str | None) -> None:
        """Set or clear the web UI password.

        Hash algorithm: PBKDF2-HMAC-SHA256, 200_000 iterations, 16-byte
        random salt per password (re-generated on every set). Plenty
        strong for friction-grade auth in a same-host same-team setting.
        """
        if plain is None or plain == "":
            self.set_meta("web_password_hash", None)
            self.set_meta("web_password_salt", None)
            return
        import hashlib
        import secrets as _secrets

        salt = _secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, 200_000)
        self.set_meta("web_password_salt", salt.hex())
        self.set_meta("web_password_hash", digest.hex())

    def verify_web_password(self, plain: str) -> bool:
        salt_hex = self.web_password_salt
        hash_hex = self.web_password_hash
        if not salt_hex or not hash_hex:
            return False
        import hashlib
        import secrets as _secrets

        digest = hashlib.pbkdf2_hmac(
            "sha256", plain.encode("utf-8"), bytes.fromhex(salt_hex), 200_000
        )
        return _secrets.compare_digest(digest.hex(), hash_hex)

    # ------------------------------------------------------------------ Me
    def get_me(self) -> Person | None:
        """Return the (single) `me` person, or None if not initialised yet."""
        row = self.conn.execute(
            "SELECT * FROM person WHERE is_me = 1 LIMIT 1"
        ).fetchone()
        return self._hydrate_person(row) if row else None

    def ensure_me(self, name: str, bio: str | None = None) -> Person:
        """Create the singleton me-person if absent, else return it.

        Also fills in `meta.display_name` from `name` on first creation
        so the web tab has something to show.
        """
        existing = self.get_me()
        if existing is not None:
            return existing
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO person (name, bio, is_me) VALUES (?, ?, 1)",
                (name, bio),
            )
            me_pid = cur.lastrowid
            assert me_pid is not None
        if not self.display_name:
            self.display_name = name
        result = self.get_person(me_pid)
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
        """Lookup by exact name, skipping the me-row so an owner whose
        display name happens to match a real contact's name does not
        accidentally get aliased onto that contact's node."""
        row = self.conn.execute(
            "SELECT * FROM person WHERE name = ? AND is_me = 0 LIMIT 1",
            (name,),
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

    # ---------------------------------------------------------- Companies
    def list_companies(self) -> list[tuple[int, str, int]]:
        """Return `(company_id, name, headcount)` for every company that
        has at least one person attached.

        Used by `lodestar normalize-companies` to decide which alias rows
        to merge.
        """
        rows = self.conn.execute(
            """
            SELECT c.id AS id, c.name AS name, COUNT(*) AS n
            FROM company c
            JOIN person_company pc ON pc.company_id = c.id
            GROUP BY c.id, c.name
            ORDER BY n DESC, c.name
            """,
        ).fetchall()
        return [(int(r["id"]), str(r["name"]), int(r["n"])) for r in rows]

    def merge_companies(
        self,
        canonical: str,
        aliases: Sequence[str],
    ) -> tuple[int, int]:
        """Merge `aliases` into `canonical` at the `company` table level.

        Concretely: re-point every `person_company.company_id` that points
        to an alias row to the canonical row instead, then delete the now
        empty alias rows. Idempotent: aliases that no longer exist are
        skipped. If `canonical` itself doesn't exist yet but at least one
        alias does, we **rename** the alias to canonical instead of
        creating a fresh row, to preserve any existing FK references.

        Returns `(reassigned_links, deleted_alias_rows)`.
        """
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
        self,
        query_vec: Sequence[float],
        limit: int = 20,
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
                    (source_id, target_id, strength, context,
                     frequency, last_contact, introduced_by_id, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, target_id) DO UPDATE SET
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

    def delete_ai_inferred_relationships(self) -> int:
        """Wipe all `ai_inferred` edges. Used by `enrich` before re-extracting
        so stale AI guesses don't pile up."""
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM relationship WHERE source = 'ai_inferred'",
            )
            return cur.rowcount or 0

    def list_relationships(self) -> list[Relationship]:
        """Return every relationship in the DB."""
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
                source=_row_get(r, "source", "manual"),
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
            params: list[Any] = [like] * 7
            for row in self.conn.execute(sql, tuple(params)).fetchall():
                pid = int(row["pid"])
                scores[pid] = scores.get(pid, 0) + 1
        return scores
