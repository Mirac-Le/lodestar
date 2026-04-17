"""Generate `examples/template.xlsx` — the canonical empty template handed
to colleagues so they can replace demo data with real contacts.

列定义是在原 `pyq.xlsx` 的 8 列之上，新增 4 列形成完整 12 列。

Run:
    uv run python examples/build_template.py
"""

from __future__ import annotations

from pathlib import Path

import xlsxwriter

OUT_PATH = Path(__file__).parent / "template.xlsx"

# --------------------------------------------------------------------- schema
# 顺序 = Excel 里的列顺序。★ 是必填，○ 是强烈推荐，- 是可选。
# (列名, 说明, 示例, 是否必填★/○/-)
COLUMNS: list[tuple[str, str, str, str, int]] = [
    # (列名, 说明, 示例, 必填度, 宽度)
    ("序号", "行号，可留空自动编", "1", "-", 6),
    ("姓名", "唯一主键；同名会合并，不要重复", "陈维国", "★", 12),
    ("所属行业",
     "大类行业标签。可多值：用 ; 或 、 分隔", "投资银行", "○", 14),
    ("公司",
     "当前任职公司。同公司两人会自动成为同事（强度 4）。多份工作用 ; 分隔",
     "中金公司", "○", 22),
    ("职务", "当前职位，用来还原 bio", "衍生品 MD", "○", 22),
    ("城市", "常驻城市。多城用 ; 分隔", "香港", "-", 10),
    ("AI标准化特征",
     "短评式画像，用 ; 分隔。系统会拿来向量化匹配",
     "港大金融;衍生品;交际广;外派经验", "○", 44),
    ("可信度（言行一致性0-5分）",
     "0-5 分；1 = 点头之交，3 = 普通朋友，5 = 核心铁磁。目标人物填 0",
     "5", "★", 12),
    ("潜在需求",
     "此人在找什么；会被用来做反向撮合（你能介绍谁当他的供给方）",
     "家办客户;海外上市资源", "○", 22),
    ("认识",
     "★★ 最关键的新列。写此人认识圈里的哪些人，建 peer 边。"
     "格式：`甲(4,大学同学); 乙(3,前同事); 丙`。"
     "括号内数字 = 强度 1-5，文字 = 关系描述，都可省。",
     "刘思敏(5,中金同事); 沈南鹏(2,中金时见过几次)", "★", 56),
    ("备注", "任意自由文本；不会进入搜索打分", "人脉通天", "-", 22),
    ("关系类型",
     "三选一，决定 `我` 与此人的关系：\n"
     "  留空/直接 → 直接好友（按可信度建 Me 边，默认）\n"
     "  弱认识   → 点头之交（只建强度=1 的 Me 边）\n"
     "  目标     → 不建 Me 边；需要通过别人的『认识』引荐到",
     "直接", "○", 12),
]

# --------------------------------------------------------------------- samples
# 三行示例，展示三种 `关系类型` 的填法。使用 FAQ 里写好的真实业务场景。
SAMPLE_ROWS: list[list[str | int]] = [
    # 直接好友
    [1, "陈维国", "投资银行", "中金公司", "衍生品 MD", "香港",
     "港大金融;衍生品;交际广;外派经验", 5,
     "家办客户;海外上市资源",
     "刘思敏(5,同事); 沈南鹏(2,中金时见过几次); 张磊(2,香港路演)",
     "人脉通天", "直接"],
    # 弱认识（微信加了没深入聊过）
    [2, "姚远", "保险资管", "某大型保险资管", "基金经理", "北京",
     "清华五道口;稳健派;保险机构视角", 1,
     "固收产品对接",
     "周景明(3,清华校友); 邱国鹭(3,高毅路演)",
     "上次饭局加的微信", "弱认识"],
    # 目标人物（自己不直接认识，靠引荐）
    [3, "沈南鹏", "风险投资", "红杉资本中国", "创始及执行合伙人", "上海/香港",
     "投资界第一;携程系;极少见生人", 0,
     "高质量项目源;长期 LP 关系",
     "",  # 目标人物不需要填认识；靠别人的认识抵达
     "目标人物 · 圈内可引荐", "目标"],
]


