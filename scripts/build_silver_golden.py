"""Validate `tests/fixtures/golden_queries.yaml` and render a human-readable
review report at `docs/golden_queries_review.md`.

This script is **idempotent and read-only**:

* It does NOT generate the yaml — that file is hand-curated AI silver
  standard. The script's job is to keep the yaml honest:
  - Every `expected_top3` / `must_not_include` name must resolve to a
    real `Person` in the named owner's subgraph (else exit non-zero).
  - Counts per category (role-cliff / ambiguous / longtail / one-hop)
    should match the documented design (5 each).
* Then it renders a Markdown report so the human reviewer can audit the
  silver set in 5 minutes — each row shows goal, rationale, expected
  hits with their bio summary, and must-not-include reasoning.

Usage:
    uv run python scripts/build_silver_golden.py

Exit codes:
    0  → all names resolved, report regenerated.
    1  → at least one name missing in the owner subgraph (yaml needs fixing).
"""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from lodestar.config import get_settings
from lodestar.db import Repository, connect, init_schema
from lodestar.models import Person


REPO_ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_queries.yaml"
REVIEW_PATH = REPO_ROOT / "docs" / "golden_queries_review.md"

CATEGORIES = ("role-cliff", "ambiguous", "longtail", "one-hop")


@dataclass(frozen=True)
class Query:
    id: str
    category: str
    owner: str
    goal: str
    rationale: str
    expected_top3: list[str]
    must_not_include: list[str]


def load_queries(path: Path) -> tuple[str, list[Query]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    disclaimer = (raw.get("silver_disclaimer") or "").strip()
    items: list[Query] = []
    for row in raw.get("queries") or []:
        items.append(
            Query(
                id=str(row["id"]),
                category=str(row["category"]),
                owner=str(row["owner"]),
                goal=str(row["goal"]),
                rationale=str(row.get("rationale") or "").strip(),
                expected_top3=[str(x) for x in row.get("expected_top3") or []],
                must_not_include=[str(x) for x in row.get("must_not_include") or []],
            )
        )
    return disclaimer, items


def index_owner(repo: Repository, slug: str) -> dict[str, Person]:
    owner = repo.get_owner_by_slug(slug)
    if owner is None:
        raise RuntimeError(f"owner slug not found: {slug}")
    return {p.name: p for p in repo.list_people(owner_id=owner.id)}


def validate(
    queries: list[Query], owner_index: dict[str, dict[str, Person]]
) -> list[str]:
    errors: list[str] = []
    for q in queries:
        if q.category not in CATEGORIES:
            errors.append(f"[{q.id}] unknown category {q.category!r}")
        if q.owner not in owner_index:
            errors.append(f"[{q.id}] unknown owner {q.owner!r}")
            continue
        people = owner_index[q.owner]
        for name in q.expected_top3:
            if name not in people:
                errors.append(
                    f"[{q.id}] expected_top3 name not in owner {q.owner}: {name!r}"
                )
        for name in q.must_not_include:
            if name not in people:
                errors.append(
                    f"[{q.id}] must_not_include name not in owner {q.owner}: {name!r}"
                )
    return errors


def category_counts(queries: Iterable[Query]) -> dict[str, int]:
    return dict(Counter(q.category for q in queries))


def _bio_oneline(p: Person, *, max_chars: int = 160) -> str:
    bio = (p.bio or "").replace("\n", " ").strip()
    return bio[:max_chars] + ("…" if len(bio) > max_chars else "")


def render_review(
    queries: list[Query],
    owner_index: dict[str, dict[str, Person]],
    disclaimer: str,
) -> str:
    counts = category_counts(queries)
    by_owner = Counter(q.owner for q in queries)

    lines: list[str] = [
        "# Silver-standard golden queries — review",
        "",
        "> **Silver disclaimer.** " + (disclaimer or ""),
        "",
        f"- Total queries: **{len(queries)}**",
        "- Per category: "
        + ", ".join(f"`{c}`={counts.get(c, 0)}" for c in CATEGORIES),
        "- Per owner: "
        + ", ".join(f"`{slug}`={n}" for slug, n in sorted(by_owner.items())),
        "",
        "如何审计：每行展示 goal + rationale + expected_top3 的 bio，",
        "你只需要扫一眼 expected 里的 bio 是否真的符合 goal 的「本人」语义。",
        "若不符合，直接修改 `tests/fixtures/golden_queries.yaml` 里对应行。",
        "",
    ]

    for cat in CATEGORIES:
        cat_queries = [q for q in queries if q.category == cat]
        if not cat_queries:
            continue
        lines.append(f"## category: `{cat}`")
        lines.append("")
        for q in cat_queries:
            people = owner_index[q.owner]
            lines.append(f"### `{q.id}` · owner `{q.owner}`")
            lines.append("")
            lines.append(f"- **goal**: {q.goal}")
            lines.append(f"- **rationale**: {q.rationale}")
            lines.append("")
            lines.append("**expected_top3**")
            lines.append("")
            for name in q.expected_top3:
                p = people.get(name)
                if p is None:
                    lines.append(f"- ⚠️ `{name}` (not found)")
                    continue
                lines.append(f"- **{name}** — {_bio_oneline(p)}")
            if q.must_not_include:
                lines.append("")
                lines.append("**must_not_include** (反例 / 断崖陷阱)")
                lines.append("")
                for name in q.must_not_include:
                    p = people.get(name)
                    bio = _bio_oneline(p) if p is not None else "(not found)"
                    lines.append(f"- {name} — {bio}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    settings = get_settings()
    conn = connect(settings.db_path)
    init_schema(conn, embedding_dim=settings.embedding_dim)
    repo = Repository(conn)

    disclaimer, queries = load_queries(YAML_PATH)
    if not queries:
        print("[error] no queries loaded; check yaml shape", file=sys.stderr)
        return 1

    owner_slugs = sorted({q.owner for q in queries})
    owner_index = {slug: index_owner(repo, slug) for slug in owner_slugs}

    errors = validate(queries, owner_index)
    if errors:
        print("[error] silver standard validation failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    REVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    REVIEW_PATH.write_text(render_review(queries, owner_index, disclaimer), encoding="utf-8")

    counts = category_counts(queries)
    print(f"[ok] {len(queries)} queries validated.")
    print(f"     per category: {counts}")
    print(f"     report: {REVIEW_PATH.relative_to(REPO_ROOT)}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
