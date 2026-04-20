"""Generate `examples/demo_network.xlsx` — a virtual Chinese quant-finance
network with realistic clusters, used as a template to hand to colleagues.

Run:
    uv run python examples/build_demo_network.py

The resulting file has three sheets:

* `联系人` — fictional + famous-name contacts. "Did I reach this person?"
              is encoded directly in the `可信度` column (0 = 未联系, 1-5 =
              已联系). No separate `关系类型` column anymore.
* `关系`   — hand-crafted peer edges (overrides any `认识` column
              content of the same pair).
* `说明`   — human-readable instructions for the person filling it in.

Replace the fictional names / companies with real ones and re-import:
    uv run lodestar import examples/demo_network.xlsx
"""

from __future__ import annotations

from pathlib import Path

import xlsxwriter

OUT_PATH = Path(__file__).parent / "demo_network.xlsx"

# --------------------------------------------------------------------- people
# Tuple = (序号, 姓名, 行业, 公司, 职务, 城市, AI特征, 可信度,
#          潜在需求, 认识, 备注)
#
# 可信度 (single source of truth for "did I reach this person?"):
#   0      → 未联系: NO Me edge built; reachable only via peers' 认识
#   1      → 点头之交 (used to be the deprecated "弱认识" tier)
#   3      → 普通朋友
#   5      → 核心铁磁
#
# IMPORTANT: every 可信度=0 person below is referenced by at least 2 direct
# friends in their `认识` column, so multi-hop paths exist.
PEOPLE: list[tuple[int, str, str, str, str, str, str, int, str, str, str]] = [
    # ---- Cluster A: 头部量化私募「沧海资本」（紧密团队 + 校友网）----
    (1, "周景明", "私募基金", "沧海资本", "CIO / 管理合伙人", "上海",
     "清华数学系;高频因子;管理严格;不善社交", 5, "LP 资金;机构投资者资源",
     "郑雅婷(5,同事); 孙伟(5,同事); 李昊然(5,同事); 钟启勋(4,清华师兄); "
     "陈维国(3,业内); 邱国鹭(2,高毅路演)", "圈内口碑极好"),
    (2, "郑雅婷", "私募基金", "沧海资本", "研究总监", "上海",
     "复旦金工;因子研究;风格稳健;产后复出", 4, "一级市场 deal flow",
     "周景明(5,同事); 冯倩(4,复旦师妹); 孙伟(4,同事)", ""),
    (3, "孙伟", "私募基金", "沧海资本", "高频交易员", "上海",
     "中科大物理;低延迟;话少;技术控", 4, "FPGA 工程师人选",
     "周景明(5,同事); 郑雅婷(4,同事); 苏晓明(4,科大校友)", ""),
    (4, "李昊然", "私募基金", "沧海资本", "风控总监", "上海",
     "券商合规出身;保守;细节控", 4, "合规律所资源",
     "周景明(5,同事); 刘思敏(3,前同事)", ""),

    # ---- Cluster B: 投行/券商系（中金脉络）----
    (5, "陈维国", "投资银行", "中金公司", "衍生品 MD", "香港",
     "港大金融;衍生品;交际广;外派经验", 5, "家办客户;海外上市资源",
     "刘思敏(5,同事); 周景明(3,业内); 林海峰(3,校友); 何文博(4,老朋友); "
     "沈南鹏(2,中金时见过几次); 张磊(2,香港路演)", "人脉通天"),
    (6, "刘思敏", "投资银行", "中金公司", "TMT 组 VP", "上海",
     "北大光华;TMT;加班狂;PPT 精美", 4, "一级项目 deal flow",
     "陈维国(5,同事); 吴翰林(3,合作); 李昊然(3,前同事); 蔡嘉怡(3,北大校友); "
     "沈南鹏(2,服务过红杉项目); 张一鸣(1,字节项目尽调)", ""),
    (7, "王博宇", "证券研究", "中信建投", "首席宏观分析师", "北京",
     "社科院博士;宏观;上电视;能说会道", 4, "政策层接触",
     "黄启明(4,圆桌会); 钟启勋(3,学术圈); 秦韵(5,常年采访)", "宏观圈 KOL"),
    (8, "林海峰", "证券研究", "国泰君安", "量化组组长", "上海",
     "港大金融;因子复盘;佛系", 3, "算力",
     "陈维国(3,校友); 周景明(2,同业); 罗子昂(4,合租室友)", ""),

    # ---- Cluster C: FOF / 三方资管 ----
    (9, "吴翰林", "私募基金", "涌泉资本 FOF", "管理合伙人", "北京",
     "人大金融;擅长募资;饭局多", 4, "优质管理人推荐",
     "徐文(5,同事); 钱坤(4,渠道合作); 刘思敏(3,合作); 秦韵(4,常联系); "
     "张磊(2,高瓴尽调过); 邱国鹭(3,高毅 LP); 朱啸虎(2,金沙江饭局)",
     "头部 FOF"),
    (10, "徐文", "私募基金", "涌泉资本 FOF", "量化研究员", "北京",
     "清北联培;Excel 快;低调", 3, "量化策略尽调",
     "吴翰林(5,同事); 郑雅婷(3,调研对象)", ""),
    (11, "钱坤", "三方财富", "磐石财富", "华东区总经理", "上海",
     "销售出身;高净值客户多;微信 5w 好友", 4, "产品代销",
     "吴翰林(4,合作); 何文博(4,常联系); 林海峰(3,业内); "
     "段永平(1,温州饭局); 张磊(1,高瓴 LP 大会)", "资源型"),

    # ---- Cluster D: 政府 / 国资 ----
    (12, "黄启明", "政府国资", "某地方金融工作局", "处长", "杭州",
     "财政部借调过;官场人情练达;讲普通话", 3, "政策理解;咨询费",
     "王博宇(4,圆桌会); 赵建华(4,老同事); 周景明(2,调研); "
     "雷军(1,杭州招商接待过)", "关键政策节点"),
    (13, "赵建华", "政府国资", "国开行总行", "二级行长助理", "北京",
     "清华经管;国开系统;稳重", 3, "市场化退出路径",
     "黄启明(4,老同事); 钟启勋(3,清华校友)", ""),

    # ---- Cluster E: 创业/AI/硬科技 ----
    (14, "苏晓明", "创业公司", "星辰算力", "创始人 / CEO", "杭州",
     "中科大计算机;Transformer;英文好;连续创业者", 5, "GPU 资源;机构融资",
     "罗子昂(4,前同事); 蔡嘉怡(3,Y 生态会); 孙伟(4,科大校友); "
     "何文博(4,BD 介绍的); 朱啸虎(2,种子轮 pitch 过); 雷军(1,小米生态投资接触)",
     "下一轮估值看涨"),
    (15, "罗子昂", "创业公司", "墨子芯", "创始人 / CEO", "深圳",
     "清华电子;芯片验证;内向;讲 PPT 一般", 4, "流片资金;光电人才",
     "林海峰(4,合租室友); 苏晓明(4,前同事); 蔡嘉怡(3,创业圈); "
     "雷军(2,小米澎湃合作过)", ""),
    (16, "蔡嘉怡", "创业公司", "倾听 AI", "创始人 / CEO", "北京",
     "北大软微;字节 L8 过;女性创业者;文艺", 4, "第一批种子用户",
     "刘思敏(3,北大校友); 苏晓明(3,生态会); 罗子昂(3,创业圈); "
     "秦韵(4,采访认识); 张一鸣(3,字节前老板); 朱啸虎(3,投过倾听 AI 天使轮)",
     ""),

    # ---- Cluster F: 学术 ----
    (17, "钟启勋", "学术研究", "清华大学", "姚班教授", "北京",
     "MIT 博士;强化学习;清高;审稿严格", 3, "博士生就业",
     "周景明(4,清华师弟); 王博宇(3,学术圈); 赵建华(3,清华校友); "
     "冯倩(3,审过博后)", ""),
    (18, "冯倩", "学术研究", "复旦大学", "统计学博士后", "上海",
     "港大博士;贝叶斯;数学好;不社交", 3, "工业界 offer",
     "郑雅婷(4,复旦师姐); 钟启勋(3,审过博后)", ""),

    # ---- Cluster G: 服务业 / 桥梁节点（连接多 cluster）----
    (19, "何文博", "专业服务", "跨界咨询", "投资人关系总监", "上海",
     "饭局之王;所有人微信都有;做局能手", 5, "长期饭票",
     "陈维国(4,老朋友); 钱坤(4,常联系); 苏晓明(4,BD 介绍过); "
     "秦韵(5,长期合作); 吴翰林(3,合作); 沈南鹏(2,红杉 IR 局认识); "
     "朱啸虎(3,饭局熟人)", "枢纽节点"),
    (20, "秦韵", "媒体", "《财经视野》", "独立记者", "北京",
     "南方系记者;文笔好;消息灵通", 4, "独家爆料;人物故事",
     "王博宇(5,常年采访); 吴翰林(4,常联系); 蔡嘉怡(4,采访认识); "
     "何文博(5,长期合作); 张一鸣(2,字节早期专访); 雷军(2,小米发布会)",
     "信息枢纽"),

    # ---- Cluster H: 现金流 / 老板 ----
    (21, "白总", "私营企业主", "白氏贸易", "董事长", "温州",
     "温州皮鞋起家;资金灵活;普通话一般", 3, "出海通道",
     "钱坤(3,客户); 吴翰林(2,LP 候选); 段永平(2,温州老乡饭局)",
     "老派民营"),
    (22, "赵大山", "私营企业主", "大山建材", "实际控制人", "绍兴",
     "地产上游;现金厚;低调", 3, "资产配置建议",
     "白总(4,老乡); 钱坤(3,客户); 段永平(2,投过他基金的小份额)", ""),

    # ---- Cluster I: 医疗 / 跨行业 ----
    (23, "贺医生", "医疗大健康", "瑞金医院", "心内科主任医师", "上海",
     "协和毕业;专家号难挂;严谨", 4, "医疗器械尽调",
     "冯倩(2,病人介绍); 李昊然(2,专家)", ""),
    (24, "郭博", "医疗大健康", "某创新药 A 轮", "联合创始人 / CSO", "苏州",
     "北大医学部;肿瘤;英文好", 4, "二级对接;上市辅导",
     "贺医生(4,医学圈); 刘思敏(2,融资合作)", ""),

    # ---- Cluster J: 销售渠道 ----
    (25, "田经理", "销售渠道", "华东某城商行", "私行部客户经理", "南京",
     "银行柜员晋升;服务意识强", 3, "基金代销渠道",
     "钱坤(4,渠道合作); 徐文(2,调研)", ""),

    # ---- Cluster K: 同学网络（桥梁）----
    (26, "姚远", "保险资管", "某大型保险资管", "基金经理", "北京",
     "清华五道口;稳健派;保险机构视角", 3, "固收产品对接",
     "周景明(3,清华校友); 赵建华(3,清华校友); 吴翰林(2,合作); "
     "邱国鹭(3,高毅路演);", ""),
    (27, "乔楠", "互联网", "某电商大厂", "战投负责人", "杭州",
     "北大光华;做过投行;年轻有冲劲", 3, "电商出海项目",
     "刘思敏(3,北大校友); 蔡嘉怡(3,字节前同事); 苏晓明(3,行业会); "
     "张一鸣(2,字节前同事)", ""),
    (28, "方亦辰", "律所", "金杜律所", "合伙人", "北京",
     "美国 JD;私募律师;写合同很快;健身", 3, "项目撮合",
     "李昊然(3,合规合作); 陈维国(3,交易律师); 吴翰林(2,法律顾问)", ""),
    (29, "简宁", "FA", "某精品 FA", "执行董事", "上海",
     "前投行;嘴甜;项目敏感度高", 3, "并购买方",
     "何文博(5,同业); 刘思敏(3,合作); 沈南鹏(1,FA 拜访过红杉)", ""),

    # ============ 「未联系」人物（你不直接认识，但圈内有人能引荐）============
    # All `关系类型 = 未联系`. They will NOT have a Me-edge. The system reaches
    # them via the `认识` references in the rows above.
    (30, "沈南鹏", "风险投资", "红杉资本中国",
     "创始及执行合伙人", "上海/香港",
     "投资界第一; 携程系; 早期 BAT 投资人; 极少见生人", 0,
     "高质量项目源; 长期 LP 关系", "",
     "想接触 · 圈内可引荐"),
    (31, "张磊", "私募基金", "高瓴资本", "创始人 / CEO", "北京/香港",
     "中国 PE 一哥; 耶鲁背景; 长期主义; 大单局; 极少见生人", 0,
     "顶级管理人协同; 大型并购 deal", "",
     "想接触 · 圈内可引荐"),
    (32, "朱啸虎", "风险投资", "金沙江创投", "主管合伙人", "上海",
     "TMT 早期投资人; 风格高调; 滴滴/小红书早期投资; 媒体活跃", 0,
     "新风口 deal flow", "",
     "想接触 · 圈内可引荐"),
    (33, "雷军", "互联网", "小米集团", "创始人 / CEO / 董事长", "北京",
     "顺为资本合伙人; 武大毕业; 工程师创始人; 生态投资活跃", 0,
     "硬科技/IoT/汽车上下游", "",
     "想接触 · 圈内可引荐"),
    (34, "张一鸣", "互联网", "字节跳动", "创始人 (已退任 CEO)", "北京/新加坡",
     "南开毕业; 工程师出身; 极度低调; 算法信仰", 0,
     "AI 应用方向 deal; 字节系协同", "",
     "想接触 · 圈内可引荐"),
    (35, "段永平", "投资人", "OPPO/vivo 系 / 步步高",
     "实际控制人 / 投资人", "美国/广州",
     "教父级实业家; 价值投资; 苹果/茅台/腾讯重仓; 极少公开露面", 0,
     "长期价值标的; 跨境配置", "",
     "想接触 · 圈内可引荐"),
    (36, "邱国鹭", "私募基金", "高毅资产", "董事长", "上海",
     "原南方基金投资总监; 价值派; 著有《投资中最简单的事》", 0,
     "成熟管理人合作", "",
     "想接触 · 圈内可引荐"),
]

