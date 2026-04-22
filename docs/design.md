# Lodestar — design notes

*Short living document of the choices behind the code. Updated as we learn.*

## Goals

1. **Personal-scale contacts database** with rich tagging (skills, industries, companies, cities, custom tags).
2. **Goal-driven retrieval**: user enters `"I want to do X"`; we return the best people and the shortest/strongest path from `me` to each of them.
3. **Zero external services**: must run fully locally with a single SQLite file. The only remote calls are to an OpenAI-compatible API for embeddings and goal parsing, and both are optional at query time.
4. **Modern Python hygiene**: `uv`, Python 3.12, ruff, ty (Astral 类型检查器), pytest.

## Non-goals (for now)

- Multi-user / shared database.
- Real-time sync with external CRMs (LinkedIn, Salesforce). Importers can be added later.
- Massive graphs (>100k people). We optimize for personal scale.

## Storage — why SQLite + sqlite-vec + NetworkX, not a graph DB?

We originally considered [Kuzu](https://kuzudb.com/), but it was [archived in October 2025](https://finance.biggo.com/news/202510130126_KuzuDB-embedded-graph-database-archived/). [RyuGraph](https://github.com/predictable-labs/ryugraph) forks it, but is still finding its footing.

For a **personal-scale** network (hundreds to a few thousand people), a dedicated graph DB is overkill. We chose:

- **SQLite** — the most mainstream storage in computing. Single file, WAL mode, portable anywhere.
- **sqlite-vec** — a SQLite extension providing HNSW-style vector search inside the same database. Semantic search becomes `SELECT ... FROM vec_person_bio WHERE embedding MATCH ? AND k = ?`.
- **NetworkX** — the canonical Python graph library. We build an in-memory `Graph` from the relationship table on each query. Shortest-path with Dijkstra weights finishes in milliseconds for this scale.

This means every piece has 10+ years of production battle-testing.

## Schema

Relational:

```
person(id, name, bio, notes, is_me, created_at, updated_at)
tag(id, name)               person_tag(person_id, tag_id)
skill(id, name)             person_skill(person_id, skill_id, level)
company(id, name, industry) person_company(person_id, company_id, role, since, is_current)
city(id, name)              person_city(person_id, city_id)

relationship(
    source_id, target_id, strength 1-5, context, frequency,
    last_contact, introduced_by_id
)
```

Vector:

```
vec_person_bio(person_id PK, embedding FLOAT[dim])   -- sqlite-vec virtual table
```

Conventions:

- Exactly one row in `person` has `is_me = 1` (enforced by a unique partial index).
- `relationship` has `UNIQUE(source_id, target_id)` so `add_relationship` behaves as an upsert.
- Edges are stored directed but treated undirected when we build the NetworkX graph.

## Retrieval pipeline

```
user goal (freeform)
  │
  ▼
LLM goal parser   →  GoalIntent(keywords, skills, industries, roles, cities, summary)
  │                                                                    │
  ▼                                                                    ▼
embed(summary)                                                keyword_candidates()
  │                                                                    │
  ▼                                                                    ▼
vec_person_bio MATCH                                   LIKE over tag / skill / company / city / bio
  │                                                                    │
  ▼                                                                    ▼
ranked list                                                      ranked list
  │                                                                    │
  └────────────── Reciprocal Rank Fusion (k=60) ───────────────────────┘
                                │
                                ▼
                      top-K candidates (score ∈ [0, 1])
                                │
                                ▼
         NetworkX graph built from relationship table
         shortest_path(weight = 1/strength)
                                │
                                ▼
              combined_score = relevance × (1 + path_strength/5) / hops
                                │
                                ▼
                           ranked PathResult[]
```

**Why RRF and not a learned re-ranker?**
RRF has no hyperparameters that need tuning per user, survives missing signals gracefully (if embeddings are disabled, keywords still work), and produces competitive results for this scale. Easy to swap later.

**Why undirected graph?**
For personal CRM purposes, if *I* know Alice and Alice knows Bob, that *is* a path. Directedness matters for outreach ("who can email whom first") but the combined_score already penalizes longer paths.

## Pluggable LLM / Embedding

Both go through the OpenAI SDK with a custom `base_url`. Any provider that exposes OpenAI-compatible `/v1/chat/completions` and `/v1/embeddings` works. Providers are configured via env vars, and the two can be different providers (e.g. DeepSeek for chat, OpenAI for embeddings).

If the LLM is unavailable, `find --no-llm` falls back to keyword-only search. If embeddings are unavailable, vector search is skipped and keyword-only RRF handles it.

## Scoring rationale

```
combined_score = relevance × (1 + path_strength/5) / hops
```

- `relevance` (0–1) = normalized RRF score from hybrid search.
- `path_strength` = sum of edge strengths along the path (each 1–5).
- `hops` = number of intermediaries; direct contact ⇒ `hops = 1`.

Stronger, shorter paths to more relevant people win. The `+1` inside the parenthesis ensures a weak 3-hop path still scores above an unreachable one.

## Testing strategy

- Unit tests at the repository level use an in-memory-ish SQLite per test (ephemeral file in `tmp_path`), with `embedding_dim=4` so vectors stay tiny.
- Path-finder tests use hand-built graphs with known expected paths.
- Hybrid-search tests exercise the keyword-only path (no embedder), which is deterministic.

LLM / embedding integration tests are intentionally skipped from CI — they require live API keys. Manual smoke test: `lodestar find "..."` against the sample CSV.

## Future work

- **Relationship decay**: strength should attenuate by `frequency` and `last_contact`. Simple exponential decay is enough.
- **Intro suggestions**: explicitly suggest the intermediary as the person to ask for an introduction, with copy-pastable intro text.
- **Data visualization**: pyvis HTML graph in `lodestar graph`.
- **Importers**: vCard, LinkedIn CSV export, WeChat contact export.
- **Encrypted at rest**: optional SQLCipher backend.
