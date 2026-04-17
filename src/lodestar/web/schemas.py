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
    direct: list[PathResultDTO] = []           # 1-hop strong contacts
    weak: list[PathResultDTO] = []             # 1-hop weak acquaintances
    wishlist: list[PathResultDTO] = []         # any kind, but is_wishlist=True
    # Deprecated alias kept for one release so older clients still parse.
    # Mirrors `indirect`. Will be removed in a follow-up.
    targets: list[PathResultDTO] = []
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
    bio: str | None = None
    notes: str | None = None
    tags: list[str] | None = None
    skills: list[str] | None = None
    companies: list[str] | None = None
    cities: list[str] | None = None
    needs: list[str] | None = None
    is_wishlist: bool | None = None
    embed: bool = False


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
    tags: list[str] = []
    skills: list[str] = []
    companies: list[str] = []
    cities: list[str] = []
    needs: list[str] = []
    related: list[dict] = []  # neighbors with strength

    @classmethod
    def from_person(cls, p: Person, industry: str, color: str, glow: str,
                    strength_to_me: int | None, related: list[dict]) -> PersonDTO:
        assert p.id is not None
        return cls(
            id=p.id, name=p.name, bio=p.bio, notes=p.notes, is_me=p.is_me,
            is_wishlist=p.is_wishlist,
            industry=industry, color=color, glow=glow,
            strength_to_me=strength_to_me,
            tags=p.tags, skills=p.skills, companies=p.companies,
            cities=p.cities, needs=p.needs, related=related,
        )
