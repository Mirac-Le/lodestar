"""Request and response models for the web API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from lodestar.models import Frequency, Person


class GraphNode(BaseModel):
    """A node ready for Cytoscape.js."""

    id: int
    label: str
    industry: str
    color: str
    glow: str
    size: int
    is_me: bool
    is_wishlist: bool = False
    strength_to_me: int | None = None
    bio: str | None = None
    tags: list[str] = []
    skills: list[str] = []
    companies: list[str] = []
    cities: list[str] = []
    needs: list[str] = []
    notes: str | None = None


class GraphEdge(BaseModel):
    """An edge ready for Cytoscape.js."""

    id: str
    source: int
    target: int
    strength: int
    context: str | None = None
    frequency: str


class GraphPayload(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    me_id: int
    weak_me_floor: int = Field(
        default=4, ge=1, le=5,
        description="Same as PathFinder: Me edges below this are weak for graph viz.",
    )
    mount_slug: str
    mount_display_name: str


# ---- mount registry (一人一库) ----------------------------------------
class MountDTO(BaseModel):
    """One mounted network exposed to the SPA top-bar."""

    slug: str
    display_name: str
    me_person_id: int | None = None
    accent_color: str | None = None
    contact_count: int = 0
    locked: bool = False  # True ⇔ this mount has a web password set


class MountsResponse(BaseModel):
    """Returned by the **root** app at ``GET /api/mounts``.

    The SPA reads this once on first load to render the owner tabs and to
    decide whether to challenge for a password. ``default_slug`` is just a
    UX hint (which tab gets focus when the user lands on ``/``).
    """

    mounts: list[MountDTO]
    default_slug: str | None = None


class UnlockRequest(BaseModel):
    password: str = ""


class UnlockResponse(BaseModel):
    token: str
    unlocked: bool = True


class PathStepDTO(BaseModel):
    person_id: int
    name: str
    strength: int | None = None
    relation_from_previous: str | None = None


class PathResultDTO(BaseModel):
    target_id: int
    target_name: str
    industry: str
    color: str
    path: list[PathStepDTO]
    relevance_score: float
    path_strength: float
    combined_score: float
    rationale: str
    path_kind: str = "direct"      # 'direct' | 'weak' | 'indirect' (topology only)
    is_wishlist: bool = False      # user-curated "I want to know them" marker
    edge_ids: list[str] = []       # graph edges making up this single path
    node_ids: list[int] = []       # graph nodes making up this single path
    # 仅当 path_kind=='indirect' 且 me 与 target 之间确实存在 1 跳直接边时
    # 填该边的 strength（1-5）；否则为 None。前端用它在 indirect 卡片底部
    # 渲染"你也直接认识他、点此改用直连"的 fallback 入口——算法因为
    # strength<weak_me_floor 默认推荐走引荐，但用户应当看到并能一键切换。
    direct_me_strength: int | None = None


class SearchRequest(BaseModel):
    goal: str
    top_k: int = 5
    no_llm: bool = False


class SearchResponse(BaseModel):
    goal: str
    intent_summary: str
    intent_keywords: list[str]
    results: list[PathResultDTO]               # combined list, sorted by combined_score
    indirect: list[PathResultDTO] = []         # multi-hop paths via intermediaries
    contacted: list[PathResultDTO] = []        # 1-hop, sorted by strength
    wishlist: list[PathResultDTO] = []         # any kind, but is_wishlist=True
    highlighted_node_ids: list[int]
    highlighted_edge_ids: list[str]


class TwoPersonPathRequest(BaseModel):
    source_id: int
    target_id: int
    max_paths: int = 5


class TwoPersonPathResponse(BaseModel):
    paths: list[PathResultDTO]


class IntroductionSuggestion(BaseModel):
    """A pair of contacts where one's needs match the other's tags/skills."""

    provider_id: int
    provider_name: str
    seeker_id: int
    seeker_name: str
    matched_keyword: str
    why: str


class IntroductionsResponse(BaseModel):
    suggestions: list[IntroductionSuggestion]


class StatsResponse(BaseModel):
    total_contacts: int
    total_relationships: int
    by_industry: dict[str, int]
    by_strength: dict[int, int]
    top_companies: list[tuple[str, int]]
    top_cities: list[tuple[str, int]]


class CreatePersonRequest(BaseModel):
    name: str
    bio: str | None = None
    notes: str | None = None
    tags: list[str] = []
    skills: list[str] = []
    companies: list[str] = []
    cities: list[str] = []
    needs: list[str] = []
    is_wishlist: bool = False
    strength_to_me: int = Field(default=3, ge=1, le=5)
    relation_context: str | None = None
    frequency: Frequency = Frequency.YEARLY
    embed: bool = True


class UpdatePersonRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    bio: str | None = None
    notes: str | None = None
    tags: list[str] | None = None
    skills: list[str] | None = None
    companies: list[str] | None = None
    cities: list[str] | None = None
    needs: list[str] | None = None
    is_wishlist: bool | None = None
    embed: bool = False


class EnrichPreviewRequest(BaseModel):
    """Free-text input for the "AI 解析背景" button on the add dialog.

    All fields are optional except (logically) `bio`/`notes`, but the
    server tolerates an empty body and just returns empty arrays.
    """

    name: str | None = None
    bio: str | None = None
    notes: str | None = None
    raw_tags: list[str] = []
    raw_cities: list[str] = []
    known_companies: list[str] = []
    known_cities: list[str] = []
    known_tags: list[str] = []


class EnrichDiff(BaseModel):
    """What the LLM proposed for one person (in addition to existing values)."""

    add_companies: list[str] = []
    add_cities: list[str] = []
    add_titles: list[str] = []
    add_tags: list[str] = []
    error: str | None = None


class EnrichJobState(BaseModel):
    task_id: str
    mount_slug: str
    status: str  # 'pending' | 'running' | 'done' | 'error'
    only_missing: bool
    total: int
    processed: int
    touched: int
    errors: int
    current_name: str | None = None
    error_message: str | None = None
    started_at: float
    finished_at: float | None = None
    elapsed_seconds: float


class EnrichJobStartRequest(BaseModel):
    only_missing: bool = True


class PersonDTO(BaseModel):
    id: int
    name: str
    bio: str | None = None
    notes: str | None = None
    is_me: bool
    is_wishlist: bool = False
    industry: str
    color: str
    glow: str
    strength_to_me: int | None = None
    # `relationship.id` of the Me↔此人 边（若存在）。
    # 前端档案面板可信度直点保存需要它走 PATCH /api/relationships/{rid}，
    # 没有这个字段就得再查一次 list_relationships。
    me_edge_id: int | None = None
    tags: list[str] = []
    skills: list[str] = []
    companies: list[str] = []
    cities: list[str] = []
    needs: list[str] = []
    related: list[dict] = []  # neighbors with strength

    @classmethod
    def from_person(
        cls, p: Person, industry: str, color: str, glow: str,
        strength_to_me: int | None, related: list[dict],
        me_edge_id: int | None = None,
    ) -> PersonDTO:
        assert p.id is not None
        return cls(
            id=p.id, name=p.name, bio=p.bio, notes=p.notes, is_me=p.is_me,
            is_wishlist=p.is_wishlist,
            industry=industry, color=color, glow=glow,
            strength_to_me=strength_to_me,
            me_edge_id=me_edge_id,
            tags=p.tags, skills=p.skills, companies=p.companies,
            cities=p.cities, needs=p.needs, related=related,
        )


# =====================================================================
# Relationships — browse, edit, NL-parse
# =====================================================================


class RelationshipDTO(BaseModel):
    """A single peer↔peer or me↔contact edge in the owner's subgraph.

    `a_*` and `b_*` mirror the underlying `source_id` / `target_id`
    columns — the orientation is preserved (writes to the DB go in the
    same direction the user picked) but downstream UI treats edges as
    undirected.
    """

    id: int
    a_id: int
    a_name: str
    b_id: int
    b_name: str
    strength: int
    context: str | None = None
    frequency: str
    source: str  # 'manual' | 'colleague_inferred' | 'ai_inferred'
    a_is_me: bool = False
    b_is_me: bool = False


class ProposedEdge(BaseModel):
    """One edge proposed by the LLM on top of free-form text input.

    The LLM is told NOT to invent a strength when the user didn't say
    so — `strength` may therefore be None. The frontend forces the user
    to pick a value before letting them submit.
    """

    a_id: int
    a_name: str
    b_id: int
    b_name: str
    strength: int | None = None
    context: str | None = None
    frequency: str | None = None
    rationale: str | None = None
    existing_edge: RelationshipDTO | None = None


class RelationshipParseRequest(BaseModel):
    text: str = Field(min_length=1)


class RelationshipParseResponse(BaseModel):
    proposals: list[ProposedEdge] = []
    unknown_mentions: list[str] = []
    context_for: dict[int, list[RelationshipDTO]] = {}
    error: str | None = None


class RelationshipApplyItem(BaseModel):
    a_id: int
    b_id: int
    strength: int = Field(ge=1, le=5)
    context: str | None = None
    frequency: Frequency = Frequency.YEARLY


class RelationshipApplyRequest(BaseModel):
    edges: list[RelationshipApplyItem]


class RelationshipApplyResponse(BaseModel):
    applied: int
    skipped: int
    items: list[RelationshipDTO] = []


class RelationshipUpdateRequest(BaseModel):
    strength: int | None = Field(default=None, ge=1, le=5)
    context: str | None = None
    frequency: Frequency | None = None


class RelationshipListResponse(BaseModel):
    items: list[RelationshipDTO]
    total: int
    offset: int
    limit: int


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
    data_base64: str


class FeedbackSubmitRequest(BaseModel):
    type: str = Field(pattern=r"^(bug|feature)$")
    form: FeedbackFormBug | FeedbackFormFeature
    submitter: str = Field(min_length=1)
    severity: str = Field(pattern=r"^(blocking|daily|nice)$")
    auto_capture: FeedbackAutoCapture
    screenshots: list[FeedbackScreenshot] = []

    @model_validator(mode="before")
    @classmethod
    def _dispatch_form_by_type(cls, data: Any) -> Any:
        # 两个 form 类有不重叠的必填字段（bug 的 history / feature 的
        # acceptance），Pydantic smart-union 在其中一侧字段不全时会歪
        # 到另一侧并报无关的错。这里按外层 type 直接挑具体子类，让
        # 下游 schema 校验报 message 和人类期望对齐。
        if isinstance(data, dict):
            t = data.get("type")
            form = data.get("form")
            if isinstance(form, dict):
                if t == "bug":
                    data = {**data, "form": FeedbackFormBug(**form)}
                elif t == "feature":
                    data = {**data, "form": FeedbackFormFeature(**form)}
        return data

    @model_validator(mode="after")
    def _bug_needs_screenshot(self) -> FeedbackSubmitRequest:
        if self.type == "bug" and not self.screenshots:
            raise ValueError("Bug 类反馈必须至少附 1 张截图")
        is_bug = isinstance(self.form, FeedbackFormBug)
        if self.type == "bug" and not is_bug:
            raise ValueError("type=bug 时 form 必须是 FeedbackFormBug")
        if self.type == "feature" and is_bug:
            raise ValueError("type=feature 时 form 必须是 FeedbackFormFeature")
        return self


class FeedbackSubmitResponse(BaseModel):
    ticket_id: str
    md_path: str
