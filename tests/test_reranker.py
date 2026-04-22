"""Unit tests for the Stage-2 reranker layer."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from lodestar.db import Repository
from lodestar.models import GoalIntent, Person
from lodestar.search.hybrid import Candidate
from lodestar.search.reranker import (
    LLMJudgeReranker,
    NoopReranker,
    build_reranker_from_settings,
)


class _ScriptedLLM:
    """LLM stub that returns pre-baked JSON for each `complete_json` call."""

    def __init__(self, responses: list[str]) -> None:
        self._responses: Iterator[str] = iter(responses)
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        try:
            return next(self._responses)
        except StopIteration as exc:  # noqa: BLE001
            raise RuntimeError("scripted LLM exhausted") from exc

    def complete(self, system: str, user: str) -> str:  # pragma: no cover
        raise NotImplementedError


def _seed(repo: Repository) -> tuple[int, int, int]:
    """Three contacts: Alice = 本人, Bob = 桥梁, Carol = 无关."""
    me = repo.ensure_me(name="Me")
    assert me.id is not None
    a = repo.add_person(
        Person(
            name="Alice",
            bio="量化私募基金合伙人，管理多只市场中性策略产品",
            tags=["量化", "私募"],
            companies=["A 资产"],
        )
    )
    b = repo.add_person(
        Person(
            name="Bob",
            bio="券商衍生品销售，对接量化私募客户",
            tags=["券商", "衍生品"],
            companies=["B 证券"],
        )
    )
    c = repo.add_person(
        Person(
            name="Carol",
            bio="美妆品牌创始人，主打线下快闪",
            tags=["消费", "品牌"],
            companies=["C 美妆"],
        )
    )
    assert a.id is not None and b.id is not None and c.id is not None
    return a.id, b.id, c.id


def test_noop_reranker_is_identity(repo: Repository) -> None:
    a, b, c = _seed(repo)
    cands = [
        Candidate(person_id=a, score=0.5),
        Candidate(person_id=b, score=0.4),
        Candidate(person_id=c, score=0.3),
    ]
    out = NoopReranker().rerank(GoalIntent(original="x"), cands, repo)
    assert [x.person_id for x in out] == [a, b, c]


def test_llm_judge_promotes_本人_over_桥梁(repo: Repository) -> None:
    """Hybrid 把 Bob (桥梁) 排第一，LLM judge 应把 Alice (本人) 顶上来。"""
    a, b, c = _seed(repo)

    intent = GoalIntent(
        original="我想找量化私募老总",
        roles=["合伙人", "总经理"],
        industries=["量化", "私募基金"],
        summary="一位实际管理量化私募基金的合伙人",
    )

    # Hybrid 给的候选顺序：Bob > Alice > Carol（典型断崖问题：桥梁居首）
    cands = [
        Candidate(person_id=b, score=0.95),
        Candidate(person_id=a, score=0.93),
        Candidate(person_id=c, score=0.20),
    ]

    # LLM judge 输出（用 Pxxx token，Anonymizer 内部按 people 顺序分配
    # P001/P002/P003 给 Bob/Alice/Carol —— 与 head 候选顺序一致）。
    response = json.dumps(
        {
            "ranking": [
                {"id": "P002", "role": "本人", "score": 0.95, "reason": "管理量化私募"},
                {"id": "P001", "role": "桥梁", "score": 0.7, "reason": "对接量化客户"},
                {"id": "P003", "role": "无关", "score": 0.1, "reason": "美妆"},
            ]
        },
        ensure_ascii=False,
    )
    llm = _ScriptedLLM([response])
    reranker = LLMJudgeReranker(llm)

    out = reranker.rerank(intent, cands, repo)

    assert [x.person_id for x in out] == [a, b, c]
    assert out[0].score == pytest.approx(1.0)
    assert out[2].score < out[1].score
    assert llm.calls, "LLM judge should have been invoked exactly once"


def test_llm_judge_falls_back_on_llm_error(repo: Repository) -> None:
    """LLM 抛异常时 reranker 必须 fallback 到原顺序，不能把搜索打挂。"""
    a, b, c = _seed(repo)
    cands = [
        Candidate(person_id=b, score=0.9),
        Candidate(person_id=a, score=0.8),
        Candidate(person_id=c, score=0.1),
    ]

    class _BoomLLM:
        def complete_json(self, *_a: str, **_kw: str) -> str:
            raise RuntimeError("upstream timeout")

        def complete(self, *_a: str, **_kw: str) -> str:  # pragma: no cover
            raise NotImplementedError

    out = LLMJudgeReranker(_BoomLLM()).rerank(
        GoalIntent(original="x"), cands, repo
    )
    assert out == cands


def test_llm_judge_handles_malformed_json(repo: Repository) -> None:
    a, _b, _c = _seed(repo)
    cands = [Candidate(person_id=a, score=0.9)]
    llm = _ScriptedLLM(["this is not json"])
    out = LLMJudgeReranker(llm).rerank(GoalIntent(original="x"), cands, repo)
    assert out == cands


def test_llm_judge_drops_unknown_tokens(repo: Repository) -> None:
    """LLM 出现没发出过的 P999 token 时不应被信任，相应人按默认 role 算分。"""
    a, b, _c = _seed(repo)
    cands = [Candidate(person_id=a, score=0.9), Candidate(person_id=b, score=0.8)]
    response = json.dumps(
        {
            "ranking": [
                {"id": "P999", "role": "本人", "score": 1.0, "reason": "fake"},
                {"id": "P001", "role": "本人", "score": 0.9, "reason": ""},
            ]
        }
    )
    out = LLMJudgeReranker(_ScriptedLLM([response])).rerank(
        GoalIntent(original="x"), cands, repo
    )
    # Both candidates remain; P999 fabrication ignored.
    assert {c.person_id for c in out} == {a, b}


def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """关掉 .env 文件加载，避免开发机里的 ``LODESTAR_*`` 漏进 settings。

    pydantic-settings 的优先级是 init kwargs > env vars > .env 文件 > defaults，
    单独 ``monkeypatch.delenv`` 只清进程 env，``.env`` 仍会被读到——这一直
    是 test_build_reranker_factory_* 的 flake 来源。这里把 model_config 里
    的 ``env_file`` 暂时打成 None，这次测试就不再读 ``.env``。
    """
    from lodestar import config

    new_cfg = dict(config.Settings.model_config)
    new_cfg["env_file"] = None
    monkeypatch.setattr(config.Settings, "model_config", new_cfg)


def test_build_reranker_factory_defaults_to_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env var → NoopReranker; bad LLM config gracefully falls back too."""
    from lodestar import config

    _isolate_settings(monkeypatch)
    monkeypatch.delenv("LODESTAR_RERANKER", raising=False)
    config.reset_settings()
    try:
        assert isinstance(build_reranker_from_settings(), NoopReranker)
    finally:
        config.reset_settings()


