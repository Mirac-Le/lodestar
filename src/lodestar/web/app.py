"""FastAPI app exposing the network as a REST API + serving the SPA."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import networkx as nx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from lodestar.config import get_settings
from lodestar.db import connect, init_schema
from lodestar.db.repository import Repository
from lodestar.embedding import get_embedding_client
from lodestar.llm import GoalParser, get_llm_client
from lodestar.models import (
    GoalIntent,
    Owner,
    PathResult,
    PathStep,
    Person,
    Relationship,
)
from lodestar.search import HybridSearch, PathFinder
from lodestar.viz.pyvis_export import infer_industry
from lodestar.web import enrich_jobs
from lodestar.web.owner_unlock import (
    assert_owner_web_access,
    mint_unlock_token,
    unlock_secret_bytes,
    verify_web_password,
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
    OwnerDTO,
    OwnersResponse,
    OwnerUnlockRequest,
    OwnerUnlockResponse,
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
    UpdatePersonRequest,
)
from lodestar.web.schemas import (
    ProposedEdge as ProposedEdgeDTO,
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
    """Render a Relationship row as a flat DTO. Returns None when either
    endpoint isn't visible to the current owner (name_lookup miss)."""
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


def _resolve_owner(repo: Repository, slug: str | None) -> Owner:
    """Pick the active owner from the optional `?owner=slug` query.

    Falls back to the first owner (lowest position / id) when the
    parameter is absent. Raises 400 if no owners exist at all and 404 if
    the requested slug is unknown — both surface as actionable errors in
    the SPA.
    """
    owners = repo.list_owners()
    if not owners:
        raise HTTPException(
            400, "Database not initialised. Run `lodestar init` first."
        )
    if slug is None:
        return owners[0]
    for o in owners:
        if o.slug == slug:
            return o
    raise HTTPException(404, f"Unknown owner '{slug}'.")


def verified_owner(
    owner: str | None = Query(None),
    x_owner_unlock: str | None = Header(default=None, alias="X-Owner-Unlock"),
    repo: Repository = Depends(get_repo),
) -> Owner:
    o = _resolve_owner(repo, owner)
    assert_owner_web_access(o, x_owner_unlock, unlock_secret_bytes())
    return o


