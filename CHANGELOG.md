# Changelog

本仓库所有面向使用者的变更记录在此。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，按日期倒序；每条尽量 1～2 句、写清用户能感知的效果。每天的**完整背景与排查过程**保存在 [`docs/raw/YYYY-MM-DD-progress-report.md`](docs/raw/)。

---

## [2026-04-22]

### Added
- 关系抽屉来源芯片与列表徽标统一为「色点 + 文案」，按 `manual` / `colleague_inferred` / `ai_inferred` 上色。
- 「AI 推断」芯片做成禁用 + TODO 占位，明确 L2（从 bio/notes 自动抽边）尚未实现，避免误以为已可用。
- 新增 [`docs/product-overview-2026-04-22.md`](docs/product-overview-2026-04-22.md) 与 `docs/imgs/` 配图：客观描述当前能力与边界，脱敏链路用 ASCII 流程图说明。

### Fixed
- 关系一句话解析里，模型常在 `context` / `rationale` 中照抄脱敏后的 `Pxxx` / `Cxxx`；现在生成提案前做反向替换，UI 与持久化字段只留真实姓名/公司。未知 token 保留原样以暴露模型幻觉。

> 详情：[`docs/raw/2026-04-22-progress-report.md`](docs/raw/2026-04-22-progress-report.md)

---

## [2026-04-21]

### Added
- **Stage-2 Reranker**：召回 → 重排两段式检索。新增 `LLMJudgeReranker`（Qwen 判官，按角色/相关性加权）与可选 `BGECrossEncoderReranker`（`pip install -e ".[rerank]"` 拉取）。开关 `LODESTAR_RERANKER=none|llm|bge`，异常自动回退 noop。
- **离线评测脚手架**：20 条 silver-standard golden queries（覆盖 role-cliff / ambiguous / longtail / one-hop），脚本 `scripts/eval_search.py` 输出 R@5 / MRR / NDCG@10 / cliff-avoid / latency；当天升级为 gold（人工 audit 全网络）。

### Changed
- `Repository.vector_search` 接受 `owner_id`，在 SQL 侧 JOIN `person_owner` 过滤；修复 KNN 前 N 个全是另一 owner 时召回为空的串台 bug。CLI `find` / `viz` 加 `--owner` + `_resolve_owner` 兜底，多 owner 不传时强制报错。

### Fixed
- `LLMJudgeReranker` prompt 砍掉下游不读的 `reason` 字段，单 query 延迟从 45.3s → 17.5s（-61%），质量未退。

> 详情：[`docs/raw/2026-04-21-progress-report.md`](docs/raw/2026-04-21-progress-report.md)

---

## [2026-04-20]

### Added
- **双 owner 平行网络**：`owner` / `person_owner` 表 + `relationship.owner_id` 把图按 owner 切片，前端顶栏 tab 切换；`Me` 节点不再唯一。当前两位 owner：`richard` / `tommy`，地位平等。
- **LLM L1 结构化抽取**：从 bio/notes 追加 `companies / cities / titles / tags`（只 append 不覆盖）。CLI `lodestar enrich`、前端 3 个入口（新增预览 / 详情重解析 / 顶栏批量后台 job）。
- **公司名脱敏**：已结构化公司在请求前替换为 `Cxxx`，云端可见公司面随入库收敛；嵌套实体最长优先匹配，幻觉 `Cxxx` 直接 drop。
- **`infer-colleagues` 子命令**：把 LLM 抽到的公司物化成 `colleague_inferred` peer 边，幂等。Tommy +62 边、Richard +3 边。
- **`normalize-companies` 子命令**：合并公司别名（builtin 中国金融机构合并表 + 用户 alias 文件 + 可选 LLM 聚类）；优先级 file > builtin > LLM，强制 dry-run review。
- `relationship.source` provenance 三档（`manual` > `colleague_inferred` > `ai_inferred`），低优先级永不覆盖高优先级。

### Changed
- **关系类型术语统一**：「目标」→「未联系」全仓重命名；importer **不向后兼容**旧词。
- **关系类型最终收成单列模型**：删除「关系类型」列，模板从 13 列降到 12 列。`可信度=0` ⇒ 未联系（不建 Me 边、走多跳引荐）；`可信度=1-5` ⇒ 已联系（数字即边强度）。事实/强度/意图三个维度彻底解耦。
- 详情页 bio 的 `key：value` 串渲染为 `<dl>` 两列对齐。
- 例子表按 owner 改名：`pyq.xlsx → richard_network.xlsx`、`contacts.xlsx → tommy_network.xlsx`。

> 详情：[`docs/raw/2026-04-20-progress-report.md`](docs/raw/2026-04-20-progress-report.md)

---

## [2026-04-17]

### Added
- **MVP 演示链路打通**：`uv run lodestar serve` 一条命令拉起 SQLite + sqlite-vec + LLM/Embedding（阿里云百炼），单页前端含力导向图谱、搜索、详情、统计。
- **Excel 人-人建边**：模板新增 `认识` 列（语法 `张三(4,大学同学); 李四(老朋友)`），加 `公司` / `城市` 列；同公司自动补强度 4 同事边；可选 `关系` sheet 覆盖。重复导入幂等。
- **多跳引荐**：`关系类型` 列的「未联系」档位**不建 Me 边**，候选只能经引荐抵达；搜索结果按 `path_kind` 分桶（direct / weak / indirect），前端胶囊链高亮 `我 → 中间人 → 候选`。
- Demo 数据 `examples/demo_network.xlsx`（36 人含 7 名「未联系」名人 / 124 条边）+ `examples/template.xlsx` 12 列空白模板。

### Changed
- **搜索四层逻辑大修**：意图解析改为「能帮我的人画像」而不是「角色本体」；不再匹配 `潜在需求` 列；关系强度从决定性因素降为 ≤10% 的 tiebreak，相关性重回主导。修复"找投资人却返回其他创业者"的核心偏差。
- 视觉重做为 Linear/Warp 风深色 obsidian 主题；移除所有 emoji；撮合不再用爱心图标。

> 详情：[`docs/raw/2026-04-17-progress-report.md`](docs/raw/2026-04-17-progress-report.md)