# --------------------------------------------------------------------- edges
# The `关系` sheet is authoritative for high-signal peer edges that need
# richer context than the inline `认识` column can express. (Anything here
# overrides the same pair parsed from `认识`.)
# Columns: (甲, 乙, 强度, 关系, 频率)
RELATIONS: list[tuple[str, str, int, str, str]] = [
    # ---- 桥梁边：让 path-finding 更有意思 ----
    ("周景明", "陈维国", 3, "衍生品业务对接", "quarterly"),
    ("王博宇", "吴翰林", 3, "宏观季度闭门会", "quarterly"),
    ("何文博", "简宁", 5, "FA 合作紧密", "weekly"),
    ("秦韵",   "陈维国", 3, "人物专访", "yearly"),
    ("苏晓明", "王博宇", 2, "同一场宏观会", "rare"),
    ("蔡嘉怡", "乔楠",   4, "字节前同事", "monthly"),
    ("郭博",   "吴翰林", 2, "医药 FOF 尽调", "yearly"),
    ("田经理", "白总",   3, "私行客户", "quarterly"),
    ("赵大山", "田经理", 2, "资产配置", "yearly"),
    ("方亦辰", "周景明", 3, "基金合规法律顾问", "quarterly"),
    ("方亦辰", "苏晓明", 3, "创业法律顾问", "quarterly"),
    ("姚远",   "林海峰", 3, "保险资管调研", "quarterly"),
    ("赵大山", "姚远",   2, "保险配置顾问", "yearly"),
    ("简宁",   "郭博",   3, "医药并购 deal", "monthly"),

    # ---- 通往「未联系」人物的高信号边（强度高一点，便于路径聚焦）----
    ("陈维国", "沈南鹏", 3, "中金时多次合作（项目顾问）",      "yearly"),
    ("何文博", "沈南鹏", 3, "红杉 LP/IR 圈饭局",              "yearly"),
    ("吴翰林", "张磊",   3, "高瓴尽调访谈、行业闭门",          "yearly"),
    ("蔡嘉怡", "朱啸虎", 4, "金沙江领投了倾听 AI 天使轮",      "monthly"),
    ("何文博", "朱啸虎", 3, "饭局熟人;曾共投一个 SaaS",        "quarterly"),
    ("罗子昂", "雷军",   3, "墨子芯曾给小米澎湃做 IP 授权",    "yearly"),
    ("苏晓明", "雷军",   2, "顺为资本对星辰算力做过 pre-A 接触", "yearly"),
    ("蔡嘉怡", "张一鸣", 4, "字节 L8 时直属老板",              "yearly"),
    ("乔楠",   "张一鸣", 2, "字节前同事(2014 加入)",           "yearly"),
    ("白总",   "段永平", 2, "温州/广东老乡饭局共同朋友",        "rare"),
    ("赵大山", "段永平", 2, "通过 FA 投过他基金的小份额",       "rare"),
    ("吴翰林", "邱国鹭", 4, "高毅长期 LP",                     "quarterly"),
    ("姚远",   "邱国鹭", 3, "高毅路演调研",                    "quarterly"),
    ("刘思敏", "沈南鹏", 2, "中金 TMT 服务过红杉的投后项目",    "yearly"),
]


