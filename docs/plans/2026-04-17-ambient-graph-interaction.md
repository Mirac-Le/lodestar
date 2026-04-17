# Ambient Graph Interaction Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Turn the current always-on network graph into a lock-screen-like ambient experience: quiet by default, informative on hover, and action-oriented after search.

**Architecture:** Keep the existing FastAPI + static SPA structure and implement this as a frontend state-machine refactor rather than a rewrite. The core change is to introduce three explicit UI modes (`ambient`, `focus`, `intent`) and make graph styling, label visibility, and panel content depend on those modes.

**Tech Stack:** FastAPI, Alpine.js, Cytoscape.js, static HTML/CSS/JS

---

## Product Rules

- Default view should feel calm, not analytical.
- Size remains a persistent signal for relationship importance.
- Industry color remains a secondary signal, but muted in ambient mode.
- Search is the primary action. Hover is the secondary action.
- Full detail is deferred until explicit click, not shown by default.
- Panels should earn their presence. Hidden by default unless relevant to current intent.

## Target UX

### Ambient Mode

- Entire graph is visible.
- Nodes are desaturated / low-contrast but still faintly color-coded by industry.
- Only `Me` and a very small number of high-value labels are visible.
- Filters and stats are collapsed by default.
- Search box and one short helper line are the only persistent chrome.

### Focus Mode

- Triggered by hover on a node.
- Highlight hovered node, its first-degree neighbors, and connecting edges.
- Reveal labels for that local neighborhood only.
- Show a compact contextual card:
  - who this person is
  - relationship strength
  - one-line reason they matter
  - immediate neighbors count or strongest link

### Intent Mode

- Triggered by successful search or explicit path-finding flow.
- Dim the entire graph except recommended path nodes/edges.
- Restore stronger industry color on nodes inside the active path.
- Show ranked path cards with:
  - target person
  - path preview
  - why this path is useful
  - combined score / confidence
- Keep this view task-oriented. Do not automatically expand full profile details.

## Information Architecture

### Always Visible

- Brand
- Search input
- Optional helper copy like `输入目标以点亮路径`
- `Me` node
- Node size encoding

### Visible Only In Focus

- Local labels around hovered node
- Local edge emphasis
- Compact focus card

### Visible Only In Intent

- Path ranking panel
- Intent summary
- Exit / clear search affordance

### Hidden By Default

- Filters panel
- Stats panel
- Dense metadata
- Full profile detail card
- All node labels everywhere

## Implementation Tasks

### Task 1: Introduce explicit UI modes

**Files:**
- Modify: `src/lodestar/web/static/app.js`

**Step 1: Add state model**

Add a single source of truth for view mode in Alpine state:

- `viewMode: "ambient" | "focus" | "intent"`
- `focusedNodeId: string | null`
- `focusSummary: object | null`
- `showAmbientHint: boolean`

Also add helpers:

- `enterAmbientMode()`
- `enterFocusMode(node)`
- `enterIntentMode(searchResponse)`

**Step 2: Replace implicit mode switching**

Current behavior depends mostly on `searchActive` and raw hover handlers. Replace that with:

- hover enters `focus` only when not in `intent`
- mouseout returns to `ambient`
- search enters `intent`
- clear search returns to `ambient`
- explicit click can still open `detail`, but should not be required for hover info

**Step 3: Verify manually**

Run:

```bash
uv run lodestar serve --reload
```

Expected:

- Initial page is ambient
- Hovering any node creates a temporary focus state
- Searching creates a persistent intent state

### Task 2: Rebuild graph styling around the three modes

**Files:**
- Modify: `src/lodestar/web/static/app.js`
- Modify: `src/lodestar/web/static/style.css`

**Step 1: Add Cytoscape classes for ambient/focus/intent**

Add or reuse classes such as:

- `ambient-dim`
- `ambient-muted`
- `focus-node`
- `focus-neighbor`
- `intent-node`
- `intent-edge`
- `label-hidden`
- `label-visible`

Do not rely only on one generic `dim` class.

**Step 2: Ambient visual rules**

Implement these defaults:

- low-saturation node appearance
- reduced edge opacity
- most labels hidden
- only `Me` and a curated small set of labels remain visible

Recommended curated labels:

- `Me`
- strongest 3 to 5 first-degree contacts by `strength_to_me`

**Step 3: Focus visual rules**

On hover:

- hovered node gets full opacity
- first-degree neighbors get medium opacity
- unrelated nodes drop to very low opacity
- local labels appear
- connected edges brighten slightly

**Step 4: Intent visual rules**

When a path is active:

