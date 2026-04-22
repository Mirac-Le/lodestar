"""Excel (.xlsx) importer，单一 canonical preset。

设计原则：**一张表配一套规则**。

整个项目共享一份 13 列基础模板（`examples/richard_network.xlsx` /
`examples/template.xlsx`）。Tommy 那套金融机构画像表是它的**严格超集**
——多出 6 列「可投金额 / 风险偏好 / 共赢性 / 关系阶段 / 兴趣偏好 /
核心标签」，没有任何冲突字段。

因此 importer 只维护**一个** `default_preset()`，列处理分三类：

  CORE       —— 直接进 Person 字段
  PROFILE_BIO —— 拼到 bio 末尾，格式 "字段：值 · 字段：值"
  PROFILE_TAGS —— 进 tags（少量、可枚举的业务身份）

任何不在白名单里的列：丢掉，import 末尾打印 "已忽略以下列" 警告。

**为什么不再分 preset？** 历史上有 `richard_network_preset` /
`tommy_contacts_preset` 两个函数，但它们的实际差异只是 tommy 多了 6 列，
而 tommy preset 还配错了一堆列名（`身份职位` vs 实际 `职务`），导致
「认识」列被忽略 → tommy.db 里 0 条 contact↔contact 边 → 网页打开像
完全空。一份 preset、白名单驱动可以同时解决：维护成本、tommy bug、
未来用户用模板填部分列时的兼容性。

列名归一化：去全/半角空格、统一别名（如 `合作价值评分（0-5）` →
`合作价值（0-5）`），让填表人不必跟字符级别较真。

If the workbook has a second sheet named `关系` (or `edges`) with columns
`(甲, 乙, 强度, 关系, 频率)`, those rows are imported as authoritative
edges and override anything parsed from the `认识` column.

If `infer_colleagues=True`, every pair of people sharing at least one
company also gets a strong (default = 4) edge.
"""

from __future__ import annotations