def _person_dto(repo: Repository, owner_obj: Owner, pid: int) -> PersonDTO:
    person = repo.get_person(pid)
    if person is None:
        raise HTTPException(404, f"Person {pid} not found")
    assert owner_obj.id is not None
    me = repo.get_me(owner_id=owner_obj.id)
    rels = repo.list_relationships(owner_id=owner_obj.id)
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

    # ---------- owners
    @app.get("/api/owners", response_model=OwnersResponse)
    def list_owners(repo: Repository = Depends(get_repo)) -> OwnersResponse:
        owners = repo.list_owners()
        if not owners:
            return OwnersResponse(owners=[], default_slug=None)
        default_slug = owners[0].slug
        out: list[OwnerDTO] = []
        for o in owners:
            assert o.id is not None
            count = len(repo.list_people(owner_id=o.id))
            out.append(OwnerDTO(
                id=o.id, slug=o.slug, display_name=o.display_name,
                me_person_id=o.me_person_id, accent_color=o.accent_color,
                contact_count=count, is_default=(o.slug == default_slug),
                web_locked=bool(o.web_password_hash),
            ))
        return OwnersResponse(owners=out, default_slug=default_slug)

    @app.post("/api/owners/unlock", response_model=OwnerUnlockResponse)
    def unlock_owner(
        body: OwnerUnlockRequest,
        repo: Repository = Depends(get_repo),
    ) -> OwnerUnlockResponse:
        o = repo.get_owner_by_slug(body.slug)
        if o is None:
            raise HTTPException(404, f"Unknown owner '{body.slug}'.")
        secret = unlock_secret_bytes()
        if not o.web_password_hash:
            return OwnerUnlockResponse(
                token=mint_unlock_token(o.slug, secret),
                unlocked=True,
            )
        if not verify_web_password(body.password, o.web_password_hash):
            raise HTTPException(
                401,
                detail={"code": "bad_password", "message": "密码错误"},
            )
        return OwnerUnlockResponse(
            token=mint_unlock_token(o.slug, secret),
            unlocked=True,
        )

    # ---------- graph
    @app.get("/api/graph", response_model=GraphPayload)
    def get_graph(
        owner_obj: Annotated[Owner, Depends(verified_owner)],
        repo: Repository = Depends(get_repo),
    ) -> GraphPayload:
        assert owner_obj.id is not None
        me = repo.get_me(owner_id=owner_obj.id)
        if me is None or me.id is None:
            raise HTTPException(400, "Owner has no `me` row; reseed the database.")
        people = repo.list_people(owner_id=owner_obj.id)
        rels = repo.list_relationships(owner_id=owner_obj.id)
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
        settings = get_settings()
        return GraphPayload(
            nodes=nodes, edges=edges, me_id=me.id,
            weak_me_floor=settings.weak_me_floor,
            owner_slug=owner_obj.slug,
            owner_display_name=owner_obj.display_name,
        )

    # ---------- search
    @app.post("/api/search", response_model=SearchResponse)
    def search(
        body: SearchRequest,
        owner_obj: Annotated[Owner, Depends(verified_owner)],
        repo: Repository = Depends(get_repo),
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
        candidates = HybridSearch(
            repo=repo, embedder=embedder, owner_id=owner_obj.id,
        ).search(intent, top_k=settings.top_k)
        if not candidates:
            return SearchResponse(
                goal=body.goal, intent_summary=intent.summary,
                intent_keywords=intent.keywords, results=[],
                highlighted_node_ids=[], highlighted_edge_ids=[],
            )
        ranked = PathFinder(
            repo=repo, max_hops=settings.max_hops, owner_id=owner_obj.id,
            weak_me_floor=settings.weak_me_floor,
        ).rank(candidates)

        # Bucket purely by graph topology. Each bucket is independently
        # truncated so direct contacts can't crowd indirect intros out of
        # the UI, but no bucket gets a free pass into the autohighlight —
        # `results` is sorted by combined_score across all buckets so the
        # client can pick the global best regardless of kind.
        indirect = [r for r in ranked if r.path_kind == "indirect"][: body.top_k]
        direct = [r for r in ranked if r.path_kind == "direct"][: body.top_k]
        weak = [r for r in ranked if r.path_kind == "weak"][: max(body.top_k - 1, 2)]

        wishlist = [r for r in ranked if r.target.is_wishlist][: body.top_k]

        # Combined list = global ranking (already sorted by combined_score).
        # Truncate after merging buckets so we keep the strongest survivors.
        bucket_union = {id(r): r for r in (indirect + direct + weak)}
        combined = sorted(
            bucket_union.values(),
            key=lambda r: r.combined_score,
            reverse=True,
        )[: body.top_k]

        nodes, edges = _highlighted_elements(combined)
        indirect_dto = [_path_result_to_dto(r) for r in indirect]
        direct_dto = [_path_result_to_dto(r) for r in direct]
        weak_dto = [_path_result_to_dto(r) for r in weak]
        wishlist_dto = [_path_result_to_dto(r) for r in wishlist]
        return SearchResponse(
            goal=body.goal,
            intent_summary=intent.summary or body.goal,
            intent_keywords=intent.keywords,
            results=[_path_result_to_dto(r) for r in combined],
            indirect=indirect_dto,
            direct=direct_dto,
            weak=weak_dto,
            wishlist=wishlist_dto,
            targets=indirect_dto,  # deprecated alias
            highlighted_node_ids=nodes,
            highlighted_edge_ids=edges,
        )

    # ---------- person detail
    @app.get("/api/people/{pid}", response_model=PersonDTO)
    def get_person(
        pid: int,
        owner_obj: Annotated[Owner, Depends(verified_owner)],
        repo: Repository = Depends(get_repo),
    ) -> PersonDTO:
        return _person_dto(repo, owner_obj, pid)

    # ---------- create
    @app.post("/api/people", response_model=PersonDTO)
    def create_person(
        body: CreatePersonRequest,
        owner_obj: Annotated[Owner, Depends(verified_owner)],
        repo: Repository = Depends(get_repo),
    ) -> PersonDTO:
        assert owner_obj.id is not None
        me = repo.get_me(owner_id=owner_obj.id)
        if me is None or me.id is None:
            raise HTTPException(400, "Owner has no `me` row.")
        person = Person(
            name=body.name, bio=body.bio, notes=body.notes,
            tags=body.tags, skills=body.skills, companies=body.companies,
            cities=body.cities, needs=body.needs,
            is_wishlist=body.is_wishlist,
        )
        saved = repo.add_person(person)
        assert saved.id is not None
        repo.attach_person_to_owner(saved.id, owner_obj.id)
        repo.add_relationship(Relationship(
            source_id=me.id, target_id=saved.id,
            strength=body.strength_to_me,
            context=body.relation_context,
            frequency=body.frequency,
        ), owner_id=owner_obj.id)
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
        pid: int, body: UpdatePersonRequest,
        owner_obj: Annotated[Owner, Depends(verified_owner)],
        repo: Repository = Depends(get_repo),
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
        return _person_dto(repo, owner_obj, pid)

    # ---------- delete
    @app.delete("/api/people/{pid}")
    def delete_person(pid: int, repo: Repository = Depends(get_repo)) -> dict:
        repo.delete_person(pid)
        return {"deleted": pid}

    # ---------- two-person path
    @app.post("/api/path", response_model=TwoPersonPathResponse)
    def find_paths(
        body: TwoPersonPathRequest,
        owner_obj: Annotated[Owner, Depends(verified_owner)],
        repo: Repository = Depends(get_repo),
    ) -> TwoPersonPathResponse:
        settings = get_settings()
        rels = repo.list_relationships(owner_id=owner_obj.id)
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
    def introductions(
        owner_obj: Annotated[Owner, Depends(verified_owner)],
        repo: Repository = Depends(get_repo),
    ) -> IntroductionsResponse:
        people = repo.list_people(owner_id=owner_obj.id)
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

    # ---------- enrich (LLM-based attribute extraction)
    @app.post("/api/enrich/preview", response_model=EnrichDiff)
    def enrich_preview(
        body: EnrichPreviewRequest,
        owner_obj: Annotated[Owner, Depends(verified_owner)],
        repo: Repository = Depends(get_repo),
    ) -> EnrichDiff:
        """A: 添加联系人对话框里的"AI 解析背景"按钮入口。

        Anonymizes any in-table names mentioned in the input, calls the
        LLM, returns the proposed structured fields. Does NOT write to
        the DB — the SPA fills the chips and the user clicks 保存 next.
        """
        from lodestar.enrich import L1Extractor, LLMClient, LLMError

        assert owner_obj.id is not None
        try:
            client = LLMClient()
        except LLMError as exc:
            raise HTTPException(503, str(exc)) from exc
        extractor = L1Extractor(repo, owner_id=owner_obj.id, client=client)
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

    @app.post("/api/enrich/person/{pid}", response_model=PersonDTO)
    def enrich_person(
        pid: int,
        owner_obj: Annotated[Owner, Depends(verified_owner)],
        only_missing: bool = Query(True),
        repo: Repository = Depends(get_repo),
    ) -> PersonDTO:
        """C: 联系人详情面板里的"AI 重新解析"按钮入口。

        only_missing=True (默认): 只在 companies/cities 为空时跑 LLM；
        only_missing=False: 强制重跑并合并新结果到已有字段（不会删除已有值）。
        """
        from lodestar.enrich import L1Extractor, LLMClient, LLMError

        person = repo.get_person(pid)
        if person is None:
            raise HTTPException(404, f"Person {pid} not found")
        if person.is_me:
            raise HTTPException(400, "不能对 me 节点跑 enrich")

        assert owner_obj.id is not None

        if only_missing and bool(person.companies) and bool(person.cities):
            # Nothing to do; just return the existing person.
            return _person_dto(repo, owner_obj, pid)

        try:
            client = LLMClient()
        except LLMError as exc:
            raise HTTPException(503, str(exc)) from exc
        extractor = L1Extractor(repo, owner_id=owner_obj.id, client=client)
        try:
            diff = extractor.extract_for_person(person)
        except LLMError as exc:
            raise HTTPException(502, f"LLM 调用失败：{exc}") from exc
        if not diff.error and not diff.is_empty():
            extractor.apply([diff])
        return _person_dto(repo, owner_obj, pid)

    @app.post("/api/enrich/owner", response_model=EnrichJobState)
    def enrich_owner_start(
        body: EnrichJobStartRequest,
        owner_obj: Annotated[Owner, Depends(verified_owner)],
        repo: Repository = Depends(get_repo),
    ) -> EnrichJobState:
        """D: 顶栏"批量 AI 解析"入口。

        非阻塞地启动后台 worker。若该 owner 已有任务在跑，返回现存
        task_id（不会并行启动两个）。前端用 GET /api/enrich/status/{id}
        每 2 秒拉一次进度。
        """
        assert owner_obj.id is not None
        # Ensure the LLM client can be constructed before launching the
        # worker — otherwise the user would only see the failure via
        # polling.
        from lodestar.enrich import LLMClient, LLMError

        try:
            LLMClient()
        except LLMError as exc:
            raise HTTPException(503, str(exc)) from exc
        state = enrich_jobs.start(
            owner_id=owner_obj.id,
            owner_slug=owner_obj.slug,
            only_missing=body.only_missing,
        )
        return EnrichJobState(**state.to_dict())

    @app.get("/api/enrich/status/{task_id}", response_model=EnrichJobState)
    def enrich_status(task_id: str) -> EnrichJobState:
        state = enrich_jobs.get(task_id)
        if state is None:
            raise HTTPException(404, f"Unknown enrich task '{task_id}'")
        return EnrichJobState(**state.to_dict())

    # ---------- relationships (browse / NL parse / apply / edit)

    def _owner_name_lookup(
        repo: Repository, owner_id: int
    ) -> tuple[dict[int, str], int | None]:
        """{person_id: name} for everyone visible to this owner, plus
        that owner's me_id (None if missing)."""
        people = repo.list_people(owner_id=owner_id)
        me = repo.get_me(owner_id=owner_id)
        lookup: dict[int, str] = {p.id: p.name for p in people if p.id is not None}
        me_id: int | None = None
        if me is not None and me.id is not None:
            lookup[me.id] = me.name
            me_id = me.id
        return lookup, me_id

    @app.get("/api/relationships", response_model=RelationshipListResponse)
    def list_relationships_endpoint(
        owner_obj: Annotated[Owner, Depends(verified_owner)],
        q: str | None = Query(None),
        min_strength: int | None = Query(None, ge=1, le=5),
        source: str | None = Query(
            None,
            description="Comma-separated subset of {manual,colleague_inferred,ai_inferred}",
        ),
        include_me: bool = Query(
            True,
            description="If False, drop edges that touch the owner's me node.",
        ),
        person_id: int | None = Query(None, description="Only edges touching this person."),
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
        repo: Repository = Depends(get_repo),
    ) -> RelationshipListResponse:
        assert owner_obj.id is not None
        name_lookup, me_id = _owner_name_lookup(repo, owner_obj.id)
        rels = repo.list_relationships(owner_id=owner_obj.id)

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

        # Stable sort: me-edges first, then strength desc, then a_name.
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

    @app.post("/api/relationships/parse", response_model=RelationshipParseResponse)
    def parse_relationships(
        body: RelationshipParseRequest,
        owner_obj: Annotated[Owner, Depends(verified_owner)],
        repo: Repository = Depends(get_repo),
    ) -> RelationshipParseResponse:
        """NL → 关系提案。不写库，只返回提案 + 上下文，让前端确认。"""
        from lodestar.enrich import LLMClient, LLMError, RelationshipParser

        assert owner_obj.id is not None
        try:
            client = LLMClient()
        except LLMError as exc:
            raise HTTPException(503, str(exc)) from exc
        parser = RelationshipParser(repo, owner_id=owner_obj.id, client=client)
        result = parser.parse(body.text)

        if result.error and not result.proposals and not result.unknown_mentions:
            return RelationshipParseResponse(error=result.error)

        name_lookup, me_id = _owner_name_lookup(repo, owner_obj.id)
        all_rels = repo.list_relationships(owner_id=owner_obj.id)
        # 索引现有边方便 O(1) 查 existing_edge：键用无序对。
        edge_index: dict[tuple[int, int], RelationshipDTO] = {}
        # 同时按人聚合所有现有边，用于 context_for。
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

    @app.post(
        "/api/relationships/apply", response_model=RelationshipApplyResponse
    )
    def apply_relationships(
        body: RelationshipApplyRequest,
        owner_obj: Annotated[Owner, Depends(verified_owner)],
        repo: Repository = Depends(get_repo),
    ) -> RelationshipApplyResponse:
        assert owner_obj.id is not None
        owner_pids = repo.list_owner_person_ids(owner_obj.id)
        name_lookup, me_id = _owner_name_lookup(repo, owner_obj.id)

        applied = 0
        skipped = 0
        out_dtos: list[RelationshipDTO] = []
        for item in body.edges:
            if item.a_id == item.b_id:
                skipped += 1
                continue
            # owner 隔离：两端必须都在该 owner 的可见集合里（或是该 owner 的 me）。
            if item.a_id not in owner_pids or item.b_id not in owner_pids:
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
            saved = repo.add_relationship(rel, owner_id=owner_obj.id)
            applied += 1
            dto = _relationship_to_dto(saved, name_lookup, me_id)
            if dto is not None:
                out_dtos.append(dto)
        return RelationshipApplyResponse(
            applied=applied, skipped=skipped, items=out_dtos
        )

    @app.patch(
        "/api/relationships/{rel_id}", response_model=RelationshipDTO
    )
    def update_relationship(
        rel_id: int,
        body: RelationshipUpdateRequest,
        owner_obj: Annotated[Owner, Depends(verified_owner)],
        repo: Repository = Depends(get_repo),
    ) -> RelationshipDTO:
        assert owner_obj.id is not None
        rels = repo.list_relationships(owner_id=owner_obj.id)
        target = next((r for r in rels if r.id == rel_id), None)
        if target is None:
            raise HTTPException(404, f"Relationship {rel_id} not found in this owner.")
        new_rel = Relationship(
            id=target.id,
            source_id=target.source_id,
            target_id=target.target_id,
            strength=body.strength if body.strength is not None else target.strength,
            context=body.context if body.context is not None else target.context,
            frequency=body.frequency if body.frequency is not None else target.frequency,
            source="manual",  # 任何手工编辑都标记为 manual
        )
        saved = repo.add_relationship(new_rel, owner_id=owner_obj.id)
        name_lookup, me_id = _owner_name_lookup(repo, owner_obj.id)
        dto = _relationship_to_dto(saved, name_lookup, me_id)
        if dto is None:
            raise HTTPException(500, "Failed to render updated relationship.")
        return dto

    @app.delete("/api/relationships/{rel_id}")
    def delete_relationship(
        rel_id: int,
        owner_obj: Annotated[Owner, Depends(verified_owner)],
        repo: Repository = Depends(get_repo),
    ) -> dict:
        assert owner_obj.id is not None
        rels = repo.list_relationships(owner_id=owner_obj.id)
        target = next((r for r in rels if r.id == rel_id), None)
        if target is None:
            raise HTTPException(404, f"Relationship {rel_id} not found in this owner.")
        with repo.conn:
            repo.conn.execute("DELETE FROM relationship WHERE id = ?", (rel_id,))
        return {"deleted": rel_id}

    # ---------- stats
    @app.get("/api/stats", response_model=StatsResponse)
    def stats(
        owner_obj: Annotated[Owner, Depends(verified_owner)],
        repo: Repository = Depends(get_repo),
    ) -> StatsResponse:
        assert owner_obj.id is not None
        people = repo.list_people(owner_id=owner_obj.id)
        rels = repo.list_relationships(owner_id=owner_obj.id)
        me = repo.get_me(owner_id=owner_obj.id)
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
