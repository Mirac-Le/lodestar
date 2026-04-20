## Learned User Preferences

- Explain and document in Simplified Chinese unless the user asks otherwise.
- Prefer a restrained, premium dark UI (Linear-style cues) over harsh neon or flashy “tech” chrome; lean on `awesome-design-md` for token-level reference; keep body type large enough to read comfortably and use size, weight, and muted color for hierarchy instead of high-luminance accents.
- Treat the top bar and the bottom-left path panel as **information-primary** surfaces: type can be stepped up there so names, chains, and actions read at a glance without shouting with color.
- Primary actions (especially search / goal query) must never feel silent: show loading, success, empty, or error feedback so users know what happened.
- Avoid heart icons (or similar) for matchmaking or tie strength in a professional contact graph; they read as intimate rather than networking.
- Default graph presentation should stay visually quiet (muted baseline); after the user searches or focuses on a goal, emphasize the relevant path(s) and trim redundant on-screen information.
- The primary workflow expectation is: state a goal in natural language (for example wanting to accomplish something), get fast retrieval over the contact database, and see one or a few best target people plus the intermediate chain(s), with those path(s) clearly highlighted.
- For **path-shaped** flows (goal search, two-person path, broker introductions), reuse the same bottom-left path list pattern; **panel copy and the highlighted edges on the graph must describe the same chain**—no orphan banners or mismatched rows.
- **事实 / 意图严格分离**：Excel 表只记录可观测的关系事实（认识谁、有多熟），**不要**预定义"目标 / 想接触谁"这类意图列；意图必须留给 web 端自然语言输入由 LLM 实时解析。任何固化在表里的"目标"字段都视为设计错误。
- 数据模型或术语调整时**一次到位**：不要保留向后兼容的过渡列、别名或双写逻辑（用户原话："不要向后兼容...不要歧义"、"动数据模型一步到位"）。优先重命名 / 删列 + 同步刷新模板和文档。
- 联系人详情页的 bio（行业 / 职务 / 地域 等键值串）用 CSS grid 把 **legend 与 value 分两列对齐**，legend 加粗，避免长串中点 `·` 堆成单行。
- `examples/` 下的样表 / 模板文件统一**小写英文下划线命名**（如 `richard_network.xlsx`、`tommy_network.xlsx`），不直接放中文文件名。

## Learned Workspace Facts

- This repo is Lodestar: a personal network navigator built on SQLite, sqlite-vec, and NetworkX, with a Typer CLI and a FastAPI static web UI.
- Public source: https://github.com/Mirac-Le/lodestar
- Run the web UI with `uv run lodestar serve` (defaults to `127.0.0.1:8765`; use `--host 0.0.0.0` when others on the LAN need access).
- SQLite connections for the web app use `check_same_thread=False` with WAL and per-request connections so FastAPI’s thread pool does not trip `ProgrammingError` across threads.
- Sample and template spreadsheets for imports live under `examples/`：
  - `richard_network.xlsx`（owner `richard` / Richard Teng，原 `pyq.xlsx`）
  - `tommy_network.xlsx`（owner `tommy` / Tommy Song，原 `contacts.xlsx`，16 列机构合作画像表）
  - `demo_network.xlsx`（自带 demo 网络，36 个虚构联系人）
  - `template.xlsx`（发给同事填写的空模板）
- Stakeholder-facing, objective capability overview (no subjective “心路历程”) lives in `docs/instructions.md`; dated narrative notes stay in `docs/` progress-style files.
- The tree includes `awesome-design-md/` as an in-repo library of design-system references for UI work.
- 关系强度采用**单列 `可信度` 0-5 模型**（v3 设计）：`0` = 未联系（importer 不建 Me 边、`Person.is_wishlist=True`、仅靠 peers 的"认识"间接连通），`1-5` = 已联系（建 Me 边，1=点头之交 / 旧版"弱认识"、3=普通朋友、5=核心铁磁）。**已废除** `关系类型` 列；importer 对历史表里的该列静默忽略，`可信度` 是唯一事实源。
- `Person.is_wishlist` 是"未联系"状态在 DB / API 层的**持久化派生位**：导入时由 `可信度==0` 写入并带 sticky 语义（一旦 wishlist，再次导入非 0 强度也不自动翻转），UI 可独立 toggle；`PathFinder` 等图算法**完全忽略** `is_wishlist`，只看图拓扑，避免把意图泄漏进路径排序。
- LLM 富化（解析 bio / 抽公司·职务·城市·标签 / 公司名归一化）走云端阿里 DashScope（Qwen 系），**调用前必须本地脱敏**：人名 → `Pxxx`、已知公司名 → `Cxxx`（最长实体优先、稳定 token 分配），云端只看 token，回包后本地映射回原名。新增联系人预览、详情页"AI 重新解析"、批量富化都共用这条 `Anonymizer` 链路。
- 多 owner 共用一个 SQLite 库：通过 `owner` / `person_owner` 表 + `relationship.owner_id` 做网络隔离；`person.is_me` 不再唯一，每个 owner 各有自己的 `Me` 节点。当前两位 owner 是 `richard`（Richard Teng，源 `richard_network.xlsx`）和 `tommy`（Tommy Song，源 `tommy_network.xlsx`），地位平等无主次，前端用顶栏 tab 切换。
- `relationship.source` 字段记录边的 provenance，优先级 `manual` > `colleague_inferred` > `ai_inferred`：repository 层在写入 / 升级边时按此优先级保护，避免 AI 推断覆盖人工录入。
