## Learned User Preferences

- Explain and document in Simplified Chinese unless the user asks otherwise.
- Prefer a restrained, premium dark UI (Linear-style cues) over harsh neon or flashy “tech” chrome; lean on `awesome-design-md` for token-level reference; keep body type large enough to read comfortably and use size, weight, and muted color for hierarchy instead of high-luminance accents.
- Avoid heart icons (or similar) for matchmaking or tie strength in a professional contact graph; they read as intimate rather than networking.
- Default graph presentation should stay visually quiet (muted baseline); after the user searches or focuses on a goal, emphasize the relevant path(s) and trim redundant on-screen information.
- The primary workflow expectation is: state a goal in natural language (for example wanting to accomplish something), get fast retrieval over the contact database, and see one or a few best target people plus the intermediate chain(s), with those path(s) clearly highlighted.

## Learned Workspace Facts

- This repo is Lodestar: a personal network navigator built on SQLite, sqlite-vec, and NetworkX, with a Typer CLI and a FastAPI static web UI.
- Run the web UI with `uv run lodestar serve` (defaults to `127.0.0.1:8765`; use `--host 0.0.0.0` when others on the LAN need access).
- Sample and template spreadsheets for imports live under `examples/` (including `pyq.xlsx`, `demo_network.xlsx`, and `template.xlsx`).
- The tree includes `awesome-design-md/` as an in-repo library of design-system references for UI work.
