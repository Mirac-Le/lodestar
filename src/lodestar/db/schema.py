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
        is_wishlist INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # NOTE: the legacy "exactly one me row" unique index has been removed
    # so that multiple owners (Richard / Tommy / ...) can each have their
    # own `is_me=1` row. Per-owner uniqueness is enforced via the `owner`
    # table's me_person_id column instead.
    """
    DROP INDEX IF EXISTS uq_person_is_me
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
    # ----------------------------------------------------------------
    # Multi-owner support: each "owner" (Richard / Tommy / ...) has
    # their own `me` person and their own slice of contacts. Persons
    # are merged across owners by name so that a shared friend shows
    # up as the same node in both subgraphs; person_owner records
    # which contact rows each owner curates.
    # ----------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS owner (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        slug            TEXT NOT NULL UNIQUE,
        display_name    TEXT NOT NULL,
        me_person_id    INTEGER NOT NULL UNIQUE REFERENCES person(id) ON DELETE CASCADE,
        accent_color    TEXT,
        position        INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS person_owner (
        person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
        owner_id  INTEGER NOT NULL REFERENCES owner(id)  ON DELETE CASCADE,
        PRIMARY KEY (person_id, owner_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_person_owner_owner ON person_owner(owner_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS relationship (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id          INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
        target_id          INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
        owner_id           INTEGER REFERENCES owner(id) ON DELETE SET NULL,
        strength           INTEGER NOT NULL DEFAULT 3
                           CHECK (strength BETWEEN 1 AND 5),
        context            TEXT,
        frequency          TEXT NOT NULL DEFAULT 'yearly',
        last_contact       TEXT,
        introduced_by_id   INTEGER REFERENCES person(id) ON DELETE SET NULL,
        -- Provenance: 'manual' = 用户/CSV/Excel 直接录入；
        --             'colleague_inferred' = 同公司自动连边；
        --             'ai_inferred' = LLM L2 抽取出来的关系。
        -- enrich 重跑时只覆盖 ai_inferred，绝不动其他两类。
        source             TEXT NOT NULL DEFAULT 'manual'
                           CHECK (source IN ('manual','colleague_inferred','ai_inferred')),
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
    """
    CREATE INDEX IF NOT EXISTS ix_rel_owner ON relationship(owner_id)
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
