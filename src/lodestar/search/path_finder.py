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

    def __init__(self, repo: Repository, max_hops: int = 3) -> None:
        self._repo = repo
        self._max_hops = max_hops
        self._graph: nx.Graph | None = None
        self._person_cache: dict[int, Person] = {}

    def rank(self, candidates: list[Candidate]) -> list[PathResult]:
        """Score and sort candidates by combined_score, best first.

        Each candidate also receives a `path_kind` label:

            direct  -- the user already has a strong (≥2) Me-edge
            weak    -- the user has a Me-edge but it's strength=1 only
            target  -- the user has NO Me-edge; reachable only via peers

        Hops are penalised, but `target` candidates use a softer hop
        penalty (linear instead of super-linear) because the entire point
        of a target candidate is the chain through intermediaries.
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

            kind = self._classify_path_kind(graph, me.id, cand.person_id)

            path_info = self._best_path(graph, me.id, cand.person_id)
            if path_info is None:
                # Unreachable: don't surface garbage. The user can't act on
                # someone they have no chain to.
                continue
            steps, path_strength = path_info
            hops = max(len(steps) - 1, 1)

            # Combined score: relevance is the primary signal. Strength only
            # nudges ranking within the same hop count (≤ ±10 %).
            avg_strength = path_strength / float(hops) if hops else 0.0
            strength_factor = 0.9 + 0.1 * (avg_strength / 5.0)  # 0.9 → 1.0

            # Softer hop penalty for "target" results (whole point is multi-hop).
            if kind == "target":
                hop_factor = 1.0 / (float(hops) ** 0.5)
            else:
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

    def _classify_path_kind(self, graph: nx.Graph, me_id: int, pid: int) -> str:
        """Tag based on the directness of Me's relation to this person."""
        if pid not in graph or me_id not in graph:
            return "target"
        if not graph.has_edge(me_id, pid):
            return "target"
        edge = graph.edges[me_id, pid]
        return "weak" if int(edge.get("strength", 3)) <= 1 else "direct"

    def _build_graph(self) -> nx.Graph:
        if self._graph is not None:
            return self._graph
        g: nx.Graph = nx.Graph()
        for rel in self._repo.list_relationships():
            weight = 1.0 / max(rel.strength, 1)
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
