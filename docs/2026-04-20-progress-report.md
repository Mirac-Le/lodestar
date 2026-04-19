# Lodestar 人脉 Copilot · 今日进度汇报

**日期**：2026-04-20  **阶段**：多 owner + LLM 结构化抽取上线，关系图谱从「单星型」升级到「带同事网」

> 今日交付 8 项：① 双 owner 切换（Richard / Tommy） ② LLM L1 结构化抽取（公司 / 城市 / 职务 / 标签） ③ 公司名脱敏：把云端可见面从「全量公司」缩到「未结构化的新公司」 ④ 前端 3 个 AI 入口 ⑤ `infer-colleagues` 子命令把 LLM 抽出的公司物化成 peer 边 ⑥ 关系来源的 provenance 优先级链 ⑦ **`normalize-companies` 公司名归一化（builtin + 用户 alias 文件 + 可选 LLM 聚类）** ⑧ **关系类型术语统一：「目标」→「未联系」，importer 不再向后兼容旧词，去除"目标 = 搜索目标"的歧义**

---

## 一、今日交付

### 1. 双 owner：Richard / Tommy 同库共存、UI 切换

之前数据库只装一个人的网络。现在：

- 新增 `owner` / `person_owner` 表，每个 person 可挂在某 owner 名下。
- `Me` 节点不再唯一，每个 owner 各有一个；`relationship.owner_id` 把 peer 边也按 owner 切片，避免 Richard 的「中泰证券同事」和 Tommy 的「中泰证券同事」自动串到一起。
- CLI：`lodestar owner add richard --display "Richard Teng"` / `lodestar owner add tommy --display "Tommy Song"`，导入时 `--owner` 指定归属。
- 前端：顶栏 owner 切换 tab，graph / search / detail 全部按当前 owner 隔离请求。
- 例子表按 owner 重命名：`pyq.xlsx → richard_network.xlsx`、`contacts.xlsx → tommy_network.xlsx`，CLI preset 同步：`finance → richard`（保留旧名兼容），新增 `tommy`。

> 设计上 **没有主次之分**：Richard 和 Tommy 是两张平行网络，不是主从关系。

---

### 2. LLM L1 抽取：把 free text 字段变成结构化属性

之前 `bio` / `notes` / `tags` 都是非结构化文本，搜索只能靠 vector + keyword 模糊命中。今天接通 LLM 把它们抽成 4 个**叠加型**结构化字段：

| 字段 | 抽取项 | 用途 |
|---|---|---|
| `companies[]` | 当前/曾任职机构正式名 | peer 边补全的输入 |
| `cities[]` | 城市级地理位置（去掉「中国」「华东」这类粗粒度词） | 城市筛选 |
| `titles[]` | 职位/角色标签（去公司前缀） | 角色检索 |
| `tags[]` | 检索性语义标签（私募 FOF / 港股 IPO / 长期共赢…） | 标签匹配 |

工程要点：

- **附加而不覆盖**：LLM 输出只 *append* 到现有列表，绝不删原值。
- **配置**：阿里云百炼 Qwen-plus，OpenAI 兼容协议，沿用既有 `LODESTAR_LLM_*` env。
- **CLI**：`lodestar enrich --owner X [--limit N] [--only-missing/--all] [--dry-run/--apply] [--show 10]`，dry-run 打表格列出每行新增。
- **provenance**：新增 `relationship.source` 字段（`manual` / `colleague_inferred` / `ai_inferred`），下面 §6 详细说。

#### 当前数据覆盖（`enrich --apply` 跑完后）

| Owner | 联系人 | 有 companies | 有 cities | 平均 tag 数 |
|---|---:|---:|---:|---:|
| Richard | 61 | 14 (23%) | 4 (6%) | 4.9 |
| Tommy   | 109 | 98 (89%) | 48 (44%) | 7.8 |

Tommy 覆盖率高是因为源 `tommy_network.xlsx` bio 字段写得饱满（"行业：金融 · 职务：淳臻基金经理 · 地域：深圳 · 背景：…"）；Richard 的 `richard_network.xlsx` bio 太稀疏（只有"行业 + 职务"），LLM 实际能抽出公司的有限。**这给了对填表人最直接的反馈**：bio 字段写得越饱满，AI 越能帮你省手工填后面的列，详见 §模板更新章节。

---

### 3. 公司名脱敏：把云端 LLM 能看到的私密度再降一档

L1 上线后，发现还有个泄漏点 —— `person.companies` 数组（同事公司分布）会以明文发到云端。在 KYC 和投融资人脉里这本身就是敏感信息。今天做了一次升级。

