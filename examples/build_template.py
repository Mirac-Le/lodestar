"""Generate `examples/template.xlsx` — the canonical empty template handed
to colleagues so they can replace demo data with real contacts.

列定义是在原 `pyq.xlsx`（现 `richard_network.xlsx`）的 8 列之上，
经过两轮迭代加到 14 列：
  v1（2026-04-17）+4：公司 / 城市 / 认识 / 关系类型
  v2（2026-04-20）+2：合作价值（0-5） / 资源类型

v2 为了对齐 Tommy 的机构合作画像表的语义，让单一表能同时服务朋友圈
（Richard）和机构对接（Tommy）两种语境。

同步：本脚本输出的列、说明、示例与 `docs/network_template_guide.md`
保持完全一致；改任一处都要改另一处。

Run:
    uv run python examples/build_template.py
"""

from __future__ import annotations

from pathlib import Path

import xlsxwriter

OUT_PATH = Path(__file__).parent / "template.xlsx"

# --------------------------------------------------------------------- schema
# 顺序 = Excel 里的列顺序。★ 是必填，○ 是强烈推荐，- 是可选。
# (列名, 说明, 示例, 必填度, 宽度)
COLUMNS: list[tuple[str, str, str, str, int]] = [
    ("序号", "行号，可留空自动编", "1", "-", 6),
    ("姓名", "唯一主键；同名会合并，不要重复。同名不同人请用括号消歧 "
     "（如 `张伟（中金）` `张伟（红杉）`）。",
     "陈维国", "★", 14),
    ("所属行业",
     "大类行业标签。可多值：用 ; 或 、 分隔。"
     "AI 能从『AI 标准化特征』反推此列，所以可空。",
     "投资银行", "○", 14),
    ("公司",
     "当前任职公司全称。同公司两人会自动成为同事（强度 4）。多份工作用 ; 分隔。"
     "AI 能从 bio/特征里抽取此列，**留空也行**。",
     "中金公司", "○", 22),
    ("职务", "当前职位。AI 能从 bio 抽，留空也行。",
     "衍生品 MD", "○", 22),
    ("城市", "常驻城市。多城用 ; 分隔。AI 能抽，留空也行。",
     "香港", "-", 10),
    ("AI标准化特征",
     "★★ 最重要的 free-text 字段。短评式画像，用 ; 分隔。"
     "**这是 AI 解析的主要食粮 + 向量检索的主要语料，写得越饱满 AI 越能"
     "帮你补后面的列**。建议 5-10 个短标签。",
     "港大金融;衍生品;交际广;外派经验;善于做局", "○", 44),
    ("可信度（言行一致性0-5分）",
     "0-5 整数。1=点头之交（旧版的『弱认识』）、3=普通朋友、5=核心铁磁。"
     "**这一列承担『有多熟』的全部表达力**——以前的『弱认识』档已废，写 1 就是。"
     "**未联系的人填 0**。",
     "5", "★", 14),
    ("合作价值（0-5）",
     "0-5 整数。和『可信度』正交：可信度 = 人靠不靠谱、合作价值 = 商业潜力大不大。"
     "朋友圈类联系人没必要评估时**直接留空**。",
     "4", "○", 12),
    ("潜在需求",
     "此人在找什么；用于反向撮合（你能介绍谁当他的供给方）。",
     "家办客户;海外上市资源", "○", 22),
    ("资源类型",
     "此人能**提供**什么资源。多选：`资金;项目;服务;技术;人脉`。"
     "和『潜在需求』是供需两端。",
     "资金;人脉", "-", 14),
    ("认识",
     "★★ 决定多跳引荐路径的唯一手填字段。"
     "格式：`甲(4,大学同学); 乙(3,前同事); 丙`。"
     "数字 = 强度 1-5，文字 = 关系描述，都可省。**写到表外的人会被跳过**。"
     "**省力：同公司同事不用写在这里——『公司』字段会自动连同事**。",
     "刘思敏(5,中金同事); 沈南鹏(2,中金时见过几次)", "★", 56),
    ("备注", "任意自由文本；**不会进入搜索打分**，只在详情页展示。",
     "人脉通天", "-", 22),
    ("关系类型",
     "二选一，纯事实标记：\n"
     "  留空/已联系 → 我直接联系到了这个人（按可信度建 Me 边，默认）\n"
     "  未联系     → 我还没联系到这个人；只能靠别人的『认识』列被引荐到\n"
     "**这一列只描述事实，不描述意图**。"
     "『我下次想接触谁』是 web 端搜索框里那句自然语言决定的，不要预先在表里"
     "标谁是『目标』——任何人都可能成为某次查询的最佳匹配。",
     "已联系", "○", 12),
]

