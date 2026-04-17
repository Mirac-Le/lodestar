# Lodestar

> *"A star that guides or serves to guide."*

Your personal network navigator. You tell it what you want to do — **"I want to raise a seed round from an AI-focused investor"** — and it searches your contacts, traces the connections, and ranks the best paths for you to take.

Built around three boring-but-reliable pieces:

- **SQLite** — one file, your data, forever.
- **[sqlite-vec](https://github.com/asg017/sqlite-vec)** — semantic search inside the same file.
- **[NetworkX](https://networkx.org/)** — path-finding on an in-memory graph, built from the DB on demand.

Embeddings and goal parsing go through any OpenAI-compatible endpoint (阿里云百炼/DashScope, OpenAI, DeepSeek, Zhipu, Kimi, …). 中文场景推荐百炼。

---

## Quick start

```bash
git clone <repo> lodestar
cd lodestar
cp .env.example .env        # fill in your LLM / embedding API key
uv sync                     # install deps into .venv

uv run lodestar init --name "Your Name"
uv run lodestar import examples/pyq.xlsx       # .xlsx / .xls / .csv all work
uv run lodestar find "量化私募" --top 5
uv run lodestar find "想找政府资源" --top 5
```

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

| Command                          | What it does                                     |
|----------------------------------|--------------------------------------------------|
| `lodestar init`                  | Create the database and the `me` record         |
| `lodestar add`                   | Interactively add a single contact              |
| `lodestar import file.csv`       | Bulk-import contacts from CSV                   |
| `lodestar find "我想..."`         | Find best contacts + path for a goal            |
| `lodestar list`                  | List all contacts                               |
| `lodestar show <name>`           | Show one person's profile                       |
| `lodestar delete <name>`         | Remove a contact                                |
| `lodestar reembed`               | Recompute every embedding                       |
| `lodestar stats`                 | DB statistics                                   |

Run `uv run lodestar --help` for the full list.

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

#### Excel (Chinese finance preset)

Works out-of-the-box with the following columns:

| Column                          | Maps to                                |
|---------------------------------|----------------------------------------|
| `姓名`                          | `name` (required)                      |
| `所属行业`                      | `tags` (also included in `bio`)        |
| `职务`                          | included in `bio` + `context`          |
| `AI标准化特征`                  | `tags` (split on `, ， 、 ; ； / ｜`)   |
| `可信度（言行一致性0-5分）`     | `strength` (1-5)                       |
| `潜在需求`                      | `needs` ← drives reciprocal matching    |

Duplicate rows (same `姓名`) are merged; later rows only add information, never erase. See `examples/pyq.xlsx` for the reference format.

For a different Excel schema, construct a `ColumnMapping` and pass it to `ExcelImporter(repo, mapping=...)`.

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
| `LODESTAR_DB_PATH`           | XDG data dir                   | `~/.local/share/lodestar/lodestar.db` on Linux |
| `LODESTAR_MAX_HOPS`          | `3`                            | Max intermediaries in a path         |
| `LODESTAR_TOP_K`             | `10`                           | Candidates considered per search     |

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
├── cli.py                   # Typer entry point
├── config.py                # pydantic-settings
├── models.py                # Person / Relationship / PathResult
├── db/
│   ├── schema.py            # DDL
│   ├── connection.py        # loads sqlite-vec extension
│   └── repository.py        # CRUD + vector + keyword
├── embedding/               # OpenAI-compatible /v1/embeddings
├── llm/                     # OpenAI-compatible /v1/chat + goal parser
├── search/
│   ├── hybrid.py            # vector + keyword (RRF)
│   └── path_finder.py       # NetworkX shortest path + scoring
├── importers/               # Polars CSV ingestion
└── ui/                      # Rich terminal rendering
```

The **graph layer lives in memory** (built from the DB at query time). For a personal network of a few hundred to a couple thousand contacts, path-finding finishes in low single-digit milliseconds. When the network grows large enough to need a proper graph database, the SQLite file can be exported to any successor (Neo4j, RyuGraph, …) without losing data.

---

## Roadmap

- [ ] vCard / LinkedIn / WeChat export importers
- [ ] `lodestar graph` — pyvis HTML visualization
- [ ] FastAPI + htmx web UI (behind a localhost-only port)
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
