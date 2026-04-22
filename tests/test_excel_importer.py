"""Excel importer tests — validate column mapping and upsert behavior."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from lodestar.db import Repository
from lodestar.importers import ExcelImporter, default_preset


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


def test_default_preset_roundtrip(repo: Repository, tmp_path: Path) -> None:
    """13 列基础形态：与单一 default preset 端到端走通。"""
    repo.ensure_me(name="我")
    xlsx_path = tmp_path / "contacts.xlsx"
    _make_xlsx(xlsx_path)

    importer = ExcelImporter(repo, mapping=default_preset())
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


def test_keyword_candidates_skip_needs_but_match_tags(
    repo: Repository, tmp_path: Path,
) -> None:
    """`keyword_candidates` deliberately skips the `need` column (see
    `Repository.keyword_candidates` docstring). Substring hits on tags /
    bio still work — e.g. tag 「客户多」matches query 「客户」."""
    repo.ensure_me(name="我")
    xlsx_path = tmp_path / "contacts.xlsx"
    _make_xlsx(xlsx_path)

    importer = ExcelImporter(repo)
    importer.import_file(xlsx_path)

    hits = repo.keyword_candidates(["客户"])
    li = repo.find_person_by_name("李四")
    wang = repo.find_person_by_name("王五")
    assert li is not None and wang is not None
    assert li.id not in hits  # needs 「客户」/「收入」are not indexed here
    assert wang.id in hits  # tag 「客户多」matches 「客户」


def test_strength_zero_marks_uncontacted_and_skips_me_edge(
    repo: Repository, tmp_path: Path,
) -> None:
    """The single source of truth for "did I reach this person?" is the
    `可信度` column: 0 → 未联系 (no Me-edge, is_wishlist=True), 1-5 →
    已联系 (Me-edge with that strength). No separate `关系类型` column."""
    from lodestar.importers import ExcelImporter, default_preset

    repo.ensure_me(name="我")
    df = pl.DataFrame({
        "姓名": ["张三", "想认识一号", "缺值默认"],
        "所属行业": ["私募", "并购投行", "FOF"],
        "可信度（言行一致性0-5分）": [4, 0, None],
    })
    xlsx_path = tmp_path / "wish.xlsx"
    df.write_excel(xlsx_path)

    ExcelImporter(repo, mapping=default_preset()).import_file(xlsx_path)

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
    from lodestar.importers import ExcelImporter, default_preset

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

    ExcelImporter(repo, mapping=default_preset()).import_file(xlsx_path)

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


def test_default_preset_absorbs_tommy_profile_columns(
    repo: Repository, tmp_path: Path,
) -> None:
    """Tommy 那 6 列金融画像（曾经只在旧 tommy_contacts_preset 里支持）
    必须在唯一的 default preset 下也被吃进 bio + tags，且不报"未识别"。"""
    repo.ensure_me(name="我")
    df = pl.DataFrame({
        "姓名": ["林高级"],
        "所属行业": ["私募fof"],
        "公司": ["某某资产"],
        "职务": ["合伙人"],
        "城市": ["上海"],
        "可信度（言行一致性0-5分）": [4],
        "单笔可投资金额": ["3000-5000万"],
        # 故意带前后空格 + 全角分号，验证 NFKC 归一化
        "  核心标签（机构自营；机构fof；私募fof；三方机构；家办；个人；券商渠道）": ["私募fof"],
        "兴趣偏好": ["量化、AI"],
    })
    xlsx_path = tmp_path / "profile.xlsx"
    df.write_excel(xlsx_path)

    ExcelImporter(repo, mapping=default_preset()).import_with_stats(xlsx_path)

    person = repo.find_person_by_name("林高级")
    assert person is not None
    assert person.bio is not None
    # PROFILE_BIO 字段以 「字段：值 · ...」 形式追加
    assert "可投金额：3000-5000万" in person.bio
    assert "兴趣偏好：量化、AI" in person.bio
    # PROFILE_TAGS 字段进 tags
    assert "私募fof" in person.tags


def test_default_preset_warns_on_unknown_columns(
    repo: Repository, tmp_path: Path, capsys,
) -> None:
    """白名单外的列应当被丢掉并在末尾打印一行 `[import] 已忽略 ...`。"""
    repo.ensure_me(name="我")
    df = pl.DataFrame({
        "姓名": ["张三"],
        "所属行业": ["私募"],
        "可信度（言行一致性0-5分）": [4],
        "随便瞎填的字段": ["should-be-ignored"],
    })
    xlsx_path = tmp_path / "noise.xlsx"
    df.write_excel(xlsx_path)

    ExcelImporter(repo, mapping=default_preset()).import_with_stats(xlsx_path)
    captured = capsys.readouterr().out
    assert "已忽略" in captured
    assert "随便瞎填的字段" in captured


def test_header_aliases_are_normalized(repo: Repository, tmp_path: Path) -> None:
    """`合作价值评分（0-5）`（带"评分"二字、全角括号）应等价于
    canonical `合作价值（0-5）`，并被拼到 bio 末尾。"""
    repo.ensure_me(name="我")
    df = pl.DataFrame({
        "姓名": ["王五"],
        "所属行业": ["银行"],
        "可信度（言行一致性0-5分）": [3],
        "合作价值评分（0-5）": [4],  # alias 形式
    })
    xlsx_path = tmp_path / "alias.xlsx"
    df.write_excel(xlsx_path)

    ExcelImporter(repo, mapping=default_preset()).import_file(xlsx_path)
    wang = repo.find_person_by_name("王五")
    assert wang is not None
    assert wang.bio is not None and "合作价值：4/5" in wang.bio
