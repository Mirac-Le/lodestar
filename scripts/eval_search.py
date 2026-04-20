"""Run silver-standard evaluation across reranker variants.

输入：
    tests/fixtures/golden_queries.yaml    — 银标 query × expected_top3 / must_not_include
    现有 SQLite DB                         — owner 网络 + bio + embedding

每条 query 对每个 variant 跑一次：
    Hybrid 召回 recall_k 个候选 → reranker 重排 → 截 top-k → 计算指标

指标：
    Recall@5             — expected_top3 ∩ top-5 / |expected_top3|
    MRR                  — 1/rank(第一个命中 expected 的位置)，未命中=0
    NDCG@10              — 标准 DCG/iDCG，相关度=1 if name∈expected else 0
    cliff_avoidance_rate — must_not_include ∩ top-5 = ∅ 的比例（断崖率反指标）

输出：
    docs/eval_<DATE>.md  — markdown 表格（per-variant + per-category）
    docs/eval_<DATE>.json — 同样数据的机器可读版本，便于 diff

用法：
    uv run python scripts/eval_search.py \
        --variants none llm \
        --out-md docs/eval_2026-04-21.md \
        --out-json docs/eval_2026-04-21.json

只跑 baseline 不开 LLM：--variants none
全档跑（含 BGE，需要先装可选依赖）：--variants none llm bge
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import yaml

from lodestar.config import get_settings, reset_settings
from lodestar.db import Repository, connect, init_schema
from lodestar.embedding import get_embedding_client
from lodestar.llm import GoalParser, get_llm_client
from lodestar.models import GoalIntent, Person
from lodestar.search import HybridSearch, build_reranker_from_settings


REPO_ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_queries.yaml"

Variant = Literal["none", "llm", "bge"]
ALL_VARIANTS: tuple[Variant, ...] = ("none", "llm", "bge")
TOP_K = 5
RECALL_K_DEFAULT = 30
NDCG_K = 10


@dataclass(frozen=True)
class Query:
    id: str
    category: str
    owner: str
    goal: str
    expected_top3: list[str]
    must_not_include: list[str]


@dataclass
class QueryResult:
    query_id: str
    category: str
    owner: str
    variant: Variant
    top_k_names: list[str]
    recall_at_5: float
    mrr: float
    ndcg_at_10: float
    cliff_hit: bool  # True 表示断崖发生（命中 must_not_include）
    elapsed_ms: float


# ----------------------------------------------------------------- yaml & db


def load_queries() -> list[Query]:
    raw = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    out: list[Query] = []
    for row in raw.get("queries") or []:
        out.append(
            Query(
                id=str(row["id"]),
                category=str(row["category"]),
                owner=str(row["owner"]),
                goal=str(row["goal"]),
                expected_top3=[str(x) for x in row.get("expected_top3") or []],
                must_not_include=[str(x) for x in row.get("must_not_include") or []],
            )
        )
    return out


def open_repo() -> Repository:
    s = get_settings()
    conn = connect(s.db_path)
    init_schema(conn, embedding_dim=s.embedding_dim)
    return Repository(conn)


# --------------------------------------------------------------- intent cache

_intent_cache: dict[str, GoalIntent] = {}


def make_intent(goal: str) -> GoalIntent:
    """Parse goal once per process; LLM call is the expensive part."""
    if goal in _intent_cache:
        return _intent_cache[goal]
    try:
        intent = GoalParser(get_llm_client()).parse(goal)
    except Exception:
        intent = GoalIntent(original=goal, keywords=[goal], summary=goal)
    _intent_cache[goal] = intent
    return intent


# ------------------------------------------------------------------- metrics


def recall_at_k(retrieved: list[str], expected: list[str], k: int) -> float:
    if not expected:
        return 0.0
    hit = len(set(retrieved[:k]) & set(expected))
    return hit / len(expected)


def mrr(retrieved: list[str], expected: list[str]) -> float:
    expected_set = set(expected)
    for idx, name in enumerate(retrieved, start=1):
        if name in expected_set:
            return 1.0 / idx
    return 0.0


def ndcg_at_k(retrieved: list[str], expected: list[str], k: int) -> float:
    if not expected:
        return 0.0
    expected_set = set(expected)
    dcg = 0.0
    for idx, name in enumerate(retrieved[:k], start=1):
        rel = 1.0 if name in expected_set else 0.0
        dcg += rel / math.log2(idx + 1)
    ideal_hits = min(len(expected), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


def cliff_hit(retrieved: list[str], must_not: list[str], k: int) -> bool:
    if not must_not:
        return False
    return bool(set(retrieved[:k]) & set(must_not))


# ----------------------------------------------------------------- one query


def run_query(
    query: Query,
    *,
    repo: Repository,
    embedder,
    owner_id_by_slug: dict[str, int],
    variant: Variant,
    recall_k: int,
) -> QueryResult:
    intent = make_intent(query.goal)
    owner_id = owner_id_by_slug[query.owner]

    started = time.perf_counter()
    candidates = HybridSearch(
        repo=repo, embedder=embedder, owner_id=owner_id
    ).search(intent, top_k=TOP_K, recall_k=recall_k)

    if variant != "none":
        os.environ["LODESTAR_RERANKER"] = variant
        reset_settings()
        reranker = build_reranker_from_settings()
        candidates = reranker.rerank(intent, candidates, repo)

    candidates = candidates[:TOP_K]
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    names = [_resolve_name(repo, c.person_id) for c in candidates]

    return QueryResult(
        query_id=query.id,
        category=query.category,
        owner=query.owner,
        variant=variant,
        top_k_names=names,
        recall_at_5=recall_at_k(names, query.expected_top3, TOP_K),
        mrr=mrr(names, query.expected_top3),
        ndcg_at_10=ndcg_at_k(names, query.expected_top3, NDCG_K),
        cliff_hit=cliff_hit(names, query.must_not_include, TOP_K),
        elapsed_ms=elapsed_ms,
    )


_person_cache: dict[int, Person | None] = {}


def _resolve_name(repo: Repository, pid: int) -> str:
    if pid not in _person_cache:
        _person_cache[pid] = repo.get_person(pid)
    p = _person_cache[pid]
    return p.name if p else f"#{pid}"


# ------------------------------------------------------------------ aggregate


def summarise(results: Iterable[QueryResult]) -> dict[str, float]:
    rows = list(results)
    n = len(rows)
    if n == 0:
        return {
            "n": 0,
            "recall_at_5": 0.0,
            "mrr": 0.0,
            "ndcg_at_10": 0.0,
            "cliff_avoidance_rate": 0.0,
            "avg_latency_ms": 0.0,
        }
    cliff_eligible = [r for r in rows if r.cliff_hit is not None and any(True for _ in [r])]
    cliff_count = sum(1 for r in rows if r.cliff_hit)
    return {
        "n": n,
        "recall_at_5": sum(r.recall_at_5 for r in rows) / n,
        "mrr": sum(r.mrr for r in rows) / n,
        "ndcg_at_10": sum(r.ndcg_at_10 for r in rows) / n,
        "cliff_avoidance_rate": 1.0 - (cliff_count / n),
        "avg_latency_ms": sum(r.elapsed_ms for r in rows) / n,
    }


def render_markdown(
    queries: list[Query],
    results_by_variant: dict[Variant, list[QueryResult]],
    *,
    date_label: str,
) -> str:
    lines: list[str] = [
        f"# Lodestar search evaluation — {date_label}",
        "",
        "> **Silver-standard disclaimer.** Golden queries auto-built from owner",
        "> bios + explicit rules. Treat all numbers as silver, not gold.",
        "",
        f"- queries: **{len(queries)}**",
        f"- top_k: {TOP_K}, recall_k (hybrid): {RECALL_K_DEFAULT}, NDCG@: {NDCG_K}",
        "",
        "## overall",
        "",
        "| variant | n | Recall@5 | MRR | NDCG@10 | cliff-avoid | avg-latency (ms) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for v, rows in results_by_variant.items():
        s = summarise(rows)
        lines.append(
            f"| `{v}` | {s['n']} "
            f"| {s['recall_at_5']:.3f} | {s['mrr']:.3f} | {s['ndcg_at_10']:.3f} "
            f"| {s['cliff_avoidance_rate']:.3f} | {s['avg_latency_ms']:.0f} |"
        )

    # By category
    lines += ["", "## by category", ""]
    cats = sorted({q.category for q in queries})
    for cat in cats:
        lines.append(f"### `{cat}`")
        lines.append("")
        lines.append("| variant | n | Recall@5 | MRR | NDCG@10 | cliff-avoid |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for v, rows in results_by_variant.items():
            sub = [r for r in rows if r.category == cat]
            s = summarise(sub)
            lines.append(
                f"| `{v}` | {s['n']} "
                f"| {s['recall_at_5']:.3f} | {s['mrr']:.3f} | {s['ndcg_at_10']:.3f} "
                f"| {s['cliff_avoidance_rate']:.3f} |"
            )
        lines.append("")

    # Per-query detail (where signal differs across variants)
    lines += ["## per-query detail", ""]
    by_id: dict[str, dict[Variant, QueryResult]] = defaultdict(dict)
    for v, rows in results_by_variant.items():
        for r in rows:
            by_id[r.query_id][v] = r
    for q in queries:
        per = by_id.get(q.id, {})
        lines.append(f"### `{q.id}` · `{q.category}` · owner `{q.owner}`")
        lines.append("")
        lines.append(f"- **goal**: {q.goal}")
        lines.append(f"- **expected**: {', '.join(q.expected_top3)}")
        if q.must_not_include:
            lines.append(f"- **must_not_include**: {', '.join(q.must_not_include)}")
        lines.append("")
        lines.append("| variant | top-5 returned | R@5 | MRR | NDCG@10 | cliff? |")
        lines.append("|---|---|---:|---:|---:|---|")
        for v in results_by_variant:
            r = per.get(v)
            if r is None:
                continue
            top = " · ".join(r.top_k_names) or "—"
            cliff_mark = "⚠️" if r.cliff_hit else "✅"
            lines.append(
                f"| `{v}` | {top} | {r.recall_at_5:.2f} "
                f"| {r.mrr:.2f} | {r.ndcg_at_10:.2f} | {cliff_mark} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------------ main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variants",
        nargs="+",
        choices=list(ALL_VARIANTS),
        default=["none"],
        help="Which reranker variants to evaluate.",
    )
    parser.add_argument("--recall-k", type=int, default=RECALL_K_DEFAULT)
    parser.add_argument(
        "--out-md",
        type=Path,
        default=REPO_ROOT / "docs" / "eval_2026-04-21.md",
    )
    parser.add_argument(
        "--out-json",
        type=Path,
        default=REPO_ROOT / "docs" / "eval_2026-04-21.json",
    )
    parser.add_argument("--date-label", default="2026-04-21")
    args = parser.parse_args()

    queries = load_queries()
    if not queries:
        print("[error] no queries loaded", file=sys.stderr)
        return 1

    repo = open_repo()
    owner_id_by_slug: dict[str, int] = {}
    for slug in {q.owner for q in queries}:
        owner = repo.get_owner_by_slug(slug)
        if owner is None or owner.id is None:
            print(f"[error] owner not found: {slug}", file=sys.stderr)
            return 1
        owner_id_by_slug[slug] = owner.id

    try:
        embedder = get_embedding_client()
    except Exception as exc:
        print(f"[warn] embedder unavailable: {exc}; vector ranks will be empty.")
        embedder = None

    results_by_variant: dict[Variant, list[QueryResult]] = {}
    for variant in args.variants:
        print(f"\n[variant={variant}] running {len(queries)} queries ...")
        rows: list[QueryResult] = []
        for q in queries:
            r = run_query(
                q,
                repo=repo,
                embedder=embedder,
                owner_id_by_slug=owner_id_by_slug,
                variant=variant,  # type: ignore[arg-type]
                recall_k=args.recall_k,
            )
            cliff = "⚠️" if r.cliff_hit else "✅"
            print(
                f"  [{r.query_id:<14}] R@5={r.recall_at_5:.2f} MRR={r.mrr:.2f} "
                f"NDCG@10={r.ndcg_at_10:.2f} {cliff}  top: {r.top_k_names}"
            )
            rows.append(r)
        results_by_variant[variant] = rows  # type: ignore[index]

    # Reset env so subsequent processes default to none again.
    os.environ.pop("LODESTAR_RERANKER", None)
    reset_settings()

    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text(
        render_markdown(queries, results_by_variant, date_label=args.date_label),
        encoding="utf-8",
    )
    args.out_json.write_text(
        json.dumps(
            {
                "date": args.date_label,
                "queries": [asdict(q) for q in queries],
                "results": {
                    v: [asdict(r) for r in rows]
                    for v, rows in results_by_variant.items()
                },
                "summary": {
                    v: summarise(rows) for v, rows in results_by_variant.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\n=== summary ===")
    for v, rows in results_by_variant.items():
        s = summarise(rows)
        print(
            f"  {v:>5} | R@5={s['recall_at_5']:.3f} MRR={s['mrr']:.3f} "
            f"NDCG@10={s['ndcg_at_10']:.3f} cliff-avoid={s['cliff_avoidance_rate']:.3f} "
            f"latency={s['avg_latency_ms']:.0f}ms"
        )
    print(f"\nreports: {args.out_md.relative_to(REPO_ROOT)} + {args.out_json.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
