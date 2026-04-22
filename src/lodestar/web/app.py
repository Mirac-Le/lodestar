"""FastAPI app: mount-router for one-db-per-owner deployments.

Layout
------

The process boots from ``serve --mount slug=path`` (or, if no
``--mount`` flag is given, from ``--db`` / ``LODESTAR_DB_PATH``).
Each mount becomes its **own** sub-application living under
``/r/<slug>/`` with this surface:

    /r/<slug>/              → SPA shell (index.html)
    /r/<slug>/api/...       → all data endpoints scoped to that db file
    /r/<slug>/api/unlock    → password challenge → HMAC token

The root app exposes only:

    /                       → SPA shell (the SPA reads /api/mounts to
                              render owner tabs and pick a default tab)
    /api/mounts             → list of mounted networks (display name,
                              accent color, contact count, locked?)
    /static/*               → shared static assets (one copy in memory)

Why mount-per-app instead of one big app + path param: it lets every
mount carry its own SQLite connection lifecycle and its own
``unlock_secret`` without smearing per-request "which slug am I?"
state through every dependency. A mount's ``Repository`` only ever
sees its own db — the only place ``slug`` shows up in handler code is
the unlock endpoint and the auth dependency.

Auth model (切 tab 必输 = "G1")
------------------------------

Every locked mount challenges on the **first** call. The frontend
never persists the unlock token across mounts (no localStorage write
keyed by slug); switching tabs throws the in-memory token away and
forces a fresh ``POST /r/<slug>/api/unlock``. The backend stays
stateless — it only verifies that ``X-Mount-Unlock`` was minted with
that mount's ``meta.unlock_secret`` and is still inside its TTL.
Mounts with no password set return a token immediately (no challenge),
which the SPA treats as "open tab, no password prompt".
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import networkx as nx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from lodestar.config import get_settings
from lodestar.db import connect, init_schema
from lodestar.db.repository import Repository
from lodestar.embedding import get_embedding_client
from lodestar.llm import GoalParser, get_llm_client
from lodestar.models import (
    GoalIntent,
    PathResult,
    PathStep,
    Person,
    Relationship,
)
from lodestar.search import HybridSearch, PathFinder, build_reranker_from_settings
from lodestar.viz.pyvis_export import infer_industry
from lodestar.web import enrich_jobs
from lodestar.web.mount_unlock import (
    assert_mount_access,
    mint_unlock_token,
)
from lodestar.web.schemas import (
    CreatePersonRequest,
    EnrichDiff,
    EnrichJobStartRequest,
    EnrichJobState,
    EnrichPreviewRequest,
    GraphEdge,
    GraphNode,
    GraphPayload,
    IntroductionsResponse,
    IntroductionSuggestion,
    MountDTO,
    MountsResponse,
    PathResultDTO,
    PathStepDTO,
    PersonDTO,
    RelationshipApplyRequest,
    RelationshipApplyResponse,
    RelationshipDTO,
    RelationshipListResponse,
    RelationshipParseRequest,
    RelationshipParseResponse,
    RelationshipUpdateRequest,
    SearchRequest,
    SearchResponse,
    StatsResponse,
    TwoPersonPathRequest,
    TwoPersonPathResponse,
    UnlockRequest,
    UnlockResponse,
    UpdatePersonRequest,
)
from lodestar.web.schemas import (
    ProposedEdge as ProposedEdgeDTO,
)

STATIC_DIR = Path(__file__).parent / "static"
_log = logging.getLogger(__name__)


# =====================================================================
# Mount registry
# =====================================================================
@dataclass(frozen=True)
class MountSpec:
    """A single ``--mount slug=path`` entry, validated at boot time."""

    slug: str
    db_path: Path


def _load_mounts() -> list[MountSpec]:
    """Read ``LODESTAR_MOUNTS_JSON`` (set by ``lodestar serve``) or fall
    back to the global ``--db`` / ``LODESTAR_DB_PATH`` as a single
    ``me`` mount.
    """
    raw = os.environ.get("LODESTAR_MOUNTS_JSON", "").strip()
    if raw:
        items = json.loads(raw)
        return [
            MountSpec(slug=m["slug"], db_path=Path(m["db_path"]).resolve())
            for m in items
        ]
    default_path = Path(get_settings().db_path).resolve()
    return [MountSpec(slug="me", db_path=default_path)]


# =====================================================================
# Helpers (pure render — no DB / mount knowledge baked in)
# =====================================================================
def _embed_text(p: Person) -> str:
    parts = [p.name]
    if p.bio:
        parts.append(p.bio)
    for label, vals in (
        ("Tags", p.tags), ("Skills", p.skills),
        ("Companies", p.companies), ("Cities", p.cities), ("Needs", p.needs),
    ):
        if vals:
            parts.append(f"{label}: " + ", ".join(vals))
    return " | ".join(parts)


def _edge_id(a: int, b: int) -> str:
    lo, hi = (a, b) if a <= b else (b, a)
    return f"e_{lo}_{hi}"


def _strength_to_me(rels: list[Relationship], me_id: int) -> dict[int, int]:
    out: dict[int, int] = {}
    for r in rels:
        if r.source_id == me_id:
            other = r.target_id
        elif r.target_id == me_id:
            other = r.source_id
        else:
            continue
        out[other] = max(out.get(other, 0), r.strength)
    return out


def _to_graph_node(p: Person, strength_to_me: int | None) -> GraphNode:
    if p.is_me:
        industry, color, glow = "我", "#f7f8f8", "#9ca4ae"
        size = 42
    else:
        industry, color, glow = infer_industry(p)
        size = 14 + (strength_to_me or 1) * 4
    assert p.id is not None
    return GraphNode(
        id=p.id, label=p.name, industry=industry,
        color=color, glow=glow, size=size, is_me=p.is_me,
        is_wishlist=p.is_wishlist,
        strength_to_me=strength_to_me,
        bio=p.bio, tags=p.tags, skills=p.skills,
        companies=p.companies, cities=p.cities,
        needs=p.needs, notes=p.notes,
    )


def _path_result_to_dto(r: PathResult) -> PathResultDTO:
    industry, color, _ = infer_industry(r.target)
    assert r.target.id is not None
    node_ids: list[int] = [s.person_id for s in r.path]
    edge_ids: list[str] = []
    prev: int | None = None
    for nid in node_ids:
        if prev is not None:
            edge_ids.append(_edge_id(prev, nid))
        prev = nid
    return PathResultDTO(
        target_id=r.target.id,
        target_name=r.target.name,
        industry=industry,
        color=color,
        path=[
            PathStepDTO(
                person_id=s.person_id, name=s.name,
                strength=s.strength,
                relation_from_previous=s.relation_from_previous,
            )
            for s in r.path
        ],
        relevance_score=r.relevance_score,
        path_strength=r.path_strength,
        combined_score=r.combined_score,
        rationale=r.rationale,
        path_kind=r.path_kind,
        is_wishlist=r.target.is_wishlist,
        node_ids=node_ids,
        edge_ids=edge_ids,
    )


def _relationship_to_dto(
    rel: Relationship,
    name_lookup: dict[int, str],
    me_id: int | None,
) -> RelationshipDTO | None:
    if rel.id is None:
        return None
    a_name = name_lookup.get(rel.source_id)
    b_name = name_lookup.get(rel.target_id)
    if a_name is None or b_name is None:
        return None
    return RelationshipDTO(
        id=rel.id,
        a_id=rel.source_id,
        a_name=a_name,
        b_id=rel.target_id,
        b_name=b_name,
        strength=rel.strength,
        context=rel.context,
        frequency=rel.frequency.value,
        source=rel.source,
        a_is_me=(me_id is not None and rel.source_id == me_id),
        b_is_me=(me_id is not None and rel.target_id == me_id),
    )


def _highlighted_elements(results: list[PathResult]) -> tuple[list[int], list[str]]:
    nodes: set[int] = set()
    edges: set[str] = set()
    for r in results:
        prev: int | None = None
        for s in r.path:
            nodes.add(s.person_id)
            if prev is not None:
                edges.add(_edge_id(prev, s.person_id))
            prev = s.person_id
    return sorted(nodes), sorted(edges)


# =====================================================================
# Per-mount sub-app factory
# =====================================================================
def _build_mount_app(spec: MountSpec) -> FastAPI:  # noqa: C901  (router-heavy)
    """Build a self-contained FastAPI app for one db file.

    All endpoints are scoped to ``spec.db_path``; no request data ever
    crosses to other mounts. The closure binds ``slug`` and ``db_path``
    so the dependency chain doesn't have to thread them through.
    """
    slug = spec.slug
    db_path = spec.db_path
    sub = FastAPI(
        title=f"Lodestar / {slug}",
        version="0.4.0",
        docs_url=None,  # share /docs at top-level if we ever add one
        redoc_url=None,
    )

    @contextmanager
    def _open_repo() -> Iterator[Repository]:
        settings = get_settings()
        conn = connect(db_path)
        init_schema(conn, embedding_dim=settings.embedding_dim)
        try:
            yield Repository(conn)
        finally:
            conn.close()

    def get_repo() -> Iterator[Repository]:
        with _open_repo() as repo:
            yield repo

    def verified(
        x_mount_unlock: str | None = Header(default=None, alias="X-Mount-Unlock"),
        repo: Repository = Depends(get_repo),
    ) -> Repository:
        assert_mount_access(repo, slug, x_mount_unlock)
        return repo

    def _name_lookup(repo: Repository) -> tuple[dict[int, str], int | None]:
        people = repo.list_people()
        me = repo.get_me()
        lookup: dict[int, str] = {p.id: p.name for p in people if p.id is not None}
        me_id: int | None = None
        if me is not None and me.id is not None:
            lookup[me.id] = me.name
            me_id = me.id
        return lookup, me_id

    def _person_dto(repo: Repository, pid: int) -> PersonDTO:
        person = repo.get_person(pid)
        if person is None:
            raise HTTPException(404, f"Person {pid} not found")
        me = repo.get_me()
        rels = repo.list_relationships()
        s2me = _strength_to_me(rels, me.id) if me and me.id else {}
        if person.is_me:
            industry, color, glow = "我", "#f7f8f8", "#9ca4ae"
        else:
            industry, color, glow = infer_industry(person)

        related: list[dict] = []
        for r in rels:
            other_id = (
                r.target_id if r.source_id == pid
                else r.source_id if r.target_id == pid
                else None
            )
            if other_id is None:
                continue
            other = repo.get_person(other_id)
            if not other:
                continue
            related.append({
                "id": other.id,
                "name": other.name,
                "strength": r.strength,
                "context": r.context or "",
                "frequency": r.frequency.value,
            })
        related.sort(key=lambda x: -x["strength"])  # type: ignore[arg-type, return-value]

        return PersonDTO.from_person(
            person, industry, color, glow,
            s2me.get(pid), related,
        )

    # ---------- SPA shell -------------------------------------------------
    @sub.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # ---------- unlock ----------------------------------------------------
    @sub.post("/api/unlock", response_model=UnlockResponse)
    def unlock(
        body: UnlockRequest,
        repo: Repository = Depends(get_repo),
    ) -> UnlockResponse:
        if not repo.web_password_hash:
            return UnlockResponse(
                token=mint_unlock_token(slug, repo.unlock_secret),
                unlocked=True,
            )
        if not repo.verify_web_password(body.password):
            raise HTTPException(
                401,
                detail={"code": "bad_password", "message": "密码错误"},
            )
        return UnlockResponse(
            token=mint_unlock_token(slug, repo.unlock_secret),
            unlocked=True,
        )

    # ---------- graph -----------------------------------------------------
    @sub.get("/api/graph", response_model=GraphPayload)
    def get_graph(repo: Repository = Depends(verified)) -> GraphPayload:
        me = repo.get_me()
        if me is None or me.id is None:
            raise HTTPException(400, "DB has no `me` row; run `lodestar init`.")
        people = repo.list_people()
        rels = repo.list_relationships()
        s2me = _strength_to_me(rels, me.id)
        nodes = [_to_graph_node(me, None)] + [
            _to_graph_node(p, s2me.get(p.id) if p.id else None)
            for p in people if not p.is_me
        ]
        edges = [
            GraphEdge(
                id=_edge_id(r.source_id, r.target_id),
                source=r.source_id, target=r.target_id,
                strength=r.strength, context=r.context,
                frequency=r.frequency.value,
            )
            for r in rels
        ]
        settings = get_settings()
        return GraphPayload(
            nodes=nodes, edges=edges, me_id=me.id,
            weak_me_floor=settings.weak_me_floor,
            mount_slug=slug,
            mount_display_name=repo.display_name or slug,
        )

    # ---------- search ----------------------------------------------------
    @sub.post("/api/search", response_model=SearchResponse)
    def search(
        body: SearchRequest,
        repo: Repository = Depends(verified),
    ) -> SearchResponse:
        if not body.goal.strip():
            raise HTTPException(400, "Empty goal")
        settings = get_settings()
        if body.no_llm:
            intent = GoalIntent(
                original=body.goal, keywords=[body.goal], summary=body.goal,
            )
        else:
            try:
                intent = GoalParser(get_llm_client()).parse(body.goal)
            except Exception:
                intent = GoalIntent(
                    original=body.goal, keywords=[body.goal], summary=body.goal,
                )
        try:
            embedder = get_embedding_client()
        except Exception:
            embedder = None
        candidates = HybridSearch(repo=repo, embedder=embedder).search(
            intent, top_k=settings.top_k, recall_k=settings.reranker_recall_k
        )
        if not candidates:
            return SearchResponse(
                goal=body.goal, intent_summary=intent.summary,
                intent_keywords=intent.keywords, results=[],
                highlighted_node_ids=[], highlighted_edge_ids=[],
            )
        reranker = build_reranker_from_settings()
        candidates = reranker.rerank(intent, candidates, repo)[: settings.top_k]
        ranked = PathFinder(
            repo=repo, max_hops=settings.max_hops,
            weak_me_floor=settings.weak_me_floor,
        ).rank(candidates)

        # Two-bucket UI: indirect (multi-hop intros) + contacted (every
        # 1-hop, sorted by strength). The frontend has been simplified
        # to those two columns — anything else risks the silent Alpine
        # `direct.length` regression we hit before.
        indirect = [r for r in ranked if r.path_kind == "indirect"][: body.top_k]
        contacted_pool = [
            r for r in ranked if r.path_kind in ("direct", "weak")
        ]
        contacted_pool.sort(
            key=lambda r: (
                -(r.path[1].strength or 0) if len(r.path) > 1 else 0,
                -r.combined_score,
            )
        )
        contacted = contacted_pool[: body.top_k * 2]

        wishlist = [r for r in ranked if r.target.is_wishlist][: body.top_k]

        bucket_union = {id(r): r for r in (indirect + contacted_pool)}
        combined = sorted(
            bucket_union.values(),
            key=lambda r: r.combined_score,
            reverse=True,
        )[: body.top_k]
        nodes, edges = _highlighted_elements(combined)
        indirect_dto = [_path_result_to_dto(r) for r in indirect]
        contacted_dto = [_path_result_to_dto(r) for r in contacted]
        wishlist_dto = [_path_result_to_dto(r) for r in wishlist]
        return SearchResponse(
            goal=body.goal,
            intent_summary=intent.summary or body.goal,
            intent_keywords=intent.keywords,
            results=[_path_result_to_dto(r) for r in combined],
            indirect=indirect_dto,
            contacted=contacted_dto,
            wishlist=wishlist_dto,
            highlighted_node_ids=nodes,
            highlighted_edge_ids=edges,
        )

    # ---------- person CRUD -----------------------------------------------
    @sub.get("/api/people/{pid}", response_model=PersonDTO)
    def get_person(
        pid: int, repo: Repository = Depends(verified)
    ) -> PersonDTO:
        return _person_dto(repo, pid)

    @sub.post("/api/people", response_model=PersonDTO)
    def create_person(
        body: CreatePersonRequest,
        repo: Repository = Depends(verified),
    ) -> PersonDTO:
        me = repo.get_me()
        if me is None or me.id is None:
            raise HTTPException(400, "Run `lodestar init` first.")
        person = Person(
            name=body.name, bio=body.bio, notes=body.notes,
            tags=body.tags, skills=body.skills, companies=body.companies,
            cities=body.cities, needs=body.needs,
            is_wishlist=body.is_wishlist,
        )
        saved = repo.add_person(person)
        assert saved.id is not None
        repo.add_relationship(Relationship(
            source_id=me.id, target_id=saved.id,
            strength=body.strength_to_me,
            context=body.relation_context,
            frequency=body.frequency,
        ))
        if body.embed and saved.bio:
            try:
                vec = get_embedding_client().embed(_embed_text(saved))
                repo.upsert_embedding(saved.id, vec)
            except Exception:
                pass
        industry, color, glow = infer_industry(saved)
        return PersonDTO.from_person(
            saved, industry, color, glow, body.strength_to_me, [],
        )

    @sub.patch("/api/people/{pid}", response_model=PersonDTO)
    def update_person(
        pid: int, body: UpdatePersonRequest,
        repo: Repository = Depends(verified),
    ) -> PersonDTO:
        existing = repo.get_person(pid)
        if existing is None:
            raise HTTPException(404, "Not found")
        if body.bio is not None:
            existing.bio = body.bio
        if body.notes is not None:
            existing.notes = body.notes
        if body.tags is not None:
            existing.tags = body.tags
        if body.skills is not None:
            existing.skills = body.skills
        if body.companies is not None:
            existing.companies = body.companies
        if body.cities is not None:
            existing.cities = body.cities
        if body.needs is not None:
            existing.needs = body.needs
        if body.is_wishlist is not None:
            existing.is_wishlist = body.is_wishlist
        updated = repo.update_person(existing)
        if body.embed and updated.bio:
            try:
                vec = get_embedding_client().embed(_embed_text(updated))
                repo.upsert_embedding(pid, vec)
            except Exception:
                pass
        return _person_dto(repo, pid)

    @sub.delete("/api/people/{pid}")
    def delete_person(
        pid: int, repo: Repository = Depends(verified)
    ) -> dict:
        repo.delete_person(pid)
        return {"deleted": pid}

    # ---------- two-person path ------------------------------------------
    @sub.post("/api/path", response_model=TwoPersonPathResponse)
    def find_paths(
        body: TwoPersonPathRequest,
        repo: Repository = Depends(verified),
    ) -> TwoPersonPathResponse:
        settings = get_settings()
        rels = repo.list_relationships()
        g: nx.Graph = nx.Graph()
        for r in rels:
            g.add_edge(
                r.source_id, r.target_id,
                weight=1.0 / max(r.strength, 1),
                strength=r.strength, context=r.context or "",
            )
        if body.source_id not in g or body.target_id not in g:
            return TwoPersonPathResponse(paths=[])
        try:
            simple_paths = list(nx.all_simple_paths(
                g, body.source_id, body.target_id,
                cutoff=settings.max_hops,
            ))
        except nx.NodeNotFound:
            return TwoPersonPathResponse(paths=[])

        results: list[PathResultDTO] = []
        for node_ids in simple_paths[: body.max_paths * 3]:
            steps: list[PathStep] = []
            total_strength = 0.0
            for idx, nid in enumerate(node_ids):
                p = repo.get_person(nid)
                if p is None:
                    break
                if idx == 0:
                    steps.append(PathStep(person_id=nid, name=p.name))
                    continue
                edge = g.edges[node_ids[idx - 1], nid]
                strength = int(edge["strength"])
                total_strength += strength
                steps.append(PathStep(
                    person_id=nid, name=p.name,
                    relation_from_previous=str(edge["context"]) or None,
                    strength=strength,
                ))
            target = repo.get_person(body.target_id)
            if target is None:
                continue
            hops = max(len(node_ids) - 1, 1)
            pr = PathResult(
                target=target, path=steps,
                relevance_score=1.0,
                path_strength=total_strength,
                combined_score=total_strength / hops,
                rationale=f"hops={hops}, strength={total_strength:.0f}",
            )
            results.append(_path_result_to_dto(pr))

        results.sort(key=lambda r: r.combined_score, reverse=True)
        return TwoPersonPathResponse(paths=results[: body.max_paths])

    # ---------- introductions you could broker ---------------------------
    @sub.get("/api/introductions", response_model=IntroductionsResponse)
    def introductions(
        repo: Repository = Depends(verified),
    ) -> IntroductionsResponse:
        people = repo.list_people()
        suggestions: list[IntroductionSuggestion] = []
        for seeker in people:
            if not seeker.needs:
                continue
            need_set = {n.lower() for n in seeker.needs}
            for provider in people:
                if provider.id == seeker.id:
                    continue
                attrs_lower = {
                    a.lower() for a in
                    [*provider.tags, *provider.skills, *provider.companies]
                }
                bio_lower = (provider.bio or "").lower()
                for need in need_set:
                    matched = next(
                        (a for a in attrs_lower if need and (
                            need in a or a in need
                        )),
                        None,
                    )
                    if matched is None and need and need in bio_lower:
                        matched = need
                    if matched:
                        assert provider.id is not None and seeker.id is not None
                        suggestions.append(IntroductionSuggestion(
                            provider_id=provider.id,
                            provider_name=provider.name,
                            seeker_id=seeker.id,
                            seeker_name=seeker.name,
                            matched_keyword=matched,
                            why=(
                                f"{seeker.name} 需要「{need}」"
                                f"，{provider.name} 在「{matched}」上对得上"
                            ),
                        ))
                        break
        return IntroductionsResponse(suggestions=suggestions[:50])

    # ---------- enrich (LLM extraction) ----------------------------------
    @sub.post("/api/enrich/preview", response_model=EnrichDiff)
    def enrich_preview(
        body: EnrichPreviewRequest,
        repo: Repository = Depends(verified),
    ) -> EnrichDiff:
        from lodestar.enrich import L1Extractor, LLMClient, LLMError

        try:
            client = LLMClient()
        except LLMError as exc:
            raise HTTPException(503, str(exc)) from exc
        extractor = L1Extractor(repo, client=client)
        try:
            result = extractor.extract_for_input(
                name=body.name,
                bio=body.bio,
                notes=body.notes,
                raw_tags=body.raw_tags,
                raw_cities=body.raw_cities,
                known_companies=body.known_companies,
                known_cities=body.known_cities,
                known_tags=body.known_tags,
            )
        except LLMError as exc:
            raise HTTPException(502, f"LLM 调用失败：{exc}") from exc
        return EnrichDiff(
            add_companies=result.add_companies,
            add_cities=result.add_cities,
            add_titles=result.add_titles,
            add_tags=result.add_tags,
            error=result.error,
        )

    @sub.post("/api/enrich/person/{pid}", response_model=PersonDTO)
    def enrich_person(
        pid: int,
        repo: Repository = Depends(verified),
        only_missing: bool = Query(True),
    ) -> PersonDTO:
        from lodestar.enrich import L1Extractor, LLMClient, LLMError

        person = repo.get_person(pid)
        if person is None:
            raise HTTPException(404, f"Person {pid} not found")
        if person.is_me:
            raise HTTPException(400, "不能对 me 节点跑 enrich")

        if only_missing and bool(person.companies) and bool(person.cities):
            return _person_dto(repo, pid)

        try:
            client = LLMClient()
        except LLMError as exc:
            raise HTTPException(503, str(exc)) from exc
        extractor = L1Extractor(repo, client=client)
        try:
            diff = extractor.extract_for_person(person)
        except LLMError as exc:
            raise HTTPException(502, f"LLM 调用失败：{exc}") from exc
        if not diff.error and not diff.is_empty():
            extractor.apply([diff])
        return _person_dto(repo, pid)

    @sub.post("/api/enrich/start", response_model=EnrichJobState)
    def enrich_start(
        body: EnrichJobStartRequest,
        _verified: Repository = Depends(verified),
    ) -> EnrichJobState:
        from lodestar.enrich import LLMClient, LLMError

        try:
            LLMClient()
        except LLMError as exc:
            raise HTTPException(503, str(exc)) from exc
        state = enrich_jobs.start(
            mount_slug=slug,
            db_path=db_path,
            only_missing=body.only_missing,
        )
        return EnrichJobState(**state.to_dict())

    @sub.get("/api/enrich/status/{task_id}", response_model=EnrichJobState)
    def enrich_status(
        task_id: str,
        _verified: Repository = Depends(verified),
    ) -> EnrichJobState:
        state = enrich_jobs.get(task_id)
        if state is None or state.mount_slug != slug:
            raise HTTPException(404, f"Unknown enrich task '{task_id}'")
        return EnrichJobState(**state.to_dict())

    # ---------- relationships --------------------------------------------
    @sub.get("/api/relationships", response_model=RelationshipListResponse)
    def list_relationships_endpoint(
        repo: Repository = Depends(verified),
        q: str | None = Query(None),
        min_strength: int | None = Query(None, ge=1, le=5),
        source: str | None = Query(
            None,
            description="Comma-separated subset of {manual,colleague_inferred,ai_inferred}",
        ),
        include_me: bool = Query(True),
        person_id: int | None = Query(None),
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ) -> RelationshipListResponse:
        name_lookup, me_id = _name_lookup(repo)
        rels = repo.list_relationships()

        sources_filter: set[str] | None = None
        if source:
            sources_filter = {s.strip() for s in source.split(",") if s.strip()}

        q_clean = (q or "").strip().lower()
        items: list[RelationshipDTO] = []
        for r in rels:
            dto = _relationship_to_dto(r, name_lookup, me_id)
            if dto is None:
                continue
            if not include_me and (dto.a_is_me or dto.b_is_me):
                continue
            if person_id is not None and dto.a_id != person_id and dto.b_id != person_id:
                continue
            if min_strength is not None and dto.strength < min_strength:
                continue
            if sources_filter is not None and dto.source not in sources_filter:
                continue
            if q_clean and (
                q_clean not in dto.a_name.lower()
                and q_clean not in dto.b_name.lower()
                and q_clean not in (dto.context or "").lower()
            ):
                continue
            items.append(dto)

        items.sort(
            key=lambda x: (
                0 if (x.a_is_me or x.b_is_me) else 1,
                -x.strength,
                x.a_name,
                x.b_name,
            )
        )
        total = len(items)
        paged = items[offset : offset + limit]
        return RelationshipListResponse(
            items=paged, total=total, offset=offset, limit=limit
        )

    @sub.post("/api/relationships/parse", response_model=RelationshipParseResponse)
    def parse_relationships(
        body: RelationshipParseRequest,
        repo: Repository = Depends(verified),
    ) -> RelationshipParseResponse:
        from lodestar.enrich import LLMClient, LLMError, RelationshipParser

        try:
            client = LLMClient()
        except LLMError as exc:
            raise HTTPException(503, str(exc)) from exc
        parser = RelationshipParser(repo, client=client)
        result = parser.parse(body.text)

        if result.error and not result.proposals and not result.unknown_mentions:
            return RelationshipParseResponse(error=result.error)

        name_lookup, me_id = _name_lookup(repo)
        all_rels = repo.list_relationships()
        edge_index: dict[tuple[int, int], RelationshipDTO] = {}
        by_person: dict[int, list[RelationshipDTO]] = {}
        for r in all_rels:
            dto = _relationship_to_dto(r, name_lookup, me_id)
            if dto is None:
                continue
            lo, hi = (dto.a_id, dto.b_id) if dto.a_id <= dto.b_id else (dto.b_id, dto.a_id)
            edge_index[(lo, hi)] = dto
            by_person.setdefault(dto.a_id, []).append(dto)
            by_person.setdefault(dto.b_id, []).append(dto)

        proposals_out: list[ProposedEdgeDTO] = []
        mentioned_pids: set[int] = set()
        for p in result.proposals:
            lo, hi = (p.a_id, p.b_id) if p.a_id <= p.b_id else (p.b_id, p.a_id)
            existing = edge_index.get((lo, hi))
            mentioned_pids.update({p.a_id, p.b_id})
            proposals_out.append(
                ProposedEdgeDTO(
                    a_id=p.a_id,
                    a_name=p.a_name,
                    b_id=p.b_id,
                    b_name=p.b_name,
                    strength=p.strength,
                    context=p.context,
                    frequency=p.frequency,
                    rationale=p.rationale,
                    existing_edge=existing,
                )
            )

        context_for = {pid: by_person.get(pid, []) for pid in mentioned_pids}

        return RelationshipParseResponse(
            proposals=proposals_out,
            unknown_mentions=result.unknown_mentions,
            context_for=context_for,
            error=result.error,
        )

    @sub.post("/api/relationships/apply", response_model=RelationshipApplyResponse)
    def apply_relationships(
        body: RelationshipApplyRequest,
        repo: Repository = Depends(verified),
    ) -> RelationshipApplyResponse:
        people = repo.list_people()
        visible_pids = {p.id for p in people if p.id is not None}
        me = repo.get_me()
        if me is not None and me.id is not None:
            visible_pids.add(me.id)
        name_lookup, me_id = _name_lookup(repo)

        applied = 0
        skipped = 0
        out_dtos: list[RelationshipDTO] = []
        for item in body.edges:
            if item.a_id == item.b_id:
                skipped += 1
                continue
            if item.a_id not in visible_pids or item.b_id not in visible_pids:
                skipped += 1
                continue
            rel = Relationship(
                source_id=item.a_id,
                target_id=item.b_id,
                strength=item.strength,
                context=item.context,
                frequency=item.frequency,
                source="manual",
            )
            saved = repo.add_relationship(rel)
            applied += 1
            dto = _relationship_to_dto(saved, name_lookup, me_id)
            if dto is not None:
                out_dtos.append(dto)
        return RelationshipApplyResponse(
            applied=applied, skipped=skipped, items=out_dtos
        )

    @sub.patch("/api/relationships/{rel_id}", response_model=RelationshipDTO)
    def update_relationship(
        rel_id: int,
        body: RelationshipUpdateRequest,
        repo: Repository = Depends(verified),
    ) -> RelationshipDTO:
        rels = repo.list_relationships()
        target = next((r for r in rels if r.id == rel_id), None)
        if target is None:
            raise HTTPException(404, f"Relationship {rel_id} not found.")
        new_rel = Relationship(
            id=target.id,
            source_id=target.source_id,
            target_id=target.target_id,
            strength=body.strength if body.strength is not None else target.strength,
            context=body.context if body.context is not None else target.context,
            frequency=body.frequency if body.frequency is not None else target.frequency,
            source="manual",
        )
        saved = repo.add_relationship(new_rel)
        name_lookup, me_id = _name_lookup(repo)
        dto = _relationship_to_dto(saved, name_lookup, me_id)
        if dto is None:
            raise HTTPException(500, "Failed to render updated relationship.")
        return dto

    @sub.delete("/api/relationships/{rel_id}")
    def delete_relationship(
        rel_id: int,
        repo: Repository = Depends(verified),
    ) -> dict:
        rels = repo.list_relationships()
        target = next((r for r in rels if r.id == rel_id), None)
        if target is None:
            raise HTTPException(404, f"Relationship {rel_id} not found.")
        with repo.conn:
            repo.conn.execute("DELETE FROM relationship WHERE id = ?", (rel_id,))
        return {"deleted": rel_id}

    # ---------- stats -----------------------------------------------------
    @sub.get("/api/stats", response_model=StatsResponse)
    def stats(repo: Repository = Depends(verified)) -> StatsResponse:
        people = repo.list_people()
        rels = repo.list_relationships()
        me = repo.get_me()
        s2me = _strength_to_me(rels, me.id) if me and me.id else {}

        ind_counter: Counter[str] = Counter()
        for p in people:
            label, _, _ = infer_industry(p)
            ind_counter[label] += 1

        strength_counter: Counter[int] = Counter()
        for pid in s2me:
            strength_counter[s2me[pid]] += 1

        company_counter: Counter[str] = Counter()
        for p in people:
            for c in p.companies:
                company_counter[c] += 1

        city_counter: Counter[str] = Counter()
        for p in people:
            for c in p.cities:
                city_counter[c] += 1

        return StatsResponse(
            total_contacts=len(people),
            total_relationships=len(rels),
            by_industry=dict(ind_counter.most_common()),
            by_strength=dict(sorted(strength_counter.items())),
            top_companies=company_counter.most_common(8),
            top_cities=city_counter.most_common(8),
        )

    return sub


# =====================================================================
# Root app
# =====================================================================
def create_app() -> FastAPI:
    """Build the root FastAPI app with one mounted sub-app per ``--mount``."""
    mounts = _load_mounts()
    if not mounts:
        raise RuntimeError(
            "No mounts configured. Run `lodestar serve` (which seeds "
            "`LODESTAR_MOUNTS_JSON`) or set `--mount` / `--db`."
        )

    # Build the per-mount registry up front so /api/mounts can read
    # display_name / lock-state without lazy-init mid-request.
    mount_meta: list[MountDTO] = []
    settings = get_settings()
    for spec in mounts:
        conn = connect(spec.db_path)
        try:
            init_schema(conn, embedding_dim=settings.embedding_dim)
            repo = Repository(conn)
            people = repo.list_people()
            me = repo.get_me()
            mount_meta.append(MountDTO(
                slug=spec.slug,
                display_name=repo.display_name or spec.slug,
                me_person_id=me.id if me else None,
                accent_color=repo.accent_color,
                contact_count=len(people),
                locked=bool(repo.web_password_hash),
            ))
        finally:
            conn.close()

    root = FastAPI(
        title="Lodestar",
        description="Personal network navigator (one db per owner).",
        version="0.4.0",
    )

    # Mount each sub-app at /r/<slug>
    for spec in mounts:
        sub_app = _build_mount_app(spec)
        root.mount(f"/r/{spec.slug}", sub_app)
        _log.info("mounted /r/%s → %s", spec.slug, spec.db_path)

    # Shared static assets (one copy in memory regardless of mount count)
    if STATIC_DIR.exists():
        root.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @root.get("/api/mounts", response_model=MountsResponse)
    def list_mounts() -> MountsResponse:
        default = mount_meta[0].slug if mount_meta else None
        return MountsResponse(mounts=mount_meta, default_slug=default)

    @root.get("/", include_in_schema=False, response_model=None)
    def index() -> FileResponse | RedirectResponse:
        # Single-mount setups skip the "pick a network" view entirely
        # and drop the user straight into the only network they have.
        if len(mount_meta) == 1:
            return RedirectResponse(f"/r/{mount_meta[0].slug}/")
        # Multi-mount: dedicated lightweight picker page (no cytoscape /
        # echarts / 1.8k-line SPA loaded). Falls back to the SPA shell
        # only if landing.html is somehow missing.
        landing = STATIC_DIR / "landing.html"
        if landing.exists():
            return FileResponse(landing)
        return FileResponse(STATIC_DIR / "index.html")

    return root


__all__ = ["create_app"]