# --------------------------------------------------------------------- samples
# 三行示例，展示三种 `关系类型` 的填法，并演示「AI 能补 / 必须手填」的边界。
# - 第 1 行：Richard 风格的朋友圈联系人（AI 能从特征抽公司/职务/城市/标签）
# - 第 2 行：Tommy 风格的机构合作（合作价值 + 资源类型有意义）
# - 第 3 行：未联系的人（需要靠他人引荐才能抵达）
SAMPLE_ROWS: list[list[str | int]] = [
    [1, "陈维国", "投资银行", "中金公司", "衍生品 MD", "香港",
     "港大金融;衍生品;交际广;外派经验;善于做局", 5, "",
     "家办客户;海外上市资源", "",
     "刘思敏(5,同事); 沈南鹏(2,中金时见过几次); 张磊(2,香港路演)",
     "人脉通天", "已联系"],
    [2, "李伟", "保险资管", "某大型保险资管", "FOF 基金经理", "上海",
     "清华五道口;稳健派;保险机构视角;做固收;长期共赢", 4, 5,
     "稳健中性策略;CTA 配置",
     "资金", "周景明(3,清华校友); 邱国鹭(3,高毅路演)",
     "v3 合作过：发了一只中性策略的 FOF 子单元", "已联系"],
    [3, "沈南鹏", "风险投资", "红杉资本中国", "创始及执行合伙人", "上海/香港",
     "投资界第一;携程系;极少见生人", 0, "",
     "高质量项目源;长期 LP 关系",
     "资金;人脉", "",
     "圈内可引荐", "未联系"],
]


