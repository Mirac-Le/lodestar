"""Stage-2 reranker: refine HybridSearch candidates before PathFinder.

设计要点
--------

HybridSearch 是 bi-encoder + 关键词的"宽召回"，对"同一行业不同角色"
（量化私募 *老总* vs 量化私募 *资源对接*）几乎打成同分——这是 bi-encoder
语义压缩的天花板。本模块在 hybrid 与 PathFinder 之间插入一层可插拔的
**Stage-2 Reranker**，让真正符合目标角色的人上浮。

三档实现：

* :class:`NoopReranker` —— 等价于关闭 reranker，保持现状行为。
* :class:`LLMJudgeReranker` —— Qwen 等 LLM 看 intent + 候选 bio 打分，
  额外把每人分类成 ``本人 / 桥梁 / 无关``，最终分 = role_weight × llm_score。
* :class:`BgeReranker` —— 本地 cross-encoder，住在 ``bge_reranker.py``，
  用 ``LODESTAR_RERANKER=bge`` 才会被 lazy-import，不污染主依赖链。

调用端契约：所有 reranker 都不能让搜索整体崩。LLM 调用失败、模型加载
失败时一律 fallback 到原顺序，并把异常吞在自己内部。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from lodestar.db.repository import Repository
from lodestar.enrich.anonymizer import Anonymizer
from lodestar.llm.base import LLMClient
from lodestar.models import GoalIntent, Person
from lodestar.search.hybrid import Candidate

__all__ = [
    "LLMJudgeReranker",
    "NoopReranker",
    "Reranker",
    "build_reranker_from_settings",
]


@runtime_checkable
class Reranker(Protocol):
    """Stage-2 reranker protocol.

    输入：上游 hybrid 召回的候选列表（按上游 score 已排序）。
    输出：重排后的候选列表，长度 ≤ 输入长度（一般等长）。
    """

    def rerank(
        self,
        intent: GoalIntent,
        candidates: list[Candidate],
        repo: Repository,
    ) -> list[Candidate]: ...


class NoopReranker:
    """Identity reranker — returns input as-is. Used when reranker is disabled."""

    def rerank(
        self,
        intent: GoalIntent,
        candidates: list[Candidate],
        repo: Repository,
    ) -> list[Candidate]:
        return candidates


# --------------------------------------------------------------------- LLMJudge

# 角色 → 权重：本人是目标重点；桥梁可以引荐但不是终点；无关压到底但
# 不删除，避免极端情况下 reranker 把所有候选打成「无关」造成空结果。
_ROLE_WEIGHTS: dict[str, float] = {
    "本人": 1.0,
    "桥梁": 0.5,
    "无关": 0.05,
}

_DEFAULT_ROLE = "无关"

_LLM_SYSTEM_PROMPT = """你是一个人脉路径规划助手，正在帮用户从他个人通讯录里挑出最适合
帮他完成目标的联系人。你拿到的是：

* user 段是 JSON：``goal`` + ``intent`` 描述目标和理想"帮手画像"，
  ``candidates`` 是已经初筛出来的候选人匿名档案。
* 每个候选人用 ``Pxxx`` token 代号，``bio_excerpt`` / ``tags`` /
  ``companies`` / ``cities`` 是他的事实型字段。

你需要为每个候选人输出两个判断：

1. **role 分类**（必填，三选一）：
   - ``本人``：候选人本身就是 goal 描述的目标角色（例如目标想找
     量化私募老总，他自己就是量化私募基金的 GP / MD / 总经理）。
   - ``桥梁``：候选人不是目标角色，但他在该行业 / 圈子里有资源、能引荐
     真正的本人（例如卖方对接型、行业研究员、媒体）。
   - ``无关``：候选人跟 goal 没有可观察的强关联，纯属召回噪声。

2. **score 打分**（0–1 之间的浮点数）：在该 role 内部还要再排序，
   越合适越接近 1。如果两个人都是「本人」，更资深 / 更对口的给更高分。

输出严格 JSON，shape 必须是：

```json
{"ranking":[{"id":"P012","role":"本人","score":0.95},{"id":"P003","role":"桥梁","score":0.7}]}
```

约束：
* 每个候选都要出现在 ``ranking`` 里，不能遗漏；
* 只用我提供的 ``Pxxx`` token 作 id，不要发明新 token；
* **只输出 id / role / score 三个字段，不要写 reason 或其他解释**——
  下游程序只读这三个字段，多写的字符纯粹浪费 latency；
