"""Build a NetworkX graph from the DB and compute path recommendations."""

from __future__ import annotations

import networkx as nx

from lodestar.db.repository import Repository
from lodestar.models import PathResult, PathStep, Person
from lodestar.search.hybrid import Candidate


class PathFinder:
    """Finds the best path from 'me' to each candidate.

    Builds an undirected NetworkX graph on demand so relationships stored
    as either (me, X) or (X, me) both work.
    """

    def __init__(
        self,
        repo: Repository,
        max_hops: int = 3,
        weak_me_floor: int = 4,
    ) -> None:
        self._repo = repo
        self._max_hops = max_hops
        self._weak_me_floor = max(1, min(5, weak_me_floor))
        self._graph: nx.Graph | None = None
        self._person_cache: dict[int, Person] = {}

    def rank(self, candidates: list[Candidate]) -> list[PathResult]:
        """Score and sort candidates by combined_score, best first.

        Each candidate also receives a `path_kind` label that is **purely
        topological** — it does not encode the user's `is_wishlist`
        curation, which is carried separately on the Person record:

            direct   -- 1-hop strong Me-edge (strength ≥ weak_me_floor)
            weak     -- 1-hop Me-edge that fell below `weak_me_floor`
            indirect -- shortest path goes through intermediaries

        The classification is derived from the path actually chosen by
        `_best_path`, **not** from "does a Me edge exist". This lets
        `_build_graph` quietly add a weight penalty to weak Me edges so
        that, whenever a more trustworthy multi-hop chain exists, the
        algorithm prefers it. Result: a contact you only nodded at once
        will be presented as "需要引荐" (indirect) when a stronger mutual
        friend is available; only when no such bridge exists does the
        weak direct edge survive as a `weak` 1-hop fallback.
        """
        me = self._repo.get_me()
        if me is None or me.id is None:
            raise RuntimeError("No 'me' record. Run `lodestar init` first.")

        graph = self._build_graph()
        results: list[PathResult] = []
        for cand in candidates:
            if cand.person_id == me.id:
                continue
            target = self._get_person_cached(cand.person_id)
            if target is None:
                continue

            path_info = self._best_path(graph, me.id, cand.person_id)
            if path_info is None:
                # Unreachable: don't surface garbage. The user can't act on
                # someone they have no chain to.
                continue
            steps, path_strength = path_info
            hops = max(len(steps) - 1, 1)
            kind = self._classify_from_steps(steps)

            # Combined score: relevance is the primary signal. Strength only
            # nudges ranking within the same hop count (≤ ±10 %).
            avg_strength = path_strength / float(hops) if hops else 0.0
            strength_factor = 0.9 + 0.1 * (avg_strength / 5.0)  # 0.9 → 1.0
            hop_factor = 1.0 / (float(hops) ** 1.3)

            combined = cand.score * strength_factor * hop_factor
            results.append(
                PathResult(
                    target=target,
                    path=steps,
                    relevance_score=cand.score,
                    path_strength=path_strength,
                    combined_score=combined,
                    rationale=_make_rationale(target, cand.score, hops, path_strength, kind),
                    path_kind=kind,
                )
            )
        results.sort(key=lambda r: r.combined_score, reverse=True)
        return results

    def _classify_from_steps(self, steps: list[PathStep]) -> str:
        """Classify based on the path the algorithm actually picked.

        A 1-hop path inherits its single edge's effective strength; if it
        sits below `weak_me_floor` the edge survived only because no
        stronger multi-hop alternative exists. Any path with > 1 hop is
        inherently `indirect`.
        """
        if len(steps) <= 1:
            return "indirect"
        if len(steps) == 2:
            s = int(steps[1].strength or 0)
            return "weak" if s < self._weak_me_floor else "direct"
        return "indirect"

    def _build_graph(self) -> nx.Graph:
        if self._graph is not None:
            return self._graph
        me = self._repo.get_me()
        me_id = me.id if me else None

        # Penalty multiplier on weak Me edges. Picked large enough that
        # ANY two-hop chain through reasonably-trusted contacts (each
        # ≥ floor) wins on shortest_path: the worst two-hop weight is
        # 2 * (1 / floor); we want `multiplier / strength` to dominate
        # that, hence we scale by max_hops^2 to stay safe up to 5 hops.
        weak_penalty = float(self._max_hops * self._max_hops * 4)

        g: nx.Graph = nx.Graph()
        for rel in self._repo.list_relationships():
            base = 1.0 / max(rel.strength, 1)
            weight = base
            if (
                me_id is not None
                and (rel.source_id == me_id or rel.target_id == me_id)
                and rel.strength < self._weak_me_floor
            ):
                # Inflate weak Me edges so shortest_path will route through
                # any plausible mutual friend. Falls back to this edge only
                # when no alternative exists in the graph.
                weight = base * weak_penalty
            g.add_edge(
                rel.source_id,
                rel.target_id,
                weight=weight,
                strength=rel.strength,
                context=rel.context or "",
            )
        self._graph = g
        return g

    def _best_path(
        self, graph: nx.Graph, source: int, target: int
    ) -> tuple[list[PathStep], float] | None:
        if source not in graph or target not in graph:
            return None
        try:
            node_ids: list[int] = nx.shortest_path(
                graph, source=source, target=target, weight="weight"
            )
        except nx.NetworkXNoPath:
            return None

        # Weighted shortest path may exceed max_hops because `_build_graph`
        # inflates weak Me edges by a large penalty: a 1-hop weak direct edge
        # can lose to a 4-hop chain through stronger hubs. When that happens,
        # *don't* return None — fall back to the unweighted (topological)
        # shortest path so weakly-connected contacts at the edge of the graph
        # still surface as a `weak` 1-hop result instead of vanishing entirely.
        # Concretely: 俞汉清/李坤 only neighbour Me through a strength=1 edge
        # plus other peers who themselves only reach Me via strength=1 — every
        # weighted path is heavily penalised and may overshoot max_hops, but
        # the user definitely *does* know them directly.
        if len(node_ids) - 1 > self._max_hops:
            try:
                node_ids = nx.shortest_path(
                    graph, source=source, target=target,
                )
            except nx.NetworkXNoPath:
                return None
            if len(node_ids) - 1 > self._max_hops:
                return None

        steps: list[PathStep] = []
        total_strength = 0.0
        for idx, node_id in enumerate(node_ids):
            person = self._get_person_cached(node_id)
            if person is None:
                return None
            if idx == 0:
                steps.append(PathStep(person_id=node_id, name=person.name))
                continue
            edge = graph.edges[node_ids[idx - 1], node_id]
            strength = int(edge["strength"])
            total_strength += strength
            steps.append(
                PathStep(
                    person_id=node_id,
                    name=person.name,
                    relation_from_previous=str(edge["context"]) or None,
                    strength=strength,
                )
            )
        return steps, total_strength

    def _get_person_cached(self, person_id: int) -> Person | None:
        if person_id not in self._person_cache:
            p = self._repo.get_person(person_id)
            if p is not None:
                self._person_cache[person_id] = p
        return self._person_cache.get(person_id)


def _make_rationale(
    target: Person,
    rel_score: float,
    hops: int,
    path_strength: float,
    kind: str = "direct",
) -> str:
    attrs: list[str] = []
    if target.tags:
        attrs.append("tags=" + "/".join(target.tags[:3]))
    if target.skills:
        attrs.append("skills=" + "/".join(target.skills[:3]))
    if target.companies:
        attrs.append("at=" + "/".join(target.companies[:2]))
    attr_str = f" [{', '.join(attrs)}]" if attrs else ""
    return (
        f"kind={kind}, relevance={rel_score:.2f}, "
        f"hops={hops}, path_strength={path_strength:.0f}{attr_str}"
    )
