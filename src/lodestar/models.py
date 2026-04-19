"""Pydantic data models shared across the app."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Frequency(StrEnum):
    """How often you stay in touch. Affects relationship decay scoring."""

    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"
    RARE = "rare"


class Owner(BaseModel):
    """A network owner — one person whose contacts make up a subgraph.

    Multiple owners share the same database; each owner has their own
    `me` Person row. Contacts are merged across owners by name (so a
    shared friend appears as a single node in both subgraphs), but the
    `me`→contact edges and the `person_owner` membership table keep
    each owner's view independent.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    slug: str = Field(description="URL-safe short id, e.g. 'richard'.")
    display_name: str
    me_person_id: int
    accent_color: str | None = None
    position: int = 0


class Person(BaseModel):
    """A single person in the network (including 'me', which has is_me=True)."""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    name: str
    bio: str | None = None
    notes: str | None = None
    is_me: bool = False
    is_wishlist: bool = Field(
        default=False,
        description=(
            "User-curated flag: 'I want to know this person'. Independent "
            "of graph topology — a wishlist person may or may not already "
            "have a Me-edge. Search ranking does NOT bias toward wishlist; "
            "the UI surfaces it as a chip / filter only."
        ),
    )
    created_at: datetime | None = None
    updated_at: datetime | None = None

    tags: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    needs: list[str] = Field(
        default_factory=list,
        description="What this person is seeking (so you can also find 'who would benefit from X').",
    )


class Relationship(BaseModel):
    """An edge between two people."""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    source_id: int
    target_id: int
    strength: int = Field(default=3, ge=1, le=5, description="1=distant, 5=close")
    context: str | None = None
    frequency: Frequency = Frequency.YEARLY
    last_contact: date | None = None
    introduced_by_id: int | None = None
    source: str = Field(
        default="manual",
        description=(
            "Provenance: 'manual' (user/spreadsheet), "
            "'colleague_inferred' (same-company auto-link), "
            "'ai_inferred' (LLM L2 extraction). "
            "`enrich` re-runs only overwrite ai_inferred edges."
        ),
    )


class GoalIntent(BaseModel):
    """Structured extraction from a natural-language goal."""

    original: str
    keywords: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    summary: str = ""


class PathStep(BaseModel):
    """One hop in a recommended path."""

    person_id: int
    name: str
    relation_from_previous: str | None = None
    strength: int | None = None


class PathResult(BaseModel):
    """One end-to-end recommendation from 'me' to a target person."""

    target: Person
    path: list[PathStep]
    relevance_score: float = Field(description="How well the target matches the goal, 0-1")
    path_strength: float = Field(description="Sum of relation strengths along the path")
    combined_score: float = Field(description="Final ranking score, higher is better")
    rationale: str = ""
    path_kind: str = Field(
        default="direct",
        description=(
            "Pure graph-topology label for how the user reaches this person: "
            "'direct'   = 1-hop strong (Me→X strength≥2); "
            "'weak'     = 1-hop weak (Me→X strength=1, vague acquaintance); "
            "'indirect' = needs 2+ hops via intermediaries (no Me-edge). "
            "This is independent of the `is_wishlist` curation flag on the "
            "target person — a wishlist contact can fall in any bucket."
        ),
    )

    @property
    def hops(self) -> int:
        return max(len(self.path) - 1, 0)
