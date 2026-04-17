"""FastAPI app exposing the network as a REST API + serving the SPA."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import networkx as nx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
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
from lodestar.search import HybridSearch, PathFinder
from lodestar.viz.pyvis_export import infer_industry
from lodestar.web.schemas import (
    CreatePersonRequest,
    GraphEdge,
    GraphNode,
    GraphPayload,
    IntroductionsResponse,
    IntroductionSuggestion,
    PathResultDTO,
    PathStepDTO,
    PersonDTO,
    SearchRequest,
    SearchResponse,
    StatsResponse,
    TwoPersonPathRequest,
    TwoPersonPathResponse,
    UpdatePersonRequest,
)

STATIC_DIR = Path(__file__).parent / "static"


# --------------------------------------------------------------- helpers
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
        node_ids=node_ids,
        edge_ids=edge_ids,
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


# ------------------------------------------------------------- DB session
@contextmanager
def _open_repo() -> Iterator[Repository]:
    settings = get_settings()
    conn = connect(settings.db_path)
    init_schema(conn, embedding_dim=settings.embedding_dim)
    try:
        yield Repository(conn)
    finally:
        conn.close()


def get_repo() -> Iterator[Repository]:
    with _open_repo() as repo:
        yield repo


# ---------------------------------------------------------------- app
def create_app() -> FastAPI:
    app = FastAPI(
        title="Lodestar",
        description="Personal network navigator API",
        version="0.1.0",
    )

    # ---------- root → SPA
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    if STATIC_DIR.exists():
        app.mount(
            "/static", StaticFiles(directory=str(STATIC_DIR)), name="static"
        )

    # ---------- graph
    @app.get("/api/graph", response_model=GraphPayload)
    def get_graph(repo: Repository = Depends(get_repo)) -> GraphPayload:
        me = repo.get_me()
        if me is None or me.id is None:
            raise HTTPException(400, "Database not initialized; run `lodestar init`.")
        people = repo.list_people()
        rels = repo.list_relationships()
        s2me = _strength_to_me(rels, me.id)
        nodes = [_to_graph_node(me, None)] + [
            _to_graph_node(p, s2me.get(p.id) if p.id else None) for p in people
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
        return GraphPayload(nodes=nodes, edges=edges, me_id=me.id)

    # ---------- search
    @app.post("/api/search", response_model=SearchResponse)
    def search(
        body: SearchRequest, repo: Repository = Depends(get_repo)
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
            intent, top_k=settings.top_k,
        )
        if not candidates:
            return SearchResponse(
                goal=body.goal, intent_summary=intent.summary,
                intent_keywords=intent.keywords, results=[],
                highlighted_node_ids=[], highlighted_edge_ids=[],
            )
        ranked = PathFinder(repo=repo, max_hops=settings.max_hops).rank(candidates)

        # Bucket by kind. Each bucket is independently truncated so
        # direct contacts can't crowd targets out of the UI.
        targets = [r for r in ranked if r.path_kind == "target"][: body.top_k]
        direct = [r for r in ranked if r.path_kind == "direct"][: body.top_k]
        weak = [r for r in ranked if r.path_kind == "weak"][: max(body.top_k - 1, 2)]

        # Legacy `results` field: targets first (most product-relevant), then
        # the rest, capped by top_k for clients that haven't moved off it yet.
        legacy = (targets + direct + weak)[: body.top_k]

        nodes, edges = _highlighted_elements(legacy)
        targets_dto = [_path_result_to_dto(r) for r in targets]
        direct_dto = [_path_result_to_dto(r) for r in direct]
        weak_dto = [_path_result_to_dto(r) for r in weak]
        return SearchResponse(
            goal=body.goal,
            intent_summary=intent.summary or body.goal,
            intent_keywords=intent.keywords,
            results=[_path_result_to_dto(r) for r in legacy],
            targets=targets_dto,
            direct=direct_dto,
            weak=weak_dto,
            highlighted_node_ids=nodes,
            highlighted_edge_ids=edges,
        )

    # ---------- person detail
    @app.get("/api/people/{pid}", response_model=PersonDTO)
    def get_person(pid: int, repo: Repository = Depends(get_repo)) -> PersonDTO:
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

        related = []
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

    # ---------- create
    @app.post("/api/people", response_model=PersonDTO)
    def create_person(
        body: CreatePersonRequest, repo: Repository = Depends(get_repo)
    ) -> PersonDTO:
        me = repo.get_me()
        if me is None or me.id is None:
            raise HTTPException(400, "Run `lodestar init` first")
        person = Person(
            name=body.name, bio=body.bio, notes=body.notes,
            tags=body.tags, skills=body.skills, companies=body.companies,
            cities=body.cities, needs=body.needs,
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

    # ---------- update
    @app.patch("/api/people/{pid}", response_model=PersonDTO)
    def update_person(
        pid: int, body: UpdatePersonRequest, repo: Repository = Depends(get_repo)
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
        updated = repo.update_person(existing)
        if body.embed and updated.bio:
            try:
                vec = get_embedding_client().embed(_embed_text(updated))
                repo.upsert_embedding(pid, vec)
            except Exception:
                pass
        return get_person(pid, repo=repo)

    # ---------- delete
    @app.delete("/api/people/{pid}")
    def delete_person(pid: int, repo: Repository = Depends(get_repo)) -> dict:
        repo.delete_person(pid)
        return {"deleted": pid}

    # ---------- two-person path
    @app.post("/api/path", response_model=TwoPersonPathResponse)
    def find_paths(
        body: TwoPersonPathRequest, repo: Repository = Depends(get_repo)
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

    # ---------- introductions you could broker
    @app.get("/api/introductions", response_model=IntroductionsResponse)
    def introductions(repo: Repository = Depends(get_repo)) -> IntroductionsResponse:
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
        # cap output
        return IntroductionsResponse(suggestions=suggestions[:50])

    # ---------- stats
    @app.get("/api/stats", response_model=StatsResponse)
    def stats(repo: Repository = Depends(get_repo)) -> StatsResponse:
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

    return app


__all__ = ["create_app", "get_repo"]