# --------------------------------------------------------------------- writer
def build() -> Path:
    wb = xlsxwriter.Workbook(str(OUT_PATH))

    header_fmt = wb.add_format({
        "bold": True, "bg_color": "#1f2937", "font_color": "#f9fafb",
        "border": 1, "border_color": "#374151", "align": "center",
        "font_name": "PingFang SC", "font_size": 11,
    })
    # 必填列的表头更醒目
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
    # 三种 `关系类型` 行各自的底色
    fmt_direct = wb.add_format({
        "font_name": "PingFang SC", "font_size": 10, "valign": "top",
        "text_wrap": True, "border": 1, "border_color": "#e5e7eb",
        "bg_color": "#ecfdf5",
    })
    fmt_direct_mono = wb.add_format({
        "font_name": "JetBrains Mono", "font_size": 10, "valign": "top",
        "text_wrap": True, "border": 1, "border_color": "#e5e7eb",
        "align": "center", "bg_color": "#ecfdf5",
    })
    fmt_weak = wb.add_format({
        "font_name": "PingFang SC", "font_size": 10, "valign": "top",
        "text_wrap": True, "border": 1, "border_color": "#e5e7eb",
        "bg_color": "#f3f4f6",
    })
    fmt_weak_mono = wb.add_format({
        "font_name": "JetBrains Mono", "font_size": 10, "valign": "top",
        "text_wrap": True, "border": 1, "border_color": "#e5e7eb",
        "align": "center", "bg_color": "#f3f4f6",
    })
    fmt_target = wb.add_format({
        "font_name": "PingFang SC", "font_size": 10, "valign": "top",
        "text_wrap": True, "border": 1, "border_color": "#e5e7eb",
        "bg_color": "#fef3c7",
    })
    fmt_target_mono = wb.add_format({
        "font_name": "JetBrains Mono", "font_size": 10, "valign": "top",
        "text_wrap": True, "border": 1, "border_color": "#e5e7eb",
        "align": "center", "bg_color": "#fef3c7",
    })
    section_fmt = wb.add_format({
        "bold": True, "font_name": "PingFang SC", "font_size": 13,
        "font_color": "#1f2937",
    })
    help_fmt = wb.add_format({
        "font_name": "PingFang SC", "font_size": 11, "text_wrap": True,
        "valign": "top",
    })
    help_mono = wb.add_format({
        "font_name": "JetBrains Mono", "font_size": 10, "text_wrap": True,
        "valign": "top", "bg_color": "#f3f4f6",
    })

    # ================ Sheet 1: 联系人 ================
    ws = wb.add_worksheet("联系人")
    for col, (name, _desc, _ex, req, width) in enumerate(COLUMNS):
        ws.set_column(col, col, width)
        fmt = header_req_fmt if req == "★" else header_fmt
        ws.write(0, col, name, fmt)

    # 3 行示例：每行按 关系类型 染色
    for row_i, row in enumerate(SAMPLE_ROWS, start=1):
        kind = str(row[-1])
        if kind == "直接":
            txt, mono = fmt_direct, fmt_direct_mono
        elif kind == "弱认识":
            txt, mono = fmt_weak, fmt_weak_mono
        elif kind == "目标":
            txt, mono = fmt_target, fmt_target_mono
        else:
            txt, mono = cell_fmt, cell_mono
        mono_cols = {0, 7}  # 序号 / 可信度
        for col_i, val in enumerate(row):
            f = mono if col_i in mono_cols else txt
            ws.write(row_i, col_i, val, f)

    # 留 200 条空白行，方便直接填数据（保留表格样式）
    for row_i in range(len(SAMPLE_ROWS) + 1, len(SAMPLE_ROWS) + 1 + 200):
        for col_i in range(len(COLUMNS)):
            ws.write_blank(row_i, col_i, None, cell_fmt)

    # 下拉校验：关系类型 只能选 直接 / 弱认识 / 目标
    kind_col = len(COLUMNS) - 1
    ws.data_validation(
        1, kind_col, 2000, kind_col,
        {
            "validate": "list",
            "source": ["直接", "弱认识", "目标"],
            "input_title": "关系类型",
            "input_message":
                "直接=默认；弱认识=只建强度1边；目标=不建 Me 边，靠他人引荐",
        },
    )
    # 下拉校验：可信度 0-5
    ws.data_validation(
        1, 7, 2000, 7,
        {
            "validate": "integer",
            "criteria": "between",
            "minimum": 0, "maximum": 5,
            "input_title": "可信度",
            "input_message": "0-5 分；目标人物填 0",
        },
    )

    ws.freeze_panes(1, 2)
    ws.autofilter(0, 0, 200, len(COLUMNS) - 1)

    # ================ Sheet 2: 关系 （可选，高信号边）================
    ws2 = wb.add_worksheet("关系")
    rel_headers = ["甲", "乙", "强度", "关系", "频率"]
    rel_widths = [12, 12, 8, 38, 10]
    for col, (h, w) in enumerate(zip(rel_headers, rel_widths, strict=True)):
        ws2.set_column(col, col, w)
        ws2.write(0, col, h, header_fmt)
    # 一行示例
    example = ("陈维国", "沈南鹏", 3, "中金时多次合作（项目顾问）", "yearly")
    for col_i, val in enumerate(example):
        ws2.write(1, col_i, val, cell_mono if col_i in (2,) else cell_fmt)
    for row_i in range(2, 120):
        for col_i in range(len(rel_headers)):
            ws2.write_blank(row_i, col_i, None, cell_fmt)
    ws2.data_validation(
        1, 2, 120, 2,
        {"validate": "integer", "criteria": "between", "minimum": 1, "maximum": 5},
    )
    ws2.data_validation(
        1, 4, 120, 4,
        {"validate": "list",
         "source": ["weekly", "monthly", "quarterly", "yearly", "rare"]},
    )
    ws2.freeze_panes(1, 0)

    # ================ Sheet 3: 说明 ================
    ws3 = wb.add_worksheet("说明")
    ws3.set_column(0, 0, 28)
    ws3.set_column(1, 1, 80)

    hdr_fmt = wb.add_format({
        "bold": True, "font_name": "PingFang SC", "font_size": 11,
        "bg_color": "#1f2937", "font_color": "#f9fafb",
        "border": 1, "border_color": "#111827", "align": "center",
    })

    ws3.merge_range(0, 0, 0, 1, "Lodestar 联系人表 · 填写说明", section_fmt)
    ws3.set_row(0, 30)

    ws3.write(2, 0, "列名", hdr_fmt)
    ws3.write(2, 1, "说明 / 示例", hdr_fmt)
    for i, (name, desc, ex, req, _w) in enumerate(COLUMNS, start=3):
        label = f"{name}  [{req}]" if req != "-" else name
        ws3.write(i, 0, label, help_fmt)
        ws3.write(i, 1, f"{desc}\n示例：{ex}", help_fmt)
        ws3.set_row(i, 44 if "\n" in desc else 28)

    # 底部：通用规则 + 关键新机制说明
    base = 3 + len(COLUMNS) + 1
    tips: list[tuple[str, str]] = [
        ("通用分隔符",
         "多值字段（行业/公司/城市/需求/AI特征）都支持 `;` `，` `、` `/` `｜` 任意分隔。"),
        ("空值规则",
         "没数据直接留空。不要填 `-` `无` `NA` `N/A`——会被当字符串存下来。"),
        ("幂等导入",
         "重复跑 `uv run lodestar import <file.xlsx>` 是安全的：按姓名去重、"
         "同对关系不会重复加边。同事分批填、你合并是可行的。"),
        ("★ 『认识』怎么写",
         "格式：`人名1(强度,描述); 人名2(强度); 人名3(描述); 人名4`。"
         "括号里数字和描述顺序不限，都可省。只需写一个方向，导入器自动建双向边。"
         "\n如果填的人还没在表里，导入会警告并跳过；补进去再跑即可。"),
        ("★ 同公司自动建边",
         "同一个 `公司` 字段里出现的所有人会自动成为同事（强度 4）。这是最高效"
         "的补边机制——你不用手写『A 认识 B，都在 XX 公司』。"),
        ("★★ 『关系类型』怎么选",
         "直接（默认）= 用可信度作为你和此人的边强度；"
         "弱认识 = 只建强度 1 的 Me 边，避免点头之交拉高评分；"
         "目标 = 不建 Me 边，只依靠别人的『认识』列抵达——这是多跳引荐的关键。"
         "建议把『想接触、不直接认识』的人全部标成『目标』。"),
        ("可选『关系』sheet",
         "如果一条关系信息很丰富（怎么认识、什么频率），写到 `关系` sheet 更合适。"
         "Sheet 里的信息会覆盖主表『认识』列里同一对的内容。"
         "频率值：weekly / monthly / quarterly / yearly / rare（留空默认 yearly）。"),
        ("导入命令",
         "uv run lodestar import examples/template.xlsx"),
    ]
    for i, (k, v) in enumerate(tips):
        ws3.write(base + i, 0, k, help_fmt)
        ws3.write(base + i, 1, v, help_mono if "uv run" in v else help_fmt)
        ws3.set_row(base + i, 52 if len(v) > 80 else 36)

    wb.close()
    return OUT_PATH


if __name__ == "__main__":
    path = build()
    print(f"Wrote {path}  ({path.stat().st_size // 1024} KB)")
    print(f"  · {len(COLUMNS)} columns  (★ 必填 {sum(1 for c in COLUMNS if c[3]=='★')} 列)")
    print(f"  · {len(SAMPLE_ROWS)} sample rows  (直接/弱认识/目标 各一行)")
    print(f"  · 200 行空白模板 + 关系 sheet + 说明 sheet")
