# Feedback System Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** 让业务同事在 WebUI 一键提交反馈，表单提交即打包全部复现上下文（db 快照、API 回放、前端状态），生成 AI 可直接消费的 markdown ticket。

**Architecture:** 新增 `feedback` 表到现有 mount-scoped SQLite，新增 `POST /api/feedback` 端点，前端 topbar 加按钮 + modal 表单。SOT 是 `feedback` 表，md 是只读衍生。所有自动捕获在前端完成后一并 POST。

**Tech Stack:** FastAPI + SQLite + Alpine.js + Pydantic v2 + Jinja2（可选；用 f-string 也行）

**Reference:** 设计稿 [`docs/plans/2026-04-23-feedback-system-design.md`](./2026-04-23-feedback-system-design.md)

---

## 全局约定

- 所有 tests 用 `pytest`，命令：`uv run pytest <path> -xvs`
- 所有 Python 文件的代码风格沿用项目现有（`from __future__ import annotations`，snake_case）
- commit 用 conventional format：`feat:` / `test:` / `chore:` / `docs:`
- 每个 Task 结束 commit 一次，**commit 步骤仅在用户明确授权后执行**（项目有 git 安全规则）

---

## Task 1: Feedback DB schema

**Files:**
- Modify: `src/lodestar/db/schema.py`（末尾追加 DDL）
- Test: `tests/test_db_schema.py` 或新建 `tests/test_feedback_repo.py`

**Step 1: 写失败测试**

新建 `tests/test_feedback_repo.py`：

```python
"""Feedback table schema smoke tests."""

from __future__ import annotations

from pathlib import Path

from lodestar.db import connect, init_schema


def test_feedback_table_exists(tmp_path: Path) -> None:
    db = tmp_path / "fb.db"
    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(feedback)").fetchall()
    }
    expected = {
        "id", "ticket_id", "type", "status", "title", "submitter",
        "severity", "payload_json", "md_path", "created_at",
        "closed_at", "closed_by", "related_pr",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_feedback_ticket_id_unique(tmp_path: Path) -> None:
    db = tmp_path / "fb.db"
    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    conn.execute(
        "INSERT INTO feedback (ticket_id, type, title, submitter, payload_json)"
        " VALUES (?,?,?,?,?)",
        ("FB-20260423-0001", "bug", "t", "s", "{}"),
    )
    import sqlite3
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO feedback (ticket_id, type, title, submitter, payload_json)"
            " VALUES (?,?,?,?,?)",
            ("FB-20260423-0001", "feature", "t2", "s2", "{}"),
        )
```

**Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_feedback_repo.py -xvs
```

预期：`sqlite3.OperationalError: no such table: feedback`

**Step 3: 追加 DDL**

编辑 `src/lodestar/db/schema.py`，在 `DDL_STATEMENTS` 元组末尾（最后的 `)` 之前）追加：

```python
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
```

**Step 4: 再跑测试确认通过**

```bash
uv run pytest tests/test_feedback_repo.py -xvs
```

预期：`2 passed`

**Step 5: 跑全量测试确保没回归**

```bash
uv run pytest tests/ -q
```

预期：所有现有测试仍过（78 个左右）。

**Step 6: Commit**

```bash
git add src/lodestar/db/schema.py tests/test_feedback_repo.py
git commit -m "feat(db): add feedback table schema"
```

---

## Task 2: Privacy scrubber utility

**Files:**
- Create: `src/lodestar/privacy.py`
- Test: `tests/test_privacy.py`

**Step 1: 写失败测试**

```python
"""PII scrubbing rules for feedback snapshots."""

from __future__ import annotations

from lodestar.privacy import scrub


def test_scrub_phone_keeps_last_four() -> None:
    assert scrub("电话 13812348888") == "电话 138****8888"


def test_scrub_id_card_fully_redacted() -> None:
    assert scrub("身份证 110101199001011234") == "身份证 [REDACTED_ID]"
    # 带 X 校验位
    assert scrub("身份证 11010119900101123X") == "身份证 [REDACTED_ID]"


def test_scrub_bank_card_fully_redacted() -> None:
    assert scrub("卡号 6228480402564890018") == "卡号 [REDACTED_CARD]"


def test_scrub_email_masks_middle() -> None:
    assert scrub("联系 wanglei@gmail.com") == "联系 w***@gmail.com"


def test_scrub_preserves_harmless_text() -> None:
    text = "董淑佳 副校长 上海 UWC"
    assert scrub(text) == text


def test_scrub_multiple_in_one_string() -> None:
    text = "手机 13812348888 邮箱 abc@x.com"
    assert scrub(text) == "手机 138****8888 邮箱 a***@x.com"
```

**Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_privacy.py -xvs
```

预期：ImportError。

**Step 3: 实现 scrubber**

创建 `src/lodestar/privacy.py`：

```python
"""PII scrubbers for feedback snapshots.

脱敏规则：
  * 手机号（11 位，1 开头）→ 保留前 3 + 后 4，中间 ****
  * 身份证（18 位，末位数字或 X）→ [REDACTED_ID]
  * 银行卡（16-19 位连续数字）→ [REDACTED_CARD]
  * 邮箱 → 本地部分保留首字母，其余 *** 替换

顺序很重要：身份证 18 位 > 银行卡 16-19 位 > 手机 11 位，长的先匹配。
"""

from __future__ import annotations

import re

_ID_CARD_RE = re.compile(r"\b\d{17}[\dXx]\b")
_BANK_CARD_RE = re.compile(r"\b\d{16,19}\b")
_PHONE_RE = re.compile(r"\b(1\d{2})\d{4}(\d{4})\b")
_EMAIL_RE = re.compile(
    r"\b([A-Za-z0-9])([A-Za-z0-9._%+-]*)"
    r"@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b",
)


def scrub(text: str | None) -> str:
    """Best-effort PII scrub of a free-text string.

    先替换长 pattern（身份证、银行卡），再替换短 pattern（手机、邮箱），
    避免银行卡号被手机号 regex 截一半。
    """
    if not text:
        return text or ""
    out = _ID_CARD_RE.sub("[REDACTED_ID]", text)
    out = _BANK_CARD_RE.sub("[REDACTED_CARD]", out)
    out = _PHONE_RE.sub(r"\1****\2", out)
    out = _EMAIL_RE.sub(lambda m: f"{m.group(1)}***@{m.group(3)}", out)
    return out
```

**Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_privacy.py -xvs
```

预期：`6 passed`

**Step 5: Commit**

```bash
git add src/lodestar/privacy.py tests/test_privacy.py
git commit -m "feat: add PII scrubber utility for feedback snapshots"
```

---

## Task 3: Feedback domain model + Pydantic schemas

**Files:**
- Modify: `src/lodestar/models.py`（加 `Feedback` dataclass）
- Modify: `src/lodestar/web/schemas.py`（加 Pydantic DTO）
- Test: `tests/test_feedback_schemas.py`

**Step 1: 写失败测试**

```python
"""Feedback Pydantic schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lodestar.web.schemas import (
    FeedbackAutoCapture,
    FeedbackFormBug,
    FeedbackFormFeature,
    FeedbackSubmitRequest,
)


def test_bug_form_requires_core_fields() -> None:
    with pytest.raises(ValidationError):
        FeedbackFormBug(
            title="太短",  # <10
            involved_person_ids=[1],
            want_to_do="x", did="x", actual="x", expected="x",
            history="new",
        )


def test_feature_form_user_story_must_match_when_then() -> None:
    with pytest.raises(ValidationError):
        FeedbackFormFeature(
            title="按城市筛选联系人",
            involved_person_ids=[1],
            user_story="我想要按城市筛选",  # 没有 "当...的时候...希望"
            acceptance=["- 能看到城市下拉"],
        )
    # 合法例子
    FeedbackFormFeature(
        title="按城市筛选联系人",
        involved_person_ids=[1],
        user_story="当我在搜索框旁边的时候，我希望能选一个城市筛选",
        acceptance=["- 城市下拉里有上海、北京"],
    )


def test_acceptance_must_have_at_least_one_bullet() -> None:
    with pytest.raises(ValidationError):
        FeedbackFormFeature(
            title="按城市筛选联系人",
            involved_person_ids=[1],
            user_story="当X的时候我希望Y",
            acceptance=[],
        )


def test_submit_request_dispatches_by_type() -> None:
    req = FeedbackSubmitRequest(
        type="bug",
        form={
            "title": "搜索漏人的情况复现",
            "involved_person_ids": [1, 2],
            "want_to_do": "找人",
            "did": "搜关键词",
            "actual": "没出来",
            "expected": "应出来",
            "history": "recent",
        },
        submitter="王磊（@wanglei）",
        severity="daily",
        auto_capture=FeedbackAutoCapture(
            mount_slug="me",
            view_mode="intent",
            search_active=True,
            query="测试",
            detail_person_id=None,
            active_path_key=None,
            direct_overrides=[],
            indirect_targets=[1, 2],
            contacted_targets=[],
            api_trace=[],
            error_buffer=[],
            frontend_version="20260423-x",
            user_agent="Mozilla/5.0",
            viewport="1920x1080",
        ),
    )
    assert req.type == "bug"
    assert req.form.title == "搜索漏人的情况复现"
```

**Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_feedback_schemas.py -xvs
```

预期：ImportError。

**Step 3: 加 Pydantic schemas**

在 `src/lodestar/web/schemas.py` 末尾追加：

```python
# ---------------------------------------------------------------------
# Feedback（业务反馈表单 + 自动捕获环境）
# ---------------------------------------------------------------------
# 表单校验故意收紧：业务如果连 When-Then 句式、验收 bullet 都填不出，
# 说明需求没想清楚，这种反馈即使进库也会浪费一轮 AI 迭代。门槛挡住
# 强于接纳后补。

import re as _re

_USER_STORY_RE = _re.compile(r"当.*的时候.*希望|when.*then", _re.I)
_BULLET_RE = _re.compile(r"^\s*([-*]|\d+\.)\s+\S", _re.M)


class FeedbackFormBug(BaseModel):
    title: str = Field(min_length=10, max_length=40)
    involved_person_ids: list[int] = Field(min_length=1)
    want_to_do: str = Field(min_length=1)
    did: str = Field(min_length=1)
    actual: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    why_expected: str | None = None
    history: str = Field(pattern=r"^(new|recent|always)$")


class FeedbackFormFeature(BaseModel):
    title: str = Field(min_length=10, max_length=40)
    involved_person_ids: list[int] = Field(min_length=1)
    user_story: str
    acceptance: list[str] = Field(min_length=1)
    workaround: str | None = None

    @field_validator("user_story")
    @classmethod
    def _user_story_must_when_then(cls, v: str) -> str:
        if not _USER_STORY_RE.search(v):
            raise ValueError("user_story 必须用「当___的时候，我希望___」句式")
        return v

    @field_validator("acceptance")
    @classmethod
    def _each_acceptance_is_bullet(cls, v: list[str]) -> list[str]:
        joined = "\n".join(v)
        if not _BULLET_RE.search(joined):
            raise ValueError("acceptance 至少要有一条 `- ` 或 `1.` 起头的 bullet")
        return v


class FeedbackApiTraceEntry(BaseModel):
    ts: str
    method: str
    path: str
    req_body: Any | None = None
    status: int | None = None
    resp_body: Any | None = None


class FeedbackErrorEntry(BaseModel):
    ts: str
    msg: str | None = None
    stack: str | None = None
    reason: str | None = None


class FeedbackAutoCapture(BaseModel):
    mount_slug: str
    view_mode: str
    search_active: bool
    query: str | None = None
    detail_person_id: int | None = None
    active_path_key: str | None = None
    direct_overrides: list[int] = []
    indirect_targets: list[int] = []
    contacted_targets: list[int] = []
    api_trace: list[FeedbackApiTraceEntry] = []
    error_buffer: list[FeedbackErrorEntry] = []
    frontend_version: str
    user_agent: str
    viewport: str


class FeedbackScreenshot(BaseModel):
    filename: str
    content_type: str = Field(pattern=r"^image/(png|jpeg|gif|webp)$")
    data_base64: str   # pure base64, no data URL prefix


class FeedbackSubmitRequest(BaseModel):
    type: str = Field(pattern=r"^(bug|feature)$")
    form: FeedbackFormBug | FeedbackFormFeature
    submitter: str = Field(min_length=1)
    severity: str = Field(pattern=r"^(blocking|daily|nice)$")
    auto_capture: FeedbackAutoCapture
    screenshots: list[FeedbackScreenshot] = []

    @model_validator(mode="after")
    def _bug_needs_screenshot(self) -> "FeedbackSubmitRequest":
        if self.type == "bug" and not self.screenshots:
            raise ValueError("Bug 类反馈必须至少附 1 张截图")
        # type 与 form 子类必须一致
        is_bug = isinstance(self.form, FeedbackFormBug)
        if self.type == "bug" and not is_bug:
            raise ValueError("type=bug 时 form 必须是 FeedbackFormBug")
        if self.type == "feature" and is_bug:
            raise ValueError("type=feature 时 form 必须是 FeedbackFormFeature")
        return self


class FeedbackSubmitResponse(BaseModel):
    ticket_id: str
    md_path: str
```

**注意：** 文件顶部如果还没 import `field_validator, model_validator`，加上：

```python
from pydantic import BaseModel, Field, field_validator, model_validator
```

**Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_feedback_schemas.py -xvs
```

预期：4 passed

**Step 5: Commit**

```bash
git add src/lodestar/web/schemas.py tests/test_feedback_schemas.py
git commit -m "feat(schemas): add feedback form + auto-capture DTOs"
```

---

## Task 4: Ticket ID generator + Repository methods

**Files:**
- Modify: `src/lodestar/db/repository.py`
- Test: 追加到 `tests/test_feedback_repo.py`

**Step 1: 写失败测试**

追加到 `tests/test_feedback_repo.py`：

```python
from lodestar.db import Repository


def test_next_ticket_id_increments_per_day(tmp_path: Path) -> None:
    db = tmp_path / "fb.db"
    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    t1 = repo.next_feedback_ticket_id(today="20260423")
    t2 = repo.next_feedback_ticket_id(today="20260423")
    assert t1 == "FB-20260423-0001"
    assert t2 == "FB-20260423-0002"


def test_next_ticket_id_resets_on_new_day(tmp_path: Path) -> None:
    db = tmp_path / "fb.db"
    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    # 插入一条昨天的
    conn.execute(
        "INSERT INTO feedback (ticket_id, type, title, submitter, payload_json)"
        " VALUES ('FB-20260422-0001', 'bug', 't', 's', '{}')",
    )
    t = repo.next_feedback_ticket_id(today="20260423")
    assert t == "FB-20260423-0001"


def test_add_feedback_returns_row_with_id(tmp_path: Path) -> None:
    db = tmp_path / "fb.db"
    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    row_id = repo.add_feedback(
        ticket_id="FB-20260423-0001",
        type_="bug",
        title="x" * 15,
        submitter="王磊",
        severity="daily",
        payload_json='{"foo":"bar"}',
        md_path="docs/feedback/me/FB-20260423-0001.md",
    )
    assert isinstance(row_id, int) and row_id > 0
    row = conn.execute(
        "SELECT * FROM feedback WHERE id = ?", (row_id,)
    ).fetchone()
    assert row["status"] == "open"
    assert row["type"] == "bug"
```

**Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_feedback_repo.py -xvs
```

预期：AttributeError: Repository has no attribute `next_feedback_ticket_id`

**Step 3: 加 Repository 方法**

在 `src/lodestar/db/repository.py` 的 `Repository` 类末尾追加：

```python
    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    def next_feedback_ticket_id(self, today: str | None = None) -> str:
        """按自然日顺序生成下一个 ticket_id（格式 FB-YYYYMMDD-NNNN）。

        today 默认 UTC 当天；测试里可以注入固定值。计数按每天独立，跨天
        自然回 0001——不需要全局自增，业务按日对账更直观。
        """
        from datetime import datetime, timezone
        if today is None:
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
        prefix = f"FB-{today}-"
        row = self.conn.execute(
            "SELECT ticket_id FROM feedback"
            " WHERE ticket_id LIKE ?"
            " ORDER BY ticket_id DESC LIMIT 1",
            (prefix + "%",),
        ).fetchone()
        seq = 1 if row is None else int(row["ticket_id"].rsplit("-", 1)[-1]) + 1
        return f"{prefix}{seq:04d}"

    def add_feedback(
        self,
        *,
        ticket_id: str,
        type_: str,
        title: str,
        submitter: str,
        severity: str | None,
        payload_json: str,
        md_path: str | None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO feedback"
            " (ticket_id, type, title, submitter, severity,"
            "  payload_json, md_path)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, type_, title, submitter, severity,
             payload_json, md_path),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def get_feedback(self, ticket_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM feedback WHERE ticket_id = ?", (ticket_id,),
        ).fetchone()
        return dict(row) if row else None
```

**Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_feedback_repo.py -xvs
```

预期：5 passed

**Step 5: Commit**

```bash
git add src/lodestar/db/repository.py tests/test_feedback_repo.py
git commit -m "feat(repo): add feedback ticket id generator + add_feedback"
```

---

## Task 5: DB snapshot builder (reverse lookup involved persons)

**Files:**
- Create: `src/lodestar/web/feedback_snapshot.py`
- Test: `tests/test_feedback_snapshot.py`

**Step 1: 写失败测试**

```python
"""反查涉及联系人的 db snapshot（Person + 1 跳邻居 + Me-edge），带脱敏。"""

from __future__ import annotations

from pathlib import Path

import pytest

from lodestar.db import Repository, connect, init_schema
from lodestar.models import Frequency, Person, Relationship
from lodestar.web.feedback_snapshot import build_snapshot


@pytest.fixture
def repo_with_graph(tmp_path: Path) -> Repository:
    conn = connect(tmp_path / "snap.db")
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    me = repo.ensure_me("我")
    alice = repo.add_person(Person(name="Alice", bio="电话 13812348888"))
    bob = repo.add_person(Person(name="Bob", bio="普通简介"))
    assert me.id and alice.id and bob.id
    repo.add_relationship(Relationship(
        source_id=me.id, target_id=alice.id, strength=4,
        frequency=Frequency.MONTHLY,
    ))
    repo.add_relationship(Relationship(
        source_id=alice.id, target_id=bob.id, strength=3,
        frequency=Frequency.QUARTERLY,
    ))
    return repo


def test_snapshot_includes_each_involved_person(repo_with_graph: Repository) -> None:
    alice_id = next(
        p.id for p in repo_with_graph.list_people() if p.name == "Alice"
    )
    snap = build_snapshot(repo_with_graph, [alice_id])
    assert len(snap) == 1
    assert snap[0]["person"]["name"] == "Alice"


def test_snapshot_scrubs_pii_in_bio(repo_with_graph: Repository) -> None:
    alice_id = next(
        p.id for p in repo_with_graph.list_people() if p.name == "Alice"
    )
    snap = build_snapshot(repo_with_graph, [alice_id])
    assert "13812348888" not in snap[0]["person"]["bio"]
    assert "138****8888" in snap[0]["person"]["bio"]


def test_snapshot_includes_me_edge_and_neighbors(
    repo_with_graph: Repository,
) -> None:
    alice_id = next(
        p.id for p in repo_with_graph.list_people() if p.name == "Alice"
    )
    snap = build_snapshot(repo_with_graph, [alice_id])
    entry = snap[0]
    # Me edge
    assert entry["me_edge"] is not None
    assert entry["me_edge"]["strength"] == 4
    # 邻居
    neighbor_names = {n["name"] for n in entry["neighbors"]}
    assert "Bob" in neighbor_names
```

**Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_feedback_snapshot.py -xvs
```

预期：ImportError。

**Step 3: 实现**

创建 `src/lodestar/web/feedback_snapshot.py`：

```python
"""Build a scrubbed db snapshot for feedback tickets.

