"""Vector + keyword hybrid retrieval using Reciprocal Rank Fusion."""

from __future__ import annotations

from dataclasses import dataclass

from lodestar.db.repository import Repository
from lodestar.embedding.base import EmbeddingClient
from lodestar.models import GoalIntent


@dataclass(frozen=True)
class Candidate:
    """A candidate person with relevance score in [0, 1]."""

    person_id: int
    score: float


class HybridSearch:
    """Combines semantic vector search with keyword matching.

    Fusion uses weighted reciprocal-rank fusion (RRF), where each source
    gets its own weight. Helper-describing fields (roles, industries,
    skills) count strongly; topic keywords count weakly — otherwise the
    shared topic vocabulary (e.g. "AI", "算力") between a goal and its
    *peers' bios* drowns out the signal from the goal's *helper profile*.

    Weights (sum ≈ 3.6):
        vector           : 1.5   — dense match against helper_description
        helper keyword   : 1.5   — roles + industries + skills LIKE match
        topic keyword    : 0.3   — topic/city LIKE match, weak nudge only
    """

    def __init__(
        self,
        repo: Repository,
        embedder: EmbeddingClient | None,
        rrf_k: int = 60,
        owner_id: int | None = None,
    ) -> None:
        self._repo = repo
        self._embedder = embedder
        self._rrf_k = rrf_k
        self._owner_id = owner_id

    def search(
        self,
        intent: GoalIntent,
        top_k: int = 20,
        recall_k: int | None = None,
    ) -> list[Candidate]:
        """Return ranked candidates best matching the goal.

        ``top_k`` 控制最终返回长度（喂给 PathFinder / UI 的列表长度）。
        ``recall_k`` 控制召回宽度——当后续要插入 Stage-2 reranker 时，
        我们需要先召回更宽的候选池让 reranker 重排，再截到 ``top_k``。

        默认 ``recall_k = top_k`` 保持现有行为；调用方有 reranker 时
        应显式传 ``recall_k=settings.reranker_recall_k``。
        """
        recall = max(top_k, recall_k or top_k)
        vec_ranks = self._vector_ranks(intent, limit=recall * 2)
        helper_ranks = self._helper_keyword_ranks(intent)
        topic_ranks = self._topic_keyword_ranks(intent)

        fused: dict[int, float] = {}
        self._fuse(fused, vec_ranks, weight=1.5)
        self._fuse(fused, helper_ranks, weight=1.5)
        self._fuse(fused, topic_ranks, weight=0.3)

        if not fused:
            return []

        max_score = max(fused.values())
        ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
        return [
            Candidate(person_id=pid, score=score / max_score)
            for pid, score in ordered[:recall]
        ]

    def _fuse(
        self,
        fused: dict[int, float],
        ranks: dict[int, int],
        *,
        weight: float,
    ) -> None:
        for pid, rank in ranks.items():
            fused[pid] = fused.get(pid, 0.0) + weight / (self._rrf_k + rank)

    def _vector_ranks(self, intent: GoalIntent, limit: int) -> dict[int, int]:
        if self._embedder is None:
            return {}
        query_text = intent.summary or intent.original
        if not query_text.strip():
            return {}
        try:
            vector = self._embedder.embed(query_text)
        except Exception:
            return {}
        hits = self._repo.vector_search(
            vector, limit=limit, owner_id=self._owner_id
        )
        return {pid: rank + 1 for rank, (pid, _dist) in enumerate(hits)}

    def _helper_keyword_ranks(self, intent: GoalIntent) -> dict[int, int]:
        terms: list[str] = []
        terms.extend(intent.roles)
        terms.extend(intent.industries)
        terms.extend(intent.skills)
        return self._rank_terms(terms)

    def _topic_keyword_ranks(self, intent: GoalIntent) -> dict[int, int]:
        terms: list[str] = []
        terms.extend(intent.keywords)
        terms.extend(intent.cities)
        return self._rank_terms(terms)

    def _rank_terms(self, terms: list[str]) -> dict[int, int]:
        if not terms:
            return {}
        scores = self._repo.keyword_candidates(terms, owner_id=self._owner_id)
        if not scores:
            return {}
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return {pid: rank + 1 for rank, (pid, _hits) in enumerate(ordered)}