# --------------------------------------------------------------------- writer
def build() -> Path:
    wb = xlsxwriter.Workbook(str(OUT_PATH))

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
    callout_fmt = wb.add_format({
        "font_name": "PingFang SC", "font_size": 11, "text_wrap": True,
        "valign": "top", "bold": True,
        "bg_color": "#fef3c7", "font_color": "#7c2d12",
        "border": 1, "border_color": "#fbbf24",
    })

    # ================ Sheet 1: 联系人 ================
    ws = wb.add_worksheet("联系人")
    for col, (name, _desc, _ex, req, width) in enumerate(COLUMNS):
        ws.set_column(col, col, width)
        fmt = header_req_fmt if req == "★" else header_fmt
        ws.write(0, col, name, fmt)

    NUMERIC_COLS = {0, 7, 8}  # 序号 / 可信度 / 合作价值
    for row_i, row in enumerate(SAMPLE_ROWS, start=1):
        kind = str(row[-1])
        if kind == "未联系":
            txt, mono = fmt_target, fmt_target_mono
        else:
            txt, mono = fmt_direct, fmt_direct_mono
        for col_i, val in enumerate(row):
            f = mono if col_i in NUMERIC_COLS else txt
            ws.write(row_i, col_i, val, f)

    # 留 200 条空白行，方便直接填数据（保留表格样式）
    for row_i in range(len(SAMPLE_ROWS) + 1, len(SAMPLE_ROWS) + 1 + 200):
        for col_i in range(len(COLUMNS)):
            ws.write_blank(row_i, col_i, None, cell_fmt)

    kind_col = len(COLUMNS) - 1
    ws.data_validation(
        1, kind_col, 2000, kind_col,
        {
            "validate": "list",
            "source": ["已联系", "未联系"],
            "input_title": "关系类型",
            "input_message":
                "二选一，纯事实：已联系=默认（按可信度建 Me 边）；"
                "未联系=不建 Me 边，靠他人引荐。",
        },
    )
    ws.data_validation(
        1, 7, 2000, 7,
        {
            "validate": "integer",
            "criteria": "between",
            "minimum": 0, "maximum": 5,
            "input_title": "可信度",
            "input_message": "0-5 整数；未联系的人填 0",
        },
    )
    ws.data_validation(
        1, 8, 2000, 8,
        {
            "validate": "integer",
            "criteria": "between",
            "minimum": 0, "maximum": 5,
            "input_title": "合作价值",
            "input_message": "0-5 整数；不评估直接留空",
            "ignore_blank": True,
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

    ws3.merge_range(0, 0, 0, 1, "Lodestar 联系人表 · 填写说明（v2 · 14 列）",
                    section_fmt)
    ws3.set_row(0, 30)

    # —— 顶部：最省力填法的 callout
    ws3.merge_range(
        1, 0, 1, 1,
        "💡 最省力填法：只把『姓名 · 可信度 · AI标准化特征 · 认识 · 关系类型』填完，"
        "其它列全部留空交给 AI——录入完后跑一次 `lodestar enrich` 即可自动补出 "
        "公司 / 城市 / 职务 / tags。",
        callout_fmt,
    )
    ws3.set_row(1, 50)

    # —— 列说明表
    ws3.write(3, 0, "列名", hdr_fmt)
    ws3.write(3, 1, "说明 / 示例", hdr_fmt)
    for i, (name, desc, ex, req, _w) in enumerate(COLUMNS, start=4):
        label = f"{name}  [{req}]" if req != "-" else name
        ws3.write(i, 0, label, help_fmt)
        ws3.write(i, 1, f"{desc}\n示例：{ex}", help_fmt)
        ws3.set_row(i, 56 if "\n" in desc or len(desc) > 60 else 32)

    # —— 通用规则 + 关键新机制
    base = 4 + len(COLUMNS) + 1
    tips: list[tuple[str, str]] = [
        ("通用分隔符",
         "多值字段（行业/公司/城市/需求/AI特征/认识）都支持 `;` `，` `、` `/` `｜` "
         "任意分隔。"),
        ("空值规则",
         "没数据直接留空。**不要**填 `-` `无` `NA` `N/A`——会被当字符串存下来污染搜索。"),
        ("幂等导入",
         "重复跑 `uv run lodestar import <file.xlsx>` 是安全的：按姓名去重、"
         "同对关系不会重复加边。同事分批填、你合并是可行的。"),
        ("★ 『AI 标准化特征』要写成短标签串",
         "把对此人的关键画像浓缩成 5-10 个短标签，用 `;` 分隔。"
         "✅ `港大金融;衍生品;交际广;外派经验`  "
         "❌ `这个人在中金做衍生品好多年了，人脉很广` （整段话向量命中率低）"),
        ("★ 『认识』怎么写",
         "格式：`人名1(强度,描述); 人名2(强度); 人名3(描述); 人名4`。"
         "数字和描述顺序不限、都可省。**只需写一个方向**，导入器自动建双向边。"
         "如果填的人还没在表里，导入会警告并跳过；补进去再跑即可。"),
        ("★ 同公司自动建边 — 省一半『认识』列",
         "同一个 `公司` 字段里出现的所有人会自动成为同事（强度 4）。"
         "所以**同事不要写到『认识』里**，『认识』只用来描述跨公司的人脉"
         "（校友、前同事、合作过的对家、饭局认识等）。"),
        ("★★ 『关系类型』就两档：已联系 / 未联系",
         "二选一，纯事实标记，不是意图："
         "  已联系（默认）= 我直接联系到了这个人，按『可信度』1-5 决定边的强度；"
         "  未联系 = 我还没联系到，不建 Me 边，只能靠别人的『认识』列被引荐到。"
         "\n旧版的『弱认识』档已废——『有多熟』完全交给『可信度』列表达，写 1 就是。"
         "\n⚠️ **这一列不要用来标『谁是我想接触的人』**。"
         "『下次想找谁』是你在 web 端搜索框里说的那句话，由 LLM 解析意图后从全表"
         "（含已联系 + 未联系）一起匹配并算路径——任何人都可能是某次查询的最佳匹配。"),
        ("★★ AI 自动补字段（v2 新机制）",
         "导入完后跑一次 `lodestar enrich --owner <你> --apply`，AI 会读"
         "『AI 标准化特征』『备注』『职务』，自动补出："
         "  · companies（公司列）  · cities（城市列）  · titles（职位 tag）  · tags（语义标签）"
         "\n隐私：所有人名 → P000/P001/...，已知公司名 → C001/C002/... 后才发到云端 LLM；"
         "本地反映射回真名后入库，云端见不到原始姓名/已结构化的公司。"
         "\n再跑一次 `lodestar infer-colleagues --owner <你> --apply` 把 AI 抽到的公司"
         "物化成同事网（peer 边）。"),
        ("可选『关系』sheet",
         "如果一条关系信息很丰富（怎么认识、什么频率），写到 `关系` sheet 更合适。"
         "Sheet 里的信息会**覆盖**主表『认识』列里同一对的内容。"
         "频率值：weekly / monthly / quarterly / yearly / rare（留空默认 yearly）。"),
        ("导入 + 解析 + 同事推断 全流程",
         "uv run lodestar import examples/template.xlsx --owner you\n"
         "uv run lodestar enrich --owner you --apply\n"
         "uv run lodestar infer-colleagues --owner you --apply"),
    ]
    for i, (k, v) in enumerate(tips):
        ws3.write(base + i, 0, k, help_fmt)
        ws3.write(base + i, 1, v, help_mono if "uv run" in v else help_fmt)
        ws3.set_row(base + i, 76 if len(v) > 130 else (52 if len(v) > 80 else 36))

    wb.close()
    return OUT_PATH


if __name__ == "__main__":
    path = build()
    n_required = sum(1 for c in COLUMNS if c[3] == "★")
    n_recommended = sum(1 for c in COLUMNS if c[3] == "○")
    print(f"Wrote {path}  ({path.stat().st_size // 1024} KB)")
    print(f"  · {len(COLUMNS)} columns "
          f"(★ 必填 {n_required} 列 / ○ 推荐 {n_recommended} 列)")
    print(f"  · {len(SAMPLE_ROWS)} sample rows  (已联系×2 / 未联系×1)")
    print(f"  · 200 行空白模板 + 关系 sheet + 说明 sheet")