Given a list of ``involved_person_ids``, return each person's Person row,
their Me-edge (if any), and their 1-hop neighbors. All free-text PII is
scrubbed via ``lodestar.privacy.scrub``.

出发点：让 AI 拿到 ticket md 时直接看到"这个联系人的 bio/tags/关系强度
都是什么"，省去反查 db 的一轮。
"""

from __future__ import annotations

from typing import Any

from lodestar.db import Repository
from lodestar.privacy import scrub


def build_snapshot(
    repo: Repository,
    involved_person_ids: list[int],
) -> list[dict[str, Any]]:
    people = {p.id: p for p in repo.list_people() if p.id is not None}
    rels = repo.list_relationships()
    me = repo.get_me()
    me_id = me.id if me else None

    out: list[dict[str, Any]] = []
    for pid in involved_person_ids:
        p = people.get(pid)
        if p is None:
            out.append({"person": None, "missing_id": pid})
            continue

        # Me-edge
        me_edge = None
        if me_id is not None:
            for r in rels:
                if {r.source_id, r.target_id} == {me_id, pid}:
                    me_edge = {
                        "strength": r.strength,
                        "frequency": r.frequency.value if r.frequency else None,
                        "context": scrub(r.context),
                    }
                    break

        # 1 跳邻居（排除 me 自己，避免和 me_edge 重复）
        neighbors: list[dict[str, Any]] = []
        for r in rels:
            if pid not in (r.source_id, r.target_id):
                continue
            other = r.target_id if r.source_id == pid else r.source_id
            if other == me_id or other not in people:
                continue
            neighbors.append({
                "id": other,
                "name": people[other].name,
                "strength": r.strength,
                "frequency": r.frequency.value if r.frequency else None,
            })

        out.append({
            "person": {
                "id": p.id,
                "name": p.name,
                "bio": scrub(p.bio),
                "notes": scrub(p.notes),
                "tags": p.tags,
                "skills": p.skills,
                "companies": p.companies,
                "cities": p.cities,
                "is_wishlist": p.is_wishlist,
            },
            "me_edge": me_edge,
            "neighbors": neighbors,
        })
    return out
```

**Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_feedback_snapshot.py -xvs
```

预期：3 passed

**Step 5: Commit**

```bash
git add src/lodestar/web/feedback_snapshot.py tests/test_feedback_snapshot.py
git commit -m "feat(web): add feedback db snapshot builder with PII scrubbing"
```

---

## Task 6: Markdown renderer

**Files:**
- Create: `src/lodestar/web/feedback_markdown.py`
- Test: `tests/test_feedback_markdown.py`

**Step 1: 写失败测试**

```python
"""Render a feedback ticket to markdown."""

from __future__ import annotations

from lodestar.web.feedback_markdown import render_ticket_md


def _sample_payload_bug() -> dict:
    return {
        "ticket_id": "FB-20260423-0001",
        "type": "bug",
        "status": "open",
        "severity": "daily",
        "submitter": "王磊（飞书 @wanglei）",
        "created_at": "2026-04-23 14:32:11",
        "form": {
            "title": "查「帮孩子上海上学」时董淑佳没出现",
            "involved_person_ids": [47],
            "want_to_do": "找人帮客户孩子上学",
            "did": "1. 搜索框输入\n2. 按回车",
            "actual": "只出了 3 人，董淑佳不在",
            "expected": "董淑佳应该在列表里",
            "why_expected": "她儿子在 UWC，bio 写了上海教育",
            "history": "recent",
        },
        "auto_capture": {
            "mount_slug": "me",
            "view_mode": "intent",
            "search_active": True,
            "query": "帮孩子上海上学",
            "detail_person_id": None,
            "active_path_key": "t-0-23",
            "direct_overrides": [],
            "indirect_targets": [23, 99, 104],
            "contacted_targets": [],
            "api_trace": [],
            "error_buffer": [],
            "frontend_version": "20260423-x",
            "user_agent": "Mozilla/5.0",
            "viewport": "1920x1080",
        },
        "db_snapshot": [{
            "person": {"id": 47, "name": "董淑佳", "bio": "教育·副校长",
                       "tags": ["上海教育资源"], "skills": [],
                       "companies": [], "cities": ["上海"],
                       "notes": None, "is_wishlist": False},
            "me_edge": {"strength": 3, "frequency": "yearly",
                        "context": "老同事介绍"},
            "neighbors": [{"id": 23, "name": "王昌尧", "strength": 5,
                           "frequency": "monthly"}],
        }],
        "screenshots": [],
    }


def test_render_bug_has_frontmatter() -> None:
    md = render_ticket_md(_sample_payload_bug())
    assert md.startswith("---\n")
    assert "ticket_id: FB-20260423-0001" in md
    assert "type: bug" in md


def test_render_bug_has_prompt_section_for_ai() -> None:
    md = render_ticket_md(_sample_payload_bug())
    assert "@assistant" in md
    assert "bug/设计之争" in md or "设计" in md


def test_render_bug_has_all_form_sections() -> None:
    md = render_ticket_md(_sample_payload_bug())
    for section in ("标题", "涉及的人", "你想干什么",
                    "你做了什么", "看到了什么", "期望什么",
                    "历史对比", "影响程度"):
        assert section in md, f"missing section: {section}"


def test_render_embeds_db_snapshot() -> None:
    md = render_ticket_md(_sample_payload_bug())
    assert "董淑佳" in md
    assert "strength=3" in md or "strength\": 3" in md
```

**Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_feedback_markdown.py -xvs
```

预期：ImportError。

**Step 3: 实现 renderer**

创建 `src/lodestar/web/feedback_markdown.py`（长但无复杂逻辑，主要是字符串拼接）：

```python
"""Render a feedback submission into a markdown ticket file.

