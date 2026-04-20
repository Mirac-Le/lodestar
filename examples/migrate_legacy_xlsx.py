"""把 Richard / Tommy 早期手填的旧版 xlsx 自动搬到当前 13 列模板布局。

策略：
  · "搬运 + 留白"，不做任何主观推断。
  · 旧字段里能 1:1 对上的 → 直接搬。
  · 多余字段 → 拼到「备注」或「AI 标准化特征」尾部，绝不丢失。
  · 缺的列（公司 / 城市 / 认识 等）→ 留空，让本人核对时补；AI enrich 后续也会补。

输出沿用各自的最终文件名：
    examples/richard_network.xlsx
    examples/tommy_network.xlsx

结构与 examples/template.xlsx 完全对齐：
  Sheet 1: 联系人  （13 列 + 数据 + 100 行空白）
  Sheet 2: 说明    （顶部红底 owner banner + 列说明 + 关键填写规则）

注：旧版的 `关系` sheet 已彻底废弃 —— peer↔peer 关系一律改在 Web 端
「新关系」按钮里用一句话录入，由 LLM 解析成结构化提案后入库。

Run:
    uv run python examples/migrate_legacy_xlsx.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
import xlsxwriter

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

# --------------------------------------------------------------------- target
# 与 build_template.py 的 COLUMNS 顺序保持完全一致。
TARGET_COLUMNS: list[tuple[str, int]] = [
    ("序号", 6),
    ("姓名", 14),
    ("所属行业", 14),
    ("公司", 22),
    ("职务", 22),
    ("城市", 10),
    ("AI标准化特征", 44),
    ("可信度（言行一致性0-5分）", 14),
    ("合作价值（0-5）", 12),
    ("潜在需求", 22),
    ("资源类型", 14),
    ("认识", 56),
    ("备注", 22),
]
REQUIRED_COL_INDICES = {1, 7, 11}  # 姓名 / 可信度 / 认识
NUMERIC_COL_INDICES = {0, 7, 8}    # 序号 / 可信度 / 合作价值

NEW_COLUMNS_TO_FILL_BY_OWNER = {
    "richard": ["公司", "城市", "认识", "合作价值（0-5）", "资源类型", "备注"],
    "tommy":   ["公司", "认识"],  # tommy 旧表已经有城市、合作价值、资源类型
}


# --------------------------------------------------------------------- helpers
def _str(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() in {"nan", "none", "null"}:
        return ""
    return s


def _join_nonempty(parts: list[str], sep: str = "；") -> str:
    return sep.join(p for p in (p.strip() for p in parts) if p)


def _kv(label: str, value: Any) -> str:
    v = _str(value)
    return f"{label}: {v}" if v else ""


# --------------------------------------------------------------------- mappers
def map_richard(df: pl.DataFrame) -> list[dict[str, Any]]:
    """Richard 旧 8 列 → 新 13 列。

    旧列：序号 / 姓名 / 所属行业 / 职务 / AI标准化特征 / 可信度 / 能量 / 潜在需求
    丢失：能量（半空，挪到备注）
    新增：公司 / 城市 / 合作价值 / 资源类型 / 认识 / 备注
    """
    rows: list[dict[str, Any]] = []
    for raw in df.iter_rows(named=True):
        notes = _kv("能量", raw.get("能量"))  # 旧字段保留进备注
        rows.append({
            "序号": raw.get("序号"),
            "姓名": _str(raw.get("姓名")),
            "所属行业": _str(raw.get("所属行业")),
            "公司": "",
            "职务": _str(raw.get("职务")),
            "城市": "",
            "AI标准化特征": _str(raw.get("AI标准化特征")),
            "可信度（言行一致性0-5分）": raw.get("可信度（言行一致性0-5分）"),
            "合作价值（0-5）": "",
            "潜在需求": _str(raw.get("潜在需求")),
            "资源类型": "",
            "认识": "",
            "备注": notes,
        })
    return rows


def map_tommy(df: pl.DataFrame) -> list[dict[str, Any]]:
    """Tommy 旧 16 列 → 新 13 列。

    旧 16 列：姓名 / 行业 / 核心标签 / 身份职位 / 主要背景 /
            资源类型 / 可合作业务范围 / 地域 / 单笔可投资金额 /
            风险承受能力 / 可信度 / 共赢性 / 兴趣偏好 / 潜在需求 /
            合作价值评分 / 关系阶段
    映射策略：
      · 行业 + 核心标签 → 所属行业（拼 `;`）
      · 身份职位 → 职务
      · 地域 → 城市
      · 可信度（承诺一致性 0-5分） → 可信度（言行一致性0-5分）
      · 合作价值评分（0-5） → 合作价值（0-5）
      · 资源类型、潜在需求 → 直接搬
      · 可合作业务范围 + 风险承受能力 + 共赢性 + 兴趣偏好 → AI 标准化特征（拼 `;`）
      · 主要背景 + 单笔可投资金额 + 关系阶段 → 备注（拼，自带 label）
      · 公司、认识 → 留空，等 Tommy 补 / AI 抽
    """
    long_tag_col = next(
        (c for c in df.columns if c.startswith("核心标签")), None,
    )
    risk_col = next(
        (c for c in df.columns if c.startswith("风险承受能力")), None,
    )
    win_col = next(
        (c for c in df.columns if c.startswith("共赢性")), None,
    )
    trust_col = next(
        (c for c in df.columns if c.startswith("可信度")), None,
    )
    value_col = next(
        (c for c in df.columns if c.startswith("合作价值评分")), None,
    )
    res_col = next(
        (c for c in df.columns if c.startswith("资源类型")), None,
    )
    stage_col = next(
        (c for c in df.columns if c.startswith("关系阶段")), None,
    )

    rows: list[dict[str, Any]] = []
    for idx, raw in enumerate(df.iter_rows(named=True), start=1):
        industry = _join_nonempty(
            [_str(raw.get("行业")), _str(raw.get(long_tag_col) if long_tag_col else "")],
        )
        feature = _join_nonempty([
            _str(raw.get("可合作业务范围")),
            _str(raw.get(risk_col) if risk_col else ""),
            _str(raw.get(win_col) if win_col else ""),
            _str(raw.get("兴趣偏好")),
        ])
        notes = _join_nonempty([
            _str(raw.get("主要背景")),
            _kv("单笔可投资金额", raw.get("单笔可投资金额")),
            _kv("关系阶段", raw.get(stage_col) if stage_col else ""),
        ], sep=" · ")
        rows.append({
            "序号": idx,
            "姓名": _str(raw.get("姓名")),
            "所属行业": industry,
            "公司": "",
            "职务": _str(raw.get("身份职位")),
            "城市": _str(raw.get("地域")),
            "AI标准化特征": feature,
            "可信度（言行一致性0-5分）": raw.get(trust_col) if trust_col else None,
            "合作价值（0-5）": raw.get(value_col) if value_col else None,
            "潜在需求": _str(raw.get("潜在需求")),
            "资源类型": _str(raw.get(res_col) if res_col else ""),
            "认识": "",
            "备注": notes,
        })
    return rows


# --------------------------------------------------------------------- writer
def write_workbook(
    out_path: Path,
    rows: list[dict[str, Any]],
    owner_label: str,
    must_fill: list[str],
    source_note: str,
) -> Path:
    wb = xlsxwriter.Workbook(str(out_path))

    header_fmt = wb.add_format({
        "bold": True, "bg_color": "#1f2937", "font_color": "#f9fafb",
        "border": 1, "border_color": "#374151", "align": "center",
        "font_name": "PingFang SC", "font_size": 11,
    })
    header_req_fmt = wb.add_format({
        "bold": True, "bg_color": "#7c2d12", "font_color": "#fff7ed",
        "border": 1, "border_color": "#431407", "align": "center",
        "font_name": "PingFang SC", "font_size": 11,
    })
    cell_fmt = wb.add_format({
        "font_name": "PingFang SC", "font_size": 10, "valign": "top",
        "text_wrap": True, "border": 1, "border_color": "#e5e7eb",
    })
    cell_mono = wb.add_format({
        "font_name": "JetBrains Mono", "font_size": 10, "valign": "top",
        "text_wrap": True, "border": 1, "border_color": "#e5e7eb",
        "align": "center",
    })
    cell_blank_must = wb.add_format({
        "font_name": "PingFang SC", "font_size": 10, "valign": "top",
        "text_wrap": True, "border": 1, "border_color": "#fbbf24",
        "bg_color": "#fef3c7",
    })
    section_fmt = wb.add_format({
        "bold": True, "font_name": "PingFang SC", "font_size": 13,
        "font_color": "#1f2937",
    })
    banner_fmt = wb.add_format({
        "bold": True, "font_name": "PingFang SC", "font_size": 12,
        "bg_color": "#7c2d12", "font_color": "#fff7ed",
        "border": 1, "border_color": "#431407",
        "align": "left", "valign": "vcenter", "text_wrap": True,
    })
    help_fmt = wb.add_format({
        "font_name": "PingFang SC", "font_size": 11, "text_wrap": True,
        "valign": "top",
    })
    help_mono = wb.add_format({
        "font_name": "JetBrains Mono", "font_size": 10, "text_wrap": True,
        "valign": "top", "bg_color": "#f3f4f6",
    })

    # ============ Sheet 1: 联系人 ============
    ws = wb.add_worksheet("联系人")
    must_fill_set = set(must_fill)
    for col, (name, width) in enumerate(TARGET_COLUMNS):
        ws.set_column(col, col, width)
        fmt = header_req_fmt if col in REQUIRED_COL_INDICES else header_fmt
        ws.write(0, col, name, fmt)

    for r_i, row in enumerate(rows, start=1):
        for c_i, (name, _w) in enumerate(TARGET_COLUMNS):
            val = row.get(name, "")
            if val is None or val == "":
                f = cell_blank_must if name in must_fill_set else cell_fmt
                ws.write_blank(r_i, c_i, None, f)
            else:
                f = cell_mono if c_i in NUMERIC_COL_INDICES else cell_fmt
                ws.write(r_i, c_i, val, f)

    # 100 行空白增量行
    blank_start = len(rows) + 1
    for r_i in range(blank_start, blank_start + 100):
        for c_i, (name, _w) in enumerate(TARGET_COLUMNS):
            f = cell_blank_must if name in must_fill_set else cell_fmt
            ws.write_blank(r_i, c_i, None, f)

    ws.data_validation(
        1, 7, blank_start + 100, 7,
        {
            "validate": "integer", "criteria": "between",
            "minimum": 0, "maximum": 5,
            "input_title": "可信度（0-5）",
            "input_message": (
                "整数 0-5，单列承载两件事：\n"
                "  0  = 未联系（不建 Me 边，靠他人引荐到达）\n"
                "  1  = 点头之交\n"
                "  3  = 普通朋友\n"
                "  5  = 核心铁磁\n"
                "留空被当成 3，所以未联系的人务必显式填 0。"
            ),
        },
    )
    ws.data_validation(
        1, 8, blank_start + 100, 8,
        {
            "validate": "integer", "criteria": "between",
            "minimum": 0, "maximum": 5,
            "input_title": "合作价值",
            "input_message": "0-5 整数；不评估直接留空",
            "ignore_blank": True,
        },
    )
    ws.freeze_panes(1, 2)
    ws.autofilter(0, 0, blank_start + 100, len(TARGET_COLUMNS) - 1)

    # ============ Sheet 2: 说明 ============
    ws3 = wb.add_worksheet("说明")
    ws3.set_column(0, 0, 28)
    ws3.set_column(1, 1, 84)

    ws3.merge_range(
        0, 0, 0, 1,
        f"📌 这是 {owner_label} 的专属网络表（按 13 列模板自动迁移自旧版）",
        banner_fmt,
    )
    ws3.set_row(0, 36)

    ws3.merge_range(
        1, 0, 1, 1,
        f"💡 本文件由旧版 {source_note} 自动转换而来：\n"
        "  • 已搬过来的列（黄底高亮 = 留空待你补 / 白底 = 已迁移）：扫一眼别串行就行\n"
        f"  • 必须你补的列：{', '.join(must_fill)}\n"
        "  • 其它留空列 AI 跑 enrich 时会自动补\n"
        "❗ 重点：『认识』列是构成多跳引荐路径的唯一原料，每个人写他认识圈里 3-5 个表内人即可。",
        wb.add_format({
            "font_name": "PingFang SC", "font_size": 11, "text_wrap": True,
            "valign": "top", "bg_color": "#fef3c7", "border": 1,
            "border_color": "#fbbf24", "font_color": "#7c2d12",
        }),
    )
    ws3.set_row(1, 96)

    ws3.write(3, 0, "列名", header_fmt)
    ws3.write(3, 1, "当前状态 + 提示", header_fmt)
    state_map: dict[str, str] = {}
    for name in must_fill:
        state_map[name] = "⚠️ 留空，必须你补"
    for c, (name, _w) in enumerate(TARGET_COLUMNS):
        if name in state_map:
            continue
        if c in REQUIRED_COL_INDICES or name in {
            "姓名", "所属行业", "职务", "AI标准化特征", "可信度（言行一致性0-5分）",
            "潜在需求",
        }:
            state_map[name] = "已从旧表搬过来 → 核对一下"
        else:
            state_map[name] = "留空，AI enrich 会自动补"
    hint_extra: dict[str, str] = {
        "姓名": "唯一主键。同名不同人请括号消歧（如 `张伟（中金）`）。",
        "所属行业": "Richard：原『所属行业』直接搬；Tommy：原『行业 + 核心标签』拼接。",
        "公司": "★强烈建议补：当前任职公司全称。同公司的人系统会自动连成同事网。",
        "职务": "Richard：原『职务』；Tommy：原『身份职位』。",
        "城市": "Richard 留空（旧表没有此列）；Tommy 已从『地域』搬过来。",
        "AI标准化特征": "Richard：原『AI 标准化特征』；Tommy：『可合作业务范围 + 风险承受能力 + 共赢性 + 兴趣偏好』拼成短标签串。",
        "可信度（言行一致性0-5分）": "0=未联系、1=点头之交、3=普通朋友、5=核心铁磁。**未联系务必显式填 0**，留空会被当 3。",
        "合作价值（0-5）": "Tommy：已从『合作价值评分』搬来；Richard：朋友圈类不评估直接留空。",
        "潜在需求": "此人在找什么。",
        "资源类型": "Tommy：已搬；Richard：留空。多选 `资金;项目;服务;技术;人脉`。",
        "认识": (
            "★★★ 必填核心。格式：`甲(4,大学同学); 乙(3,前同事); 丙`。"
            "数字=强度1-5，文字=描述，都可省。**只需写一个方向**，"
            "导入器自动建双向边。**写到表外的人会被跳过**。"
        ),
        "备注": (
            "Richard：原『能量』并入这里；Tommy：原『主要背景 + 单笔可投资金额 + 关系阶段』拼接。"
            "纯展示用，不进搜索打分。"
        ),
    }
    for i, (name, _w) in enumerate(TARGET_COLUMNS, start=4):
        marker = ""
        if name in must_fill:
            marker = "  [必补]"
        elif name in {"姓名", "可信度（言行一致性0-5分）", "认识"}:
            marker = "  [必填]"
        ws3.write(i, 0, name + marker, help_fmt)
        ws3.write(
            i, 1,
            f"状态: {state_map[name]}\n说明: {hint_extra.get(name, '')}",
            help_fmt,
        )
        ws3.set_row(i, 56)

    base = 4 + len(TARGET_COLUMNS) + 1
    tips: list[tuple[str, str]] = [
        ("peer↔peer 关系不在这填",
         "本表只录『你和这个人多熟（可信度）』。两个人之间的关系（同事/朋友/合作）"
         "导入后请在 Web 端的『新关系』按钮里用一句话录入，AI 会脱敏后解析成结构化提案让你确认入库。"),
        ("最省力填法",
         "只把『姓名 · 可信度 · AI标准化特征 · 认识』填完，其它列留空交给 AI。"
         "录入完后跑 `lodestar enrich` 自动补 公司 / 城市 / 职务 / tags。"),
        ("分隔符",
         "多值字段（行业/公司/城市/认识/AI 特征/资源类型）支持 `;` `，` `、` `/` `｜` 任意分隔。"),
        ("空值规则",
         "没数据直接留空。**不要**填 `-` `无` `NA` `N/A`——会被当字符串污染搜索。"),
        ("『认识』示例",
         "✅ `刘思敏(5,中金同事); 沈南鹏(2,饭局认识); 张磊`\n"
         "❌ `认识很多人。` （没法解析）\n"
         "❌ `刘思敏，沈南鹏。` （括号写法都没有也可以，但失去强度信息）"),
        ("同公司自动建边",
         "同一个『公司』字段里出现的所有人会自动成为同事（强度 4）。"
         "**所以同事不要写到『认识』里**——『认识』只用来写跨公司的人脉。"),
        ("『可信度』填法",
         "0 = 未联系、1 = 点头之交、2 = 偶尔联系、3 = 普通朋友、4 = 熟人常合作、5 = 核心铁磁。"
         "⚠️ 不要在表里预先标谁是『想接触的目标』——『下次想找谁』是 web 端搜索那句自然语言决定的。"),
        ("交回流程",
         "填完直接把这份 .xlsx 发回。导入侧会跑：\n"
         "  uv run lodestar import <this>.xlsx --owner <你>\n"
         "  uv run lodestar enrich --owner <你> --apply\n"
         "  uv run lodestar normalize-companies --owner <你> --apply\n"
         "  uv run lodestar infer-colleagues --owner <你> --apply"),
    ]
    for i, (k, v) in enumerate(tips):
        ws3.write(base + i, 0, k, help_fmt)
        ws3.write(base + i, 1, v, help_mono if "uv run" in v else help_fmt)
        ws3.set_row(base + i, 76 if len(v) > 130 else (52 if len(v) > 80 else 36))

    wb.close()
    return out_path


# --------------------------------------------------------------------- main
def main() -> None:
    rich_src = EXAMPLES / "richard_network.xlsx"
    tommy_src = EXAMPLES / "tommy_network.xlsx"

    # 第一次跑会做备份；后续重跑直接从备份读，避免读到已经迁移过的版本
    for src in (rich_src, tommy_src):
        bak = src.with_suffix(".xlsx.legacy.bak")
        if not bak.exists():
            bak.write_bytes(src.read_bytes())
            print(f"backup: {src.name} → {bak.name}")

    rich_legacy = rich_src.with_suffix(".xlsx.legacy.bak")
    tommy_legacy = tommy_src.with_suffix(".xlsx.legacy.bak")

    rich_df = pl.read_excel(rich_legacy, sheet_name="通讯录")
    tommy_df = pl.read_excel(tommy_legacy, sheet_name="Sheet1", raise_if_empty=False)

    rich_rows = map_richard(rich_df)
    tommy_rows = map_tommy(tommy_df)

    rich_out = write_workbook(
        rich_src,
        rich_rows,
        owner_label="Richard Teng",
        must_fill=NEW_COLUMNS_TO_FILL_BY_OWNER["richard"],
        source_note="`pyq.xlsx` (8 列朋友圈格式)",
    )
    tommy_out = write_workbook(
        tommy_src,
        tommy_rows,
        owner_label="Tommy Song",
        must_fill=NEW_COLUMNS_TO_FILL_BY_OWNER["tommy"],
        source_note="`contacts.xlsx` (16 列机构合作格式)",
    )

    print(f"\nwrote {rich_out.relative_to(REPO_ROOT)}  "
          f"({rich_out.stat().st_size // 1024} KB, {len(rich_rows)} rows)")
    print(f"wrote {tommy_out.relative_to(REPO_ROOT)}  "
          f"({tommy_out.stat().st_size // 1024} KB, {len(tommy_rows)} rows)")


if __name__ == "__main__":
    main()
