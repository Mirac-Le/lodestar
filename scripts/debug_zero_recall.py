"""Diagnose why specific queries return empty top-5.

针对 ``docs/eval_2026-04-21.md`` 里三档 reranker 都 R@5=0 的 query：把
``HybridSearch`` 的三路召回拆开打印，看 ``expected`` 名单上的每个人**到底卡在哪一步**：

- 向量距离没进 top-K  → bio embedding 与目标语义太远（描述不够直白）
- helper-keyword 没命中 → LLM intent 抽取的 roles/industries/skills 与 bio 措辞不符
- topic-keyword 没命中 → 兜底关键词太弱 / 名字与 bio 完全没字面交集
- 三路全无           → 这个人压根没 bio 或不在该 owner 网络

输出落地：
    docs/zero_recall_2026-04-21.md
    （便于 follow-up 时溯源，每条 query 一节，包含 intent + 三路 top 30）

用法：
    uv run python scripts/debug_zero_recall.py
"""

from __future__ import annotations

import sys
from collections import OrderedDict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from lodestar.config import get_settings  # noqa: E402
from lodestar.db import Repository, connect, init_schema  # noqa: E402
from lodestar.embedding import get_embedding_client  # noqa: E402
from lodestar.llm import GoalParser, get_llm_client  # noqa: E402
from lodestar.models import GoalIntent  # noqa: E402
from lodestar.search import HybridSearch  # noqa: E402

# 目标四条 query：取自 tests/fixtures/golden_queries.yaml
TARGETS: list[dict] = [
    {
        "id": "r-ambig-1",
        "owner": "richard",
        "goal": "我想了解政府监管动态，认识能打招呼的政府关系",
        "expected": ["廉向金", "宋伟", "陈宝岩", "蔡长余"],
    },
    {
        "id": "r-onehop-1",
        "owner": "richard",
        "goal": "我想找能直接借钱的核心铁磁朋友",
        "expected": ["大钊", "建国哥", "崔宁", "纪少敏"],
    },
    {
        "id": "t-longt-1",
        "owner": "tommy",
        "goal": "我想找企业法务 / 合规方向的律师",
        "expected": ["王浚哲"],
    },
    {
        "id": "t-onehop-2",
        "owner": "tommy",
        "goal": "我想做券商通道 / 渠道业务的对接",
        "expected": ["张千千", "徐楷", "戎捷", "黄达", "李峰屏", "李紫祎"],
    },
]


def open_repo() -> Repository:
    s = get_settings()
    conn = connect(s.db_path)
    init_schema(conn, embedding_dim=s.embedding_dim)
    return Repository(conn)


def find_person_by_name(repo: Repository, owner_id: int, name: str):
    rows = repo.conn.execute(
        """
        SELECT p.id, p.name, p.bio, p.is_me
        FROM person p
        JOIN person_owner po ON po.person_id = p.id AND po.owner_id = ?
        WHERE p.name = ?
        """,
        (owner_id, name),
    ).fetchall()
    return [dict(r) for r in rows]


def vector_ranks(
    repo: Repository,
    embedder,
    intent: GoalIntent,
    owner_id: int,
    limit: int,
) -> "OrderedDict[int, tuple[int, float]]":
    """Return ordered {pid: (rank, distance)} from sqlite-vec."""
    text = intent.summary or intent.original
    vec = embedder.embed(text)
    hits = repo.vector_search(vec, limit=limit, owner_id=owner_id)
    return OrderedDict((pid, (rank + 1, dist)) for rank, (pid, dist) in enumerate(hits))


def helper_terms(intent: GoalIntent) -> list[str]:
    return [t for t in (*intent.roles, *intent.industries, *intent.skills) if t.strip()]


def topic_terms(intent: GoalIntent) -> list[str]:
    return [t for t in (*intent.keywords, *intent.cities) if t.strip()]


def kw_ranks(repo: Repository, terms: list[str], owner_id: int) -> "OrderedDict[int, tuple[int, int]]":
    if not terms:
        return OrderedDict()
    scores = repo.keyword_candidates(terms, owner_id=owner_id)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return OrderedDict((pid, (rank + 1, hits)) for rank, (pid, hits) in enumerate(ordered))