生成的 md 结构：
  1. YAML frontmatter（机读）
  2. @assistant 处理指引段（给 AI 看）
  3. 业务填的内容（人类可读）
  4. 自动捕获的技术数据（AI 直接消费）

不使用 Jinja2，纯 f-string 拼接——依赖小、可读性好、diff 友好。
"""

from __future__ import annotations

import json
from typing import Any

_PROMPT_HEADER = """\
> **@assistant 处理指引（给 AI 看）**
>
> 这是一份业务同事通过 WebUI 反馈按钮提交的 {kind}，下面所有信息
> 已由系统自动打包，无需再问人。请按这个顺序处理：
> 1. 读「涉及的人 / db 快照」理解数据状态
> 2. 读「实际 vs 期望 / 验收标准」确认修复目标
> 3. 读「API 回放」对照代码路径，定位根因函数
> 4. 若「业务的期望」与「代码现行设计」冲突（例：算法是故意这样），
>    **先停下来和开发（repo owner）确认**是改代码还是改 UI 提示，
>    不要擅自改算法
> 5. 写测试 → 改代码 → 跑 `uv run pytest` → 报告
"""

_HISTORY_LABEL = {
    "new": "🆕 新需求 / 第一次用这个功能",
    "recent": "✅ 以前能用，最近才坏的",
    "always": "❌ 一直就是这样",
}

_SEVERITY_LABEL = {
    "blocking": "🔥 阻塞签单",
    "daily": "⚠️ 每天都遇到，靠绕能过",
    "nice": "💭 没这个也行，有了更好",
}


def render_ticket_md(payload: dict[str, Any]) -> str:
    """Render the full markdown body for a feedback ticket."""
    ticket_id = payload["ticket_id"]
    type_ = payload["type"]
    form = payload["form"]
    auto = payload["auto_capture"]
    snapshot = payload.get("db_snapshot", [])
    screenshots = payload.get("screenshots", [])

    frontmatter = (
        "---\n"
        f"ticket_id: {ticket_id}\n"
        f"type: {type_}\n"
        f"status: {payload.get('status', 'open')}\n"
        f"severity: {payload.get('severity', '')}\n"
        f"submitter: {payload['submitter']}\n"
        f"created_at: {payload.get('created_at', '')}\n"
        f"mount_slug: {auto['mount_slug']}\n"
        f"frontend_version: {auto['frontend_version']}\n"
        "---\n\n"
    )

    prompt = _PROMPT_HEADER.format(
        kind=("bug" if type_ == "bug" else "需求"),
    ) + "\n---\n\n"

    if type_ == "bug":
        body = _render_bug_body(form)
    else:
        body = _render_feature_body(form)

    # 涉及的人（通过 snapshot 里已有的 person 信息渲染，省得再去查库）
    involved = _render_involved(snapshot)

    severity = _SEVERITY_LABEL.get(payload.get("severity", ""), "")
    impact = f"## 影响程度\n{severity}\n\n" if severity else ""

    # 截图部分（md 里只写文件引用，真实 base64 由 endpoint 落盘到 attachments/）
    screenshots_md = ""
    if screenshots:
        screenshots_md = "## 截图\n" + "\n".join(
            f"![{s['filename']}](./attachments/{s['filename']})"
            for s in screenshots
        ) + "\n\n"

    tech = _render_tech_data(auto, snapshot)

    return "".join([
        frontmatter,
        prompt,
        involved,
        body,
        impact,
        screenshots_md,
        "---\n\n## 🔧 自动打包的技术数据\n\n",
        tech,
    ])


def _render_bug_body(f: dict[str, Any]) -> str:
    history = _HISTORY_LABEL.get(f.get("history", ""), f.get("history", ""))
    why = f"## 为什么这样期望\n{f['why_expected']}\n\n" if f.get("why_expected") else ""
    return (
        f"## 🐛 标题\n{f['title']}\n\n"
        f"## 你想干什么\n{f['want_to_do']}\n\n"
        f"## 你做了什么\n{f['did']}\n\n"
        f"## 看到了什么（实际）\n{f['actual']}\n\n"
        f"## 期望什么\n{f['expected']}\n\n"
        f"{why}"
        f"## 历史对比\n{history}\n\n"
    )


def _render_feature_body(f: dict[str, Any]) -> str:
    acceptance = "\n".join(f["acceptance"])
    workaround = (
        f"## 现在你是怎么凑合的\n{f['workaround']}\n\n"
        if f.get("workaround") else ""
    )
    return (
        f"## 💡 标题\n{f['title']}\n\n"
        f"## 用户故事\n{f['user_story']}\n\n"
        f"## 验收标准\n{acceptance}\n\n"
        f"{workaround}"
    )


def _render_involved(snapshot: list[dict[str, Any]]) -> str:
    if not snapshot:
        return ""
    lines = ["## 涉及的人"]
    for entry in snapshot:
        p = entry.get("person")
        if p is None:
            lines.append(f"- ⚠️ 联系人 id={entry.get('missing_id')} 已不存在")
        else:
            lines.append(f"- {p['name']} (id={p['id']})")
    return "\n".join(lines) + "\n\n"


def _render_tech_data(
    auto: dict[str, Any],
    snapshot: list[dict[str, Any]],
) -> str:
    state_json = json.dumps(
        {k: auto[k] for k in (
            "mount_slug", "view_mode", "search_active", "query",
            "detail_person_id", "active_path_key", "direct_overrides",
            "indirect_targets", "contacted_targets",
        )},
        ensure_ascii=False, indent=2,
    )
    api_json = json.dumps(auto.get("api_trace", []),
                          ensure_ascii=False, indent=2)
    err_json = json.dumps(auto.get("error_buffer", []),
                          ensure_ascii=False, indent=2)

    parts = [
        "### 前端状态（提交时）\n```json\n",
        state_json, "\n```\n\n",
        "### db 快照（涉及联系人 + 1 跳邻居，已脱敏）\n\n",
    ]
    for entry in snapshot:
        p = entry.get("person")
        if p is None:
            continue
        me_edge = entry.get("me_edge")
        parts.append(f"**{p['name']} (id={p['id']})**\n")
        if p.get("bio"):
            parts.append(f"- bio: `{p['bio']}`\n")
        if p.get("tags"):
            parts.append(f"- tags: `{json.dumps(p['tags'], ensure_ascii=False)}`\n")
        if p.get("notes"):
            parts.append(f"- notes: `{p['notes']}`\n")
        if me_edge:
            parts.append(
                f"- Me → {p['name']}: strength={me_edge['strength']}, "
                f"frequency={me_edge.get('frequency')}, "
                f"context={me_edge.get('context')!r}\n"
            )
        for n in entry.get("neighbors", []):
            parts.append(
                f"- 1 跳邻居: {n['name']} (strength={n['strength']}, "
                f"frequency={n.get('frequency')})\n"
            )
        parts.append("\n")

    parts.extend([
        "### API 回放（最近 10 次请求）\n```json\n",
        api_json, "\n```\n\n",
        "### 前端错误 buffer\n```json\n",
        err_json, "\n```\n\n",
        f"### 浏览器 / 视口\n- UA: `{auto['user_agent']}`\n"
        f"- Viewport: `{auto['viewport']}`\n",
    ])
    return "".join(parts)
```

**Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_feedback_markdown.py -xvs
```

预期：4 passed

**Step 5: Commit**

```bash
git add src/lodestar/web/feedback_markdown.py tests/test_feedback_markdown.py
git commit -m "feat(web): add feedback ticket markdown renderer"
```

---

## Task 7: `POST /api/feedback` endpoint

**Files:**
- Modify: `src/lodestar/web/app.py`（在 mount 子 app 内加路由）
- Test: 新增到 `tests/test_web.py` 或独立 `tests/test_feedback_endpoint.py`

**Step 1: 写失败测试**

创建 `tests/test_feedback_endpoint.py`：

```python
"""End-to-end test for POST /api/feedback."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lodestar.config import Settings
from lodestar.db import Repository, connect, init_schema
from lodestar.models import Frequency, Person, Relationship
from lodestar.web import create_app