**原本只脱敏人名**：`P000` = 我，`P001/P002…` = 在表里的人，free text 里的所有名字 longest-first 替换。

**现在追加公司脱敏**：

| 数据 | 脱敏方式 |
|---|---|
| `person.companies` 已结构化的公司 | `Cxxx` token |
| bio / notes / tags 里的"已知公司"提及 | 以 longest-first 替换为 `Cxxx` |
| bio / notes 里第一次出现的"新公司" | 仍明文（L1 任务的目的就是抽它） |
| LLM 输出 `companies[]` 是 `Cxxx` | 本地反映射回真名入库 |
| LLM 输出未知 `Cxxx`（幻觉） | 直接 drop，绝不还原 |

**关键不变量**：

- token id 在同一进程内**确定**：`国信证券` 永远拿到同一个 `Cxxx`（用 `(-len(name), name)` 双重排序锁定）；
- **嵌套不串**：`国信证券深圳分公司`（C002）优先匹配，再匹配 `国信证券`（C003），不会出现"`C003`深圳分公司"这种半截替换；
- **是收敛过程**：第一次跑时新公司明文，落库后第二次跑就是 `Cxxx` 了 —— 长跑下来云端能看到的公司数会渐近于 0。

**实测一条 payload**（严明的真实记录，已脱敏）：

```
{
  "person_token": "P001",
  "raw_text_fields": {
    "bio": "行业：金融 · 职务：C022合伙人 · 背景：长居香港，紫光最大个人LP，股权投资+港股IPO · …",
    ...
  },
  "already_known": {
    "companies": ["C022"],   ← 不是「九方智投」
    "cities":    ["香港"],
    ...
  }
}
```

LLM 收到 `C022`，输出仍是 `C022`，本地反映射回 `九方智投` 入库。

---

### 4. 前端 3 个 AI 解析入口

光有 CLI 不够，得让用户在录入时就能用上。三处入口：

| 入口 | 触发 | 行为 |
|---|---|---|
| **A. 新增联系人对话框** | "AI 解析" 按钮 | 调 `POST /api/enrich/preview`，把 bio + notes 抽出 chip 回填到表单，用户可改可弃 |
| **B. 联系人详情页** | "AI 重新解析" 按钮 | 调 `POST /api/enrich/person/{pid}`，对当前人重跑 L1，结果直接 patch 进数据库并刷新面板 |
| **C. 顶栏批量** | "AI 解析" → "批量 AI 解析" 模态框 | 调 `POST /api/enrich/owner` 起后台 job，前端轮询 `/api/enrich/status/{task_id}` 显示进度条 |

后端 `web/enrich_jobs.py` 是个 in-process 简易 job registry：`threading.Thread` + per-thread SQLite 连接，同 owner 同时只允许一个 job 跑。

---

### 5. `infer-colleagues` 子命令：把 LLM 抽出的公司**物化**成 peer 边

L1 enrich 跑完之后还需要走最后一步 —— 把"两个人在同一家公司"这件事真正落成一条 `colleague_inferred` 边。原本 import 时跑过一次，但当时 `companies` 还稀疏，等于没跑。今天抽成独立可重跑的子命令：

```bash
uv run lodestar infer-colleagues --owner tommy --dry-run     # 看会加多少
uv run lodestar infer-colleagues --owner tommy --apply       # 真的写
```

idempotent：repeat run 不会重复加边、不会降级已有 manual 边（见 §6）。

#### 实际效果

| Owner | 跑前 peer 边 | 跑后 peer 边 | 增量 | 最大 clique |
|---|---:|---:|---:|---|
| Richard | 0 | 3 | +3 | 中泰证券 / 平安证券 / 东吴证券 各 2 人 |
| **Tommy** | **0** | **62** | **+62** | 中金财富 9 人（36 条边）/ 海通 4 人 / 国泰君安 3 人 / … |

Tommy 关系图从纯星型变成「我连 109 人 + 17 簇同事网」。

> **顺手发现的数据规范化问题**：LLM 把"国泰君安"和"国泰海通"（合并改名后是同一家）抽成两个独立公司，所以这些人没在一个 clique 里。✅ 当天后半场补了 §7 的 `normalize-companies` 解决这个。

---

### 6. 关系来源的 provenance 优先级链

新增 `relationship.source` 字段，三档：

| source | 含义 | 优先级 |
|---|---|---:|
| `manual` | 用户/CSV/Excel 直接录入 | 2 |
| `colleague_inferred` | 同公司自动连边（包括 import 和 `infer-colleagues`） | 1 |
| `ai_inferred` | LLM L2 关系抽取（schema 已留位，逻辑待做） | 0 |

