"""L1 attribute extraction.

Goal: turn each contact's free-text fields (`bio`, `notes`, `tags`,
`cities` originally storing a region phrase, etc.) into clean,
deduplicated structured attributes:

  * companies — concrete employer / institution names
  * cities    — city-level locations only (drops "中国" / "华东")
  * titles    — short role labels (eg "基金经理", "投顾")
  * extra_tags — semantic tags the LLM judges as discriminating

Behavior:

  * **Additive only.** If a field already has values from manual
    import, we only *append* what the LLM finds (deduped) — we never
    delete existing values.
  * **Privacy.** All free text is anonymized through `Anonymizer`
    before leaving the machine. The L1 task does not need to mention
    other people, so we do not expect Pxxx tokens in the output; if
    they do appear we silently strip them.
  * **Resilience.** Any single-row failure is logged and skipped;
    the run continues. The CLI prints a summary at the end.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from lodestar.db.repository import Repository
from lodestar.enrich.anonymizer import Anonymizer
from lodestar.enrich.client import LLMClient, LLMError
from lodestar.models import Person


_log = logging.getLogger(__name__)


_PXXX_RE = re.compile(r"\bP\d{3}\b")
_CXXX_RE = re.compile(r"\bC\d{3}\b")
# Strings that look like geography but aren't a city — strip from cities[].
_NON_CITY = {
    "中国", "全国", "海外", "国内", "境外", "亚洲", "欧洲", "北美",
    "华东", "华南", "华北", "西南", "西北", "东北", "中部",
}


@dataclass
class L1Result:
    """Per-row diff: what we propose to add to the Person."""

    person_id: int
    name: str
    add_companies: list[str] = field(default_factory=list)
    add_cities: list[str] = field(default_factory=list)
    add_titles: list[str] = field(default_factory=list)
    add_tags: list[str] = field(default_factory=list)
    error: str | None = None

    def is_empty(self) -> bool:
        return not (
            self.add_companies
            or self.add_cities
            or self.add_titles
            or self.add_tags
        )


SYSTEM_PROMPT = """你是结构化抽取助手。任务：从一个联系人的中文自由文本字段里，
抽取出**规范化的属性**，输出严格 JSON。

输入说明（重要）：
- 文本中可能出现 `Pxxx`（如 P000、P037）—— 这是已脱敏的人名 token，**不是**
  公司或地点，也**不是**该联系人本人，请忽略。
- 文本中可能出现 `Cxxx`（如 C001、C042）—— 这是已脱敏的**机构 token**。
  它们指代我们已经在结构化字段里登记过的机构，等价于公司名。
- `already_known.companies` 里也会用 `Cxxx` token 表示已登记机构。

输出字段要求：

1. companies: 该联系人当前/曾经任职的**机构名**列表。
   - **如果该机构在原文里以 `Cxxx` 形式出现，请直接照原样输出 `Cxxx`，
     不要还原成猜测的真名**（你看不到真名）。
   - 如果原文出现的是明文机构名（说明系统还不认识它），请输出**机构正式名**，
     去掉部门/分公司后缀（"国信证券深圳分公司" → "国信证券"）。
   - 没有就空数组。
2. cities: **城市级别**地理位置，仅城市名（"上海"、"深圳"、"杭州"）。
   - 不要省、地区、"中国"、"全国"这种粗粒度词。
   - cities 里**绝不**输出 Pxxx 或 Cxxx。
3. titles: 简短的**职位/角色**标签（"基金经理"、"投顾"、"FOF 总监"）。
   - 不要带公司前缀，只要职位本身。
   - 不要输出 Pxxx 或 Cxxx。
4. extra_tags: 你认为有助于检索的**语义标签**（如"私募 FOF"、"股多策略"、
   "券商渠道"）。只放具有区分度的术语，不要照抄整段文字，不要重复
   companies/cities/titles 已有内容。不要输出 Pxxx 或 Cxxx。
5. 不要输出任何不在原文里的事实。原文没提的字段就给空数组。
6. 不要解释；只输出 JSON。