MOUNT_SLUG = "me"
PFX = f"/r/{MOUNT_SLUG}"
# 1x1 PNG (transparent)
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8A"
    "AAIIAQBoQzN3AAAAAElFTkSuQmCC"
)


def _bootstrap_mount(db: Path, monkeypatch) -> None:
    ts = Settings(db_path=db, embedding_dim=8,
                  llm_api_key="x", embedding_api_key="x")
    monkeypatch.setattr("lodestar.config.get_settings", lambda: ts)
    monkeypatch.setattr("lodestar.web.app.get_settings", lambda: ts)
    monkeypatch.setenv(
        "LODESTAR_MOUNTS_JSON",
        json.dumps([{"slug": MOUNT_SLUG, "db_path": str(db)}]),
    )


@pytest.fixture
def client_with_data(
    tmp_path: Path, monkeypatch
) -> tuple[TestClient, int, Path]:
    """Returns (client, alice_id, feedback_dir)."""
    db = tmp_path / "fb.db"
    _bootstrap_mount(db, monkeypatch)
    conn = connect(db)
    init_schema(conn, embedding_dim=8)
    repo = Repository(conn)
    me = repo.ensure_me("我")
    alice = repo.add_person(Person(name="Alice", bio="研究员"))
    assert me.id and alice.id
    repo.add_relationship(Relationship(
        source_id=me.id, target_id=alice.id, strength=3,
        frequency=Frequency.MONTHLY,
    ))
    conn.close()
    feedback_dir = tmp_path / "feedback-out"
    feedback_dir.mkdir()
    monkeypatch.setenv("LODESTAR_FEEDBACK_DIR", str(feedback_dir))
    return TestClient(create_app()), alice.id, feedback_dir


def _valid_bug_body(alice_id: int) -> dict:
    return {
        "type": "bug",
        "form": {
            "title": "搜索漏人的情况这里描述",
            "involved_person_ids": [alice_id],
            "want_to_do": "找人",
            "did": "1. 输入\n2. 回车",
            "actual": "没出来",
            "expected": "应该出来",
            "history": "recent",
        },
        "submitter": "王磊",
        "severity": "daily",
        "auto_capture": {
            "mount_slug": "me",
            "view_mode": "intent",
            "search_active": True,
            "query": "研究员",
            "detail_person_id": None,
            "active_path_key": None,
            "direct_overrides": [],
            "indirect_targets": [alice_id],
            "contacted_targets": [],
            "api_trace": [],
            "error_buffer": [],
            "frontend_version": "20260423-x",
            "user_agent": "Mozilla/5.0",
            "viewport": "1920x1080",
        },
        "screenshots": [{
            "filename": "scr1.png",
            "content_type": "image/png",
            "data_base64": _TINY_PNG_B64,
        }],
    }


def test_submit_bug_returns_ticket_id_and_writes_md(
    client_with_data: tuple[TestClient, int, Path],
) -> None:
    client, alice_id, feedback_dir = client_with_data
    r = client.post(f"{PFX}/api/feedback", json=_valid_bug_body(alice_id))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ticket_id"].startswith("FB-")
    assert data["ticket_id"].endswith("-0001")
    # md file written
    md_path = feedback_dir / "me" / f"{data['ticket_id']}.md"
    assert md_path.exists()
    md = md_path.read_text(encoding="utf-8")
    assert "Alice" in md
    assert "@assistant" in md


def test_submit_bug_without_screenshot_rejected(
    client_with_data: tuple[TestClient, int, Path],
) -> None:
    client, alice_id, _ = client_with_data
    body = _valid_bug_body(alice_id)
    body["screenshots"] = []
    r = client.post(f"{PFX}/api/feedback", json=body)
    assert r.status_code == 422


def test_submit_feature_without_when_then_rejected(
    client_with_data: tuple[TestClient, int, Path],
) -> None:
    client, alice_id, _ = client_with_data
    body = {
        "type": "feature",
        "form": {
            "title": "按城市筛选联系人列表",
            "involved_person_ids": [alice_id],
            "user_story": "我想按城市筛选",
            "acceptance": ["- 城市下拉里有上海"],
        },
        "submitter": "王磊",
        "severity": "nice",
        "auto_capture": _valid_bug_body(alice_id)["auto_capture"],
        "screenshots": [],
    }
    r = client.post(f"{PFX}/api/feedback", json=body)
    assert r.status_code == 422


def test_ticket_id_increments_same_day(
    client_with_data: tuple[TestClient, int, Path],
) -> None:
    client, alice_id, _ = client_with_data
    r1 = client.post(f"{PFX}/api/feedback", json=_valid_bug_body(alice_id))
    r2 = client.post(f"{PFX}/api/feedback", json=_valid_bug_body(alice_id))
    assert r1.json()["ticket_id"] != r2.json()["ticket_id"]
    assert r2.json()["ticket_id"].endswith("-0002")
```

**Step 2: 跑测试确认失败**

```bash
uv run pytest tests/test_feedback_endpoint.py -xvs
```

预期：404 / route not found

**Step 3: 加 endpoint + 文件落盘**

在 `src/lodestar/web/app.py` 的 mount 子 app 注册块里（和其他 `@sub.post(...)` 同一区域）加：

```python
# ---- Feedback ----
# 业务 WebUI 反馈入口。表单 + 自动捕获的 payload 一次性 POST 进来，
# 服务端生成 ticket_id，反查 db snapshot，落 feedback 行，渲染 md。
# LODESTAR_FEEDBACK_DIR 环境变量控制输出根目录，默认 `docs/feedback/`
# 相对于 CWD；测试里会 monkey-patch 成 tmp_path。
import base64
import os
from datetime import datetime, timezone
from pathlib import Path

from lodestar.web.feedback_markdown import render_ticket_md
from lodestar.web.feedback_snapshot import build_snapshot
from lodestar.web.schemas import FeedbackSubmitRequest, FeedbackSubmitResponse


def _feedback_root() -> Path:
    return Path(os.environ.get("LODESTAR_FEEDBACK_DIR", "docs/feedback"))


@sub.post("/api/feedback", response_model=FeedbackSubmitResponse)
def submit_feedback(
    body: FeedbackSubmitRequest,
    repo: Repository = Depends(verified),
) -> FeedbackSubmitResponse:
    # 1. 生成 ticket_id
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    ticket_id = repo.next_feedback_ticket_id(today=today)

    # 2. 反查 snapshot
    snapshot = build_snapshot(repo, body.form.involved_person_ids)

    # 3. 写截图到 attachments/
    out_dir = _feedback_root() / slug
    attachments_dir = out_dir / "attachments"
    out_dir.mkdir(parents=True, exist_ok=True)
    attachments_dir.mkdir(exist_ok=True)
    saved_screenshots: list[dict] = []
    for i, s in enumerate(body.screenshots, start=1):
        ext = s.content_type.split("/")[-1]
        fname = f"{ticket_id}-{i:02d}.{ext}"
        (attachments_dir / fname).write_bytes(base64.b64decode(s.data_base64))
        saved_screenshots.append({"filename": fname})

    # 4. 渲染 md
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    md_payload = {
        "ticket_id": ticket_id,
        "type": body.type,
        "status": "open",
        "severity": body.severity,
        "submitter": body.submitter,
        "created_at": created_at,
        "form": body.form.model_dump(),
        "auto_capture": body.auto_capture.model_dump(),
        "db_snapshot": snapshot,
        "screenshots": saved_screenshots,
    }
    md_text = render_ticket_md(md_payload)
    md_path = out_dir / f"{ticket_id}.md"
    md_path.write_text(md_text, encoding="utf-8")

    # 5. 落 db
    repo.add_feedback(
        ticket_id=ticket_id,
        type_=body.type,
        title=body.form.title,
        submitter=body.submitter,
        severity=body.severity,
        payload_json=json.dumps(md_payload, ensure_ascii=False, default=str),
        md_path=str(md_path),
    )

    return FeedbackSubmitResponse(
        ticket_id=ticket_id,
        md_path=str(md_path),
    )
