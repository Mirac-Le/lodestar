# 2026-04-22 · 关系解析反脱敏、关系来源 UI、产品能力说明

> 进度报告 / 交接笔记。本轮补齐「脱敏上云 → 回写明文」链路在**关系一句话解析**上的缺口；关系抽屉里把来源展示和「AI 推断 L2」预期说清楚；另有一份**客观能力说明**（非汇报体）+ 配图入库，方便对外说明当前版本边界。

## 1. 为什么要做反脱敏

关系解析的 prompt 里给 LLM 看的是脱敏文本。模型在 `context`、`rationale` 里经常**原样引用**句子里的人名占位符（`Pxxx` / `Cxxx`）。若不做反向替换，用户会在抽屉里看到「P052 和 P014」这类 token，而不是真实姓名。

`Anonymizer` 侧新增 `deanonymize_text()`，在 `RelationshipParser` 组装 `ProposedEdge` 前对 `context` / `rationale` 跑一遍；映射表里没有的 token 保留不动，便于发现模型幻觉而非静默吞字。

## 2. 这一版做了什么

### Web · 关系抽屉

- 来源筛选芯片与列表里的来源徽标统一为「色点 + 文案」，并按 `manual` / `colleague_inferred` / `ai_inferred` 上色（与图例一致）。
- 可点击筛选仍只开放**已落地的** `manual` 与 `colleague_inferred`；**「AI 推断」单独做成禁用态 + TODO 说明**，避免用户误以为能从 bio 自动抽边（该能力规划为 L2）。
- 「覆盖已有边」提示按来源加 `src-*` 样式，悬停 title 用 `relationSourceLabel` 可读文案。

### 文档 · 能力说明（非进度体例）

- 新增 [`product-overview-2026-04-22.md`](product-overview-2026-04-22.md)：按模块写**已实现**功能、数据流与界面行为；脱敏段用 ASCII 流程图 + 约束列表，不堆「给老板看」式叙事。
- 截图集中在 [`imgs/`](imgs/)，正文用 `![](imgs/…)` 相对本文档解析；**仅**产品说明引用的命名 PNG 纳入 Git（`page-*` / `_tmp_*` 一类本地截屏不入库）。

### 测试

- `tests/test_relationship_parser.py` 增加 `test_parse_deanonymizes_rationale_and_context`，锁定含 token 的 rationale/context 经解析后变为明文。

## 3. 文件清单（本轮）

**新增**

- `docs/2026-04-22-progress-report.md`（本文件）
- `docs/product-overview-2026-04-22.md`
- `docs/imgs/*.png`（与产品说明引用一致的 14 张）
- `CHANGELOG.md`（根目录，**按日 progress 的索引**，见该文件表头说明）

**改动**

- `src/lodestar/enrich/anonymizer.py` — `deanonymize_text`、`_P_TOKEN_RE`
- `src/lodestar/enrich/relationship_parser.py` — context/rationale 反脱敏
- `src/lodestar/web/static/index.html` — 关系来源 UI、静态资源 `?v=` 缓存戳
- `src/lodestar/web/static/style.css` — chip / pill / swatch / disabled TODO
- `tests/test_relationship_parser.py` — 上述单测

## 4. 怎么验证

```bash
uv run pytest tests/test_relationship_parser.py -q
```

Web：`uv run lodestar serve`，打开关系抽屉，确认筛选芯片与列表徽标一致，且「AI 推断」为禁用 TODO；用会产出 rationale 的解析请求确认界面无 `P\d{3}` 泄露。

## 5. 没做 / 后续

- L2：从 bio/notes 批量推断 `ai_inferred` 边（当前芯片仅为规划占位）。
- 根目录 `CHANGELOG.md` 只作**索引导航**；**正文变更仍以本系列 `docs/YYYY-MM-DD-progress-report.md` 为准**。
