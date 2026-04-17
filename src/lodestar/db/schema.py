"""SQL DDL statements.

Naming conventions:
  * snake_case for tables and columns.
  * Surrogate `id` integer primary keys everywhere.
  * `created_at` / `updated_at` managed by triggers.
  * Vector column lives in a separate `vec0` virtual table so the
    relational schema stays simple and portable.
"""

from __future__ import annotations

DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS person (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        bio         TEXT,
        notes       TEXT,
        is_me       INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # Exactly one 'me' row allowed.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_person_is_me
        ON person(is_me) WHERE is_me = 1
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_person_name ON person(name)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS person_updated_at
    AFTER UPDATE ON person
    FOR EACH ROW
    BEGIN
        UPDATE person SET updated_at = datetime('now') WHERE id = OLD.id;
    END
    """,
    """
    CREATE TABLE IF NOT EXISTS tag (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS company (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        name     TEXT NOT NULL UNIQUE,
        industry TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS city (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS need (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS person_tag (
        person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
        tag_id    INTEGER NOT NULL REFERENCES tag(id)    ON DELETE CASCADE,
        PRIMARY KEY (person_id, tag_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS person_skill (
        person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
        skill_id  INTEGER NOT NULL REFERENCES skill(id)  ON DELETE CASCADE,
        level     INTEGER CHECK (level BETWEEN 1 AND 5),
        PRIMARY KEY (person_id, skill_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS person_company (
        person_id  INTEGER NOT NULL REFERENCES person(id)  ON DELETE CASCADE,
        company_id INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
        role       TEXT,
        since      TEXT,
        is_current INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (person_id, company_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS person_city (
        person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
        city_id   INTEGER NOT NULL REFERENCES city(id)   ON DELETE CASCADE,
        PRIMARY KEY (person_id, city_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS person_need (
        person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
        need_id   INTEGER NOT NULL REFERENCES need(id)   ON DELETE CASCADE,
        PRIMARY KEY (person_id, need_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS relationship (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id          INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
        target_id          INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
        strength           INTEGER NOT NULL DEFAULT 3
                           CHECK (strength BETWEEN 1 AND 5),
        context            TEXT,
        frequency          TEXT NOT NULL DEFAULT 'yearly',
        last_contact       TEXT,
        introduced_by_id   INTEGER REFERENCES person(id) ON DELETE SET NULL,
        created_at         TEXT NOT NULL DEFAULT (datetime('now')),
        CHECK (source_id <> target_id),
        UNIQUE (source_id, target_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_rel_source ON relationship(source_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_rel_target ON relationship(target_id)
    """,
)


def vec_ddl(dim: int) -> str:
    """DDL for the sqlite-vec virtual table holding person bio embeddings.

    Kept in its own helper because `dim` depends on the chosen embedding model.
    """
    return f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS vec_person_bio USING vec0(
        person_id INTEGER PRIMARY KEY,
        embedding FLOAT[{dim}]
    )
    """
