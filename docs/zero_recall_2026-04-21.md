# Hybrid recall diagnosis — 2026-04-21

针对 `docs/eval_2026-04-21.md` 里三档 reranker 同时 R@5=0 的 4 条 query，
拆解 vector / helper-keyword / topic-keyword 三路召回，定位 expected 人物**卡在哪一步**。

## ✅ Root cause 已修复：补了 embedding，4 条死角全部回阳

**初次跑诊断时发现的根因**：`vec_person_bio` 在 richard（62 人）和 tommy（110 人）
两个网络下都是**空表**——170 个 bio 全没 embedding，`HybridSearch` 三路里
vector 通道**完全没工作**。当时 `docs/eval_2026-04-21.md` 里所有 reranker 对比
其实都是**keyword-only + 重排**，带系统偏差；4 条死角 R@5=0 是因为 keyword
LIKE 跟 bio 字面不重合（语义同义但字符串不同）：

| query | LLM intent | bio 实写 | LIKE 命中？ |
|---|---|---|---|
| r-ambig-1 | `政府机构` / `政府关系顾问` | `政府单位` / `药监部门` / `法院` | ❌ |
| r-onehop-1 | `个人朋友` / `铁磁关系人` | `私募基金 老板` / `券商 分公司老总` | ❌（bio 不写交情） |
| t-longt-1 | `律师事务所` / `企业法务` | `法务；律所` / `德恒律所律师` | ❌（"律师事务所" ≠ "律所"） |
| t-onehop-2 | `券商通道` / `证券公司` | `券商渠道` / `中泰证券券商渠道` | ❌（"通道" ≠ "渠道"） |

**修复**：跑 `uv run lodestar reembed` 为 170 个 bio 补 dashscope `text-embedding-v4`
（1024 维），重跑评测后 4 条全部回阳：

| query | 修复前 R@5 (bge) | 修复后 R@5 (bge) |
|---|---:|---:|
| r-ambig-1 | 0.00 | **0.50** |
| r-onehop-1 | 0.00 | **0.50** |
| t-longt-1  | 0.00 | **1.00** |
| t-onehop-2 | 0.00 | **0.33** |

下面每节的 vector top-15 / per-expected 表是补完 embedding 后的状态，
helper / topic keyword 仍空——印证了「这些查询全靠 vector 通道兜底」。

## 三档 reranker baseline（hybrid 真的工作之后）

详见 `docs/eval_2026-04-21.md`，要点：

| variant | R@5 | MRR | NDCG@10 | cliff-avoid | 平均延迟 |
|---|---:|---:|---:|---:|---:|
| none | 0.676 | 0.758 | 0.666 | 0.750 | 195ms |
| llm  | 0.769 | 0.900 | 0.782 | 0.800 | 45.3s |
| bge  | 0.766 | 0.842 | 0.753 | **0.850** | 11.7s |

**结论**：`bge` 与 `llm` 质量打平（R@5 差 0.003），cliff-avoid 反超，延迟低 **4 倍**，
零 token 费——一旦装好 `[rerank]` extra，**默认应当用 `bge`**，
`llm` 只在 MRR 略胜的高精度场景考虑。

## 仍未解决的长期问题

1. **`r-onehop-1` 的"借钱铁磁"语义**仍然是 50%——根本上是**信号层错位**：
   "借钱级别的铁磁"是 `relationship.strength=5` + `frequency` 的图侧维度，
   bio 文本压根不记交情。要么从 golden 移除这条，要么给 search 加一条
   「intent 含 '借钱/铁磁/熟人' → 退化为 strength≥5 的 contacted 列表」分支。
2. **keyword 通道的字面失配**长期存在（"通道" ≠ "渠道"、"律师事务所" ≠ "律所"）。
   有了 vector 之后已不再致命，但若想让 keyword 兜底更强，可在 `_rank_terms`
   前做轻量同义扩展，或在 enrich 阶段把 bio 归一化到统一标签表。