```

**注意事项：**
- 把 `import json` 确保在文件顶部已有；没有则添加
- `Depends(verified)` 是该 mount 已有的鉴权依赖，复用即可
- 如果 `app.py` 里 endpoint 注册是 `root.post(...)` 直接挂在主 app，需按实际情况调整 `sub` / `root` 名字

**Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_feedback_endpoint.py -xvs
```

预期：4 passed

**Step 5: 跑全量测试无回归**

```bash
uv run pytest tests/ -q
```

**Step 6: Commit**

```bash
git add src/lodestar/web/app.py tests/test_feedback_endpoint.py
git commit -m "feat(web): add POST /api/feedback endpoint"
```

---

## Task 8: Frontend API trace ring buffer

**Files:**
- Modify: `src/lodestar/web/static/modules/api.js`

**Step 1: 定位现有 `api()` wrapper**

```bash
grep -n "async function api\|export.*api\|export const api" src/lodestar/web/static/modules/api.js
```

**Step 2: 在 wrapper 里注入 buffer**

在 `api()` 函数的 `fetch()` 调用前后挂采集逻辑。核心改动：

```javascript
// ---- API trace ring buffer ----
// 反馈表单打开时，从这里读取最近 10 次请求上下文，一并 POST
// 给 /api/feedback。保留整段 req_body + resp_body 让 AI 能精确
// 复现服务端返回了什么。size=10 是拍脑袋值，业务一次反馈通常只
// 关心最后几次操作，10 足够且不让 md 过长。
const API_TRACE_MAX = 10;
const __apiTrace = [];
window.__getApiTrace = () => __apiTrace.slice();

// 在现有 api() 函数里包一层：
export async function api(path, opts = {}) {
  const ts = new Date().toISOString();
  const method = (opts.method || "GET").toUpperCase();
  const reqBody = opts.body ?? null;
  let status = 0;
  let respBody = null;
  try {
    // ... 原有 fetch 逻辑，保留不动 ...
    // 末端 resp.json() 前后把 status / body 记下来
    const resp = await fetch(/* ... */);
    status = resp.status;
    const text = await resp.text();
    try { respBody = text ? JSON.parse(text) : null; }
    catch { respBody = text?.slice(0, 500) || null; }
    // 重新走原有的 status != 2xx 抛错逻辑
    if (!resp.ok) throw new Error(/* ... */);
    return respBody;
  } finally {
    __apiTrace.push({ ts, path, method, req_body: reqBody, status, resp_body: respBody });
    if (__apiTrace.length > API_TRACE_MAX) __apiTrace.shift();
  }
}
```

**注意：** 这是个**最小侵入**改造——保持 api() 现有签名和行为，只在外层 try/finally 挂一个观察器。`finally` 保证即便请求抛错也会被记录。

具体改动要**对照 api.js 现有代码**调整；不要盲抄。

**Step 3: 手动冒烟**

无单测（涉及浏览器 runtime）。启动服务手动点几下：

```bash
uv run lodestar serve --mount me=./demo.db --host 127.0.0.1 --port 8765
```

浏览器打开 http://127.0.0.1:8765/r/me/，F12 console：

```javascript
window.__getApiTrace()
```

预期：看到 `/api/graph`、`/api/stats` 等最近几次请求的完整 req/resp。

**Step 4: Commit**

```bash
git add src/lodestar/web/static/modules/api.js
git commit -m "feat(frontend): add API trace ring buffer for feedback capture"
```

---

## Task 9: Frontend error buffer

**Files:**
- Modify: `src/lodestar/web/static/app.js` 或 `modules/state.js`（全局一次初始化即可）

**Step 1: 加错误捕获**

在 `app.js` 入口（Alpine.start() 之前）或 `state.js` 的 `init()` 里加：

```javascript
// ---- Frontend error buffer ----
// 反馈表单提交时一并打包，帮 AI 定位"业务没看见但控制台红了"的错误。
// 20 条足够覆盖一次会话；大于此值环形丢弃最老的。
const ERR_MAX = 20;
window.__errBuffer = [];
window.addEventListener("error", (e) => {
  window.__errBuffer.push({
    ts: new Date().toISOString(),
    msg: e.message,
    stack: e.error?.stack || null,
  });
  if (window.__errBuffer.length > ERR_MAX) window.__errBuffer.shift();
});
window.addEventListener("unhandledrejection", (e) => {
  window.__errBuffer.push({
    ts: new Date().toISOString(),
    reason: String(e.reason),
    stack: e.reason?.stack || null,
  });
  if (window.__errBuffer.length > ERR_MAX) window.__errBuffer.shift();
});
```

**Step 2: 手动冒烟**

浏览器 console：

```javascript
throw new Error("test");
window.__errBuffer
```

预期：能看到刚才的 error 被记录。

**Step 3: Commit**

```bash
git add src/lodestar/web/static/app.js  # 或 state.js
git commit -m "feat(frontend): add window error buffer for feedback capture"
```

---

## Task 10: Alpine state for feedback form

**Files:**
- Modify: `src/lodestar/web/static/modules/state.js`

**Step 1: 在 `appState()` 返回的对象里加 state + methods**

位置：参考现有 `showAdd` / `showBatchEnrich` 同层。

