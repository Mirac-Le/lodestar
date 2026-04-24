# Changelog

面向使用者的变更摘要，按日期倒序。每天的完整背景与排查过程保存在 [`docs/raw/YYYY-MM-DD-progress-report.md`](docs/raw/)。

---

## [2026-04-24]

### Added
- **WebUI 反馈系统**：topbar 新增「📝 反馈」按钮，业务同事可直接提交 Bug（必含截图 + 实际/期望）或需求（必用 When-Then 句式 + 验收 bullet）。提交时前端自动打包当前 `mount_slug` / `view_mode` / `query` / 高亮路径 / `directOverrides` 状态，外加最近 10 次 API 请求的完整 req+resp、最近 20 条前端错误、涉及联系人的 db 快照（Person + Me-edge + 1 跳邻居，跑过 PII 脱敏）。后端落 `feedback` 表 + 渲染 md 到 `docs/feedback/<slug>/FB-YYYYMMDD-NNNN.md`，md 开头带一段给 AI 的处理指引（尤其"bug/设计之争时先停下问开发"的 guardrail）。
- **PII 脱敏工具**：新增 `lodestar.privacy.scrub`，手机号保留后 4、身份证 / 银行卡全 redact、邮箱 mask 中段。主要给 feedback snapshot 用，未来可推广到其他场景。

### Changed
- `docs/feedback/` 加入 `.gitignore`（默认不入库，反馈 md 内联联系人姓名 / 关系，当作本地 artifact 管理）。
- **反馈表单 UI 对齐工具整体风格**：重写 `.modal-feedback` 的 CSS，`input / textarea / select` 统一 `display:block; width:100%`（此前 textarea 走浏览器默认 cols 宽度导致和 input 不等宽、看起来"歪"）；边框从 `--border` 换到 `--border-strong`，纯黑底上对比度明显提升；`<select>` 自绘下拉箭头贴近 `.mode-banner .pair-input` 风格；联系人补全下拉复用 `.pair-suggest-row` class，视觉与双人路径一致。
- **反馈填写指南**：新增 [`docs/feedback-guide.md`](docs/feedback-guide.md)，给业务同事的填写规约（标题 / 涉及的人 / Bug 五字段 / When-Then 句式 / 影响程度 / 截图），含好例与坏例。`docs/instructions.md` 里的简短反馈小节改为摘要 + 链接到该指南。

> 设计稿 & 实施计划：[`docs/plans/2026-04-23-feedback-system-design.md`](docs/plans/2026-04-23-feedback-system-design.md) / [`docs/plans/2026-04-23-feedback-system-plan.md`](docs/plans/2026-04-23-feedback-system-plan.md)

---

## [2026-04-23]

### Added
- **多挂载落地页**：根 URL `/` 在配置了 2 个及以上 `--mount` 时不再加载 1.8k 行 SPA shell，改为渲染轻量 picker 页（标题 + 副标题 + hero 图，Alpine.js 拉 `/api/mounts` 列出所有网络）；单挂载继续 302 跳到唯一 mount。
- **联系人档案直改**：右侧档案面板里的姓名 / 可信度 / 简介 / 标签全部支持 inline 编辑——点姓名变 input、点可信度某一档即时设到该值（限 1–5，不允许从 UI 把"已联系"打回 wishlist）、简介支持 KV 模式逐项改 + 添加 / 删除字段（适配 richard 模板 4 项与 tommy 表全部 9 项画像）、自由文本 bio 退化成 textarea；标签末尾加虚线框「＋ 标签」按钮，回车确认 / Esc 取消，鼠标悬停现有 chip 出 ✕ 移除。所有保存默认 `embed=false`，避免一次小改触发 embedding 调用；档案底部新增「刷新语义索引」按钮供用户改完一批字段后手动 reembed。后端 `UpdatePersonRequest` 新增 `name` 字段，`PersonDTO` 新增 `me_edge_id`（前端调可信度走 `PATCH /api/relationships/{rid}` 直接拿 rid，不再额外查一次 `list_relationships`）。
- **弱直连 fallback 提示**：当某 indirect 卡片对应的目标其实和"我"也有 1 跳直接边（只是 strength<`weak_me_floor` 被算法惩罚到走了引荐），卡片底部新增一条灰色提示，明示直连强度（如 "你也直接认识他（强度 3/5）"）并附「改用直连」按钮；点击就地把该行的 path / node_ids / edge_ids 替换成"我 → 目标"合成直连，同时图谱高亮跟着切换。再点「改回引荐链」无损还原，target_id 不变，列表 DOM 顺序稳定。后端 `PathResultDTO` 新增 `direct_me_strength: int | None`，仅 indirect 桶填充；contacted / wishlist 与 two-person path 不受影响。算法本身（`weak_me_floor=4`、Me-边惩罚）保持不变——既保留"先推熟人引荐"的默认值判断，也让用户随时能一眼看到并覆盖。

### Changed
- **Excel 导入合并为单一 canonical preset**：`lodestar import` 删除 `--preset` 参数；所有 `.xlsx` 共用同一套规则。列名先做 NFKC + 去空白 + alias 归一化（如 `合作价值评分（0-5）` 自动等价于 `合作价值（0-5）`），再按 CORE / PROFILE_BIO / PROFILE_TAGS 三组白名单分发；不在白名单的列丢弃，import 末尾打印 `[import] 已忽略 N 个未识别列：...`。Tommy 表多出的 6 列金融画像（可投金额 / 风险偏好 / 共赢性 / 关系阶段 / 兴趣偏好）以「字段：值 · ...」拼到 `bio`，`核心标签` 进 `tags`。
- 同步刷新 README 的 Excel 章节与 quickstart contract（**tommy.db = 110 contacts / 156 relationships**），删除所有 `--preset richard|tommy` 提示。

### Fixed
- **Tommy 网络曾"任意两人之间都没有关系"**：双 preset 时代 `tommy_contacts_preset` 漏配 `peers_column="认识"` + 列名拼错（`身份职位` vs 实际 `职务`），导致 `认识` 列被忽略 → 110 个联系人全部退化成「我」一颗星 → 前端 `hideMeEdgesAmbient` 把所有 me-edge 藏起来后看起来全空。统一 preset 后 tommy.db 重跑回归 110 Me 边 + 46 横向边。
- **改名/改简介 UI 看似没变化**：`/static` 上线 inline edit 之后部分老用户浏览器复用了旧版 `state.js`（HTML 入口虽然带了 `?v=` cache-bust，但其内 `import "./modules/state.js"` 走的是相对 URL，浏览器不会再校验），表象是模板里的姓名 input/保存按钮存在但 `saveName` 行为缺失。修法：给 `/static` mount 加 `Cache-Control: no-cache, must-revalidate`（`_NoCacheStaticFiles`）。这不是"不缓存"，而是"每次必须带 ETag 回源校验"——文件没动就 304，开销可忽略；文件一改下次访问立即拿新版，不再需要逐 module 维护 `?v=`。

### Removed
- ⚠️ BREAKING：删除 `richard_network_preset()` / `tommy_contacts_preset()` 公共导出与 CLI `--preset` 选项。外部脚本若 import 这两个名字需改为 `from lodestar.importers import default_preset`。按 AGENTS.md 数据模型一次到位原则，不保留过渡别名。

---

## [2026-04-22]

### Added
- 关系抽屉的来源筛选与列表徽标统一为「色点 + 文案」样式，按 `manual` / `colleague_inferred` / `ai_inferred` 上色。
- 「AI 推断」筛选项标为禁用并加 TODO 说明，明确「从 bio/notes 自动抽边」(L2) 尚未实现，避免误以为已可用。
- 新增 [`docs/product-overview-2026-04-22.md`](docs/product-overview-2026-04-22.md) 与 `docs/imgs/` 配图：客观描述当前能力与边界，脱敏链路改用 ASCII 流程图说明。

### Fixed
- 关系一句话解析里，模型常把脱敏后的 `Pxxx` / `Cxxx` 原样写进 `context` / `rationale`。现在生成提案前先做反向替换，界面与持久化字段只显示真实姓名/公司；未知 token 保留原样，便于暴露模型幻觉而非静默吞字。

> 详情：[`docs/raw/2026-04-22-progress-report.md`](docs/raw/2026-04-22-progress-report.md)

---

## [2026-04-21]

### Added
- **Stage-2 重排**：检索改为「召回 → 重排」两段式。新增 `LLMJudgeReranker`（Qwen 作判官，按角色/相关性加权）与可选 `BGECrossEncoderReranker`（`pip install -e ".[rerank]"` 才会拉本地模型）。环境变量 `LODESTAR_RERANKER=none|llm|bge`，任何异常自动退回到不重排，主链路不会被拖死。
- **离线评测脚手架**：20 条 golden queries（覆盖 role-cliff / ambiguous / longtail / one-hop），脚本 `scripts/eval_search.py` 输出 R@5 / MRR / NDCG@10 / cliff-avoid / 平均延迟；当天人工核对了全网络人画像，把 silver 标升级为 gold。

### Changed
- 向量召回的 owner 过滤下沉到 SQL：`Repository.vector_search` 接 `owner_id`，在 `person_owner` 上 JOIN。修掉了 KNN 前 N 个全是另一 owner 时召回结果为空的串台 bug。CLI `find` / `viz` 加 `--owner` 参数与多 owner 兜底，不传时强制报错。

### Fixed
- LLM 重排 prompt 砍掉下游不消费的 `reason` 字段，单 query 延迟从 45.3s 降到 17.5s（-61%），质量没退。

> 详情：[`docs/raw/2026-04-21-progress-report.md`](docs/raw/2026-04-21-progress-report.md)

---

## [2026-04-20]

### Added
- **双 owner 平行网络**：新增 `owner` / `person_owner` 表，`relationship.owner_id` 把图按 owner 切片；`Me` 节点不再唯一，前端顶栏 tab 切换。当前 owner：`richard` 与 `tommy`，地位平等无主次。
- **LLM 结构化抽取（L1）**：从 bio/notes 追加 `companies / cities / titles / tags`，只追加不覆盖。CLI `lodestar enrich` + 前端三个入口（新建预览 / 详情页重解析 / 顶栏批量后台任务）。
- **公司名脱敏**：已结构化的公司在请求前替换为 `Cxxx`，云端可见的公司面随入库逐步收敛；嵌套实体最长优先匹配，模型幻觉出的 `Cxxx` 直接丢弃。
- **`infer-colleagues` 子命令**：把 LLM 抽到的公司物化成 `colleague_inferred` 同事边，幂等可重跑。Tommy +62 条、Richard +3 条。
- **`normalize-companies` 子命令**：按「用户别名文件 > 内置中国金融机构合并表 > 可选 LLM 聚类」三档叠加合并公司别名；强制先 dry-run，避免 LLM 错合。
- 关系来源 `relationship.source` 引入 `manual` > `colleague_inferred` > `ai_inferred` 三档优先级，低优先级永远不覆盖高优先级。

### Changed
- **关系类型术语统一**：旧档位「目标」全仓重命名为「未联系」；importer 明确不向后兼容旧词。
- **关系档位收敛到单列模型**：删除「关系类型」列，模板从 13 列降到 12 列。`可信度=0` ⇒ 未联系（不建 Me 边、走多跳引荐）；`可信度=1-5` ⇒ 已联系（数字即边强度）。事实 / 强度 / 意图三个维度彻底解耦。
- 详情页 bio 里 `key：value` 串改用 `<dl>` 两列网格对齐。
- 示例表按 owner 改名：`pyq.xlsx → richard_network.xlsx`、`contacts.xlsx → tommy_network.xlsx`。

> 详情：[`docs/raw/2026-04-20-progress-report.md`](docs/raw/2026-04-20-progress-report.md)

---

## [2026-04-17]

### Added
- **MVP 演示链路打通**：`uv run lodestar serve` 一条命令拉起 SQLite + sqlite-vec + LLM/Embedding（阿里云百炼）；单页前端含力导向图谱、搜索、详情、统计四块。
- **Excel 人-人建边**：模板新增 `认识` 列（语法 `张三(4,大学同学); 李四(老朋友)`），并加 `公司` / `城市` 列；同公司自动补强度 4 的同事边；可选 `关系` sheet 覆盖。重复导入幂等。
- **多跳引荐**：「未联系」档位的人**不建 Me 边**，只能经别人引荐抵达；搜索结果按 `path_kind` 分桶（direct / weak / indirect），前端用胶囊状链表示 `我 → 中间人 → 候选`。
- 演示数据 `examples/demo_network.xlsx`（36 人含 7 名「未联系」名人 / 124 条边）与 `examples/template.xlsx` 12 列空白模板。

### Changed
- **搜索四层逻辑大修**：意图解析改成「能帮我的人画像」而不是「角色本体」；不再匹配 `潜在需求` 列；关系强度从决定性因素降为 ≤10% 的并列微调，相关性重回主导。修掉「找投资人却返回其他创业者」的核心偏差。
- 视觉重做为 Linear / Warp 风格的深色主题；移除所有 emoji；撮合相关 UI 不再用爱心图标。

> 详情：[`docs/raw/2026-04-17-progress-report.md`](docs/raw/2026-04-17-progress-report.md)