import re
import unicodedata
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

    There is no separate `kind_column` — the "did I reach this person?"
    fact is fully derivable from `strength_column`:

        可信度 == 0 → 未联系: NO Me-edge built; `Person.is_wishlist=True`,
                      reachable only via peers' `认识` references.
        可信度 1-5  → 已联系: Me-edge with that strength (1=barely know,
                      5=core inner circle).

    Keeping these in a single column eliminates the redundancy of the
    old `kind_column` (whose only signal was "is this row at strength 0
    or not?") and removes the contradiction surface where `关系类型=已联系`
    + `可信度=0` was syntactically possible but semantically nonsense.

    The table only records facts. Intent ("who do I want to talk to
    next?") is parsed at query time by the LLM from the user's natural
    -language prompt, never pre-baked into rows.
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


# ---------------------------------------------------------------------------
# Header normalization & canonical preset
# ---------------------------------------------------------------------------
#
# 所有判定都基于「归一化后的列名」：
#   - 去除全/半角空白
#   - NFKC unicode 折叠（全角字符 → 半角等价物）
#   - 统一别名（同语义不同写法）
#
# 这样使用者填表时不必跟字符级别较真，"合作价值评分（0-5）" 与
# "合作价值（0-5）" 会被识别成同一列。
_HEADER_ALIASES: dict[str, str] = {
    # canonical name : 任何会归一化到 canonical 的别名 / 旧写法
    "合作价值（0-5）": "合作价值评分（0-5）",
    # NOTE: `可信度（言行一致性0-5分）` vs `可信度（言行一致性 0-5分）`
    # 仅差一个空格，归一化阶段会自动 strip，无需在此显式列出。
}


def _normalize_header(raw: object) -> str:
    """Map a raw header cell to a canonical comparable form.

    步骤：
      1. None / 空 → ""
      2. NFKC：全角空格、全角括号、全角分号等 → 半角等价
      3. 去掉所有空白字符（空格、tab、零宽空格等）
      4. 应用 alias 表把别名收敛到唯一 canonical 写法
    """
    if raw is None:
        return ""
    text = unicodedata.normalize("NFKC", str(raw))
    text = re.sub(r"\s+", "", text)
    if not text:
        return ""
    # alias 表的 key/value 也走一遍归一化，保证用 "合作价值评分（0-5）"
    # 这种带全角括号的别名时也能命中
    for canonical, alias in _HEADER_ALIASES.items():
        alias_n = re.sub(r"\s+", "", unicodedata.normalize("NFKC", alias))
        canonical_n = re.sub(r"\s+", "", unicodedata.normalize("NFKC", canonical))
        if text == alias_n:
            return canonical_n
    return text


# CORE 字段：与 Person dataclass 字段直接对应。
# 值是 canonical 归一化形式（注意全角括号经 NFKC 后变半角）。
_CORE_NAME = _normalize_header("姓名")
_CORE_NOTES = _normalize_header("备注")
_CORE_STRENGTH = _normalize_header("可信度（言行一致性0-5分）")
_CORE_CONTEXT = _normalize_header("职务")
_CORE_PEERS = _normalize_header("认识")
_CORE_COMPANIES = [_normalize_header(c) for c in ("公司",)]
_CORE_CITIES = [_normalize_header(c) for c in ("城市",)]
_CORE_NEEDS = [_normalize_header(c) for c in ("潜在需求",)]
_CORE_TAGS = [_normalize_header(c) for c in ("所属行业", "AI标准化特征", "资源类型")]
# 只用于 compose_bio 的 CORE 列（不会自动进其他字段）
_CORE_BIO_LOOKUP_KEYS = [
    _normalize_header(c) for c in ("所属行业", "职务", "公司", "城市", "合作价值（0-5）")
]

# PROFILE_BIO：金融画像的定量/定性自由文本，拼到 bio 末尾。
# (label_for_bio, normalized_column_name)
_PROFILE_BIO_FIELDS: list[tuple[str, str]] = [
    ("可投金额", _normalize_header("单笔可投资金额")),
    (
        "风险偏好",
        _normalize_header(
            "风险承受能力激进（可接受高回撤，追求高收益）、平衡（兼顾收益与风险）、"
            "稳健（低回撤优先，收益次之）"
        ),
    ),
    (
        "共赢性",
        _normalize_header(
            "共赢性（长：动机纯粹、追求长期共赢、中：有短期诉求，不排斥长期、"
            "短：动机不纯、仅追求短期利益）"
        ),
    ),
    (
        "关系阶段",
        _normalize_header(
            "关系阶段（A：有良好合作基础；B：未合作但熟悉；C：未合作且不熟悉；"
            "D:合作过但结果较差）"
        ),
    ),
    ("兴趣偏好", _normalize_header("兴趣偏好")),
]

# PROFILE_TAGS：少量、真正用来分群的业务身份。
_PROFILE_TAG_FIELDS: list[str] = [
    _normalize_header("核心标签（机构自营；机构fof；私募fof；三方机构；家办；个人；券商渠道）"),
]

# 仅用于忽略列计算：所有"已知"列名集合（CORE + PROFILE）。
# 任何其他列在 import 末尾会被 warning 提示。
_KNOWN_NORMALIZED_HEADERS: set[str] = (
    {
        _CORE_NAME,
        _CORE_NOTES,
        _CORE_STRENGTH,
        _CORE_CONTEXT,
        _CORE_PEERS,
    }
    | set(_CORE_COMPANIES)
    | set(_CORE_CITIES)
    | set(_CORE_NEEDS)
    | set(_CORE_TAGS)
    | {key for _, key in _PROFILE_BIO_FIELDS}
    | set(_PROFILE_TAG_FIELDS)
    | {_normalize_header("合作价值（0-5）")}
    # 表内常见的 housekeeping 列，不算 unknown：
    | {_normalize_header(c) for c in ("序号", "id", "ID")}
)


def default_preset() -> ColumnMapping:
    """The single canonical preset used for every spreadsheet import.

    覆盖：
      - `examples/richard_network.xlsx` 的 13 列基础形态
      - `examples/tommy_network.xlsx` 在此之上多出的 6 列金融画像
      - 任何只填了部分列的子集模板

    设计点见 module docstring。
    """

    def compose_bio(row: dict[str, object]) -> str | None:
        parts: list[str] = []
        # 先放 CORE 5 列，跟旧 richard preset 输出顺序一致
        for label, key in (
            ("行业", _normalize_header("所属行业")),
            ("职务", _normalize_header("职务")),
            ("公司", _normalize_header("公司")),
            ("城市", _normalize_header("城市")),
        ):
            v = _cell(row.get(key))
            if v:
                parts.append(f"{label}：{v}")
        coop = _cell(row.get(_normalize_header("合作价值（0-5）")))
        if coop:
            parts.append(f"合作价值：{coop}/5")
        # 然后追加 PROFILE_BIO（金融用户填了就吃，没填就跳过）
        for label, key in _PROFILE_BIO_FIELDS:
            v = _cell(row.get(key))
            if v:
                parts.append(f"{label}：{v}")
        return " · ".join(parts) if parts else None

    return ColumnMapping(
        name=_CORE_NAME,
        bio=compose_bio,
        notes=_CORE_NOTES,
        tags=list(_CORE_TAGS) + list(_PROFILE_TAG_FIELDS),
        companies=list(_CORE_COMPANIES),
        cities=list(_CORE_CITIES),
        needs=list(_CORE_NEEDS),
        strength_column=_CORE_STRENGTH,
        context_column=_CORE_CONTEXT,
        peers_column=_CORE_PEERS,
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


def _normalize_dataframe_headers(df: pl.DataFrame) -> tuple[pl.DataFrame, dict[str, str]]:
    """Rename df columns to their canonical normalized form.

    Returns the renamed df plus the original→canonical mapping so the
    caller can later compute "which raw columns were ignored".

    重名冲突：如果两个原始列归一化到同一 canonical 写法（例如同时填了
    `合作价值（0-5）` 和 `合作价值评分（0-5）`），第一次出现的胜出，
    后续重复的列保留原名以保证 dataframe rename 不报错——它会自动
    出现在 unknown 列里被 warning 出来。
    """
    new_names: list[str] = []
    seen: set[str] = set()
    raw_to_canonical: dict[str, str] = {}
    for raw in df.columns:
        canonical = _normalize_header(raw)
        if canonical and canonical not in seen:
            seen.add(canonical)
            new_names.append(canonical)
            raw_to_canonical[raw] = canonical
        else:
            # empty header or duplicate — keep raw to avoid pl.rename collision
            new_names.append(raw)
            if canonical:
                raw_to_canonical[raw] = raw  # surface as "ignored" later
    return df.rename(dict(zip(df.columns, new_names))), raw_to_canonical


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
        self._mapping = mapping or default_preset()
        self._infer_colleagues = infer_colleagues
        self._colleague_strength = colleague_strength

    # Back-compat: the CLI still calls `.import_file(path)` expecting an int.
    def import_file(self, path: str | Path) -> int:
        stats = self.import_with_stats(path)
        return stats.people

    def import_with_stats(self, path: str | Path) -> ImportStats:
        me = self._repo.get_me()
        if me is None or me.id is None:
            raise RuntimeError(
                "No `me` record. Run `lodestar init` first."
            )

        df = _read_any_spreadsheet(path)
        df, raw_to_canonical = _normalize_dataframe_headers(df)

        if self._mapping.name not in df.columns:
            raise ValueError(
                f"Required column '{self._mapping.name}' not found. "
                f"Available columns (after normalization): {df.columns}"
            )

        # 在 import 末尾打印「这些列我看见了但没用」提示，方便用户发现
        # 自己填了一列结果没生效（typo / 多余字段 / 旧版 schema）。
        ignored_pairs: list[tuple[str, str]] = []
        for raw, canonical in raw_to_canonical.items():
            if canonical and canonical not in _KNOWN_NORMALIZED_HEADERS:
                ignored_pairs.append((raw, canonical))
        self._ignored_columns = ignored_pairs

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

        if self._ignored_columns:
            cols = ", ".join(f"'{raw}'" for raw, _ in self._ignored_columns)
            print(f"[import] 已忽略 {len(self._ignored_columns)} 个未识别列：{cols}")

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

        # The "did I reach this person?" fact is derived directly from
        # the strength column: 0 = 未联系 (no Me-edge, wishlist), 1-5 =
        # 已联系 (Me-edge built with that strength). One column, no
        # contradiction surface.
        strength = self._parse_strength(row)
        is_wishlist_row = strength == 0

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

        if is_wishlist_row:
            # 可信度=0 → 未联系: skip the Me-edge entirely. Reachable only
            # via peers' `认识` references in pass 2. is_wishlist is already
            # persisted above and is decoupled from path topology — search
            # and ranking treat every contact equally regardless of it.
            return True

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
                source="manual",
            ),
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
                        source="manual",
                    ),
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
        c_b = pick("乙", "B", "target", "name_b")
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
                    source="manual",
                ),
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
                            source="colleague_inferred",
                        ),
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
        """Parse 可信度 0-5; **0 is meaningful** (means 未联系 — no Me-edge,
        is_wishlist=True). Empty / non-numeric → 3 (普通朋友) which is a
        safe default for "I forgot to fill this", because it preserves a
        Me-edge rather than silently demoting the row to wishlist."""
        col = self._mapping.strength_column
        if col is None:
            return 3
        raw = row.get(col)
        try:
            value = int(raw) if raw is not None else 3  # type: ignore[arg-type]
        except (ValueError, TypeError):
            return 3
        return max(0, min(5, value))