3. **`r-onehop-1` / `t-onehop-2` / `r-longt-3` / `t-longt-3`** 这几条 R@5 仍 ≤ 0.50。
   下次评测迭代可以从这几条入手定位剩余的召回 / 重排间隙。

---

下面是补 embedding 后的逐 query 拆解（vector top-15 + per-expected 三路命中表）：

## `r-ambig-1` · owner `richard`

- **goal**: 我想了解政府监管动态，认识能打招呼的政府关系
- **expected**: 廉向金, 宋伟, 陈宝岩, 蔡长余
- **hybrid top-5 returned**: 宋伟 · 廉向金 · 崔宁 · 康英杰 · 马刚

### parsed GoalIntent (LLM)
- summary: `一位在发改委、工信部、网信办、证监会或地方监管局担任处级及以上职务的政府官员，或大型国企/金融机构中专职负责政府关系与政策对接的总监级人士，熟悉行业监管节奏并具备实际协调打招呼能力。`
- roles: ['政府处长', '监管局副处长', '发改委/工信部/网信办/证监会等部委处室负责人', '政府关系总监', '政策研究室主任']
- industries: ['政府机构', '国资平台', '金融监管', '产业主管部门', '大型国企政企部']
- skills: ['监管政策解读', '政府关系维护', '政策动向预判', '跨部门协调', '合规准入支持']
- keywords: ['监管动态', '政府关系', '政策合规', '行业准入']
- cities: —

### per-expected diagnosis

| expected | in DB? | bio (first 120 chars) | vec rank/dist | helper rank/hits | topic rank/hits |
|---|---|---|---:|---:|---:|
| 廉向金 | ✅ id=29 | 行业：药监部门 · 职务：处长 · 公司：四川药监 · 城市：成都 · 合作价值：3/5 | 2 / 0.937 | — | — |
| 宋伟 | ✅ id=37 | 行业：政府单位 · 职务：科长 · 公司：审批局 · 城市：唐山 · 合作价值：3/5 | 1 / 0.932 | — | — |
| 陈宝岩 | ✅ id=8 | 行业：政府单位 · 职务：科长 · 公司：城市更新局 · 城市：深圳 · 合作价值：3/5 | 37 / 1.033 | — | — |
| 蔡长余 | ✅ id=14 | 行业：法院 · 职务：审判长 · 公司：法院 · 城市：天津 · 合作价值：3/5 | 54 / 1.068 | — | — |

### top-15 of each recall channel (for context)

**vector (cosine distance)**

| rank | name | score |
|---:|---|---:|
| 1 | 宋伟 | 0.932 |
| 2 | 廉向金 | 0.937 |
| 3 | 崔宁 | 0.953 |
| 4 | 康英杰 | 0.957 |
| 5 | 马刚 | 0.961 |
| 6 | 梁杏 | 0.963 |
| 7 | 徐永帅 | 0.966 |
| 8 | 黄振一 | 0.968 |
| 9 | 杜晓晗 | 0.971 |
| 10 | 吴忠超 | 0.971 |
| 11 | 高中帅 | 0.974 |
| 12 | 王涛 | 0.980 |
| 13 | 李坤 | 0.986 |
| 14 | 顾熀乾 | 0.986 |
| 15 | 别华荣 | 0.988 |

**helper keywords (roles+industries+skills)**

(empty)

**topic keywords (keywords+cities)**

(empty)

## `r-onehop-1` · owner `richard`

- **goal**: 我想找能直接借钱的核心铁磁朋友
- **expected**: 大钊, 建国哥, 崔宁, 纪少敏
- **hybrid top-5 returned**: 康英杰 · 崔宁 · 白龙 · 徐永帅 · 宋伟

### parsed GoalIntent (LLM)
- summary: `一位与你关系极为紧密、彼此绝对信任的核心铁磁朋友，愿意在你急需时直接出借个人资金，不依赖合同或抵押，重情义轻形式。`
- roles: ['个人朋友', '亲密关系人', '可信任的私人借贷方']
- industries: —
- skills: ['个人信用借贷', '无抵押借款', '短期资金周转支持', '基于信任的财务互助']
- keywords: ['借钱', '铁磁', '核心朋友', '私人借贷']
- cities: —

