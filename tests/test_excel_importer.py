"""Excel importer tests — validate column mapping and upsert behavior."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from lodestar.db import Repository
from lodestar.importers import ExcelImporter, richard_finance_preset


def _make_xlsx(path: Path) -> None:
    df = pl.DataFrame(
        {
            "序号": [1, 2, 3, 3],
            "姓名": ["张三", "李四", "王五", "王五"],  # duplicate last row
            "所属行业": ["私募基金", "券商", "银行", "银行"],
            "职务": ["基金经理", "营业部总经理", "支行经理", "支行经理"],
            "AI标准化特征": [
                "研究能力强,关注芯片,投资激进",
                "耿直，事业心强、人脉丰富",
                "佛系",
                "佛系,客户多",  # second occurrence adds a tag
            ],
            "可信度（言行一致性0-5分）": [5, 4, 3, 3],
            "能量": [None, None, None, None],
            "潜在需求": ["资金", "客户，收入", "", "资金规模"],
        }
    )
    df.write_excel(path)


def test_richard_finance_preset_roundtrip(repo: Repository, tmp_path: Path) -> None:
    repo.ensure_me(name="我")
    xlsx_path = tmp_path / "contacts.xlsx"
    _make_xlsx(xlsx_path)

    importer = ExcelImporter(repo, mapping=richard_finance_preset())
    count = importer.import_file(xlsx_path)

    assert count == 4  # four rows processed

    zhang = repo.find_person_by_name("张三")
    assert zhang is not None
    assert "私募基金" in zhang.tags
    assert "研究能力强" in zhang.tags
    assert "关注芯片" in zhang.tags
    assert "投资激进" in zhang.tags
    assert zhang.needs == ["资金"]
    assert zhang.bio is not None and "基金经理" in zhang.bio

    li = repo.find_person_by_name("李四")
    assert li is not None
    # Chinese full-width comma and ideographic comma both work as separators
    assert set(li.needs) == {"客户", "收入"}
    assert "耿直" in li.tags
    assert "事业心强" in li.tags
    assert "人脉丰富" in li.tags

    # Duplicate 王五: later row with extra data is merged, no duplication
    wang = repo.find_person_by_name("王五")
    assert wang is not None
    assert "佛系" in wang.tags
    assert "客户多" in wang.tags
    assert "资金规模" in wang.needs

    # Strength mapped from 可信度 column
    rels = repo.list_relationships()
    zhang_rel = next(r for r in rels if r.target_id == zhang.id)
    assert zhang_rel.strength == 5
    li_rel = next(r for r in rels if r.target_id == li.id)
    assert li_rel.strength == 4


def test_needs_are_searchable(repo: Repository, tmp_path: Path) -> None:
    """Someone searching for '客户' should surface people whose need is 客户."""
    repo.ensure_me(name="我")
    xlsx_path = tmp_path / "contacts.xlsx"
    _make_xlsx(xlsx_path)

    importer = ExcelImporter(repo)
    importer.import_file(xlsx_path)

    hits = repo.keyword_candidates(["客户"])
    li = repo.find_person_by_name("李四")
    wang = repo.find_person_by_name("王五")
    assert li is not None and wang is not None
    assert li.id in hits
    assert wang.id in hits  # '客户多' also matches '客户'


def test_strength_zero_marks_uncontacted_and_skips_me_edge(
    repo: Repository, tmp_path: Path,
) -> None:
    """The single source of truth for "did I reach this person?" is the
    `可信度` column: 0 → 未联系 (no Me-edge, is_wishlist=True), 1-5 →
    已联系 (Me-edge with that strength). No separate `关系类型` column."""
    from lodestar.importers import ExcelImporter, extended_network_preset

    repo.ensure_me(name="我")
    df = pl.DataFrame({
        "姓名": ["张三", "想认识一号", "缺值默认"],
        "所属行业": ["私募", "并购投行", "FOF"],
        "可信度（言行一致性0-5分）": [4, 0, None],
    })
    xlsx_path = tmp_path / "wish.xlsx"
    df.write_excel(xlsx_path)

    ExcelImporter(repo, mapping=extended_network_preset()).import_file(xlsx_path)

    star = repo.find_person_by_name("想认识一号")
    assert star is not None
    assert star.is_wishlist is True, "可信度=0 must imply is_wishlist=True"
    rels = repo.list_relationships()
    assert all(r.target_id != star.id for r in rels), \
        "可信度=0 contact must NOT have a Me-edge"

    zhang = repo.find_person_by_name("张三")
    assert zhang is not None
    assert zhang.is_wishlist is False
    zhang_rels = [r for r in rels if r.target_id == zhang.id]
    assert len(zhang_rels) == 1 and zhang_rels[0].strength == 4

    # Blank 可信度 falls back to the default 3 (普通朋友) — NOT to 0.
    # Otherwise empty cells would silently demote contacts to wishlist,
    # which is the worst possible default behaviour.
    blank = repo.find_person_by_name("缺值默认")
    assert blank is not None
    assert blank.is_wishlist is False, "blank 可信度 must default to contacted, not wishlist"
    blank_rels = [r for r in rels if r.target_id == blank.id]
    assert len(blank_rels) == 1 and blank_rels[0].strength == 3


def test_legacy_关系类型_column_is_silently_ignored(
    repo: Repository, tmp_path: Path,
) -> None:
    """Old workbooks may still carry a `关系类型` column. The new importer
    has no `kind_column` mapping, so this column should be silently ignored
    and behaviour should be driven purely by `可信度`. We use intentionally
    contradictory values (legacy 关系类型 says one thing, 可信度 says the
    opposite) and assert that 可信度 always wins."""
    from lodestar.importers import ExcelImporter, extended_network_preset

    repo.ensure_me(name="我")
    df = pl.DataFrame({
        "姓名": ["A", "B"],
        "所属行业": ["VC", "PE"],
        # legacy column says A is 未联系 + B is 已联系 — it must NOT take effect
        "关系类型": ["未联系", "已联系"],
        # truth: A 可信度=3 (so A is contacted) + B 可信度=0 (so B is uncontacted)
        "可信度（言行一致性0-5分）": [3, 0],
    })
    xlsx_path = tmp_path / "legacy.xlsx"
    df.write_excel(xlsx_path)

    ExcelImporter(repo, mapping=extended_network_preset()).import_file(xlsx_path)

    a = repo.find_person_by_name("A")
    b = repo.find_person_by_name("B")
    assert a is not None and b is not None
    assert a.is_wishlist is False, "可信度=3 wins over legacy 关系类型=未联系"
    assert b.is_wishlist is True, "可信度=0 wins over legacy 关系类型=已联系"
    rels = repo.list_relationships()
    a_rels = [r for r in rels if r.target_id == a.id]
    b_rels = [r for r in rels if r.target_id == b.id]
    assert len(a_rels) == 1 and a_rels[0].strength == 3
    assert b_rels == [], "可信度=0 row must not produce a Me-edge"