- path nodes recover stronger color
- path edges use accent violet
- unrelated graph fades aggressively
- labels show only on active path

**Step 5: Verify manually**

Check:

- ambient graph is calm
- hover creates a readable local neighborhood
- search clearly isolates useful paths

### Task 3: Simplify persistent chrome

**Files:**
- Modify: `src/lodestar/web/static/index.html`
- Modify: `src/lodestar/web/static/style.css`
- Modify: `src/lodestar/web/static/app.js`

**Step 1: Collapse non-essential panels by default**

Change defaults:

- `showFilters: false`
- `showStats: false`

Keep reopen buttons, but visually downplay them.

**Step 2: Add ambient helper copy**

Add one quiet helper line near the search input or beneath top bar:

- `输入目标以点亮路径`
- or `悬停查看局部关系`

Show this only in ambient mode.

**Step 3: Remove dashboard feel**

Reduce the visual priority of:

- stats panel
- filter panel
- mode banner

They should feel optional, not primary.

**Step 4: Verify manually**

Expected:

- page opens with minimal chrome
- user attention goes first to the graph and search box

### Task 4: Replace full detail-on-hover with compact focus card

**Files:**
- Modify: `src/lodestar/web/static/index.html`
- Modify: `src/lodestar/web/static/style.css`
- Modify: `src/lodestar/web/static/app.js`

**Step 1: Add compact focus card**

Create a lightweight card distinct from the current full detail panel.

Suggested fields:

- name
- industry
- strength to me
- short bio snippet
- `为什么值得联系` one-line derived summary

The existing full detail panel remains click-driven.

**Step 2: Keep focus card small**

The hover card should not include:

- full tags list
- full companies list
- cities
- notes
- all related people

Those stay in the click detail panel.

**Step 3: Verify manually**

Expected:

- hover gives immediate context
- click still opens full detail when needed

### Task 5: Reframe intent results around action, not biography

**Files:**
- Modify: `src/lodestar/web/static/index.html`
- Modify: `src/lodestar/web/static/style.css`
- Modify: `src/lodestar/web/static/app.js`

**Step 1: Keep intent panel concise**

Path cards should prioritize:

- person
- route
- rationale
- score / confidence

Do not auto-open the detail panel after search unless the user explicitly selects a result.

**Step 2: Add action framing**

For each result, support one short “next step” phrase, for example:

- `先联系 A，请他引荐 B`
- `你与目标相距 2 跳，优先走强关系节点`

If this is not yet returned by the backend, derive it in the frontend from existing path data.

**Step 3: Verify manually**

Expected:

- search results feel like recommendations, not raw data dumps

### Task 6: Tune label strategy for readability

**Files:**
- Modify: `src/lodestar/web/static/app.js`

**Step 1: Add label visibility heuristics**

Ambient labels should be limited by simple deterministic rules:

- always show `Me`
- show strongest nearby contacts
- hide long-tail labels

Focus labels:

- show hovered node and first-degree neighbors

Intent labels:

- show path nodes only

**Step 2: Avoid label flicker**

Debounce hover transitions slightly if needed so labels do not flash when crossing dense node clusters.

**Step 3: Verify manually**

Expected:

- graph stays legible
- labels feel deliberate, not noisy

### Task 7: Final polish and validation

**Files:**
- Modify: `src/lodestar/web/static/style.css`
- Modify: `src/lodestar/web/static/app.js`
- Modify: `src/lodestar/web/static/index.html`

**Step 1: Motion polish**

Use subtle transitions only:

- opacity
- scale
- edge brightness
- card fade/slide

No flashy animation. The metaphor is lock-screen calm, not sci-fi dashboard.

**Step 2: Manual test checklist**

Run:

```bash
uv run lodestar serve --reload
```

Verify:

- page opens in quiet ambient mode
- hover reveals only local context
- search isolates paths clearly
- clearing search returns to ambient state
- filters/stats remain optional
- click still opens full detail
- keyboard shortcut `/` still focuses search
- `Esc` restores a calm default state

**Step 3: Lint / diagnostics**

Run whatever is appropriate after edits:

```bash
uv run ruff check .
uv run mypy src
```

Then check edited frontend files with IDE diagnostics.

## Non-Goals

- No backend schema change unless absolutely needed.
- No new frontend framework.
- No fully animated onboarding flow.
- No permanent dashboard widgets competing with search.

## Acceptance Criteria

- The default screen feels calm and uncluttered.
- Users can understand “who matters” from size without reading every label.
- Hover reveals just enough local information to orient the user.
- Search transitions the app into a clearly task-oriented path view.
- The UI shows less information by default, but more useful information at the right moment.