JSON schema（严格遵循 key 名）:
{
  "companies": ["..."],
  "cities":    ["..."],
  "titles":    ["..."],
  "extra_tags":["..."]
}"""


class L1Extractor:
    """Run L1 on every person in the network."""

    def __init__(
        self,
        repo: Repository,
        client: LLMClient,
    ) -> None:
        self._repo = repo
        self._client = client

    # ------------------------------------------------------------------
    def build_anonymizer(self) -> Anonymizer:
        """Construct an Anonymizer covering the entire roster.

        Includes:
          - every person (`Pxxx` tokens; `me` is always P000)
          - every distinct company already structured under any
            contact's `companies[]` (`Cxxx` tokens, longest first by
            char length so substring collisions don't bite)

        Public so callers (e.g. the web `preview` endpoint) can reuse the
        same mapping when running ad-hoc extractions on synthetic input.
        """
        all_people = self._repo.list_people()
        me = self._repo.get_me()
        me_name = me.name if me else "我"
        me_id = me.id if me and me.id is not None else -1
        # Collect every known company across all contacts. Sort longest
        # first so the anonymizer assigns Cxxx tokens to the longer
        # variants ("国信证券深圳分公司") before the shorter prefixes
        # ("国信证券"). Secondary sort by name keeps token IDs
        # deterministic across calls (set iteration order is hash-seeded).
        company_set: set[str] = set()
        for p in all_people:
            for c in p.companies or []:
                if c and c.strip():
                    company_set.add(c.strip())
        companies_sorted = sorted(company_set, key=lambda c: (-len(c), c))
        return Anonymizer.from_people_and_companies(
            me_id=me_id,
            me_name=me_name,
            people=[(p.id, p.name) for p in all_people if p.id is not None],
            companies=companies_sorted,
        )

    def run(
        self,
        *,
        limit: int | None = None,
        only_missing: bool = True,
        progress_cb: object | None = None,
    ) -> list[L1Result]:
        """Iterate the owner's people and produce a list of diffs.

        Caller is responsible for actually applying them via `apply()`.
        This split lets the CLI implement `--dry-run`.

        `progress_cb` is an optional callable `(idx, total, current_name)`
        invoked after each row — used by the web background-job runner
        to push progress to the frontend.
        """
        people = self._repo.list_people()
        if limit is not None:
            people = people[:limit]

        # Build the anonymizer once, covering EVERY contact in the roster
        # (not just the rows we're enriching). This way the LLM sees
        # consistent Pxxx tokens even if a row's bio mentions a contact
        # that's not currently being processed.
        anonymizer = self.build_anonymizer()

        results: list[L1Result] = []
        total = len(people)
        for idx, p in enumerate(people, start=1):
            if p.id is None:
                continue
            if only_missing and self._already_enriched(p):
                if callable(progress_cb):
                    progress_cb(idx, total, p.name)  # type: ignore[misc]
                continue
            try:
                diff = self.extract_for_person(p, anonymizer=anonymizer)
            except LLMError as exc:
                results.append(L1Result(person_id=p.id, name=p.name, error=str(exc)))
                _log.warning("L1 失败 [%s]: %s", p.name, exc)
                if callable(progress_cb):
                    progress_cb(idx, total, p.name)  # type: ignore[misc]
                continue
            results.append(diff)
            if callable(progress_cb):
                progress_cb(idx, total, p.name)  # type: ignore[misc]
        return results

    def apply(self, results: list[L1Result]) -> int:
        """Persist diffs back to the DB. Returns the number of rows touched."""
        touched = 0
        for r in results:
            if r.error or r.is_empty():
                continue
            person = self._repo.get_person(r.person_id)
            if person is None:
                continue
            new_companies = _merge(person.companies, r.add_companies)
            new_cities = _merge(person.cities, r.add_cities)
            new_tags = _merge(person.tags, r.add_tags + r.add_titles)
            if (
                new_companies == person.companies
                and new_cities == person.cities
                and new_tags == person.tags
            ):
                continue
            person.companies = new_companies
            person.cities = new_cities
            person.tags = new_tags
            self._repo.update_person(person)
            touched += 1
        return touched

    # ------------------------------------------------------------------
    def extract_for_person(
        self,
        person: Person,
        *,
        anonymizer: Anonymizer | None = None,
    ) -> L1Result:
        """Run L1 on a single existing Person. Used by the per-person
        web endpoint and (internally) by `run()`.

        If `anonymizer` is omitted we build a fresh one — safe but extra
        DB work; pass one in when looping.
        """
        if person.id is None:
            raise LLMError("person.id is None")
        anon = anonymizer or self.build_anonymizer()
        return self._extract_one(person, anon)

    def extract_for_input(
        self,
        *,
        name: str | None,
        bio: str | None = None,
        notes: str | None = None,
        raw_tags: list[str] | None = None,
        raw_cities: list[str] | None = None,
        known_companies: list[str] | None = None,
        known_cities: list[str] | None = None,
        known_tags: list[str] | None = None,
    ) -> L1Result:
        """Run L1 on freeform input (a person not yet in the DB).

        Used by the `POST /api/enrich/preview` endpoint that powers the
        "AI 解析背景" button on the add-contact dialog. We piggyback on
        the existing anonymizer so any in-table names mentioned inside
        `bio` still get redacted; the synthetic name itself is passed
        through with a placeholder token (P_NEW) since this person is
        not yet known to the graph.
        """
        anon = self.build_anonymizer()
        synthetic = Person(
            id=-1,  # placeholder; never written
            name=(name or "").strip() or "(unnamed)",
            bio=bio,
            notes=notes,
            tags=raw_tags or [],
            companies=known_companies or [],
            cities=(known_cities or []) + (raw_cities or []),
            needs=[],
        )
        # Build a payload that uses P_NEW for the row being previewed, and
        # otherwise reuses the anonymizer for any in-table mentions.
        payload = self._build_input_for_synthetic(
            synthetic,
            anon,
            already_known={
                "companies": known_companies or [],
                "cities": known_cities or [],
                "tags": known_tags or [],
            },
        )
        result = self._client.chat_json(system=SYSTEM_PROMPT, user=payload)
        return self._parse_llm_response(
            person_id=-1, name=synthetic.name, data=result.data, anonymizer=anon
        )

    # ------------------------------------------------------------------
    def _already_enriched(self, p: Person) -> bool:
        """Skip rows that already look populated. We use the presence of
        BOTH companies and cities as a cheap proxy — if either is missing,
        the row is still worth enriching."""
        return bool(p.companies) and bool(p.cities)

    def _extract_one(self, person: Person, anonymizer: Anonymizer) -> L1Result:
        if person.id is None:
            raise LLMError("person.id is None")
        payload = self._build_input(person, anonymizer)
        result = self._client.chat_json(system=SYSTEM_PROMPT, user=payload)
        return self._parse_llm_response(
            person_id=person.id,
            name=person.name,
            data=result.data,
            anonymizer=anonymizer,
        )

    def _parse_llm_response(
        self,
        *,
        person_id: int,
        name: str,
        data: dict[str, object],
        anonymizer: Anonymizer,
    ) -> L1Result:
        # Companies: deanonymize Cxxx tokens back to real names; pass-through
        # any free-text new company names; drop stray Pxxx tokens entirely.
        raw_companies = _clean_list(data.get("companies"))
        raw_companies = [c for c in raw_companies if not _PXXX_RE.fullmatch(c)]
        companies = anonymizer.deanonymize_companies(raw_companies)

        # Cities / titles / tags should never be Pxxx OR Cxxx — strip both.
        cities = [
            c
            for c in _clean_list(data.get("cities"))
            if c not in _NON_CITY
            and not _PXXX_RE.fullmatch(c)
            and not _CXXX_RE.fullmatch(c)
        ]
        titles = [
            c
            for c in _clean_list(data.get("titles"))
            if not _PXXX_RE.fullmatch(c) and not _CXXX_RE.fullmatch(c)
        ]
        tags = [
            c
            for c in _clean_list(data.get("extra_tags"))
            if not _PXXX_RE.fullmatch(c) and not _CXXX_RE.fullmatch(c)
        ]
        return L1Result(
            person_id=person_id,
            name=name,
            add_companies=companies,
            add_cities=cities,
            add_titles=titles,
            add_tags=tags,
        )

    def _build_input_for_synthetic(
        self,
        synthetic: Person,
        anonymizer: Anonymizer,
        *,
        already_known: dict[str, list[str]],
    ) -> str:
        import json as _json

        body: dict[str, Any] = {
            "person_token": "P_NEW",
            "raw_text_fields": {
                "bio": anonymizer.anonymize_text(synthetic.bio),
                "notes": anonymizer.anonymize_text(synthetic.notes),
                "raw_tags": [anonymizer.anonymize_text(t) for t in synthetic.tags],
                "raw_cities": [
                    anonymizer.anonymize_text(c) for c in synthetic.cities
                ],
            },
            # Anonymize known companies in the structured payload too — for
            # synthetic input these may be brand-new strings that the
            # owner-scoped anonymizer hasn't seen, in which case
            # `anonymize_company` returns them as-is. That's intentional:
            # this is incremental privacy on already-structured fields.
            "already_known": {
                "companies": [
                    anonymizer.anonymize_company(c) or c
                    for c in already_known.get("companies", [])
                ],
                "cities": already_known.get("cities", []),
                "tags": already_known.get("tags", []),
            },
        }
        return _json.dumps(body, ensure_ascii=False, indent=2)

    def _build_input(self, person: Person, anonymizer: Anonymizer) -> str:
        """Construct the JSON user-message payload sent to the LLM.

        Only this person's free-text content is forwarded. Their own name
        is replaced with their Pxxx token so it never leaves the machine.
        Fields already structured (companies/cities/tags) are passed in
        too as `already_known` so the LLM can see what's already known and
        not duplicate it. Companies are forwarded as `Cxxx` tokens — the
        LLM is told in the system prompt to echo back the same `Cxxx`
        when the contact's bio still mentions the same employer."""
        own_token = (
            anonymizer.token_for_person(person.id) if person.id is not None else "P???"
        )
        body: dict[str, Any] = {
            "person_token": own_token,
            "raw_text_fields": {
                "bio": anonymizer.anonymize_text(person.bio),
                "notes": anonymizer.anonymize_text(person.notes),
                "raw_tags": [anonymizer.anonymize_text(t) for t in person.tags],
                "raw_cities": [anonymizer.anonymize_text(c) for c in person.cities],
            },
            "already_known": {
                "companies": [
                    anonymizer.anonymize_company(c) or c for c in person.companies
                ],
                "cities": person.cities,
                "tags": person.tags,
            },
        }
        import json as _json

        return _json.dumps(body, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------- helpers


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        out.append(text)
    # de-dup preserving order
    seen: set[str] = set()
    result: list[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            result.append(x)
    return result


def _merge(existing: list[str], additions: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in list(existing) + list(additions):
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out
