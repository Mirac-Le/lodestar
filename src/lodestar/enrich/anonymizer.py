"""Local name + company anonymizer.

Rewrites in-table person names → opaque `Pxxx` tokens and known company
names → `Cxxx` tokens before any text leaves the machine, and reverses
both mappings on LLM output. The mapping is constructed once per
owner-scope and reused across all rows in a run so that the same person
or company consistently maps to the same token — this is what lets the
LLM say "P037 在 C005" and us recover the real edge afterwards.

Why two prefixes:

* Persons (`P`) and companies (`C`) live in different namespaces in
  the prompt, so the LLM can be told *companies named with `Cxxx` are
  already-known employers* without ambiguity with person tokens.
* L1 task explicitly *needs* to surface new company names from free
  text — we therefore can't pre-anonymize companies the user hasn't
  seen yet. We can only protect the ones already structured under
  `person.companies` for any contact in this owner's roster. That's an
  incremental privacy win, not a perfect one — documented intentionally.

Design choices:

* **Longest entity first** when substituting — otherwise "国信证券" inside
  "国信证券深圳分公司" would get replaced before the longer match. We
  pre-sort all entities (persons + companies) by `len()` desc and walk
  them in one pass.
* **Boundary-free matching** is intentional: Chinese names/companies
  usually appear without word boundaries inside free text. We do guard
  against partial English/digit collisions by skipping replacements
  where the matched span is preceded or followed by an ASCII alphanumeric
  character.
* **`me` is included** in the alias table (always P000) so the LLM can
  reason about "P000 认识 P037" without us leaking the owner's display
  name.
* **Unknown tokens** in the LLM output (e.g. a `P999` we didn't issue)
  are dropped silently rather than invented — `*_for_token` returns None.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


_ASCII_ALNUM = re.compile(r"[A-Za-z0-9]")

EntityKind = Literal["person", "company"]


@dataclass(frozen=True)
class _Entry:
    kind: EntityKind
    entity_id: int  # person_id for persons; positional id for companies
    name: str
    token: str


class Anonymizer:
    """Bidirectional name ↔ token mapping covering persons and companies.

    Build with `from_people_and_companies(...)`. Then call:
      - `anonymize_text(...)` on every free-text field before sending.
      - `token_for_person(pid)` / `token_for_company(name)` to refer to a
        known entity in the prompt structure.
      - `person_id_for_token(...)` / `company_for_token(...)` to resolve a
        token returned by the LLM.
    """

    def __init__(self, entries: list[_Entry]) -> None:
        self._entries = entries
        self._person_by_pid: dict[int, _Entry] = {
            e.entity_id: e for e in entries if e.kind == "person"
        }
        self._person_by_token: dict[str, _Entry] = {
            e.token: e for e in entries if e.kind == "person"
        }
        self._company_by_name: dict[str, _Entry] = {
            e.name: e for e in entries if e.kind == "company"
        }
        self._company_by_token: dict[str, _Entry] = {
            e.token: e for e in entries if e.kind == "company"
        }
        # Substitution order: longest name first (across both kinds) to
        # avoid prefix collisions like "国信证券" inside "国信证券深圳".
        self._sorted: list[_Entry] = sorted(
            entries, key=lambda e: len(e.name), reverse=True
        )

    # ------------------------------------------------------------------ build
    @classmethod
    def from_people_and_companies(
        cls,
        *,
        me_id: int,
        me_name: str,
        people: list[tuple[int, str]],
        companies: list[str],
    ) -> Anonymizer:
        """Build an anonymizer covering `me` (always P000), every contact
        in `people` (assigned P001, P002, … in input order), and every
        distinct company in `companies` (assigned C001, C002, … by first
        occurrence)."""
        entries: list[_Entry] = [
            _Entry(kind="person", entity_id=me_id, name=me_name, token="P000")
        ]
        seen_pids: set[int] = {me_id}
        idx = 1
        for pid, name in people:
            if pid in seen_pids:
                continue
            seen_pids.add(pid)
            entries.append(
                _Entry(kind="person", entity_id=pid, name=name, token=f"P{idx:03d}")
            )
            idx += 1

        seen_companies: set[str] = set()
        cidx = 1
        for raw in companies:
            name = (raw or "").strip()
            if not name or name in seen_companies:
                continue
            seen_companies.add(name)
            entries.append(
                _Entry(
                    kind="company",
                    entity_id=cidx,
                    name=name,
                    token=f"C{cidx:03d}",
                )
            )
            cidx += 1
        return cls(entries)

    # ------------------------------------------------------------------ stats
    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def person_count(self) -> int:
        return len(self._person_by_pid)

    @property
    def company_count(self) -> int:
        return len(self._company_by_name)

    # ------------------------------------------------------------ person side
    def token_for_person(self, person_id: int) -> str | None:
        e = self._person_by_pid.get(person_id)
        return e.token if e else None

    def name_for_person_token(self, token: str) -> str | None:
        e = self._person_by_token.get(token)
        return e.name if e else None

    def person_id_for_token(self, token: str) -> int | None:
        e = self._person_by_token.get(token)
        return e.entity_id if e else None

    # ----------------------------------------------------------- company side
    def token_for_company(self, name: str) -> str | None:
        e = self._company_by_name.get((name or "").strip())
        return e.token if e else None

    def company_for_token(self, token: str) -> str | None:
        e = self._company_by_token.get(token)
        return e.name if e else None

    # ----------------------------------------------------------- substitution
    def anonymize_text(self, text: str | None) -> str | None:
        """Replace every in-table person name AND every known company name
        with its token (`Pxxx` / `Cxxx`).

        Entities are matched longest-first across both namespaces; matches
        preceded or followed by an ASCII alphanumeric character are skipped
        (defensive — keeps e.g. an English-letter table key from getting
        partially rewritten).
        """
        if not text:
            return text
        out = text
        for entry in self._sorted:
            if not entry.name:
                continue
            out = self._safe_replace(out, entry.name, entry.token)
        return out

    def anonymize_company(self, name: str | None) -> str | None:
        """Replace `name` with its `Cxxx` token if known, else return as-is.

        Used when the *structured* `already_known.companies` list is being
        forwarded — we want each entry to be a token if we have one,
        rather than a free-text substitution result."""
        if not name:
            return name
        cleaned = name.strip()
        e = self._company_by_name.get(cleaned)
        return e.token if e else cleaned

    # ----------------------------------------------------------- output side
    def deanonymize_companies(self, items: list[str]) -> list[str]:
        """Map LLM output company list back to real names.

        For each item:
          - exact `Cxxx` token → its real name (if known; else dropped).
          - free-text containing one or more `Cxxx` tokens (defensive,
            shouldn't normally happen) → tokens get reverse-substituted.
          - everything else → kept as-is (these are *new* companies the
            LLM extracted from free text; they were never anonymized).
        """
        out: list[str] = []
        for raw in items:
            if not isinstance(raw, str):
                continue
            text = raw.strip()
            if not text:
                continue
            # Exact-token match → reverse map (drop unknown tokens entirely
            # so the LLM can't fabricate references to companies it never saw).
            if _C_TOKEN_RE.fullmatch(text):
                real = self.company_for_token(text)
                if real is not None:
                    out.append(real)
                continue
            # Mixed string with embedded tokens → reverse-substitute.
            if _C_TOKEN_RE.search(text):
                text = _C_TOKEN_RE.sub(
                    lambda m: self.company_for_token(m.group(0)) or "", text
                ).strip()
                if not text:
                    continue
            out.append(text)
        # de-dup preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for x in out:
            if x not in seen:
                seen.add(x)
                deduped.append(x)
        return deduped

    # ----------------------------------------------------------- internals
    @staticmethod
    def _safe_replace(text: str, needle: str, token: str) -> str:
        if needle not in text:
            return text
        parts: list[str] = []
        i = 0
        nlen = len(needle)
        while True:
            j = text.find(needle, i)
            if j == -1:
                parts.append(text[i:])
                break
            prev_ch = text[j - 1] if j > 0 else ""
            next_ch = text[j + nlen] if j + nlen < len(text) else ""
            if _ASCII_ALNUM.match(prev_ch) or _ASCII_ALNUM.match(next_ch):
                # Looks like part of an English word/identifier — skip.
                parts.append(text[i : j + nlen])
                i = j + nlen
                continue
            parts.append(text[i:j])
            parts.append(token)
            i = j + nlen
        return "".join(parts)


_C_TOKEN_RE = re.compile(r"\bC\d{3}\b")