### per-expected diagnosis

| expected | in DB? | bio (first 120 chars) | vec rank/dist | helper rank/hits | topic rank/hits |
|---|---|---|---:|---:|---:|
| 大钊 | ✅ id=13 | 行业：私募基金 · 职务：营销总监 · 公司：进化论 · 城市：深圳，上海，青岛 · 合作价值：5/5 | 11 / 1.046 | — | — |
| 建国哥 | ✅ id=25 | 行业：私募基金 · 职务：老板 · 公司：银方基金 · 城市：青岛 · 合作价值：5/5 | 7 / 1.038 | — | — |
| 崔宁 | ✅ id=11 | 行业：券商 · 职务：分公司老总 · 公司：银河山分 · 城市：青岛 · 合作价值：5/5 | 2 / 1.018 | — | — |
| 纪少敏 | ✅ id=26 | 行业：私募基金 · 职务：老板 · 公司：神明投资 · 城市：青岛 · 合作价值：5/5 | 23 / 1.074 | — | — |

### top-15 of each recall channel (for context)

**vector (cosine distance)**

| rank | name | score |
|---:|---|---:|
| 1 | 康英杰 | 0.990 |
| 2 | 崔宁 | 1.018 |
| 3 | 白龙 | 1.022 |
| 4 | 徐永帅 | 1.028 |
| 5 | 宋伟 | 1.032 |
| 6 | 马刚 | 1.035 |
| 7 | 建国哥 | 1.038 |
| 8 | 李靖 | 1.038 |
| 9 | 德民 | 1.042 |
| 10 | 王强 | 1.042 |
| 11 | 大钊 | 1.046 |
| 12 | 胡斌 | 1.048 |
| 13 | 吴忠超 | 1.049 |
| 14 | 张坤 | 1.050 |
| 15 | 李文军 | 1.060 |

**helper keywords (roles+industries+skills)**

(empty)

**topic keywords (keywords+cities)**

(empty)

## `t-longt-1` · owner `tommy`

- **goal**: 我想找企业法务 / 合规方向的律师
- **expected**: 王浚哲
- **hybrid top-5 returned**: 王浚哲 · 钱之政 · 王方 · 陶琳 · 郑盛

### parsed GoalIntent (LLM)
- summary: `一位在律所或企业法务服务机构执业的资深企业法律顾问或合规律师，专注公司治理、数据隐私、反垄断及行业监管合规，能为企业提供体系化法律支持与风险防控方案。`
- roles: ['企业法律顾问', '合规律师', '公司法律事务合伙人', '数据合规专家', '反垄断与监管合规律师']
- industries: ['律师事务所', '企业法务服务', '金融科技合规', '互联网平台治理', '跨国公司合规']
- skills: ['企业日常法律咨询', '合规体系建设', '数据隐私与GDPR/PIPL落地', '反垄断申报与应对', '监管检查应对']
- keywords: ['企业法务', '合规', '公司法律', '数据合规', '监管应对']
- cities: —

### per-expected diagnosis

| expected | in DB? | bio (first 120 chars) | vec rank/dist | helper rank/hits | topic rank/hits |
|---|---|---|---:|---:|---:|
| 王浚哲 | ✅ id=142 | 行业：法务；律所 · 职务：德恒律所律师 · 合作价值：4/5 | 1 / 0.916 | — | — |

### top-15 of each recall channel (for context)

**vector (cosine distance)**