```javascript
// ---- Feedback form state ----
// 业务 WebUI「📝 反馈」modal 的所有状态。设计门槛故意高：
// 字段校验不过就提交按钮置灰、不点亮；宁可让业务放弃也不收残废反馈。
showFeedback: false,
feedbackSubmitting: false,
feedback: {
  type: "bug",          // "bug" | "feature"
  title: "",
  involvedPersons: [],  // [{id, name}]
  // bug fields
  wantToDo: "",
  did: "",
  actual: "",
  expected: "",
  whyExpected: "",
  history: "",          // "new" | "recent" | "always"
  // feature fields
  userStory: "",
  acceptance: "",       // raw textarea; split by \n on submit
  workaround: "",
  // common
  severity: "",         // "blocking" | "daily" | "nice"
  submitterName: "",
  submitterContact: "",
  screenshots: [],      // [{filename, content_type, data_base64}]
},
feedbackPersonQuery: "",

openFeedback() {
  this.feedback = {
    type: "bug", title: "", involvedPersons: [],
    wantToDo: "", did: "", actual: "", expected: "", whyExpected: "",
    history: "",
    userStory: "", acceptance: "", workaround: "",
    severity: "", submitterName: "", submitterContact: "",
    screenshots: [],
  };
  this.feedbackPersonQuery = "";
  this.showFeedback = true;
},
closeFeedback() { this.showFeedback = false; },

feedbackPersonMatches() {
  const q = (this.feedbackPersonQuery || "").toLowerCase().trim();
  const nodes = (this.graph?.nodes || [])
    .filter(n => !n.is_me)
    .filter(n => !this.feedback.involvedPersons.some(p => p.id === n.id));
  if (!q) return nodes.slice(0, 20);
  return nodes.filter(n => (n.label || "").toLowerCase().includes(q)).slice(0, 20);
},

addFeedbackPerson(node) {
  this.feedback.involvedPersons.push({ id: node.id, name: node.label });
  this.feedbackPersonQuery = "";
},

removeFeedbackPerson(pid) {
  this.feedback.involvedPersons =
    this.feedback.involvedPersons.filter(p => p.id !== pid);
},

async onFeedbackScreenshotPick(fileList) {
  const files = Array.from(fileList || []).slice(0, 3);
  for (const f of files) {
    if (f.size > 5 * 1024 * 1024) {
      this.notify(`${f.name} 超过 5MB，已跳过`, "error");
      continue;
    }
    const b64 = await new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(",")[1] || "");
      r.onerror = reject;
      r.readAsDataURL(f);
    });
    this.feedback.screenshots.push({
      filename: f.name,
      content_type: f.type || "image/png",
      data_base64: b64,
    });
  }
},

removeFeedbackScreenshot(i) {
  this.feedback.screenshots.splice(i, 1);
},

feedbackValid() {
  const f = this.feedback;
  if (f.title.length < 10 || f.title.length > 40) return false;
  if (f.involvedPersons.length === 0) return false;
  if (!f.severity) return false;
  if (!f.submitterName.trim() || !f.submitterContact.trim()) return false;
  if (f.type === "bug") {
    if (f.screenshots.length === 0) return false;
    if (!f.wantToDo || !f.did || !f.actual || !f.expected) return false;
    if (!f.history) return false;
    return true;
  } else {
    if (!/当.*的时候.*希望|when.*then/i.test(f.userStory)) return false;
    const bullets = f.acceptance.split("\n").filter(l => /^\s*([-*]|\d+\.)\s+\S/.test(l));
    if (bullets.length === 0) return false;
    return true;
  }
},

async submitFeedback() {
  if (!this.feedbackValid() || this.feedbackSubmitting) return;
  this.feedbackSubmitting = true;
  const f = this.feedback;
  const form = f.type === "bug" ? {
    title: f.title,
    involved_person_ids: f.involvedPersons.map(p => p.id),
    want_to_do: f.wantToDo, did: f.did,
    actual: f.actual, expected: f.expected,
    why_expected: f.whyExpected || null,
    history: f.history,
  } : {
    title: f.title,
    involved_person_ids: f.involvedPersons.map(p => p.id),
    user_story: f.userStory,
    acceptance: f.acceptance.split("\n").filter(l => l.trim()),
    workaround: f.workaround || null,
  };
  try {
    const body = {
      type: f.type,
      form,
      submitter: `${f.submitterName}（${f.submitterContact}）`,
      severity: f.severity,
      auto_capture: {
        mount_slug: this.mountSlug,
        view_mode: this.viewMode,
        search_active: this.searchActive,
        query: this.query || null,
        detail_person_id: this.detail?.id || null,
        active_path_key: this.activePathKey,
        direct_overrides: Object.keys(this.directOverrides || {}).map(Number),
        indirect_targets: (this.indirect || []).map(r => r.target_id),
        contacted_targets: (this.contacted || []).map(r => r.target_id),
        api_trace: (window.__getApiTrace?.() || []),
        error_buffer: (window.__errBuffer || []).slice(),
        frontend_version: document.querySelector(
          'script[type="module"][src*="app.js"]'
        )?.src.split("?v=")[1] || "unknown",
        user_agent: navigator.userAgent,
        viewport: `${window.innerWidth}x${window.innerHeight}`,
      },
      screenshots: f.screenshots,
    };
    const resp = await api("/api/feedback", { method: "POST", body });
    this.notify(`已提交 ${resp.ticket_id}，请发给技术同事`, "info", 6000);
    this.closeFeedback();
  } catch (e) {
    this.notify(`提交失败：${e.message}`, "error", 5000);
  } finally {
    this.feedbackSubmitting = false;
  }
},
```

**Step 2: Commit**

```bash
git add src/lodestar/web/static/modules/state.js
git commit -m "feat(frontend): add Alpine state for feedback form"
```

---

## Task 11: Feedback modal HTML + CSS

**Files:**
- Modify: `src/lodestar/web/static/index.html`
- Modify: `src/lodestar/web/static/style.css`

**Step 1: 加 topbar 按钮**

在现有 topbar 里（和「批量 AI 解析」「关系抽屉」同一行），按现有按钮的风格加：

```html
<button class="btn btn-ghost" @click="openFeedback()" title="反馈 Bug 或提需求">
  📝 反馈
</button>
```

**Step 2: 加 modal（放到 index.html 末尾，和其他 modal 并列）**

```html
<!-- ========== Feedback modal ========== -->
<template x-if="showFeedback">
  <div class="modal-bg" @click.self="closeFeedback()">
    <div class="modal modal-feedback" @click.stop>
      <h2>📝 反馈</h2>

      <div class="form-row">
        <label>
          <input type="radio" value="bug" x-model="feedback.type">
          🐛 报告 Bug
        </label>
        <label>
          <input type="radio" value="feature" x-model="feedback.type">
          💡 提需求
        </label>
      </div>

      <label>标题（一句话概括，10–40 字）
        <input type="text" x-model="feedback.title"
               maxlength="40" minlength="10"
               placeholder="示例：查『私募』时李四没出现">
        <span class="hint" x-text="`${feedback.title.length}/40`"></span>
      </label>

      <label>涉及的人（从联系人里选，必选至少 1 人）
        <input type="text" x-model="feedbackPersonQuery"
               placeholder="输入姓名关键词搜索">
        <div class="pair-suggest" x-show="feedbackPersonQuery">
          <template x-for="n in feedbackPersonMatches()" :key="n.id">
            <div class="pair-item" @mousedown.prevent="addFeedbackPerson(n)">
              <span x-text="n.label"></span>
            </div>
          </template>
        </div>
        <div class="tag-chips">
          <template x-for="p in feedback.involvedPersons" :key="p.id">
            <span class="tag-chip">
              <span x-text="p.name"></span>
              <button type="button" @click="removeFeedbackPerson(p.id)">✕</button>
            </span>
          </template>
        </div>
      </label>

      <!-- Bug 专属 -->
      <template x-if="feedback.type === 'bug'">
        <div>
          <label>你想干什么<input type="text" x-model="feedback.wantToDo"></label>
          <label>你做了什么（一行一步）
            <textarea x-model="feedback.did" rows="3"
                      placeholder="1. 点了搜索框&#10;2. 输入关键词&#10;3. 按回车"></textarea>
          </label>
          <label>看到了什么（实际）<textarea x-model="feedback.actual" rows="2"></textarea></label>
          <label>期望什么<textarea x-model="feedback.expected" rows="2"></textarea></label>
          <label>为什么这样期望（可选）<textarea x-model="feedback.whyExpected" rows="2"></textarea></label>
          <label>历史对比
            <select x-model="feedback.history">
              <option value="">请选</option>
              <option value="new">🆕 新需求 / 第一次用</option>
              <option value="recent">✅ 以前能用，最近才坏</option>
              <option value="always">❌ 一直就是这样</option>
            </select>
          </label>
        </div>
      </template>

      <!-- 需求专属 -->
      <template x-if="feedback.type === 'feature'">
        <div>
          <label>用户故事（必须用「当___的时候，我希望___」句式）
            <textarea x-model="feedback.userStory" rows="3"
                      placeholder="示例：当我在查『私募』但没有直接认识的人时，我希望系统能按城市再过一遍"></textarea>
          </label>
          <label>验收标准（每行一条 bullet，至少 1 条）
            <textarea x-model="feedback.acceptance" rows="3"
                      placeholder="- 城市下拉里有上海、北京&#10;- 选中后结果实时过滤"></textarea>
          </label>
          <label>现在怎么凑合（可选）
            <textarea x-model="feedback.workaround" rows="2"></textarea>
          </label>
        </div>
      </template>

      <label>影响程度
        <select x-model="feedback.severity">
          <option value="">请选</option>
          <option value="blocking">🔥 阻塞签单</option>
          <option value="daily">⚠️ 每天都遇到</option>
          <option value="nice">💭 有了更好</option>
        </select>
      </label>

      <div class="form-row">
        <label>你是谁<input type="text" x-model="feedback.submitterName" placeholder="姓名"></label>
        <label>飞书 / 手机尾号<input type="text" x-model="feedback.submitterContact"></label>
      </div>

      <label>截图
        <template x-if="feedback.type === 'bug'">
          <span class="hint">Bug 必须至少 1 张</span>
        </template>
        <input type="file" accept="image/*" multiple
               @change="onFeedbackScreenshotPick($event.target.files); $event.target.value=''">
        <div class="tag-chips">
          <template x-for="(s, i) in feedback.screenshots" :key="i">
            <span class="tag-chip">
              <span x-text="s.filename"></span>
              <button type="button" @click="removeFeedbackScreenshot(i)">✕</button>
            </span>
          </template>
        </div>
      </label>

      <p class="hint">
        ℹ️ 提交时会一并打包最近 10 次操作产生的技术数据，供开发定位问题。
      </p>

      <div class="modal-actions">
        <button class="btn" @click="closeFeedback()">取消</button>
        <button class="btn btn-primary"
                :disabled="!feedbackValid() || feedbackSubmitting"
                @click="submitFeedback()">
          <span x-show="!feedbackSubmitting">提交</span>
          <span x-show="feedbackSubmitting">提交中...</span>
        </button>
      </div>
    </div>
  </div>
</template>
```

