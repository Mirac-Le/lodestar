"""Company-name normalization for the `lodestar normalize-companies` CLI.

Why this exists
---------------
After `enrich` populates `person.companies` from free text, the same
underlying employer often shows up under two or three different
strings — most notoriously merger/rename pairs like 国泰君安 / 国泰海通
(the two firms merged in 2024). Until those strings collapse into a
single canonical row, `infer-colleagues` won't connect colleagues across
the alias gap.

This module is the source of truth for *which* names are aliases of *what*
canonical employer. Three independent contributors:

  1. **`BUILTIN_ALIASES`** — a small, hand-curated, high-confidence map
     of well-known China-finance merger/rename / abbreviation cases.
     Boring, deterministic, zero-cost, easy to audit. Exclusively for
     cases where the merger is publicly documented.

  2. **User alias file** (`--alias-file path.yaml`) — owner-specific
     overrides. Same shape as `BUILTIN_ALIASES`. Trumps the builtin map
     on conflict (last write wins).

  3. **LLM clustering** (`--use-llm`, optional) — for everything the
     above two miss, ship the deduped company list to the LLM and ask
     it to group near-duplicates. Always presented to the user via
     dry-run before any DB write.

The three sources are *combined*, not exclusive — typical run is
"builtin + file" with `--use-llm` added when desired.

Privacy: company names are public information by nature, so the LLM
clustering pass does NOT go through `Anonymizer`. Only person names and
free-text bios get the Pxxx/Cxxx treatment, in `extractor.py`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from lodestar.enrich.client import LLMClient, LLMError

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Built-in alias map
# ---------------------------------------------------------------------------
# Format: { canonical_name: [alias1, alias2, ...] }
#
# Inclusion bar: only confirmed merger/rename, well-known abbreviation, or
# 100% obvious typos. When in doubt LEAVE IT OUT — the user can add it via
# `--alias-file` or `--use-llm`.
#
# Things deliberately NOT folded:
#   - parent ↔ subsidiary  (e.g. 中金公司 vs 中金财富 — different teams,
#     keep separate)
#   - same group, different business lines (e.g. 平安保险 / 平安证券 / 平安银行)
#   - look-alike unrelated firms (e.g. 一创证券 ≠ 首创证券)
BUILTIN_ALIASES: dict[str, list[str]] = {
    # 2024 历史合并：国泰君安 + 海通证券 → 国泰海通证券
    "国泰海通证券": [
        "国泰君安",
        "国泰海通",
        "国泰君安证券",
        "海通证券",
        "国君",
        "国君证券",
    ],
    # 2015 历史合并：申银万国 + 宏源证券 → 申万宏源
    "申万宏源证券": [
        "申万宏源",
        "申银万国",
        "宏源证券",
    ],
    # 中金公司 / 中国国际金融 / 中金（口语缩写）—— 同一上市主体；
    # 但 *不* 含 中金财富 / 中金资管（独立子公司）
    "中金公司": [
        "中国国际金融",
        "中国国际金融股份有限公司",
        "中金",
    ],
}


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------
@dataclass
class AliasGroup:
    """One proposed merge: `aliases` will be folded into `canonical`.

    `headcount` is the total distinct people across all members under the
    target owner, *before* the merge — used in the dry-run table so the
    reviewer can prioritise high-impact groups. `source` records who
    proposed the group (`builtin` / `file` / `llm`) for auditability.
    """

    canonical: str
    aliases: list[str] = field(default_factory=list)
    headcount: int = 0
    source: str = "builtin"

    def members(self) -> list[str]:
        """All names this group claims (canonical + aliases)."""
        out = [self.canonical]
        out.extend(a for a in self.aliases if a != self.canonical)
        return out


# ---------------------------------------------------------------------------
# Combiner
# ---------------------------------------------------------------------------
def build_groups(
    *,
    present: dict[str, int],
    builtin: bool = True,
    user_aliases: dict[str, list[str]] | None = None,
    llm_groups: list[AliasGroup] | None = None,
) -> list[AliasGroup]:
    """Combine the three alias sources into a final list of merge groups.

    Only groups that match **at least 2 of the present company names**
    are returned — a singleton match means there's nothing to merge.

    `present` maps `company_name → headcount` and is the universe of
    company names actually attached to people for the target owner.

    Resolution rules when a name appears in multiple sources:
      - file > builtin (user override always wins)
      - builtin/file > llm (LLM never overrides explicit map)
      - within llm: first group wins, later groups drop the duplicate
    """
    groups: list[AliasGroup] = []
    claimed: set[str] = set()

    def _emit_group(canonical: str, members: Iterable[str], source: str) -> None:
        # Filter to names that actually exist in the owner's roster, drop
        # already-claimed ones, dedup. Skip empty / singleton results.
        present_members = [
            m for m in dict.fromkeys(members) if m in present and m not in claimed
        ]
        # The canonical itself counts as a member of the group whether it
        # exists in the roster or not — but we only emit if at least 2
        # roster names will fold in.
        roster_aliases = [m for m in present_members if m != canonical]
        roster_canonical_count = 1 if canonical in present else 0
        if roster_canonical_count + len(roster_aliases) < 2:
            return
        head = sum(present.get(m, 0) for m in present_members)
        if canonical in present and canonical not in present_members:
            head += present[canonical]
            present_members.append(canonical)
        groups.append(AliasGroup(
            canonical=canonical,
            aliases=[m for m in present_members if m != canonical],
            headcount=head,
            source=source,
        ))
        claimed.update(present_members)

    # 1) user file (highest priority)
    for canon, aliases in (user_aliases or {}).items():
        _emit_group(canon, [canon, *aliases], source="file")

    # 2) builtin
    if builtin:
        for canon, aliases in BUILTIN_ALIASES.items():
            _emit_group(canon, [canon, *aliases], source="builtin")

    # 3) LLM proposals (lowest priority — only fills gaps)
    for g in llm_groups or []:
        _emit_group(g.canonical, [g.canonical, *g.aliases], source="llm")

    return groups


# ---------------------------------------------------------------------------
# User alias file loader
# ---------------------------------------------------------------------------
def load_alias_file(path: Path) -> dict[str, list[str]]:
    """Load `{canonical: [aliases...]}` from JSON or YAML.

    Supports two payload shapes:

      Shape A (dict, preferred):
        { "国泰海通证券": ["国泰君安", "海通证券"] }

      Shape B (list of objects, easier for LLM-style output):
        [{"canonical": "国泰海通证券", "aliases": ["国泰君安", "海通证券"]}]

    YAML support is optional — only loaded if PyYAML is available, since
    the project currently doesn't pin it.
    """
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    raw: object
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "Reading YAML alias files requires `pyyaml`. "
                "Install with `uv add pyyaml`, or use a .json file."
            ) from exc
        raw = yaml.safe_load(text)
    else:
        raw = json.loads(text)

    out: dict[str, list[str]] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if not isinstance(k, str) or not isinstance(v, list):
                continue
            out[k.strip()] = [str(x).strip() for x in v if str(x).strip()]
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            canon = str(item.get("canonical") or "").strip()
            aliases = item.get("aliases") or []
            if not canon or not isinstance(aliases, list):
                continue
            out[canon] = [str(x).strip() for x in aliases if str(x).strip()]
    else:
        raise ValueError(f"Unsupported alias file shape: {type(raw).__name__}")
    return out


# ---------------------------------------------------------------------------
# LLM clustering
# ---------------------------------------------------------------------------
LLM_SYSTEM_PROMPT = """你是公司名归一化助手。任务：从给定的公司名清单里，找出
**指代同一法人主体**的不同写法，分组合并，输出严格 JSON。