def test_build_reranker_factory_llm_no_apikey_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lodestar import config

    _isolate_settings(monkeypatch)
    monkeypatch.setenv("LODESTAR_RERANKER", "llm")
    monkeypatch.setenv("LODESTAR_LLM_API_KEY", "")
    config.reset_settings()
    try:
        # Missing API key → get_llm_client raises → factory falls back to noop
        assert isinstance(build_reranker_from_settings(), NoopReranker)
    finally:
        config.reset_settings()


def test_bge_reranker_optional_dep_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """BgeReranker 在 FlagEmbedding 已装时可被构造；模型加载是 lazy 的。

    真去 ``BAAI/bge-reranker-v2-m3``（~560MB）下载会让 CI / 裸网开发机
    挂住，所以这里 stub 掉 ``FlagReranker`` 的真实加载，只验证我们的
    lazy-import 链路本身没断。
    """
    pytest.importorskip("FlagEmbedding")
    import FlagEmbedding

    class _StubFlagReranker:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def compute_score(
            self, _pairs: list[tuple[str, str]], normalize: bool = True
        ) -> list[float]:
            return [0.5 for _ in _pairs]

    monkeypatch.setattr(FlagEmbedding, "FlagReranker", _StubFlagReranker)

    from lodestar.search.bge_reranker import BgeReranker

    bge = BgeReranker()
    assert bge is not None
