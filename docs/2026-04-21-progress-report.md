# 2026-04-21 · Stage-2 Reranker 与"角色断崖"治理

> 进度报告 / 交接笔记。本轮目标：把 search 链路从「单跑 bi-encoder 后直接出 top-K」升级为「召回 → 重排 → top-K」两段式，重点解决 bi-encoder 把"资源型/桥梁型"联系人排到"本人型"前面的**角色断崖**问题，并搭起一套可复跑的离线评测基线。

## 1. 背景：为什么要做这件事

之前观测到，类似 _「我想找量化私募老总」_ 这种 query，top-1 经常是**俞汉清**（券商研究员，跟"量化"高度共现，但本人不是私募老板），把真正的 _建国哥 / 李靖 / 纪少敏_ 这些"实控人"挤到 top-3 之后甚至 top-5 之外。

诊断结论：

- 召回层（`HybridSearch` = 向量 + 关键词 RRF 融合）能把候选大致圈出来，**但分辨不出"角色"**——bi-encoder 看 bio 文本相似度，"做量化的研究员"和"量化私募老板"在向量空间里几乎重合。
- `PathFinder` 的 `weak_me_floor` 只解决了"弱关系 Me 边乱抢路径"的问题，对**同等强度但角色不对**的人没办法。

所以本轮加了一段 Stage-2 Reranker——拿到召回的 30 个候选后，让一个比 bi-encoder 更懂语义的判官（LLM 或 cross-encoder）重新排，再截 top-K 喂给 PathFinder。

## 2. 这一版做了什么

### P0 · LLM-as-Judge Reranker（默认关，按需开）

新增模块 `src/lodestar/search/reranker.py`：

- `Reranker` 协议 + `NoopReranker`：默认实现，零开销，只是把候选原样返回。
- `LLMJudgeReranker`：**复用项目里已有的 `Anonymizer`**（人名 → `Pxxx`、公司名 → `Cxxx`），把候选 bio 脱敏后扔给 Qwen，LLM 输出每个候选的 `role ∈ {本人, 桥梁, 无关}` 与 `relevance ∈ [0,1]`，再按 `_ROLE_WEIGHTS = {本人:1.0, 桥梁:0.5, 无关:0.05}` 乘起来作为最终重排分。
- `build_reranker_from_settings()`：工厂方法，按 `LODESTAR_RERANKER` 环境变量挑实现，**任何异常（缺 key、网络失败、可选依赖未装）都静默回退到 `NoopReranker`**——保证主链路永远不被 reranker 拖死。

接入点：

- `src/lodestar/cli.py` 的 `find` / `viz` 命令；
- `src/lodestar/web/app.py` 的 `/api/search`；
- `HybridSearch.search` 增加 `recall_k` 参数（默认等于 `top_k`，调用方显式传 `settings.reranker_recall_k=30` 才会拓宽召回）。

### P1 · 评测脚手架（silver-standard，AI 自构建）

用户明确表态不愿手填金标，所以这一轮的"金标"是 **silver standard**：

- `tests/fixtures/golden_queries.yaml` 共 **20 条** query，按 owner（richard / tommy）× 类别（role-cliff / ambiguous / longtail / one-hop）覆盖。
- 每条 query 显式列出 `expected_top3` 与 `must_not_include`，**判定依据是 person 的 bio / tags / companies 这些客观事实，不是 LLM 的语义判分**——这样评测和被测系统之间就不形成循环依赖。
- `scripts/build_silver_golden.py`：**只读**的校验 + 渲染脚本。会校验 yaml 里所有人名都真实存在于 DB，再生成 `docs/golden_queries_review.md` 让你逐条人工 review，发现明显错的可以直接改 yaml。
- `scripts/eval_search.py`：跑评测的入口。一次 invocation 可以并跑多个 reranker 变体，输出：
  - `docs/eval_<DATE>.md`：人读版，含整体表 + 分类表 + per-query 明细；
  - `docs/eval_<DATE>.json`：机读版，方便后续做 trend。
  
  指标：Recall@5、MRR、NDCG@10、**`cliff_avoidance_rate`**（must_not_include 不出现在 top-5 的比例，专门测断崖）、平均延迟。

### P2 · BGE Cross-Encoder Reranker（可选模块）

新增 `src/lodestar/search/bge_reranker.py`，包了 `FlagEmbedding.FlagReranker` (`BAAI/bge-reranker-v2-m3`)：

- 完全 lazy-import：`FlagEmbedding` / `torch` 没装时，`build_reranker_from_settings("bge")` 会失败回退到 noop，不会污染主依赖链。
- 走 `pyproject.toml` 的可选 extras：`uv pip install -e ".[rerank]"` 才会拉 torch (~1GB) + 模型 (~560MB)。
- 国内拉模型建议同时 `export HF_ENDPOINT=https://hf-mirror.com`，已写进 `.env.example`。

**这一轮没跑 BGE 评测**——torch 太大，验证 `LLMJudgeReranker` 已经看到明显收益就先 ship 模块本身，BGE 留给下一轮看是否值得装。

### 顺手收掉的一个 owner 串台 bug

之前 `Repository.vector_search` 不接受 `owner_id`，`HybridSearch._vector_ranks` 是「召回 N 个，再用 `list_owner_person_ids` 做 Python 侧 `in` 过滤」，**意味着如果 sqlite-vec KNN 里前 N 个全是另一个 owner 的人，过滤完就剩 0**。改成在 SQL 侧 JOIN `person_owner` 直接过滤，并取 `limit*4` 作为 KNN 池子（让 owner 过滤后还能保持 `limit` 数量）。CLI 的 `find` / `viz` 也补了 `--owner` 参数 + `_resolve_owner` 兜底（多 owner 时不传强制报错，避免静默跑错网络）。