* 如果 bio 信息不足以判断，给 ``role=无关`` + 低分，不要硬猜。
"""


@dataclass(frozen=True)
class _JudgeVerdict:
    role: str
    score: float


class LLMJudgeReranker:
    """Use an OpenAI-compatible LLM as a stage-2 judge.

    流程：

    1. 把每个候选人的姓名 / 公司脱敏成 ``Pxxx`` / ``Cxxx``（复用现有
       :class:`Anonymizer`），bio / tags / companies / cities 一起送 LLM。
    2. LLM 返回 ``{ranking: [{id, role, score, reason}]}``，三选一 role
       分类 + 0-1 打分。
    3. 反脱敏，按 ``role_weight × llm_score`` 重新排序，原 hybrid score
       作为 tiebreaker（保留进 ``Candidate.score``，避免下游 PathFinder
       拿到一堆 1.0 假分）。
    4. 任何异常（LLM 超时 / JSON 解析失败 / API key 缺失）都吞在内部，
       fallback 到 candidates 原顺序——reranker 永远不能把整个搜索打挂。
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        max_candidates: int = 30,
        bio_chars: int = 400,
        role_weights: dict[str, float] | None = None,
    ) -> None:
        self._llm = llm
        self._max_candidates = max(1, max_candidates)
        self._bio_chars = max(80, bio_chars)
        self._role_weights = dict(role_weights or _ROLE_WEIGHTS)

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

        people: list[Person] = []
        for c in head:
            p = repo.get_person(c.person_id)
            if p is not None:
                people.append(p)

        if not people:
            return candidates

        anonymizer = self._build_anonymizer(people)
        payload = self._build_user_payload(intent, head, people, anonymizer)

        try:
            raw = self._llm.complete_json(_LLM_SYSTEM_PROMPT, payload)
            verdicts = self._parse_verdicts(raw, anonymizer)
        except Exception:
            return candidates

        if not verdicts:
            return candidates

        rescored: list[tuple[Candidate, float]] = []
        for c in head:
            v = verdicts.get(c.person_id)
            if v is None:
                rescored.append((c, c.score * self._role_weights.get(_DEFAULT_ROLE, 0.05)))
                continue
            weight = self._role_weights.get(v.role, self._role_weights.get(_DEFAULT_ROLE, 0.05))
            rescored.append((c, weight * v.score))

        rescored.sort(key=lambda kv: (kv[1], kv[0].score), reverse=True)
        max_score = max((s for _, s in rescored), default=0.0) or 1.0
        head_out = [
            Candidate(person_id=c.person_id, score=s / max_score)
            for c, s in rescored
        ]
        return head_out + tail

    # ------------------------------------------------------------- internals
    def _build_anonymizer(self, people: list[Person]) -> Anonymizer:
        return Anonymizer.from_people_and_companies(
            me_id=0,
            me_name="__me__",
            people=[(p.id or 0, p.name) for p in people if p.id is not None],
            companies=sorted({c for p in people for c in p.companies if c}),
        )

    def _build_user_payload(
        self,
        intent: GoalIntent,
        head: list[Candidate],
        people: list[Person],
        anonymizer: Anonymizer,
    ) -> str:
        people_by_id = {p.id: p for p in people if p.id is not None}
        items: list[dict[str, object]] = []
        for c in head:
            p = people_by_id.get(c.person_id)
            if p is None or p.id is None:
                continue
            token = anonymizer.token_for_person(p.id) or f"P???{p.id}"
            bio = anonymizer.anonymize_text(p.bio or "") or ""
            companies = [
                anonymizer.anonymize_company(name) or name for name in p.companies
            ]
            items.append(
                {
                    "id": token,
                    "bio_excerpt": bio[: self._bio_chars],
                    "tags": p.tags[:8],
                    "companies": companies[:6],
                    "cities": p.cities[:3],
                    "hybrid_score": round(c.score, 4),
                }
            )

        payload = {
            "goal": intent.original,
            "intent": {
                "summary": intent.summary,
                "helper_roles": intent.roles,
                "helper_industries": intent.industries,
                "helper_skills": intent.skills,
                "topic_keywords": intent.keywords,
                "cities": intent.cities,
            },
            "candidates": items,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _parse_verdicts(
        self, raw: str, anonymizer: Anonymizer
    ) -> dict[int, _JudgeVerdict]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        ranking = data.get("ranking")
        if not isinstance(ranking, list):
            return {}

        out: dict[int, _JudgeVerdict] = {}
        for entry in ranking:
            if not isinstance(entry, dict):
                continue
            token = entry.get("id")
            if not isinstance(token, str):
                continue
            pid = anonymizer.person_id_for_token(token.strip())
            if pid is None:
                continue
            role = str(entry.get("role") or _DEFAULT_ROLE).strip()
            if role not in self._role_weights:
                role = _DEFAULT_ROLE
            try:
                score = float(entry.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            score = max(0.0, min(1.0, score))
            out[pid] = _JudgeVerdict(role=role, score=score)
        return out


# ------------------------------------------------------------------- factory


def build_reranker_from_settings() -> Reranker:
    """Build the reranker chosen by `LODESTAR_RERANKER` env var.

    * ``none`` (default) → :class:`NoopReranker`
    * ``llm``            → :class:`LLMJudgeReranker` 用配置好的 LLM
    * ``bge``            → 懒加载 :class:`bge_reranker.BgeReranker`，
                            缺少可选依赖时打印一次 warning 并 fallback 到 noop
    """
    from lodestar.config import get_settings

    settings = get_settings()
    choice = (getattr(settings, "reranker", "none") or "none").lower()

    if choice == "llm":
        try:
            from lodestar.llm import get_llm_client

            return LLMJudgeReranker(get_llm_client())
        except Exception:
            return NoopReranker()

    if choice == "bge":
        try:
            from lodestar.search.bge_reranker import BgeReranker

            return BgeReranker()
        except Exception:
            return NoopReranker()

    return NoopReranker()