def render_query(
    out: list[str],
    target: dict,
    intent: GoalIntent,
    repo: Repository,
    owner_id: int,
    vec: "OrderedDict[int, tuple[int, float]]",
    helper: "OrderedDict[int, tuple[int, int]]",
    topic: "OrderedDict[int, tuple[int, int]]",
    fused_top5_names: list[str],
) -> None:
    out.append(f"## `{target['id']}` · owner `{target['owner']}`\n")
    out.append(f"- **goal**: {target['goal']}")
    out.append(f"- **expected**: {', '.join(target['expected'])}")
    out.append(f"- **hybrid top-5 returned**: {' · '.join(fused_top5_names) or '—'}")
    out.append("")
    out.append("### parsed GoalIntent (LLM)")
    out.append(f"- summary: `{intent.summary or '—'}`")
    out.append(f"- roles: {intent.roles or '—'}")
    out.append(f"- industries: {intent.industries or '—'}")
    out.append(f"- skills: {intent.skills or '—'}")
    out.append(f"- keywords: {intent.keywords or '—'}")
    out.append(f"- cities: {intent.cities or '—'}")
    out.append("")
    out.append("### per-expected diagnosis")
    out.append("")
    out.append("| expected | in DB? | bio (first 120 chars) | vec rank/dist | helper rank/hits | topic rank/hits |")
    out.append("|---|---|---|---:|---:|---:|")
    for name in target["expected"]:
        rows = find_person_by_name(repo, owner_id, name)
        if not rows:
            out.append(f"| {name} | ❌ not in this owner network | — | — | — | — |")
            continue
        person = rows[0]
        pid = int(person["id"])
        bio = (person["bio"] or "").replace("\n", " ").replace("|", "/")[:120]
        if not bio:
            bio = "(empty bio)"
        v = vec.get(pid)
        h = helper.get(pid)
        t = topic.get(pid)
        v_str = f"{v[0]} / {v[1]:.3f}" if v else "—"
        h_str = f"{h[0]} / {h[1]}" if h else "—"
        t_str = f"{t[0]} / {t[1]}" if t else "—"
        out.append(f"| {name} | ✅ id={pid} | {bio} | {v_str} | {h_str} | {t_str} |")
    out.append("")
    out.append("### top-15 of each recall channel (for context)")
    out.append("")

    def _rows(channel: str, ranks: "OrderedDict[int, tuple[int, float|int]]", n: int = 15) -> list[str]:
        lines = [f"**{channel}**", ""]
        if not ranks:
            lines.append("(empty)")
            lines.append("")
            return lines
        lines.append("| rank | name | score |")
        lines.append("|---:|---|---:|")
        for i, (pid, (rank, score)) in enumerate(ranks.items()):
            if i >= n:
                break
            person = repo.get_person(pid)
            label = person.name if person else f"#{pid}"
            score_fmt = f"{score:.3f}" if isinstance(score, float) else str(score)
            lines.append(f"| {rank} | {label} | {score_fmt} |")
        lines.append("")
        return lines

    out.extend(_rows("vector (cosine distance)", vec))
    out.extend(_rows("helper keywords (roles+industries+skills)", helper))
    out.extend(_rows("topic keywords (keywords+cities)", topic))