# --------------------------------------------------------------------- writer
def build() -> Path:
    wb = xlsxwriter.Workbook(str(OUT_PATH))

    header_fmt = wb.add_format({
        "bold": True, "bg_color": "#1f2937", "font_color": "#f9fafb",
        "border": 1, "border_color": "#374151", "align": "center",
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
    cell_target = wb.add_format({
        "font_name": "PingFang SC", "font_size": 10, "valign": "top",
        "text_wrap": True, "border": 1, "border_color": "#e5e7eb",
        "bg_color": "#fef3c7",  # 浅金色：「未联系」高亮
    })
    cell_mono_target = wb.add_format({
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
    headers = [
        "序号", "姓名", "所属行业", "公司", "职务", "城市",
        "AI标准化特征", "可信度（言行一致性0-5分）", "潜在需求",
        "认识", "备注",
    ]
    widths = [6, 10, 12, 22, 22, 10, 48, 10, 22, 56, 24]
    for col, (h, w) in enumerate(zip(headers, widths, strict=True)):
        ws.set_column(col, col, w)
        ws.write(0, col, h, header_fmt)

    for row_i, p in enumerate(PEOPLE, start=1):
        (idx, name, industry, company, role, city, feats, trust,
         needs, peers, notes) = p
        # "未联系" is encoded by 可信度 == 0; color the row金 in that case.
        is_target = trust == 0
        text_fmt = cell_target if is_target else cell_fmt
        mono_fmt = cell_mono_target if is_target else cell_mono
        ws.write(row_i, 0, idx, mono_fmt)
        ws.write(row_i, 1, name, text_fmt)
        ws.write(row_i, 2, industry, text_fmt)
        ws.write(row_i, 3, company, text_fmt)
        ws.write(row_i, 4, role, text_fmt)
        ws.write(row_i, 5, city, text_fmt)
        ws.write(row_i, 6, feats, text_fmt)
        ws.write(row_i, 7, trust, mono_fmt)
        ws.write(row_i, 8, needs, text_fmt)
        ws.write(row_i, 9, peers, text_fmt)
        ws.write(row_i, 10, notes, text_fmt)

    ws.freeze_panes(1, 2)
    ws.autofilter(0, 0, len(PEOPLE), len(headers) - 1)

    # ================ Sheet 2: 关系 ================
    ws2 = wb.add_worksheet("关系")
    rel_headers = ["甲", "乙", "强度", "关系", "频率"]
    rel_widths = [12, 12, 8, 38, 10]
    for col, (h, w) in enumerate(zip(rel_headers, rel_widths, strict=True)):
        ws2.set_column(col, col, w)
        ws2.write(0, col, h, header_fmt)
    for row_i, (a, b, s, ctx, freq) in enumerate(RELATIONS, start=1):
        ws2.write(row_i, 0, a, cell_fmt)
        ws2.write(row_i, 1, b, cell_fmt)
        ws2.write(row_i, 2, s, cell_mono)
        ws2.write(row_i, 3, ctx, cell_fmt)
        ws2.write(row_i, 4, freq, cell_mono)
    ws2.freeze_panes(1, 0)

    # ================ Sheet 3: 说明 ================
    ws3 = wb.add_worksheet("说明")
    ws3.set_column(0, 0, 110)
    ws3.write(0, 0, "Lodestar 人脉表 · 填写说明", section_fmt)
    ws3.set_row(0, 30)

    text_blocks: list[tuple[str, str]] = [
        ("1. 基本原则", ""),
        ("", "• 每一行 = 一个联系人。`姓名` 是唯一主键，表内不要重复。"),
        ("", "• 所有『行业/公司/城市/需求』支持多值，用 `;` `，` 或 `、` 分隔都行。"),
        ("", "• 没数据的格留空就行，不要填 `-` `无` `NA`。"),

        ("2. 最关键的一列：可信度（0-5，单列承载一切）", ""),
        ("", "整张表里**唯一**用来表达「我和这个人的关系」的列，只填一个 0-5 的整数："),
        ("", "  0 → 未联系：我还没直接接触过此人。系统不建 Me 边；命中时会自动算"),
        ("", "       1-3 跳的引荐路径并放在搜索结果的『需要引荐』段。"),
        ("", "  1 → 点头之交：刚加微信、饭局认识、被介绍认识但没深聊。"),
        ("", "  3 → 普通朋友。"),
        ("", "  5 → 核心铁磁。"),
        ("", "⚠️ 留空 = 默认 3，所以未联系的人**务必显式填 0**。"),
        ("", "⚠️ **不要在表里预先标谁是『想接触的目标』**。下次想找谁是 web 端搜索框"),
        ("", "    里那句自然语言决定的，由 LLM 解析意图后从全表（含已联系 + 未联系）一起"),
        ("", "    匹配——任何人都可能是某次查询的最佳匹配。"),
        ("", "Demo 表里 沈南鹏/张磊/朱啸虎/雷军/张一鸣/段永平/邱国鹭 都是 可信度=0。"),

        ("3. 关键列：认识", ""),
        ("", "把此人认识的其他人写进来。格式：用 `;` 分隔，每一项可以附加强度(1-5)和关系描述。"),
        ("格式示例", "王毅; 建国哥(4,大学同学); 李总(3,前同事); 陈博(老朋友)"),
        ("", "• 括号内数字 = 关系强度 1-5（可省，默认 3）"),
        ("", "• 括号内文字 = 关系描述（可省）"),
        ("", "• 数字和描述顺序不限，用逗号分开"),
        ("", "• 只需写一个方向：你写了 'A→B'，导入器会自动建立双向边"),
        ("", "• 如果填的人不在表里，导入时会警告并跳过"),
        ("", "• ★ 想被引荐到某个 可信度=0 的人，就在中间人那一行的『认识』里写上他。"),

        ("4. 关键列：公司", ""),
        ("", "同一家公司的人会自动被视为同事（强度 4）。这是最高效的补边机制。"),
        ("", "如果一个人在多家公司任职，用 `;` 分隔。例：`红杉中国; 某创业公司 董事`"),

        ("5. 潜在需求", ""),
        ("", "这个人在找什么。系统会用来做『引荐』—— 你可以介绍 A 给 B 当对方的供给方。"),
        ("", "示例：`LP 资金; 一级 deal flow`；`合规律所资源`；`GPU 算力`。"),

        ("6. Sheet「关系」（可选）", ""),
        ("", "如果某条关系信息很丰富（怎么认识的、频率如何），用 `关系` sheet 更合适。"),
        ("", "Sheet 里的信息会覆盖主表『认识』列里同一对的内容。"),
        ("", "频率值：weekly / monthly / quarterly / yearly / rare（留空默认 yearly）"),

        ("7. 重新导入", ""),
        ("", "改完 Excel 后，在项目根跑："),
        ("", "    uv run lodestar import examples/demo_network.xlsx"),
        ("", "重复导入是安全的：按姓名/公司/人对去重，只会增量更新。"),
    ]
    for i, (label, text) in enumerate(text_blocks, start=2):
        if label and not text:
            fmt = section_fmt
        elif "uv run" in text or "(4,大学" in text:
            fmt = help_mono
        else:
            fmt = help_fmt
        content = f"{label}    {text}" if label and text else (label or text)
        ws3.write(i, 0, content, fmt)
        ws3.set_row(i, 22 if text else 26)

    wb.close()
    return OUT_PATH


if __name__ == "__main__":
    path = build()
    n_targets = sum(1 for p in PEOPLE if p[7] == 0)  # 可信度==0 即未联系
    print(f"Wrote {path}  ({path.stat().st_size // 1024} KB)")
    print(f"  · {len(PEOPLE)} people  (含 {n_targets} 个『未联系』人物)")
    print(f"  · {len(RELATIONS)} authoritative edges in 关系 sheet")