`Repository.add_relationship` 一次写入按这个优先级判断：**低 prio 永远不覆盖高 prio，同 prio 允许 idempotent 重写**。具体保证：

- `infer-colleagues` 重跑不会把"Alice→Bob 朋友 5★"降级成"同事 4★"
- 未来 L2 抽出的 `ai_inferred` 边不会覆盖任何手动/同事边
- 但 `colleague_inferred` 可以覆盖 `ai_inferred`（同事关系比 AI 推测更可信）

---

### 7. `normalize-companies` 子命令：解决 alias / 合并改名问题

§5 跑完后注意到 Tommy 的图里**国泰君安 / 国泰海通 / 国君 / 海通证券**各占一个簇 —— 实际上 2024 年这两家就合并成了「国泰海通证券」。这种 alias 问题不解决，同事边永远连不上。今天补了 `normalize-companies` 子命令，三档别名来源**叠加**：

| 来源 | 怎么用 | 何时用 |
|---|---|---|
| **`--builtin`**（默认开） | 内置一份高确定性中国金融机构合并改名表（国泰海通 / 申万宏源 / 中金） | 想要"开箱即用"的最小修复 |
| **`--alias-file FILE`** | 用户维护一份 JSON/YAML alias 文件 | 内部代号 / 公司主数据库 / 从 LLM 输出里精挑过的安全合并 |
| **`--use-llm`** | 把残余公司名 dump 给 Qwen 做聚类 | 大批量 / 你不知道有什么 alias 时；**LLM 输出必须 dry-run review** |

工程要点：

- **同 prio 时 file > builtin > LLM**，保证用户显式覆盖永远赢
- 默认 `--dry-run`，apply 之前看一张表："canonical | aliases(headcount) | 总人数 | 来源"
- DB 层：`Repository.merge_companies` 把 `person_company.company_id` 重定向到 canonical 行，`INSERT OR IGNORE` 处理"某人原本同时挂了两家"的去重，再删空 alias 行
- 如果 canonical 名字在 db 里还不存在，就 rename 第一个 alias 行（保住其他表的 FK 引用）

#### 实测效果（Tommy）

| 阶段 | companies 数 | 含 ≥2 人的簇 | colleague_inferred 边 |
|---|---:|---:|---:|
| §5 跑完后 | 88 | 14 | 62 |
| **+ builtin merge**（5 alias → 国泰海通证券） | 84 | 14 | — |
| **+ user file**（艾克朗科 / 国泰君安资管） | 82 | 15 | — |
| **+ infer-colleagues 重跑** | 82 | **15** | **79** |

净增 **+17 条** colleague_inferred 边，最大功臣是国泰海通簇：8 人、28 条边、原本 5 个孤岛。

#### LLM 错合案例（一手记录）

`--use-llm` dry-run 输出里有这么一组：

```
恒德律师事务所 ← 德恒律所(1), 恒德(1)    [llm]
```

**这是错的**：北京德恒律所（1993 成立、A股上市公司常用所）和"恒德"是不同的两家所，名字看着像而已。Prompt 已经写了"少合远好于错合"，但 LLM 还是会偶尔翻车。所以 LLM 输出**必须**先 dry-run、人工挑出可信组、写到 `--alias-file` 里再 apply。这正是上面"用户文件 > LLM"优先级设计的来由。

---

## 二、其他配套小改

| 改动 | 说明 |
|---|---|
| **bio KV 渲染** | 详情页的 bio 如果是 `key：value · key：value` 这种 KV 串，前端切成 `<dl>` 两列网格：key 加粗 + muted 色，value 主色，自动按最长 key 对齐。否则原样显示。 |
| **owner 数据隔离** | 各 owner 自己的 `me` person、自己的 graph 子图、自己的 PathFinder。`build_owner_anonymizer` 的 anonymizer 也是 owner 范围。 |
| **example 文件改名** | `pyq.xlsx → richard_network.xlsx`、`contacts.xlsx → tommy_network.xlsx`；CLI preset：`richard` / `tommy` / `extended`，`finance` 仍可作为兼容别名。 |
| **「目标」→「未联系」全仓统一** | 关系类型档位之前叫"目标"，会让人误以为它就是搜索语义上的"目标人物"。其实搜索本身完全不偏向它（PathFinder 早就不再把 `is_wishlist` 当 ranking 信号），它只决定**要不要建 Me 边**——也就是"我有没有直接联系到这个人"。今天把这个档位重命名为「未联系」，UI / 模板 / 文档全部对齐。importer **明确不向后兼容**：旧值（"目标" / "target" / "想认识" / "陌生" / "未认识"）不再被识别为 wishlist，会按默认的 `direct` 处理，并加了回归测试守住这条线。这样填表人和提示词里都不会再有"目标 = 想要找的人"的歧义。 |

---

## 三、当前能力清单（截至今日）

- [x] 自然语言 query → top-N 候选 + 路径 + 理由
- [x] 多跳引荐：从「我」到任何候选人的最短/最强路径，并按 path_kind 分桶（direct / weak / indirect）
- [x] **双 owner 平行网络** ⭐今日
- [x] **LLM L1：从 bio/notes 自动抽公司/城市/职务/标签** ⭐今日
- [x] **LLM 调用全程脱敏：人名 + 已知公司均以 token 形式发出** ⭐今日
- [x] **同公司自动建 peer 边（可独立重跑）** ⭐今日
- [x] **关系来源的 provenance 保护链** ⭐今日
- [x] **公司名归一化（builtin + 用户 alias 文件 + 可选 LLM 聚类）** ⭐今日
- [x] 前端 3 个 AI 入口（新增预览 / 详情重解析 / 批量后台 job）⭐今日
- [x] Excel/CSV 批量导入、人-人关系建边
- [x] 三类 Me 边声明（直接/弱认识/未联系）支持多跳引荐 UI
- [x] 图谱可视化、行业/强度筛选、节点详情抽屉

---

## 四、下一步建议（按价值排序）

1. **L2 关系抽取** —— 从 bio/notes 抽"P012 是 P003 的同事/朋友/客户"这类边，落 `source='ai_inferred'`。schema 全就绪，差 prompt 和评测。
2. **Richard 表的 bio 加厚** —— 当前 Richard 的 LLM 抽取只覆盖 23% 公司，根因是源 bio 太短。给 Richard 一份升级后的填表指引，鼓励把"职务"列里嵌的"XX 公司基金经理"展开到独立列。📋 **本次模板更新就是为了这个**。
3. **alias-file 沉淀**：把 `--use-llm` dry-run 里挑出来的公司合并组写到 `examples/aliases.json` 入库，下次 import 链上自动跑。
4. **引荐话术生成** —— 路径找出来后再调一次 LLM 生成「转发给中间人的微信文案」。
5. **企业实体库** —— 维护一份内部公司主库（含合并改名 / 别名），让 LLM 抽公司时直接对齐这个主库（即上面 alias-file 的"超大版本"）。

---

## 五、技术债 / 已知边界

- `normalize-companies --use-llm` 偶尔会错合（实测：把"德恒律所"和"恒德"误当同一家）。当前缓解方案是强制 dry-run + 把可信组写到 user alias 文件再 apply，工程上没法纯靠 LLM 自动化。
- `infer-colleagues` 的"clique 全连"在大公司（>20 人）下边数会爆 N²。Tommy 当前最大簇 9 人没问题；将来若某家公司接入 100+ 人需要加 `--max-clique` 参数。
- `enrich` 的 batch job 是 in-process threading，进程重启 job 状态会丢。要持久化得换 Celery / RQ 之类。
- 公司脱敏只能保护**已经结构化在 `person.companies` 里的公司**；bio 文本里第一次出现的新公司必须明文给 LLM —— 这是收敛过程，但首跑时仍有暴露面。

---

## 六、演示入口

```bash
# 启动
uv run lodestar serve              # http://127.0.0.1:8765/

# 切换 owner（前端顶栏 tab）
#   Richard Teng — 61 联系人，源自 richard_network.xlsx
#   Tommy Song   — 109 联系人 + 62 同事边，源自 tommy_network.xlsx

# 跑 LLM 增量解析
uv run lodestar enrich --owner tommy --only-missing --apply

# 合并同一家公司的不同写法（国泰君安/国泰海通/国君...）
uv run lodestar normalize-companies --owner tommy --apply

# 把 LLM 抽到的公司物化成 peer 边
uv run lodestar infer-colleagues --owner tommy --apply
```

---

## 七、一句话给老板的总结

> 今天的本质改动：人脉图谱从「我作为唯一中心、所有关系都是 1-hop」的星型，升级为「双中心 + LLM 自动补出来的同事网」。Tommy 的图从 109 条边变成 188 条，多出来的 79 条都是 LLM 从 bio 自由文本里抽公司、`normalize-companies` 把"国泰君安/国泰海通/国君"这种同一家公司的不同写法合并、再跨人对齐出来的同事边——零额外手填，已经在 web 端可见。
