## Learned User Preferences

- Explain and document in Simplified Chinese unless the user asks otherwise.
- Prefer a restrained, premium dark UI (Linear-style cues) over harsh neon or flashy “tech” chrome; lean on `awesome-design-md` for token-level reference; keep body type large enough to read comfortably and use size, weight, and muted color for hierarchy instead of high-luminance accents.
- Treat the top bar and the bottom-left path panel as **information-primary** surfaces: type can be stepped up there so names, chains, and actions read at a glance without shouting with color.
- Primary actions (especially search / goal query) must never feel silent: show loading, success, empty, or error feedback so users know what happened.
- Avoid heart icons (or similar) for matchmaking or tie strength in a professional contact graph; they read as intimate rather than networking.
- Default graph presentation should stay visually quiet (muted baseline); after the user searches or focuses on a goal, emphasize the relevant path(s) and trim redundant on-screen information.
- The primary workflow expectation is: state a goal in natural language (for example wanting to accomplish something), get fast retrieval over the contact database, and see one or a few best target people plus the intermediate chain(s), with those path(s) clearly highlighted.
- For **path-shaped** flows (goal search, two-person path, broker introductions), reuse the same bottom-left path list pattern; **panel copy and the highlighted edges on the graph must describe the same chain**—no orphan banners or mismatched rows.

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
