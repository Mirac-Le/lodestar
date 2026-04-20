"""Cross-encoder reranker backed by BAAI/bge-reranker-v2-m3 (本地推理).

为什么单独成文件
----------------

`FlagEmbedding` + `torch` 大小 ~1GB，普通用户跑 CLI 不应该被迫装。所以
这个 module 必须满足：

* 模块顶层不 import 任何 heavy dep，仅当真正构造 :class:`BgeReranker`
  时才在 ``__init__`` 内 lazy-import；
* 所有 import / 模型加载 / 推理失败都在 :func:`build_reranker_from_settings`
  外层捕获，回退到 noop——reranker 永远不能把搜索整体打挂。

如何启用
--------

```bash
uv pip install -e ".[rerank]"
export LODESTAR_RERANKER=bge
# 模型首次加载 ~560MB；国内网络建议预先 export HF_ENDPOINT=https://hf-mirror.com
```
"""

from __future__ import annotations

from dataclasses import dataclass

from lodestar.db.repository import Repository
from lodestar.models import GoalIntent, Person
from lodestar.search.hybrid import Candidate

__all__ = ["BgeReranker"]


@dataclass(frozen=True)
class _CandidateText:
    person_id: int
    text: str
    hybrid_score: float


class BgeReranker:
    """Wrap :class:`FlagReranker` with the project's :class:`Reranker` protocol.

    构造代价（首次 forward 前）= 加载 ~560MB 模型权重 + tokenizer，所以
    建议进程级单例缓存。本类自身保证 reranker 只 init 一次（实例变量
    保存 ``self._model``）。
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        *,
        use_fp16: bool = True,
        max_candidates: int = 30,
        bio_chars: int = 400,
    ) -> None:
        from FlagEmbedding import FlagReranker  # noqa: PLC0415

        self._model = FlagReranker(model_name, use_fp16=use_fp16)
        self._max_candidates = max(1, max_candidates)
        self._bio_chars = max(80, bio_chars)

    def rerank(
        self,
        intent: GoalIntent,
        candidates: list[Candidate],
        repo: Repository,
    ) -> list[Candidate]:
        if not candidates:
            return candidates

        head = candidates[: self._max_candidates]
        tail = candidates[self._max_candidates :]

        items: list[_CandidateText] = []
        for c in head:
            person = repo.get_person(c.person_id)
            if person is None:
                continue
            text = self._compose_text(person)
            if not text:
                continue
            items.append(
                _CandidateText(
                    person_id=c.person_id, text=text, hybrid_score=c.score
                )
            )
        if not items:
            return candidates

        query = (intent.summary or intent.original or "").strip()
        if not query:
            return candidates

        try:
            scores = self._model.compute_score(
                [(query, it.text) for it in items], normalize=True
            )
        except Exception:
            return candidates

        if isinstance(scores, (int, float)):
            scores = [float(scores)]
        else:
            scores = [float(s) for s in scores]

        scored = sorted(
            zip(items, scores, strict=False),
            key=lambda kv: (kv[1], kv[0].hybrid_score),
            reverse=True,
        )
        max_score = max((s for _, s in scored), default=0.0) or 1.0
        head_out = [
            Candidate(person_id=it.person_id, score=s / max_score)
            for it, s in scored
        ]
        return head_out + tail

    def _compose_text(self, person: Person) -> str:
        parts: list[str] = [person.name]
        if person.bio:
            parts.append(person.bio[: self._bio_chars])
        if person.tags:
            parts.append("标签：" + "、".join(person.tags[:8]))
        if person.companies:
            parts.append("公司：" + "、".join(person.companies[:6]))
        if person.cities:
            parts.append("城市：" + "、".join(person.cities[:3]))
        return " | ".join(parts)
