"""Excel (.xlsx) importer with flexible column mapping.

Designed around the real-world format used by `pyq.xlsx` / `demo_network.xlsx`:

    序号 | 姓名 | 所属行业 | 公司 | 职务 | 城市 | AI标准化特征 |
    可信度（...） | 潜在需求 | 认识 | 备注

…but any column present can be mapped. Two built-in presets:

* `chinese_finance_preset()` — legacy pyq.xlsx (no peer edges).
* `extended_network_preset()` — adds 公司 / 城市 / 认识 / 备注 columns,
  and builds peer-to-peer edges from the `认识` column.

If the workbook has a second sheet named `关系` (or `edges`) with columns
`(甲, 乙, 强度, 关系, 频率)`, those rows are imported as authoritative
edges and override anything parsed from the `认识` column.

If `infer_colleagues=True`, every pair of people sharing at least one
company also gets a strong (default = 4) edge.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from lodestar.db.repository import Repository
from lodestar.models import Frequency, Person, Relationship

# Any of these, possibly mixed inside a single cell, separates list items.
_LIST_SPLIT_RE = re.compile(r"[,，、;；/／\|｜]+")

# Only `;` / `；` / newlines separate peer entries
# (names themselves may contain commas in parenthetical `(strength,context)`).
_PEER_SPLIT_RE = re.compile(r"[;；\n]+")

# `名字(4,大学同学)` / `名字(4)` / `名字(大学同学)` / `名字`
_PEER_ENTRY_RE = re.compile(
    r"""^\s*
        (?P<name>[^()（）,，]+?)
        (?:\s*[（(]\s*
            (?P<inside>[^）)]*)
         \s*[)）])?
        \s*$
    """,
    re.VERBOSE,
)

_FREQ_ALIASES: dict[str, Frequency] = {
    "w": Frequency.WEEKLY,
    "weekly": Frequency.WEEKLY,
    "每周": Frequency.WEEKLY,
    "周": Frequency.WEEKLY,
    "m": Frequency.MONTHLY,
    "monthly": Frequency.MONTHLY,
    "每月": Frequency.MONTHLY,
    "月": Frequency.MONTHLY,
    "q": Frequency.QUARTERLY,
    "quarterly": Frequency.QUARTERLY,
    "每季": Frequency.QUARTERLY,
    "季": Frequency.QUARTERLY,
    "y": Frequency.YEARLY,
    "yearly": Frequency.YEARLY,
    "每年": Frequency.YEARLY,
    "年": Frequency.YEARLY,
    "r": Frequency.RARE,
    "rare": Frequency.RARE,
    "极少": Frequency.RARE,
    "少": Frequency.RARE,
}


def _split_multi(text: str) -> list[str]:
    """Split a Chinese-or-English comma/slash-separated string into clean items."""
    if not text:
        return []
    parts = [p.strip() for p in _LIST_SPLIT_RE.split(text)]
    return [p for p in parts if p]


def _split_peers(text: str) -> list[str]:
    if not text:
        return []
    parts = [p.strip() for p in _PEER_SPLIT_RE.split(text)]
    return [p for p in parts if p]


@dataclass
class PeerEntry:
    name: str
    strength: int | None = None
    context: str | None = None


def _parse_peer_entry(raw: str) -> PeerEntry | None:
    """Parse one entry like `建国哥(4,大学同学)` or `王毅`."""
    m = _PEER_ENTRY_RE.match(raw)
    if not m:
        return None
    name = m.group("name").strip()
    inside = (m.group("inside") or "").strip()
    strength: int | None = None
    context: str | None = None
    if inside:
        # Split on , 、 ，
        parts = [p.strip() for p in re.split(r"[,，、]", inside) if p.strip()]
        for part in parts:
            if part.isdigit() and strength is None:
                try:
                    strength = max(1, min(5, int(part)))
                except ValueError:
                    pass
            else:
                context = part if context is None else f"{context}/{part}"
    return PeerEntry(name=name, strength=strength, context=context)


def _parse_frequency(raw: str | None) -> Frequency:
    if not raw:
        return Frequency.YEARLY
    return _FREQ_ALIASES.get(raw.strip().lower(), Frequency.YEARLY)


@dataclass
class ColumnMapping:
    """Declarative mapping from Excel columns to Person fields.

    List-typed fields (tags/skills/companies/cities/needs) accept any
    number of source columns; their cell contents are concatenated and
    split on `,` `，` `、` `;` `；` `/` `｜`.

    `kind_column` controls Me-edge construction:
        "直接" / "direct" / empty → Me→X edge with row's strength (default)
        "弱"   / "弱认识" / "weak" → Me→X edge forced to strength = 1
        "目标" / "target"          → NO Me-edge; person reachable only
                                     through other peers' `认识` references
    """

    name: str
    bio: str | Callable[[dict[str, object]], str | None] | None = None
    notes: str | None = None
    tags: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)
    strength_column: str | None = None
    context_column: str | None = None
    peers_column: str | None = None
    kind_column: str | None = None


_KIND_DIRECT = "direct"
_KIND_WEAK = "weak"
_KIND_TARGET = "target"


def _normalize_kind(raw: str) -> str:
    """Map various Chinese / English values to one of the three canonical kinds."""
    text = raw.strip().lower()
    if text in {"目标", "target", "未认识", "想认识", "陌生"}:
        return _KIND_TARGET
    if text in {"弱", "弱认识", "weak", "听说", "认识", "听说过"}:
        return _KIND_WEAK
    return _KIND_DIRECT


def chinese_finance_preset() -> ColumnMapping:
    """Legacy preset matching the columns of pyq.xlsx (no peer edges)."""

    def compose_bio(row: dict[str, object]) -> str | None:
        parts: list[str] = []
        role = _cell(row.get("职务"))
        industry = _cell(row.get("所属行业"))
        if industry:
            parts.append(f"行业：{industry}")
        if role:
            parts.append(f"职务：{role}")
        return " · ".join(parts) if parts else None

    return ColumnMapping(
        name="姓名",
        bio=compose_bio,
        tags=["所属行业", "AI标准化特征"],
        needs=["潜在需求"],
        strength_column="可信度（言行一致性0-5分）",
        context_column="职务",
    )


def extended_network_preset() -> ColumnMapping:
    """Preset for the richer template (demo_network.xlsx).

    Adds `公司`, `城市`, `认识`, `备注` columns on top of the chinese
    finance preset. Any of these columns can be absent — the importer
    simply skips anything it doesn't find.
    """

    def compose_bio(row: dict[str, object]) -> str | None:
        parts: list[str] = []
        industry = _cell(row.get("所属行业"))
        role = _cell(row.get("职务"))
        company = _cell(row.get("公司"))
        city = _cell(row.get("城市"))
        if industry:
            parts.append(f"行业：{industry}")
        if role:
            parts.append(f"职务：{role}")
        if company:
            parts.append(f"公司：{company}")
        if city:
            parts.append(f"城市：{city}")
        return " · ".join(parts) if parts else None

    return ColumnMapping(
        name="姓名",
        bio=compose_bio,
        notes="备注",
        tags=["所属行业", "AI标准化特征"],
        companies=["公司"],
        cities=["城市"],
        needs=["潜在需求"],
        strength_column="可信度（言行一致性0-5分）",
        context_column="职务",
        peers_column="认识",
        kind_column="关系类型",
    )


def _cell(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null", "-"}:
        return ""
    return text


def _read_any_spreadsheet(path: str | Path) -> pl.DataFrame:
    suffix = Path(path).suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        return pl.read_excel(path)
    if suffix == ".csv":
        return pl.read_csv(path, infer_schema_length=0)
    raise ValueError(f"Unsupported spreadsheet format: {suffix}")


def _read_relations_sheet(path: str | Path) -> pl.DataFrame | None:
    """Read the optional `关系` / `edges` sheet if present."""
    suffix = Path(path).suffix.lower()
    if suffix not in {".xlsx", ".xlsm"}:
        return None
    for sheet_name in ("关系", "人脉", "edges", "relationships"):
        try:
            df = pl.read_excel(path, sheet_name=sheet_name)
        except Exception:
            continue
        if df.height > 0:
            return df
    return None


@dataclass
class ImportStats:
    people: int
    peer_edges: int
    colleague_edges: int


class ExcelImporter:
    """Reads a spreadsheet and upserts people + Me-edges + peer-edges.

    Two-pass design:

    * Pass 1 — upsert every Person row (deduplicated by name, later rows
      only add information, never erase).
    * Pass 2 — parse the `认识` column + optional `关系` sheet + optional
      same-company inference into Relationship rows between peers.
    """

    def __init__(
        self,
        repo: Repository,
        mapping: ColumnMapping | None = None,
        *,
        infer_colleagues: bool = True,
        colleague_strength: int = 4,
    ) -> None:
        self._repo = repo
        self._mapping = mapping or extended_network_preset()
        self._infer_colleagues = infer_colleagues
        self._colleague_strength = colleague_strength

    # Back-compat: the CLI still calls `.import_file(path)` expecting an int.
    def import_file(self, path: str | Path) -> int:
        stats = self.import_with_stats(path)
        return stats.people

    def import_with_stats(self, path: str | Path) -> ImportStats:
        me = self._repo.get_me()
        if me is None or me.id is None:
            raise RuntimeError("No 'me' record. Run `lodestar init` first.")

        df = _read_any_spreadsheet(path)
        if self._mapping.name not in df.columns:
            raise ValueError(
                f"Required column '{self._mapping.name}' not found. Available columns: {df.columns}"
            )

        # Pass 1 — upsert people + Me-edges.
        people_count = 0
        for row in df.iter_rows(named=True):
            if self._upsert_person_with_me_edge(row, me_id=me.id):
                people_count += 1

        # Pass 2 — peer edges from `认识` column.
        peer_count = self._build_peer_edges_from_column(df, me_id=me.id)

        # Pass 2b — authoritative edges from optional `关系` sheet.
        rel_sheet = _read_relations_sheet(path)
        if rel_sheet is not None:
            peer_count += self._build_peer_edges_from_sheet(rel_sheet, me_id=me.id)

        # Pass 3 — infer colleague edges (same company → medium-strong edge).
        colleague_count = 0
        if self._infer_colleagues:
            colleague_count = self._infer_colleague_edges(me_id=me.id)

        return ImportStats(
            people=people_count,
            peer_edges=peer_count,
            colleague_edges=colleague_count,
        )

    # ---------- pass 1 ----------
    def _upsert_person_with_me_edge(self, row: dict[str, object], *, me_id: int) -> bool:
        name = _cell(row.get(self._mapping.name))
        if not name:
            return False

        # Decide whether (and how) to build the Me → Person edge.
        kind_raw = (
            _cell(row.get(self._mapping.kind_column)) if self._mapping.kind_column else ""
        )
        kind = _normalize_kind(kind_raw)
        is_wishlist_row = kind == _KIND_TARGET

        person = Person(
            name=name,
            bio=self._resolve_bio(row),
            notes=_cell(row.get(self._mapping.notes)) if self._mapping.notes else None,
            tags=self._collect_attribute(row, self._mapping.tags),
            skills=self._collect_attribute(row, self._mapping.skills),
            companies=self._collect_attribute(row, self._mapping.companies),
            cities=self._collect_attribute(row, self._mapping.cities),
            needs=self._collect_attribute(row, self._mapping.needs),
            is_wishlist=is_wishlist_row,
        )

        existing = self._repo.find_person_by_name(name)
        if existing and existing.id is not None:
            person.id = existing.id
            if not person.bio:
                person.bio = existing.bio
            if not person.notes:
                person.notes = existing.notes
            person.tags = _merge_lists(existing.tags, person.tags)
            person.skills = _merge_lists(existing.skills, person.skills)
            person.companies = _merge_lists(existing.companies, person.companies)
            person.cities = _merge_lists(existing.cities, person.cities)
            person.needs = _merge_lists(existing.needs, person.needs)
            # Wishlist is sticky: once a row marks someone as a wishlist
            # contact, a later non-wishlist row should not silently drop it.
            person.is_wishlist = existing.is_wishlist or is_wishlist_row
            saved = self._repo.update_person(person)
        else:
            saved = self._repo.add_person(person)

        assert saved.id is not None

        if kind == _KIND_TARGET:
            # Wishlist contacts have NO Me-edge; they are reachable only via
            # peers' `认识` references in pass 2. The is_wishlist flag is
            # already persisted above and is now decoupled from path topology.
            return True

        if kind == _KIND_WEAK:
            strength = 1
        else:
            strength = self._parse_strength(row)

        context = (
            _cell(row.get(self._mapping.context_column)) if self._mapping.context_column else ""
        )
        self._repo.add_relationship(
            Relationship(
                source_id=me_id,
                target_id=saved.id,
                strength=strength,
                context=context or None,
                frequency=Frequency.YEARLY,
            )
        )
        return True

    # ---------- pass 2 ----------
    def _build_peer_edges_from_column(self, df: pl.DataFrame, *, me_id: int) -> int:
        col = self._mapping.peers_column
        if col is None or col not in df.columns:
            return 0

        added: set[tuple[int, int]] = set()
        warnings: list[str] = []

        for row in df.iter_rows(named=True):
            src_name = _cell(row.get(self._mapping.name))
            if not src_name:
                continue
            peers_raw = _cell(row.get(col))
            if not peers_raw:
                continue
            src = self._repo.find_person_by_name(src_name)
            if not src or src.id is None:
                continue

            for raw in _split_peers(peers_raw):
                entry = _parse_peer_entry(raw)
                if entry is None:
                    continue
                target = self._repo.find_person_by_name(entry.name)
                if not target or target.id is None:
                    warnings.append(f"[认识] '{src_name}' → '{entry.name}' 未在表内找到，已跳过")
                    continue
                if target.id == src.id or target.id == me_id or src.id == me_id:
                    # Me-edges are created in pass 1; skip self-loops.
                    continue

                pair = _canonical_pair(src.id, target.id)
                if pair in added:
                    continue
                added.add(pair)
                self._repo.add_relationship(
                    Relationship(
                        source_id=pair[0],
                        target_id=pair[1],
                        strength=entry.strength or 3,
                        context=entry.context,
                        frequency=Frequency.YEARLY,
                    )
                )

        for w in warnings:
            print(w)
        return len(added)

    def _build_peer_edges_from_sheet(self, df: pl.DataFrame, *, me_id: int) -> int:
        # Column aliases (be lenient).
        cols = {c.strip(): c for c in df.columns}

        def pick(*names: str) -> str | None:
            for n in names:
                if n in cols:
                    return cols[n]
            return None

        c_a = pick("甲", "A", "源", "source", "name_a")
        c_b = pick("乙", "B", "目标", "target", "name_b")
        c_s = pick("强度", "strength")
        c_c = pick("关系", "context", "描述")
        c_f = pick("频率", "frequency")
        if not c_a or not c_b:
            return 0

        added: set[tuple[int, int]] = set()
        for row in df.iter_rows(named=True):
            a = _cell(row.get(c_a))
            b = _cell(row.get(c_b))
            if not a or not b:
                continue
            pa = self._repo.find_person_by_name(a)
            pb = self._repo.find_person_by_name(b)
            if not pa or not pb or pa.id is None or pb.id is None:
                print(f"[关系] '{a}' ↔ '{b}' 未在表内找到，已跳过")
                continue
            if pa.id == pb.id or pa.id == me_id or pb.id == me_id:
                continue

            strength_raw = _cell(row.get(c_s)) if c_s else ""
            try:
                strength = max(1, min(5, int(strength_raw))) if strength_raw else 3
            except ValueError:
                strength = 3
            context = _cell(row.get(c_c)) if c_c else ""
            freq = _parse_frequency(_cell(row.get(c_f))) if c_f else Frequency.YEARLY

            pair = _canonical_pair(pa.id, pb.id)
            added.add(pair)
            self._repo.add_relationship(
                Relationship(
                    source_id=pair[0],
                    target_id=pair[1],
                    strength=strength,
                    context=context or None,
                    frequency=freq,
                )
            )
        return len(added)

    # ---------- pass 3 ----------
    def _infer_colleague_edges(self, *, me_id: int) -> int:
        """For every company with ≥2 members, connect them all (clique)."""
        people = self._repo.list_people()
        company_to_members: dict[str, list[int]] = {}
        for p in people:
            if p.id is None or p.id == me_id:
                continue
            for c in p.companies:
                key = c.strip()
                if not key:
                    continue
                company_to_members.setdefault(key, []).append(p.id)

        added: set[tuple[int, int]] = set()
        for members in company_to_members.values():
            if len(members) < 2:
                continue
            for i, a in enumerate(members):
                for b in members[i + 1 :]:
                    pair = _canonical_pair(a, b)
                    if pair in added:
                        continue
                    added.add(pair)
                    self._repo.add_relationship(
                        Relationship(
                            source_id=pair[0],
                            target_id=pair[1],
                            strength=self._colleague_strength,
                            context="同事",
                            frequency=Frequency.MONTHLY,
                        )
                    )
        return len(added)

    # ---------- helpers ----------
    def _resolve_bio(self, row: dict[str, object]) -> str | None:
        bio = self._mapping.bio
        if bio is None:
            return None
        if callable(bio):
            return bio(row) or None
        return _cell(row.get(bio)) or None

    def _collect_attribute(self, row: dict[str, object], columns: list[str]) -> list[str]:
        items: list[str] = []
        for col in columns:
            text = _cell(row.get(col))
            if not text:
                continue
            items.extend(_split_multi(text))
        # Deduplicate while preserving order
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    def _parse_strength(self, row: dict[str, object]) -> int:
        col = self._mapping.strength_column
        if col is None:
            return 3
        raw = row.get(col)
        try:
            value = int(raw) if raw is not None else 3  # type: ignore[arg-type]
        except (ValueError, TypeError):
            return 3
        return max(1, min(5, value))


def _canonical_pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


def _merge_lists(old: list[str], new: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in list(old) + list(new):
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