判定标准（必须同时满足）：
1. 真的是同一家公司，包括以下三种情况：
   a) 已经发生的合并改名（如 国泰君安 + 海通证券 → 国泰海通证券）
   b) 缩写 / 全称 / 行业内俗称（如 中金 = 中金公司 = 中国国际金融）
   c) 明显的笔误 / 抽取噪声（如 "艾克朗科创始人" 实为 "艾克朗科"）

2. **绝不**合并以下情况：
   - 母公司 ↔ 子公司（如 中金公司 ↔ 中金财富 / 中金资管）
   - 同集团不同业务线（如 平安保险 / 平安证券 / 平安银行）
   - 同行业看着像但不同公司（如 一创证券 vs 首创证券）
   - 学校 / 政府机构 / 通用占位词（如 "公募基金" "投资公司"）

输出格式：
{
  "groups": [
    {
      "canonical": "国泰海通证券",
      "aliases":   ["国泰君安", "国泰海通", "国君", "海通证券"],
      "reason":    "2024 年合并改名，国君与海通已为同一上市主体"
    },
    {
      "canonical": "艾克朗科",
      "aliases":   ["艾克朗科创始人"],
      "reason":    "后者疑似抽取噪声，把'X 是艾克朗科创始人'误当公司名"
    }
  ]
}

不确定就**不要**放进 groups。少合远好于错合 —— 错合会导致同事关系污染。
"""


def cluster_with_llm(
    company_names: list[str],
    *,
    client: LLMClient,
) -> list[AliasGroup]:
    """Ask the LLM to cluster `company_names` into alias groups.

    Returns groups with `source='llm'`. Caller is responsible for
    intersecting with the actual roster (we don't filter here — that
    happens in `build_groups`).

    Raises `LLMError` on transport / JSON parse failure.
    """
    if not company_names:
        return []
    payload = json.dumps(
        {"company_names": sorted(set(company_names))},
        ensure_ascii=False,
        indent=2,
    )
    result = client.chat_json(
        system=LLM_SYSTEM_PROMPT,
        user=payload,
        temperature=0.0,
    )
    raw_groups = result.data.get("groups")
    if not isinstance(raw_groups, list):
        raise LLMError(
            f"LLM clustering 返回结构非预期，缺少 groups 数组：{result.raw[:200]}"
        )
    out: list[AliasGroup] = []
    for g in raw_groups:
        if not isinstance(g, dict):
            continue
        canon = str(g.get("canonical") or "").strip()
        aliases_raw = g.get("aliases") or []
        if not canon or not isinstance(aliases_raw, list):
            continue
        aliases = [str(a).strip() for a in aliases_raw if str(a).strip()]
        aliases = [a for a in aliases if a != canon]
        if not aliases:
            continue
        out.append(AliasGroup(
            canonical=canon,
            aliases=aliases,
            source="llm",
        ))
    return out


__all__ = [
    "BUILTIN_ALIASES",
    "AliasGroup",
    "build_groups",
    "cluster_with_llm",
    "load_alias_file",
]
