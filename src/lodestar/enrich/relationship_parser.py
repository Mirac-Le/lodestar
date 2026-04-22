"""自然语言 → peer↔peer 关系提案。

Web 端 `新关系` 入口的后端核心。用户在前端输入一段中文（"老张和小李
是同事，每月聚一次。Mike 介绍了小李给我"），我们：

1. 用 owner 范围内的 `Anonymizer` 把所有已知人名替换成 `Pxxx` token，
   公司名替换成 `Cxxx`（配合 L1 已有的脱敏管道）。
2. 把脱敏后的文本交给 LLM，要求它输出严格 JSON：
   - `edges`: 一组 `(a_token, b_token, strength?, context?, frequency?)`
   - `unknown_mentions`: 文本里出现但不在 roster 里的明文人名。
3. 反匿名：把 `Pxxx` 还原成 `person_id`；丢弃端点不齐的边（包括引用
   了 `me` 的边——本端点只处理 peer↔peer，不动 me 边）。
4. 返回 `RelationshipParseResult`，由 web 层附上 `existing_edge` 上下
   文后给前端。

设计取舍：
- **绝不自动建人**：未知姓名只放进 `unknown_mentions`，由用户去添加联
  系人后再来一次。这与本期 plan 的"peer-only"决策对齐。
- **强度不要瞎猜**：prompt 明确说"用户没说就 null"，让前端逼用户主动
  填，避免一句话凭空写入 strength=3。
- **可重入**：parser 不写库；`apply()` 在 web 层用 `Repository.add_relationship`
  做 upsert（source="manual"），自动覆盖任何 ai/colleague_inferred 旧边。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from lodestar.db.repository import Repository
from lodestar.enrich.anonymizer import Anonymizer
from lodestar.enrich.client import LLMClient, LLMError
from lodestar.models import Frequency

_log = logging.getLogger(__name__)

_PXXX_RE = re.compile(r"\bP\d{3}\b")
_VALID_FREQUENCY = {f.value for f in Frequency}


SYSTEM_PROMPT = """你是关系抽取助手。任务：从一段中文叙述中识别**两个具体人之间**的人际关系，并输出严格 JSON。

输入说明：
- 文本里出现的人名，凡是已经登记在我们通讯录里的，会被替换成 `Pxxx` token（如 P017、P032）。
- 文本里出现的公司/机构名，凡是已经登记的，会被替换成 `Cxxx` token（如 C005）。
- `roster` 列出当前所有已知 token 及一个简短提示词，**只允许**输出 roster 内的 token 之间的关系。
- 我们用 P000 表示通讯录主人本人（"我"），**不要**把 P000 作为一条 peer-edge 的端点输出（它属于另一个流程）。

输出严格 JSON：
{
  "edges": [
    {
      "a": "P017",
      "b": "P032",
      "strength": 3,           // 1-5 整数；用户没说就给 null，**不要瞎猜**
      "context": "同事，一起做了某项目",  // 一句话，可空
      "frequency": "monthly",  // weekly|monthly|quarterly|yearly|rare；说不出来就 null
      "rationale": "原文：『老张和小李是同事，每月聚一次』"
    }
  ],
  "unknown_mentions": ["王某"]   // 文本里提到的明文人名（非 token），但不在 roster 里
}