**Step 3: 加样式（style.css 末尾）**

```css
/* ---- Feedback modal ---- */
.modal-feedback {
  max-width: 560px;
  width: 92vw;
  max-height: 88vh;
  overflow-y: auto;
}

.modal-feedback .form-row {
  display: flex;
  gap: 12px;
}

.modal-feedback label {
  display: block;
  margin-bottom: 12px;
}

.modal-feedback .hint {
  color: var(--text-tertiary, #8a8f96);
  font-size: 12px;
  display: inline-block;
  margin-left: 8px;
}

.modal-feedback .tag-chip button {
  margin-left: 4px;
  background: transparent;
  border: none;
  color: inherit;
  cursor: pointer;
}
```

**Step 4: bump 版本号**

```html
<link rel="stylesheet" href="/static/style.css?v=20260424-feedback">
<script type="module" src="/static/app.js?v=20260424-feedback"></script>
```

**Step 5: 手动冒烟**

```bash
uv run lodestar serve --mount me=./demo.db --port 8765
```

浏览器打开 → 点「📝 反馈」→ 试提一个 bug，看 toast 返回 ticket_id，检查 `docs/feedback/me/FB-*.md`。

**Step 6: Commit**

```bash
git add src/lodestar/web/static/index.html src/lodestar/web/static/style.css
git commit -m "feat(frontend): add feedback modal UI"
```

---

## Task 12: End-to-end smoke test

**Files:**
- 已经在 Task 7 覆盖，这里只是**补一条 feature 流程 + 断言 md 内容正确**

**Step 1: 追加测试**

在 `tests/test_feedback_endpoint.py` 追加：

```python
def test_submit_feature_end_to_end(
    client_with_data: tuple[TestClient, int, Path],
) -> None:
    client, alice_id, feedback_dir = client_with_data
    body = {
        "type": "feature",
        "form": {
            "title": "按城市筛选联系人列表这里",
            "involved_person_ids": [alice_id],
            "user_story": "当我搜索没结果的时候，我希望能按城市再过滤一遍",
            "acceptance": [
                "- 城市下拉里有上海、北京",
                "- 选中后列表实时过滤",
            ],
            "workaround": "现在只能手动翻",
        },
        "submitter": "王磊",
        "severity": "nice",
        "auto_capture": {
            "mount_slug": "me", "view_mode": "ambient",
            "search_active": False, "query": None,
            "detail_person_id": None, "active_path_key": None,
            "direct_overrides": [], "indirect_targets": [],
            "contacted_targets": [], "api_trace": [],
            "error_buffer": [],
            "frontend_version": "20260424-feedback",
            "user_agent": "Mozilla/5.0", "viewport": "1920x1080",
        },
        "screenshots": [],
    }
    r = client.post(f"{PFX}/api/feedback", json=body)
    assert r.status_code == 200, r.text
    ticket = r.json()["ticket_id"]
    md = (feedback_dir / "me" / f"{ticket}.md").read_text(encoding="utf-8")
    assert "用户故事" in md
    assert "当我搜索没结果的时候" in md
    assert "验收标准" in md
    assert "城市下拉" in md
```

**Step 2: 跑**

```bash
uv run pytest tests/test_feedback_endpoint.py -xvs
```

预期：5 passed（含之前 4 + 新 1）

**Step 3: 全量测试回归**

```bash
uv run pytest tests/ -q
```

**Step 4: Commit**

```bash
git add tests/test_feedback_endpoint.py
git commit -m "test(feedback): add feature ticket end-to-end smoke"
```

---

## Task 13: Docs + .gitignore + CHANGELOG

**Files:**
- Modify: `.gitignore`
- Modify: `CHANGELOG.md`
- Modify: `docs/instructions.md`（加反馈系统的使用说明给业务）

**Step 1: .gitignore 加 feedback dir**

```bash
echo "" >> .gitignore
echo "# Feedback tickets (contain PII; keep local)" >> .gitignore
echo "docs/feedback/" >> .gitignore
```

**Step 2: CHANGELOG 追加**

在 `CHANGELOG.md` 最上方（紧跟 `## [2026-04-23]` 的 Added 段下）或新建 `[2026-04-24]` 段加：

```markdown
## [2026-04-24]

### Added
- **WebUI 反馈系统**：topbar 新增「📝 反馈」按钮，业务同事可直接提交 Bug（必含截图 + 实际/期望）或需求（必用 When-Then 句式 + 验收 bullet）。提交时前端自动打包当前 `mount_slug` / `view_mode` / `query` / 高亮路径 / `directOverrides` 状态，外加最近 10 次 API 请求的完整 req+resp、最近 20 条前端错误、涉及联系人的 db 快照（Person + Me-edge + 1 跳邻居，跑过 PII 脱敏）。后端落 `feedback` 表 + 渲染 md 到 `docs/feedback/<slug>/FB-YYYYMMDD-NNNN.md`，md 开头带一段给 AI 的处理指引（尤其"bug/设计之争时先停下问开发"的 guardrail）。
- **PII 脱敏工具**：新增 `lodestar.privacy.scrub`，手机号保留后 4、身份证 / 银行卡全 redact、邮箱 mask 中段。主要给 feedback snapshot 用，未来可推广到其他场景。

### Changed
- `docs/feedback/` 加入 `.gitignore`（默认不入库，反馈 md 内联联系人姓名 / 关系，当作本地 artifact 管理）。

> 设计稿 & 实施计划：[`docs/plans/2026-04-23-feedback-system-design.md`](docs/plans/2026-04-23-feedback-system-design.md) / [`docs/plans/2026-04-23-feedback-system-plan.md`](docs/plans/2026-04-23-feedback-system-plan.md)
```

**Step 3: docs/instructions.md 补业务使用说明**

在文档里找个合适位置加一节「如何提交反馈」：

```markdown
## 如何提交反馈（给业务同事）

发现 bug 或想加功能，请点右上角「📝 反馈」按钮提交。提交时请注意：

- **一份反馈只提一件事**。顺便想到的其他问题请再提一条。
- **bug 必须附截图**。最好在截图上用红框圈出"不对"的地方。
- **涉及哪个人请在下拉框里选**，不要手打姓名——同名人会混。
- **「期望什么」要写具体**：不是"更准"，而是"查 XX 时应看到 YY"。
- **需求必须写清「用户故事」**：当___的时候，我希望___。填不出句式就说明这个需求还没想清，建议先想清再填。

提交后会弹出反馈号（形如 `FB-20260424-0001`），请把这个号发到开发的飞书/微信，方便跟进。
```

**Step 4: Commit**

```bash
git add .gitignore CHANGELOG.md docs/instructions.md
git commit -m "docs: document feedback system usage and gitignore feedback dir"
```

---

## 最终 sanity check

```bash
uv run pytest tests/ -q
```

预期：全过，新测试贡献 ~15 个（2 schema + 3 snapshot + 4 markdown + 5 endpoint + 1 feature e2e + lint smoke）。

```bash
uv run ruff check src/ tests/
```

预期：无新增错误（预存的可选依赖 import 警告不算）。

---

## 执行方式选择

> **Plan 完整保存到 `docs/plans/2026-04-23-feedback-system-plan.md`。执行有两个选项：**
>
> **1. 本会话顺序执行** —— 我继续在当前会话按 Task 1 → 13 顺序实施，每个 Task 做完向你汇报，等你 review 后再下一步。不用 subagent（遵守你的用户规则）。
>
> **2. 另开会话批量执行** —— 你开一个新的 Cursor 会话，在新会话里用 `superpowers:executing-plans` 技能批量跑这份 plan，到检查点停下让你 review。
>
> 推荐 **选项 1**，因为：
> - 本会话已经有完整的设计 + 计划上下文，换会话要重新加载
> - 13 个 Task 粒度细、commit 频繁，不需要大段静默批量
> - Frontend 部分（Task 8-11）需要手动浏览器冒烟，本会话你看着我跑更直观
>
> 请选择 1 / 2。