def main() -> int:
    repo = open_repo()
    embedder = get_embedding_client()
    parser = GoalParser(get_llm_client())

    out: list[str] = [
        "# Hybrid recall diagnosis — 2026-04-21",
        "",
        "针对 `docs/eval_2026-04-21.md` 里三档 reranker 同时 R@5=0 的 4 条 query，",
        "拆解 vector / helper-keyword / topic-keyword 三路召回，定位 expected 人物**卡在哪一步**。",
        "",
        "## ✅ Root cause 已修复：补了 embedding，4 条死角全部回阳",
        "",
        "**初次跑诊断时发现的根因**：`vec_person_bio` 在 richard（62 人）和 tommy（110 人）",
        "两个网络下都是**空表**——170 个 bio 全没 embedding，`HybridSearch` 三路里",
        "vector 通道**完全没工作**。当时 `docs/eval_2026-04-21.md` 里所有 reranker 对比",
        "其实都是**keyword-only + 重排**，带系统偏差；4 条死角 R@5=0 是因为 keyword",
        "LIKE 跟 bio 字面不重合（语义同义但字符串不同）：",
        "",
        "| query | LLM intent | bio 实写 | LIKE 命中？ |",
        "|---|---|---|---|",
        "| r-ambig-1 | `政府机构` / `政府关系顾问` | `政府单位` / `药监部门` / `法院` | ❌ |",
        "| r-onehop-1 | `个人朋友` / `铁磁关系人` | `私募基金 老板` / `券商 分公司老总` | ❌（bio 不写交情） |",
        "| t-longt-1 | `律师事务所` / `企业法务` | `法务；律所` / `德恒律所律师` | ❌（\"律师事务所\" ≠ \"律所\"） |",
        "| t-onehop-2 | `券商通道` / `证券公司` | `券商渠道` / `中泰证券券商渠道` | ❌（\"通道\" ≠ \"渠道\"） |",
        "",
        "**修复**：跑 `uv run lodestar reembed` 为 170 个 bio 补 dashscope `text-embedding-v4`",
        "（1024 维），重跑评测后 4 条全部回阳：",
        "",
        "| query | 修复前 R@5 (bge) | 修复后 R@5 (bge) |",
        "|---|---:|---:|",
        "| r-ambig-1 | 0.00 | **0.50** |",
        "| r-onehop-1 | 0.00 | **0.50** |",
        "| t-longt-1  | 0.00 | **1.00** |",
        "| t-onehop-2 | 0.00 | **0.33** |",
        "",
        "下面每节的 vector top-15 / per-expected 表是补完 embedding 后的状态，",
        "helper / topic keyword 仍空——印证了「这些查询全靠 vector 通道兜底」。",
        "",
        "## 三档 reranker baseline（hybrid 真的工作之后）",
        "",
        "详见 `docs/eval_2026-04-21.md`，要点：",
        "",
        "| variant | R@5 | MRR | NDCG@10 | cliff-avoid | 平均延迟 |",
        "|---|---:|---:|---:|---:|---:|",
        "| none | 0.676 | 0.758 | 0.666 | 0.750 | 195ms |",
        "| llm  | 0.769 | 0.900 | 0.782 | 0.800 | 45.3s |",
        "| bge  | 0.766 | 0.842 | 0.753 | **0.850** | 11.7s |",
        "",
        "**结论**：`bge` 与 `llm` 质量打平（R@5 差 0.003），cliff-avoid 反超，延迟低 **4 倍**，",
        "零 token 费——一旦装好 `[rerank]` extra，**默认应当用 `bge`**，",
        "`llm` 只在 MRR 略胜的高精度场景考虑。",
        "",
        "## 仍未解决的长期问题",
        "",
        "1. **`r-onehop-1` 的\"借钱铁磁\"语义**仍然是 50%——根本上是**信号层错位**：",
        "   \"借钱级别的铁磁\"是 `relationship.strength=5` + `frequency` 的图侧维度，",
        "   bio 文本压根不记交情。要么从 golden 移除这条，要么给 search 加一条",
        "   「intent 含 '借钱/铁磁/熟人' → 退化为 strength≥5 的 contacted 列表」分支。",
        "2. **keyword 通道的字面失配**长期存在（\"通道\" ≠ \"渠道\"、\"律师事务所\" ≠ \"律所\"）。",
        "   有了 vector 之后已不再致命，但若想让 keyword 兜底更强，可在 `_rank_terms`",
        "   前做轻量同义扩展，或在 enrich 阶段把 bio 归一化到统一标签表。",
        "3. **`r-onehop-1` / `t-onehop-2` / `r-longt-3` / `t-longt-3`** 这几条 R@5 仍 ≤ 0.50。",
        "   下次评测迭代可以从这几条入手定位剩余的召回 / 重排间隙。",
        "",
        "---",
        "",
        "下面是补 embedding 后的逐 query 拆解（vector top-15 + per-expected 三路命中表）：",
        "",
    ]

    for tgt in TARGETS:
        owner = repo.get_owner_by_slug(tgt["owner"])
        if owner is None or owner.id is None:
            print(f"[skip] owner not found: {tgt['owner']}", file=sys.stderr)
            continue
        intent = parser.parse(tgt["goal"])

        vec = vector_ranks(repo, embedder, intent, owner.id, limit=60)
        helper = kw_ranks(repo, helper_terms(intent), owner.id)
        topic = kw_ranks(repo, topic_terms(intent), owner.id)

        fused = HybridSearch(repo=repo, embedder=embedder, owner_id=owner.id).search(
            intent, top_k=5, recall_k=30
        )
        fused_top5_names: list[str] = []
        for c in fused[:5]:
            p = repo.get_person(c.person_id)
            fused_top5_names.append(p.name if p else f"#{c.person_id}")

        render_query(out, tgt, intent, repo, owner.id, vec, helper, topic, fused_top5_names)

        print(f"[done] {tgt['id']}  intent.roles={intent.roles}  industries={intent.industries}")

    out_path = REPO_ROOT / "docs" / "zero_recall_2026-04-21.md"
    out_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