def _canonical_pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


def infer_colleague_edges(
    repo: Repository,
    *,
    strength: int = 4,
    dry_run: bool = False,
) -> tuple[int, int, list[tuple[str, int]]]:
    """Re-run same-company colleague inference over the current roster.

    Idempotent: re-runs are safe because `Repository.add_relationship`
    enforces the provenance hierarchy (manual > colleague_inferred), so
    edges the user already curated by hand are never downgraded.

    Returns (companies_with_clique, edges_added_or_would_add, top_companies)
    where `top_companies` is a sorted [(company, member_count), ...] list
    capped at the 10 largest cliques — useful for the CLI summary table.
    """
    me = repo.get_me()
    me_id = me.id if me else None
    people = repo.list_people()
    company_to_members: dict[str, list[int]] = {}
    for p in people:
        if p.id is None or p.id == me_id:
            continue
        for c in p.companies or []:
            key = c.strip()
            if not key:
                continue
            company_to_members.setdefault(key, []).append(p.id)

    cliques = [(c, m) for c, m in company_to_members.items() if len(m) >= 2]
    cliques.sort(key=lambda x: -len(x[1]))
    top = [(c, len(m)) for c, m in cliques[:10]]

    # Build the dedup'd pair set first, so dry-run and apply agree on
    # the count even when one person sits in two cliques.
    pairs: set[tuple[int, int]] = set()
    for _company, members in cliques:
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                pairs.add(_canonical_pair(a, b))

    if dry_run:
        return len(cliques), len(pairs), top

    for src, tgt in pairs:
        repo.add_relationship(
            Relationship(
                source_id=src,
                target_id=tgt,
                strength=strength,
                context="同事",
                frequency=Frequency.MONTHLY,
                source="colleague_inferred",
            ),
        )
    return len(cliques), len(pairs), top


def _merge_lists(old: list[str], new: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in list(old) + list(new):
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
