"""SQL DDL statements.

设计原则（v4，post owner-removal）：
  * 一人一库：每个 SQLite 文件 = 一个 owner 的网络。**不再**有 ``owner``
    / ``person_owner`` 表，``relationship`` 也**不再**有 ``owner_id``
    列。多人共用同一进程靠 web 层的 ``--mount slug=path`` 把不同 db
    挂在不同 URL 前缀下，**进程内多个 db handle，文件级隔离**。
  * ``person.is_me`` 全库唯一（UNIQUE INDEX）—— "我" 在这个网络里只有一个。
  * snake_case，整数代理键，``created_at`` / ``updated_at`` 触发器维护，
    向量列住在独立 ``vec0`` 虚表里保持关系层简单可移植。
  * ``meta(key, value)`` 是一个**极简 KV 表**，存 web 密码 hash / 解锁
    token secret / 显示名 / 主题色这种 db-scoped 单值配置 —— 比为每条
    都开一列灵活，比独立小表轻量。
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
    # 全库只能有一行 is_me=1（一人一库）。partial unique index 让 is_me=0
    # 的几百行不参与去重，is_me=1 的行强制唯一。
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
    # ----------------------------------------------------------------
    # meta KV：db-scoped 单值配置
    #   web_password_salt / web_password_hash → web 密码门
    #   unlock_secret                         → per-db HMAC token 签名 key
    #   display_name                          → web 顶栏 tab 文本
    #   accent_color                          → web 顶栏 tab 颜色
    # 不要把多值数据塞进来；多值数据该建专表。
    # ----------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    # ----------------------------------------------------------------
    # feedback：业务同事 WebUI 提交的 bug / 需求 ticket。
    #   * ticket_id 形如 FB-YYYYMMDD-NNNN，对每个自然日独立自增
    #   * payload_json 存渲染 md 时的完整输入（form + auto_capture +
    #     db_snapshot + screenshots），md_path 指向落盘的 md 文件
    #   * md 是 payload_json 的只读衍生物；SOT 始终是这张表
    # ----------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS feedback (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id    TEXT NOT NULL UNIQUE,
        type         TEXT NOT NULL CHECK(type IN ('bug','feature')),
        status       TEXT NOT NULL DEFAULT 'open'
                     CHECK(status IN ('open','in_progress','done','wontfix')),
        title        TEXT NOT NULL,
        submitter    TEXT NOT NULL,
        severity     TEXT CHECK(severity IN ('blocking','daily','nice')),
        payload_json TEXT NOT NULL,
        md_path      TEXT,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        closed_at    TEXT,
        closed_by    TEXT,
        related_pr   TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_feedback_status
        ON feedback(status, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_feedback_created
        ON feedback(created_at DESC)
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