新增 `tests/test_hybrid.py` 的 owner 隔离两个 case 守这条线。

## 3. 评测结果（2026-04-21 一次跑）

20 条 silver-standard query，top_k=5，recall_k=30：

| variant | Recall@5 | MRR | NDCG@10 | cliff-avoid | avg-latency |
|---|---:|---:|---:|---:|---:|
| `none` (baseline) | 0.625 | 0.568 | 0.530 | 0.850 | 266 ms |
| `llm` (Qwen judge) | 0.633 | **0.700** | **0.609** | **0.900** | 22 419 ms |

亮点 / 折扣：

- **MRR +0.132、NDCG@10 +0.079**——LLM 判官把"对的人"往前推确实有效；
- **cliff-avoid 0.85 → 0.90**：role-cliff 类下从 0.500 → 0.667，最关键的 `r-rolecliff-1`（"我想找量化私募老总"）top-5 里**俞汉清被拿掉**了，建国哥 / 纪少敏被推上去；
- **延迟 80×**：266ms → 22.4s，瓶颈在云端 Qwen 串行调用——目前每条 query 一次 LLM call 看全 30 个候选，prompt 偏长。性能优化路径见下面 §5。
- **召回为空的 4 条**（`r-ambig-1` / `r-onehop-1` / `t-longt-1` / `t-onehop-2`）跟 reranker 无关，是 `GoalParser` 解出来的 keywords 跟现有 bio 词面对不上——属于召回侧问题，留作下一轮课题。

详细 per-query 明细见 `docs/eval_2026-04-21.md`。

## 4. 文件清单（这一轮新增 / 改动）

新增：

- `src/lodestar/search/reranker.py`
- `src/lodestar/search/bge_reranker.py`
- `tests/test_reranker.py`（7 通过 + 1 BGE skip）
- `tests/fixtures/golden_queries.yaml`
- `scripts/build_silver_golden.py`
- `scripts/eval_search.py`
- `docs/golden_queries_review.md`
- `docs/eval_2026-04-21.md` / `.json`
- `docs/2026-04-21-progress-report.md`（本文件）

改动：

- `src/lodestar/db/repository.py` — `vector_search` 加 `owner_id` 参数（SQL 侧过滤）。
- `src/lodestar/search/hybrid.py` — 用 `vector_search(owner_id=…)`；`search` 加 `recall_k`。
- `src/lodestar/search/__init__.py` — re-export reranker 公共 API。
- `src/lodestar/config.py` — 加 `reranker` / `reranker_recall_k`。
- `src/lodestar/cli.py` — `_resolve_owner` 兜底 + `find` / `viz` 接 `--owner` 与 reranker。
- `src/lodestar/web/app.py` — `/api/search` 接 reranker。
- `tests/test_hybrid.py` — 加 owner 隔离两个 case。
- `.env.example` — 写明 `LODESTAR_RERANKER` / `LODESTAR_RERANKER_RECALL_K` / `HF_ENDPOINT` 的取值与含义。
- `pyproject.toml` — 加 `[project.optional-dependencies].rerank`。
- `uv.lock` — 同步。

回归：`uv run pytest` 全过（63 passed, 1 skipped），无 regression。

## 5. 没做 / 留给下一轮

按性价比从高到低：

1. **silver → gold 升级**。`tests/fixtures/golden_queries.yaml` 顶部已写明升级路径；现在的 `expected_top3` / `must_not_include` 是基于 bio 字面规则推出来的，建议本人逐条 review `docs/golden_queries_review.md` 之后改成手填的 gold，未来评测才能真正反映"人脑认为应该谁排第一"。
2. **LLM judge 的延迟优化**。当前 22s/query 主要是云端 Qwen 串行 + prompt 偏长。三个可压方向：
   - 候选数从 30 砍到 15（看下 NDCG 损失多少）；
   - 把 prompt 换成 batch 模式（一次 call 把全部候选打包）——已是；当前其实就是一次 call，瓶颈在 token 数；
   - 上 BGE 本地 cross-encoder（`LODESTAR_RERANKER=bge`），预期延迟 < 1s，但需要装 torch。建议先在 BGE 上跑同一份 silver 评测对比效果。
3. **召回侧补强**。`r-onehop-1`（"我想找能直接借钱的核心铁磁朋友"）这种 query 召回直接为空，原因是 `GoalParser` 把它解成 `["核心铁磁","朋友"]`，bio 里没有这种词。下一轮可以：
   - 把 `relationship.strength` / `frequency` 等结构化字段也喂给 reranker / GoalParser，让"借钱"这类需求能映射到"strength≥4 且 frequency=高频"；
   - GoalParser 加同义词扩展（铁磁 → 老朋友 / 死党 / 强信任）。
4. **CHANGELOG**。仓库目前没有 CHANGELOG.md，本轮没新建。如果未来要建议从这一轮起步，把 stage-2 reranker 作为首条 entry。

## 6. 怎么用 / 怎么验证

```bash
# 默认（不重排，跟上一版完全一样）
uv run lodestar find "我想找量化私募老总" --owner richard

# 开 LLM Judge（需要 LODESTAR_LLM_API_KEY 已配）
LODESTAR_RERANKER=llm uv run lodestar find "我想找量化私募老总" --owner richard

# 开 BGE 本地 cross-encoder（需要先装可选依赖）
uv pip install -e ".[rerank]"
LODESTAR_RERANKER=bge uv run lodestar find "..." --owner richard

# 重新跑离线评测
uv run python scripts/eval_search.py --variants none llm

# silver 标的人工 review
uv run python scripts/build_silver_golden.py
# → 看 docs/golden_queries_review.md
```

Web 端走同一条链路，重启 `uv run lodestar serve` 即可生效；前端无任何改动。
