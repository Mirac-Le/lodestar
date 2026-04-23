"""Request and response models for the web API."""

from __future__ import annotations

from pydantic import BaseModel, Field

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
