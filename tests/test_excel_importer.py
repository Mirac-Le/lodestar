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


def test_kind_uncontacted_sets_wishlist_and_skips_me_edge(
    repo: Repository, tmp_path: Path,
) -> None:
    """`关系类型 = 未联系` must set Person.is_wishlist=True (curation flag) AND
    skip the Me-edge (so reach is forced through peers). Earlier the only
    observable signal was the missing edge — the curation intent was lost."""
    from lodestar.importers import ExcelImporter, extended_network_preset

    repo.ensure_me(name="我")
    df = pl.DataFrame({
        "姓名": ["张三", "想认识一号"],
        "所属行业": ["私募", "并购投行"],
        "关系类型": ["已联系", "未联系"],
        "可信度（言行一致性0-5分）": [4, 0],
    })
    xlsx_path = tmp_path / "wish.xlsx"
    df.write_excel(xlsx_path)

    ExcelImporter(repo, mapping=extended_network_preset()).import_file(xlsx_path)

    star = repo.find_person_by_name("想认识一号")
    assert star is not None
    assert star.is_wishlist is True
    rels = repo.list_relationships()
    assert all(r.target_id != star.id for r in rels), \
        "uncontacted (wishlist) contact must NOT have a Me-edge"

    zhang = repo.find_person_by_name("张三")
    assert zhang is not None
    assert zhang.is_wishlist is False


def test_kind_legacy_values_collapse_to_contacted(
    repo: Repository, tmp_path: Path,
) -> None:
    """The legacy vocabulary (`直接` / `弱认识` / `目标` / `target` /
    `想认识` / `陌生`) is intentionally not recognised any more.
    Only `已联系` / `contacted` / 留空 / `未联系` / `uncontacted` are
    canonical. Anything else silently falls back to the default
    `已联系` (= contacted) kind, which builds a Me-edge using the
    row's `可信度`. This test pins the behavior so we don't silently
    re-introduce alias drift, and crucially also verifies that the
    deprecated `弱认识` no longer takes the strength=1 fast path —
    closeness now comes solely from the 可信度 column."""
    from lodestar.importers import ExcelImporter, extended_network_preset

    repo.ensure_me(name="我")
    df = pl.DataFrame({
        "姓名": ["旧词目标", "旧词弱认识"],
        "所属行业": ["VC", "PE"],
        "关系类型": ["目标", "弱认识"],
        "可信度（言行一致性0-5分）": [3, 4],
    })
    xlsx_path = tmp_path / "legacy.xlsx"
    df.write_excel(xlsx_path)

    ExcelImporter(repo, mapping=extended_network_preset()).import_file(xlsx_path)

    target = repo.find_person_by_name("旧词目标")
    weak = repo.find_person_by_name("旧词弱认识")
    assert target is not None and weak is not None
    assert target.is_wishlist is False, \
        "legacy '目标' must NOT silently behave as '未联系'"
    assert weak.is_wishlist is False
    rels = repo.list_relationships()
    target_rels = [r for r in rels if r.target_id == target.id]
    weak_rels = [r for r in rels if r.target_id == weak.id]
    assert len(target_rels) == 1, "legacy '目标' must fall back to a default Me-edge"
    assert len(weak_rels) == 1, "legacy '弱认识' must fall back to a default Me-edge"
    assert target_rels[0].strength == 3, \
        "legacy '目标' edge must use 可信度, not the wishlist 0"
    assert weak_rels[0].strength == 4, \
        "legacy '弱认识' must NOT silently force strength=1 anymore — closeness is 可信度 only"