| rank | name | score |
|---:|---|---:|
| 1 | 王浚哲 | 0.916 |
| 2 | 钱之政 | 1.004 |
| 3 | 王方 | 1.009 |
| 4 | 陶琳 | 1.011 |
| 5 | 郑盛 | 1.028 |
| 6 | 晁亚新 | 1.034 |
| 7 | 赵凯 | 1.039 |
| 8 | 王志良 | 1.040 |
| 9 | 陈数 | 1.042 |
| 10 | 鞠强 | 1.043 |
| 11 | 向雯琦 | 1.046 |
| 12 | 邢自强 | 1.046 |
| 13 | 李紫祎 | 1.047 |
| 14 | 王雪源 | 1.051 |
| 15 | 常宇亮 | 1.052 |

**helper keywords (roles+industries+skills)**

(empty)

**topic keywords (keywords+cities)**

(empty)

## `t-onehop-2` · owner `tommy`

- **goal**: 我想做券商通道 / 渠道业务的对接
- **expected**: 张千千, 徐楷, 戎捷, 黄达, 李峰屏, 李紫祎
- **hybrid top-5 returned**: 李紫祎 · 赵思思 · 戎捷 · 刘畅 · 李想

### parsed GoalIntent (LLM)
- summary: `一位在证券公司机构业务部、财富管理部或营业部担任负责人/总监级职务的专业人士，深耕券商渠道与同业合作，熟悉私募产品代销、交易通道接入及监管合规落地。`
- roles: ['券商机构业务部负责人', '券商渠道业务总监', '券商财富管理部渠道合作经理', '券商营业部总经理', '券商同业业务BD']
- industries: ['证券公司', '金融同业', '财富管理', '资管通道']
- skills: ['券商通道业务对接', '代销产品引入', '私募基金托管与交易通道合作', '渠道分润机制设计', '监管合规适配（如中基协、证监会要求）']
- keywords: ['券商通道', '渠道代销', '同业合作', '资管通道', '私募接入']
- cities: —

### per-expected diagnosis

| expected | in DB? | bio (first 120 chars) | vec rank/dist | helper rank/hits | topic rank/hits |
|---|---|---|---:|---:|---:|
| 张千千 | ✅ id=88 | 行业：金融；券商渠道 · 职务：中泰证券券商渠道 · 城市：北京 · 合作价值：5/5 | 9 / 0.874 | — | — |
| 徐楷 | ✅ id=65 | 行业：金融；券商渠道 · 职务：国信证券深圳分公司 · 城市：深圳 · 合作价值：5/5 | 12 / 0.890 | — | — |
| 戎捷 | ✅ id=89 | 行业：金融；券商渠道 · 职务：中泰证券信息技术部负责人 · 城市：上海 · 合作价值：4/5 | 3 / 0.827 | — | — |
| 黄达 | ✅ id=72 | 行业：金融；券商渠道 · 职务：广发证券券商渠道 · 城市：广州 · 合作价值：5/5 | 7 / 0.864 | — | — |
| 李峰屏 | ✅ id=68 | 行业：金融；券商渠道 · 职务：中金财富券商渠道 · 城市：杭州 · 合作价值：5/5 | 11 / 0.883 | — | — |
| 李紫祎 | ✅ id=101 | 行业：金融；券商渠道 · 职务：国君海通大券商渠道 · 城市：北京 · 合作价值：5/5 | 1 / 0.818 | — | — |

### top-15 of each recall channel (for context)

**vector (cosine distance)**

| rank | name | score |
|---:|---|---:|
| 1 | 李紫祎 | 0.818 |
| 2 | 赵思思 | 0.824 |
| 3 | 戎捷 | 0.827 |
| 4 | 刘畅 | 0.838 |
| 5 | 李想 | 0.839 |
| 6 | 熊博 | 0.849 |
| 7 | 黄达 | 0.864 |
| 8 | 索喆 | 0.865 |
| 9 | 张千千 | 0.874 |
| 10 | 梁红智 | 0.877 |
| 11 | 李峰屏 | 0.883 |
| 12 | 徐楷 | 0.890 |
| 13 | 李嘉祺 | 0.899 |
| 14 | 易卫东 | 0.899 |
| 15 | 赵梓汝 | 0.900 |

**helper keywords (roles+industries+skills)**

(empty)

**topic keywords (keywords+cities)**

(empty)

