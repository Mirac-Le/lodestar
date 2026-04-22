# Lodestar

> *"A star that guides or serves to guide."*

Your personal network navigator. You tell it what you want to do — **"I want to raise a seed round from an AI-focused investor"** — and it searches your contacts, traces the connections, and ranks the best paths for you to take.

Built around three boring-but-reliable pieces:

- **SQLite** — one file per owner, your data, forever（**一人一库**：`richard.db`、`tommy.db` 各自独立，OS 文件权限就是 ACL）。
- **[sqlite-vec](https://github.com/asg017/sqlite-vec)** — semantic search inside the same file.
- **[NetworkX](https://networkx.org/)** — path-finding on an in-memory graph, built from the DB on demand.

Embeddings and goal parsing 走任何 OpenAI 兼容 endpoint（阿里云百炼 / DashScope、OpenAI、DeepSeek、智谱、Kimi …）。中文场景推荐百炼。

Web 端用 FastAPI + Alpine.js + Cytoscape，多个网络通过 `serve --mount slug=path` 同进程挂在 `/r/<slug>/` 子路由下，每个 mount 可单独设网页密码，**切 tab 必重新解锁**。

---

## Quick start

下面这套命令在干净环境里端到端跑通：两个网络（Richard + Tommy）各自一个
SQLite 文件，CLI 用全局 `--db <path>` 切库，Web 端用 `serve --mount slug=path`
把它们挂在同一个端口的不同子路由下。

```bash
git clone <repo> lodestar
cd lodestar
cp .env.example .env        # 填入 LLM / embedding API key
uv sync                     # 安装依赖到 .venv

# 1) 建第一个网络（每个 db 文件 = 一位 owner，person.is_me UNIQUE 约束）
uv run lodestar --db ./richard.db init --name "Richard Teng"

# 2) 导入 Richard 的联系人；--embed 务必带，否则 vec_person_bio 为空、
#    后续 hybrid 检索的向量通道会静默失效（详见 AGENTS.md）。
uv run lodestar --db ./richard.db import \
    examples/richard_network.xlsx --preset richard --embed

# 3) 同样建好 Tommy 的网络
uv run lodestar --db ./tommy.db init --name "Tommy Song"
uv run lodestar --db ./tommy.db import \
    examples/tommy_network.xlsx --preset tommy --embed

# 4) 给两个网络各设一个 web 密码（可选；不设就是无锁）
uv run lodestar --db ./richard.db web-password --set 'r-secret'
uv run lodestar --db ./tommy.db   web-password --set 't-secret'

# 5) 单库 CLI 检索：--db 指定哪本库就在哪本库里查
uv run lodestar --db ./richard.db find "量化私募" --top 5

# 6) 起 Web UI：把两个 db 同时挂在 8765 端口
uv run lodestar serve \
    --mount richard=./richard.db \
    --mount tommy=./tommy.db \
    --host 0.0.0.0
# → http://<host>:8765/r/richard/  (输入 r-secret)
# → http://<host>:8765/r/tommy/    (输入 t-secret)
# 顶栏切 tab = 浏览器整页 reload，必重新输入对方密码。
```

只用一个网络时可以省掉 `--mount`：

```bash
# CLI 默认 db 路径 = LODESTAR_DB_PATH 或 ~/.local/share/lodestar/lodestar.db
uv run lodestar init --name "Me"
uv run lodestar import examples/demo_network.xlsx --preset richard --embed
uv run lodestar serve              # 自动以默认 db 单挂在 /r/me/，根 URL 自动跳转
```

> **Demo data 可复现**：仓库里的 `examples/*.xlsx` 是真实表的事实源（已脱敏），
> `*.db` 全部进 `.gitignore`，不入仓。任何时候想重建演示库，跑一遍上面 1-3 步
> 即可在干净环境重产出 **richard.db = 61 contacts / 142 relationships** 和
> **tommy.db = 110 contacts / 110 relationships**——这就是 quickstart 的契约。

**Bi-directional matching**: every contact has a `needs` list. So you can
also search for *"who would benefit from what I have?"* — if someone's
`needs` field says `客户`, they will surface when you query `客户`.

---

## How the search works

```
"我想做X"
    │
    ▼
┌───────────────────┐
│   LLM goal parser │  →  { keywords, skills, industries, roles, cities }
└───────────────────┘
    │
    ▼
┌───────────────────┐   ┌──────────────────────────┐
│ vector search     │   │ keyword match over        │
│ (embedding <-> bio)│  │ tags/skills/companies/etc.│
└──────┬────────────┘   └──────────┬───────────────┘
       │    Reciprocal Rank Fusion │
       └────────────┬──────────────┘
                    ▼
         candidate people (top-K)
                    │
                    ▼
       for each candidate:
       NetworkX shortest-path(me → candidate)
       weight = 1 / relationship_strength
                    │
                    ▼
       combined_score = relevance × (1 + path_strength/5) / hops
                    │
                    ▼
              ranked results
```

- If you don't want LLM parsing (e.g. offline, no API key), use `--no-llm`.
- If embeddings are not configured, vector search is skipped gracefully and keyword matching does the work.

---

## Commands

所有 CLI 子命令都接受全局 `--db <path>`（也可用 `LODESTAR_DB_PATH` 环境变量），
省略时落到 `~/.local/share/lodestar/lodestar.db`。

| Command                                    | What it does                                                          |
|--------------------------------------------|-----------------------------------------------------------------------|
| `lodestar --db <p> init`                   | 在 `<p>` 创建 db 文件并写入 `me` 单例                                  |
| `lodestar --db <p> add`                    | 交互式新增一个联系人                                                    |
| `lodestar --db <p> import file.{csv,xlsx}` | 批量导入；`.xlsx` 必填 `--preset richard\|tommy`                       |
| `lodestar --db <p> find "我想..."`         | 按目标语义找最优联系人 + 引荐路径                                       |
| `lodestar --db <p> list / show / delete`   | 联系人 CRUD                                                            |
| `lodestar --db <p> reembed`                | 重新生成全部 bio embedding（首次/换模型用）                              |
| `lodestar --db <p> stats`                  | DB 统计                                                                |
| `lodestar --db <p> web-password`           | 设 / 清 / 查该 db 的 web 锁；`--set 'pw'`、`--clear`、`--status`        |
| `lodestar --db <p> reset --yes`            | ⚠️ 硬删该 db 文件（含 WAL/SHM）；先 `cp` 备份                            |
| `lodestar serve --mount slug=path ...`     | 起 web UI，多个 `--mount` 把不同 db 挂在 `/r/<slug>/`                   |

Run `uv run lodestar --help` for the full list (还有 `enrich` / `infer-colleagues`
/ `normalize-companies` / `viz` 等管理命令)。

### Supported import formats

`lodestar import` auto-detects by file extension.

#### CSV

Columns (order does not matter; extras are ignored):

```
name, bio, tags, skills, companies, cities, needs, strength, context, frequency, notes
```

- `tags`, `skills`, `companies`, `cities`, `needs` are semicolon-separated (`;`).
- `strength` is an integer 1 (acquaintance) → 5 (very close). Defaults to 3.
- `frequency` ∈ `weekly | monthly | quarterly | yearly | rare`.

#### Excel — `--preset richard` / `--preset tommy`

按数据源 schema 选 preset；xlsx 导入时 `--preset` 必填（CSV 不需要）。
Preset 命名沿用第一位采用该列结构的 owner 名字，仅做识别，跟"哪本库"无关
——任何 db 文件都可以导入任意 preset 的表。

**`--preset richard`** — `examples/richard_network.xlsx` 的 13 列通用形态
（`template.xlsx` / `demo_network.xlsx` 共用同一 schema）：

| Column                          | Maps to                                |
|---------------------------------|----------------------------------------|
| `姓名`                          | `name` (required)                      |
| `所属行业`                      | `tags` + 拼进 `bio`                     |
| `公司`                          | `company` + 拼进 `bio`                  |
| `职务`                          | 拼进 `bio` + `context`                  |
| `城市`                          | `city` + 拼进 `bio`                     |
| `AI标准化特征`                  | `tags` (split on `, ， 、 ; ； / ｜`)   |
| `可信度（言行一致性0-5分）`     | `strength`（0=未联系/wishlist，1-5=已联系） |
| `合作价值（0-5）`               | 拼到 `bio` 末尾                          |
| `潜在需求`                      | `needs` ← drives reciprocal matching    |
| `资源类型`                      | 折进 `tags`                              |
| `认识`                          | peer-to-peer 边                         |
| `备注`                          | 拼进 `bio`                               |

**`--preset tommy`** — `examples/tommy_network.xlsx` 的 16 列机构合作画像表
（列名长且自描述，刻意不规范化）。

Duplicate rows (same `姓名`) are merged; later rows only add information, never erase.

如果 schema 不同，新增一个 `ColumnMapping` preset 函数（命名按"第一个用这种
表结构的人 / 来源"）传给 `ExcelImporter(repo, mapping=...)`；不要保留过渡
别名或双写映射。

---

## Configuration

All tunables live in `.env` (or environment variables with the `LODESTAR_` prefix):

| Variable                     | Default                        | Notes                                |
|------------------------------|--------------------------------|--------------------------------------|
| `LODESTAR_LLM_API_KEY`       | —                              | Required for goal parsing            |
| `LODESTAR_LLM_BASE_URL`      | `https://api.openai.com/v1`    | OpenAI-compatible                    |
| `LODESTAR_LLM_MODEL`         | `gpt-4o-mini`                  | Any chat model the endpoint exposes  |
| `LODESTAR_EMBEDDING_API_KEY` | —                              | Can be the same as the LLM key       |
| `LODESTAR_EMBEDDING_BASE_URL`| `https://api.openai.com/v1`    | OpenAI-compatible                    |
| `LODESTAR_EMBEDDING_MODEL`   | `text-embedding-3-small`       | Must match `DIM` below               |
| `LODESTAR_EMBEDDING_DIM`     | `1536`                         | 1024 for text-embedding-v4/BGE, 1536 for text-embedding-3-small |
| `LODESTAR_EMBEDDING_BATCH_SIZE` | `10`                        | Inputs per /embeddings call. 10=DashScope v3/v4, 25=v2, 2048=OpenAI |
| `LODESTAR_DB_PATH`           | XDG data dir                   | CLI 默认 db；被全局 `--db <path>` 覆盖。Web 端忽略，由 `serve --mount` 决定 |
| `LODESTAR_MAX_HOPS`          | `3`                            | Max intermediaries in a path         |
| `LODESTAR_TOP_K`             | `10`                           | Candidates considered per search     |
| `LODESTAR_WEAK_ME_FLOOR`     | `4`                            | Me 边强度低于该 floor 时在路径搜索里被惩罚，优先走更熟的多跳引荐 |
| `LODESTAR_RERANKER`          | `none`                         | Stage-2 重排器：`none` / `llm`（多调一次 Qwen）/ `bge`（本地 cross-encoder，需 `[rerank]` extra） |

> **网页密码不放 env**。每个 db 文件的密码哈希 + salt + HMAC 签名密钥
> 都写在该 db 的 `meta` 表里（`set_web_password` 写入），cp 走 db 的人把
> 密码态一起带走，互不相干。设密码用 `lodestar --db <p> web-password --set`。

Example: **阿里云百炼 (DashScope) — 推荐**

```env
LODESTAR_LLM_API_KEY=sk-...
LODESTAR_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LODESTAR_LLM_MODEL=qwen-plus

LODESTAR_EMBEDDING_API_KEY=sk-...
LODESTAR_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LODESTAR_EMBEDDING_MODEL=text-embedding-v4
LODESTAR_EMBEDDING_DIM=1024
LODESTAR_EMBEDDING_BATCH_SIZE=10
```

Example: **OpenAI**

```env
LODESTAR_LLM_API_KEY=sk-...
LODESTAR_LLM_BASE_URL=https://api.openai.com/v1
LODESTAR_LLM_MODEL=gpt-4o-mini

LODESTAR_EMBEDDING_API_KEY=sk-...
LODESTAR_EMBEDDING_BASE_URL=https://api.openai.com/v1
LODESTAR_EMBEDDING_MODEL=text-embedding-3-small
LODESTAR_EMBEDDING_DIM=1536
LODESTAR_EMBEDDING_BATCH_SIZE=2048
```

---

## Architecture

```
src/lodestar/
├── cli.py                   # Typer 入口；全局 --db；serve --mount
├── config.py                # pydantic-settings
├── models.py                # Person / Relationship / PathResult
├── db/
│   ├── schema.py            # DDL（meta / person / relationship / vec_*）
│   ├── connection.py        # 加载 sqlite-vec；首次开库写 meta.unlock_secret
│   └── repository.py        # CRUD + 向量 + 关键词；web_password / unlock_secret
├── embedding/               # OpenAI 兼容 /v1/embeddings
├── llm/                     # OpenAI 兼容 /v1/chat + goal parser + Anonymizer
├── search/
│   ├── hybrid.py            # vector + keyword (RRF)
│   ├── path_finder.py       # NetworkX 最短路径 + 评分
│   └── reranker.py          # Stage-2: NoopReranker / LLMJudgeReranker / BgeReranker
├── importers/               # Polars CSV / Excel ingestion + preset mapping
├── enrich/                  # LLM bio 解析 / 公司归一化 / 关系自然语言解析
├── web/
│   ├── app.py               # FastAPI 工厂：root + 每 mount 一个 sub-app
│   ├── mount_unlock.py      # 每 mount HMAC token（slug 烤进签名）
│   ├── enrich_jobs.py       # 后台 enrich 任务（mount-aware）
│   └── static/              # Alpine.js + Cytoscape.js SPA
└── ui/                      # Rich 终端渲染
```

**一人一库**：每个 SQLite 文件 = 一个 owner = 一个 web mount。CLI 进程只面对
一个 db；Web 进程通过 `_build_mount_app(spec)` 工厂为每个 `--mount slug=path`
建一个 FastAPI 子应用，挂到 `/r/<slug>/`。文件级 OS 权限就是 ACL，跨网络
的隔离不需要应用层做任何 owner check。

**Graph layer lives in memory** (built from the DB at query time). 几百到几千
联系人量级，路径搜索都在毫秒级完成。规模真涨上去时，SQLite 文件本身可以
导出到任何后继（Neo4j、RyuGraph …）而不丢数据。

---

## Roadmap

- [x] FastAPI Web UI（一人一库 + `/r/<slug>/` 子路由 + 切 tab 必重输）
- [x] `lodestar viz` — pyvis HTML 可视化
- [x] Stage-2 重排（`llm` / `bge`）
- [ ] vCard / LinkedIn / WeChat export importers
- [ ] Relationship decay (strength drops automatically past `last_contact`)
- [ ] Reminders ("haven't talked to X in 6 months")

---

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
```

MIT license.