硬规则：
1. **只输出 roster 内 Pxxx 之间的关系**。文本里若提到了未在 roster 内的明文人名（如 "Mike"、"老王"），把这个明文加入 `unknown_mentions`，**不要**为它编一个 Pxxx，也**不要**让它出现在任何 edge 里。
2. **不要包含 P000**。如果原文是"我和老张吃了饭"，跳过这条；如果是"老张和小李"，照常输出。
3. **不要重复同一条边**：同一对 (a, b) 只能出现一次；a/b 可以无序，去重时视作无序。
4. **strength**：用户明确说"很熟/铁磁"=4-5；"普通朋友"=3；"点头之交/弱认识"=1-2；说不出来给 null。
5. **frequency**：必须是允许值之一，否则 null。
6. **不要解释**，只输出 JSON。原文没说的字段就给 null 或空数组。"""


@dataclass
class ProposedEdge:
    """一条由 LLM 提议的 peer↔peer 边，端点已反匿名为 person_id。"""

    a_id: int
    a_name: str
    b_id: int
    b_name: str
    strength: int | None = None
    context: str | None = None
    frequency: str | None = None
    rationale: str | None = None


@dataclass
class RelationshipParseResult:
    proposals: list[ProposedEdge] = field(default_factory=list)
    unknown_mentions: list[str] = field(default_factory=list)
    error: str | None = None

    def is_empty(self) -> bool:
        return not self.proposals and not self.unknown_mentions


class RelationshipParser:
    """LLM 包装：anonymize → chat_json → deanonymize → 提案。"""

    def __init__(
        self,
        repo: Repository,
        client: LLMClient,
    ) -> None:
        self._repo = repo
        self._client = client

    # --------------------------------------------------------------- public
    def parse(self, text: str) -> RelationshipParseResult:
        text = (text or "").strip()
        if not text:
            return RelationshipParseResult(error="输入为空")

        anon, name_lookup = self._build_anonymizer()
        anon_text = anon.anonymize_text(text) or text
        roster = self._build_roster(anon, name_lookup)

        # 没有任何联系人就没有可输出的 token；早返回防止给 LLM 一个空场景。
        if not roster:
            return RelationshipParseResult(
                error="当前网络还没有联系人，先添加一些再来录入关系。",
            )

        user_payload = json.dumps(
            {"text": anon_text, "roster": roster},
            ensure_ascii=False,
            indent=2,
        )
        try:
            llm_result = self._client.chat_json(
                system=SYSTEM_PROMPT, user=user_payload
            )
        except LLMError as exc:
            return RelationshipParseResult(error=f"LLM 调用失败：{exc}")

        return self._parse_response(llm_result.data, anon)

    # --------------------------------------------------------------- helpers
    def _build_anonymizer(self) -> tuple[Anonymizer, dict[int, str]]:
        people = self._repo.list_people()
        me = self._repo.get_me()
        me_name = me.name if me else "我"
        me_id = me.id if me and me.id is not None else -1

        # 收集 owner 范围内已结构化的全部公司，用于把 bio 里的公司名也脱敏。
        company_set: set[str] = set()
        for p in people:
            for c in p.companies or []:
                if c and c.strip():
                    company_set.add(c.strip())
        # 长度优先以避免子串冲突（"国信证券" 早于 "国信证券深圳分公司"）。
        companies_sorted = sorted(company_set, key=lambda c: (-len(c), c))

        anon = Anonymizer.from_people_and_companies(
            me_id=me_id,
            me_name=me_name,
            people=[(p.id, p.name) for p in people if p.id is not None],
            companies=companies_sorted,
        )
        # token → 真实姓名的快查表（含 me），便于反匿名时拿 display name。
        lookup: dict[int, str] = {me_id: me_name}
        for p in people:
            if p.id is not None:
                lookup[p.id] = p.name
        return anon, lookup

    def _build_roster(
        self, anon: Anonymizer, name_lookup: dict[int, str]
    ) -> list[dict]:
        """给 LLM 看的 token 名册：仅 peer 联系人，附 4 字符姓名提示。

        提示词只截前几个字符是为了让 LLM 在歧义时有点上下文又不至于
        把全名再泄露一次（虽然名字本来就在用户输入里出现过，所以这里
        的"脱敏"更像是 token 命名提示，不是隐私保护）。
        """
        roster: list[dict] = []
        for pid, name in name_lookup.items():
            tok = anon.token_for_person(pid)
            if tok is None or tok == "P000":
                continue
            roster.append({"token": tok, "hint": name[:6]})
        # 稳定排序方便调试
        roster.sort(key=lambda x: x["token"])
        return roster

    def _parse_response(
        self, data: dict, anon: Anonymizer
    ) -> RelationshipParseResult:
        edges_raw = data.get("edges")
        unknowns_raw = data.get("unknown_mentions")

        unknowns = _clean_str_list(unknowns_raw)

        if not isinstance(edges_raw, list):
            return RelationshipParseResult(
                unknown_mentions=unknowns,
                error="LLM 输出缺少 edges 字段或类型不对",
            )

        seen_pairs: set[tuple[int, int]] = set()
        proposals: list[ProposedEdge] = []
        for raw in edges_raw:
            if not isinstance(raw, dict):
                continue
            a_tok = _as_token(raw.get("a"))
            b_tok = _as_token(raw.get("b"))
            if not a_tok or not b_tok or a_tok == b_tok:
                continue
            if a_tok == "P000" or b_tok == "P000":
                # peer-only：me 边由别的流程处理
                continue
            a_id = anon.person_id_for_token(a_tok)
            b_id = anon.person_id_for_token(b_tok)
            if a_id is None or b_id is None:
                # LLM 编了一个不存在的 token；丢弃
                _log.debug("丢弃未知 token: %s ↔ %s", a_tok, b_tok)
                continue
            lo, hi = (a_id, b_id) if a_id <= b_id else (b_id, a_id)
            if (lo, hi) in seen_pairs:
                continue
            seen_pairs.add((lo, hi))

            strength = _coerce_strength(raw.get("strength"))
            frequency = _coerce_frequency(raw.get("frequency"))
            # LLM 看到的是脱敏文本，所以 context / rationale 里可能直接
            # 嵌着 Pxxx / Cxxx token（rationale 尤其常见，因为它就是引用
            # 原句）。落到 ProposedEdge 之前先反匿名，避免泄露到 UI。
            context = anon.deanonymize_text(_coerce_optional_str(raw.get("context")))
            rationale = anon.deanonymize_text(
                _coerce_optional_str(raw.get("rationale"))
            )

            a_name = anon.name_for_person_token(a_tok) or f"P{a_id}"
            b_name = anon.name_for_person_token(b_tok) or f"P{b_id}"
            proposals.append(
                ProposedEdge(
                    a_id=a_id,
                    a_name=a_name,
                    b_id=b_id,
                    b_name=b_name,
                    strength=strength,
                    context=context,
                    frequency=frequency,
                    rationale=rationale,
                )
            )

        return RelationshipParseResult(
            proposals=proposals,
            unknown_mentions=unknowns,
        )


# ---------------------------------------------------------------- helpers
def _as_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _PXXX_RE.fullmatch(text):
        return None
    return text


def _coerce_strength(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):  # 防 True/False 漂进 int 校验
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 5 else None
    if isinstance(value, float) and value.is_integer():
        iv = int(value)
        return iv if 1 <= iv <= 5 else None
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            iv = int(s)
            return iv if 1 <= iv <= 5 else None
    return None


def _coerce_frequency(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    return s if s in _VALID_FREQUENCY else None


def _coerce_optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s or None


def _clean_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out
