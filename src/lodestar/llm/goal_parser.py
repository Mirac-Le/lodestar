"""Parse "我想做X" freeform goals into structured intent.

IMPORTANT framing: every field below describes the CONTACT THE USER
NEEDS TO REACH in order to achieve the goal — *not* attributes of
the goal topic, and *not* attributes of the user themselves.

Example — "我想募 AI 算力种子轮" (I want to raise a seed round for an
AI-compute startup) should yield attributes of investors / FOF
managers / VCs / BDs — i.e. the people who can help — not attributes
of other AI-compute founders.
"""

from __future__ import annotations

import json

from lodestar.llm.base import LLMClient
from lodestar.models import GoalIntent

_SYSTEM_PROMPT = """You are an expert at analyzing a user's goal and extracting
the profile of the HELPFUL CONTACT they should reach. You are searching a
personal relationship graph; every output describes *the person they need
to find*, not the topic of the goal and not the user.

Given a goal in any language, return a JSON object with these keys:

  helper_roles:      target person's job roles / titles (e.g. "投资人",
                     "FOF 管理合伙人", "衍生品 MD", "政府处长"). Roles
                     must make sense as a HELPER for the goal.
  helper_industries: industries the helper works in (e.g. "私募基金",
                     "风险投资", "三方财富", "政府国资").
  helper_skills:     expertise / resources the helper should have
                     (e.g. "募资", "二级市场撮合", "监管政策解读",
                     "法律合规").
  topic_keywords:    short keywords describing the SUBJECT of the goal,
                     useful for loose matching (e.g. "AI", "算力",
                     "种子轮"). Keep it short (≤ 5 items).
  cities:            cities / regions explicitly relevant to the helper's
                     location, if any.
  helper_description: ONE vivid Chinese sentence describing the ideal
                     helper, written in the form of a contact-card bio.
                     Must mention the helper's role and industry.
                     This sentence is embedded for vector search.

Guidelines:
  • Never output attributes of the USER's own role (e.g. "创始人" / "CEO"
    for a founder's goal). Output the COUNTERPARTY.
  • Never output attributes of the goal topic as a role (e.g. for an AI
    fundraising goal, do NOT say role="AI架构师" — that's the topic, not
    the helper).
  • If a goal has multiple valid helper types (e.g. "投资人 OR 有资源的
    BD OR 政府背书"), list all of them in helper_roles.
  • If a field is unknown, return an empty array or empty string.
  • Output JSON only, no markdown.
  • Do NOT include any other keys.

Example input:  "我想募 AI 算力种子轮"
Example output:
{
  "helper_roles":       ["投资人", "VC 合伙人", "FOF 管理合伙人",
                          "家办负责人", "投资人关系 BD"],
  "helper_industries":  ["私募基金", "风险投资", "投资银行",
                          "三方财富", "FOF"],
  "helper_skills":      ["种子轮募资", "机构 LP 对接", "项目撮合",
                          "估值谈判"],
  "topic_keywords":     ["AI", "算力", "种子轮", "初创"],
  "cities":             [],
  "helper_description": "一位活跃在私募 / 风投 / FOF 领域的资深投资人或管理合伙人，\
擅长种子轮募资与机构 LP 对接，手上有可以对接 AI 算力项目的资金或撮合资源。"
}
"""


class GoalParser:
    """Turns a natural-language goal into a `GoalIntent`.

    Internally the LLM returns helper-centric fields
    (helper_roles / helper_industries / helper_skills /
    topic_keywords / helper_description); we map them onto the
    existing GoalIntent schema to keep downstream code unchanged:

        helper_roles        → GoalIntent.roles
        helper_industries   → GoalIntent.industries
        helper_skills       → GoalIntent.skills
        topic_keywords      → GoalIntent.keywords
        helper_description  → GoalIntent.summary   (embedded for vector search)
    """

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def parse(self, goal: str) -> GoalIntent:
        raw = self._llm.complete_json(_SYSTEM_PROMPT, goal)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}

        # New helper-centric schema (preferred)
        helper_roles = _coerce_list(data.get("helper_roles"))
        helper_inds = _coerce_list(data.get("helper_industries"))
        helper_skills = _coerce_list(data.get("helper_skills"))
        topic = _coerce_list(data.get("topic_keywords"))
        cities = _coerce_list(data.get("cities"))
        helper_desc = str(data.get("helper_description") or "").strip()

        # Back-compat: fall back to the old schema if the LLM produced it.
        if not helper_roles and "roles" in data:
            helper_roles = _coerce_list(data.get("roles"))
        if not helper_inds and "industries" in data:
            helper_inds = _coerce_list(data.get("industries"))
        if not helper_skills and "skills" in data:
            helper_skills = _coerce_list(data.get("skills"))
        if not topic and "keywords" in data:
            topic = _coerce_list(data.get("keywords"))
        if not helper_desc and "summary" in data:
            helper_desc = str(data.get("summary") or "").strip()

        return GoalIntent(
            original=goal,
            keywords=topic,
            skills=helper_skills,
            industries=helper_inds,
            roles=helper_roles,
            cities=cities,
            summary=helper_desc,
        )


def _coerce_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []
