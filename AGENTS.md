## Learned User Preferences

- Explain and document in Simplified Chinese unless the user asks otherwise.
- Prefer a restrained, premium dark UI (Linear-style cues) over harsh neon or flashy “tech” chrome; lean on `awesome-design-md` for token-level reference; keep body type large enough to read comfortably and use size, weight, and muted color for hierarchy instead of high-luminance accents. Treat the top bar and the bottom-left path panel as **information-primary** surfaces: type can be stepped up there so names, chains, and actions read at a glance without shouting with color.
- Primary actions (especially search / goal query) must never feel silent: show loading, success, empty, or error feedback so users know what happened.
- Avoid heart icons (or similar) for matchmaking or tie strength in a professional contact graph; they read as intimate rather than networking.
- Default graph presentation should stay visually quiet (muted baseline); after the user searches or focuses on a goal, emphasize the relevant path(s) and trim redundant on-screen information. 悬停中心「我」时 Me 边按 `weak_me_floor` 区分强高亮与弱淡化，避免弱关系在视觉上与核心边同等「满屏放射」。
- The primary workflow expectation is: state a goal in natural language (for example wanting to accomplish something), get fast retrieval over the contact database, and see one or a few best target people plus the intermediate chain(s), with those path(s) clearly highlighted.
- For **path-shaped** flows (goal search, two-person path, broker introductions), reuse the same bottom-left path list pattern; **panel copy and the highlighted edges on the graph must describe the same chain**—no orphan banners or mismatched rows.
- **事实 / 意图严格分离**：Excel 表只记录可观测的关系事实（认识谁、有多熟），**不要**预定义"目标 / 想接触谁"这类意图列；意图必须留给 web 端自然语言输入由 LLM 实时解析。任何固化在表里的"目标"字段都视为设计错误。
- 数据模型或术语调整时**一次到位**：不要保留向后兼容的过渡列、别名或双写逻辑（用户原话："不要向后兼容...不要歧义"、"动数据模型一步到位"）。优先重命名 / 删列 + 同步刷新模板和文档。**导入表**若列格式不合约定（如「认识」列粘连、缺分隔），优先在 Excel/模板侧改规范，而不是为畸形单元格堆叠 importer 特例。
- 联系人详情页的 bio（行业 / 职务 / 地域 等键值串）用 CSS grid 把 **legend 与 value 分两列对齐**，legend 加粗，避免长串中点 `·` 堆成单行。
- `examples/` 下的样表 / 模板文件统一**小写英文下划线命名**（如 `richard_network.xlsx`、`tommy_network.xlsx`），不直接放中文文件名。
- 根目录 `CHANGELOG.md`、能力向页面（如 `docs/product-overview-*.md`）与 `docs/instructions.md`：用准确、自然的中文技术表述，避免机翻腔或误译专业词；不要把一次性口头需求混进 changelog 正文。

## Learned Workspace Facts

- **Lodestar**（SQLite + sqlite-vec + NetworkX，Typer CLI，FastAPI 静态 Web UI）。源码 https://github.com/Mirac-Le/lodestar。启动：`uv run lodestar serve`（默认 `127.0.0.1:8765`，局域网需 `--host 0.0.0.0`）。Web 端 SQLite 使用 `check_same_thread=False`、WAL、每请求连接，避免 FastAPI 线程池触发跨线程 `ProgrammingError`。
- 导入用样表 / 模板在 `examples/`：`richard_network.xlsx`（Richard Teng，原 `pyq.xlsx`）、`tommy_network.xlsx`（Tommy Song，原 `contacts.xlsx`）、`demo_network.xlsx`（虚构 demo）、`template.xlsx`（空模板）。
- 对外说明与归档：客观能力说明在 `docs/instructions.md` 与 `docs/product-overview-*.md`（避免主观「心路历程」）；按日完整过程 / 长文报告在 `docs/raw/YYYY-MM-DD-*.md`；用户向按日摘要在根目录 `CHANGELOG.md`（通常链到 raw）。UI 可参考仓库内 `awesome-design-md/`。
- 关系强度为**单列 `可信度` 0–5**（v3）：`0`=未联系（不建 Me 边、`Person.is_wishlist=True`、仅靠他人「认识」间接连通），`1–5`=已联系。**已废除** `关系类型` 列。`is_wishlist` 为 sticky 派生位；`PathFinder` 等**忽略** `is_wishlist`，只看拓扑。
- LLM 富化（bio / 公司·职务·城市·标签 / 公司名归一化）用阿里 DashScope（Qwen），**必须先经本地 `Anonymizer` 脱敏**（`Pxxx`/`Cxxx`），回包后再映射回原名；预览、详情「AI 重新解析」、批量富化同链路。
- 多 owner 共用库：`owner` / `person_owner`、`relationship.owner_id`；各 owner 有独立 `Me`；顶栏 tab 切换 `richard` / `tommy`。每 owner 可设网页标签密码：`uv run lodestar owner web-password <slug> [--set|--clear]`；解锁令牌仅在页内内存，切换 owner 或刷新即失效；多机建议 `.env` 中 `LODESTAR_OWNER_UNLOCK_SECRET`。
- `relationship.source`：`manual` > `colleague_inferred` > `ai_inferred`，写入时按优先级保护，避免 AI 覆盖人工。
- 路径：`Settings.weak_me_floor`（默认 4，`LODESTAR_WEAK_ME_FLOOR`）对低于 floor 的 Me 边加权惩罚；`path_kind` 按**实际选中路径**分类；必要时回退拓扑最短路径。前端路径结果只用 `indirect` 与 `contacted` 两桶（勿再引用已移除的 `direct`/`weak` state，否则 Alpine 会静默失败）。`src/lodestar/web/static/index.html` 中 `style.css` / `app.js` 用 `?v=YYYYMMDD-<tag>` cache-bust，改前端须 bump。
- Stage-2 `rerank` extra：`transformers` 钉在 `>=4.45,<5.0`（与 `FlagEmbedding`/MiniCPM 兼容）；`torch` 经 `pytorch-cpu` 索引避免拉 CUDA 大包。国内拉 HF 权重设 `HF_ENDPOINT=https://hf-mirror.com` 且 `HF_HUB_DISABLE_XET=1`。国内 PyPI 镜像应写在用户级 `~/.config/uv/uv.toml` 的 `[[index]] default = true`，勿在 `pyproject.toml` 用无效裸 `[[index]]`。
- 2026-04-21 20 条 golden hybrid 评测（见 `docs/eval_2026-04-21.md`，需已补 embedding）：`bge` 聚合略优于 `llm`，延迟更短；默认推荐 `LODESTAR_RERANKER=bge`，强语义单 query 可临时用 `llm`。`LLMJudgeReranker` **不要**让模型输出下游不消费的字段（如已删除的 `reason`），否则延迟暴涨。
- **搜索 / 评测前确认 `vec_person_bio` 非空**；否则向量路静默失效、指标偏 keyword-only。可 `uv run lodestar reembed`；`scripts/debug_zero_recall.py` 可拆三路诊断。
