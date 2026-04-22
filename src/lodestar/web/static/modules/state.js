/* ============================================================
 * Alpine.js application state factory.
 *
 * Returns a fresh Alpine "data object" with everything the SPA
 * binds to in index.html (search, hover/focus, two-person path,
 * AI enrich, NL relationship parse, relationships drawer, mount
 * unlock flow, etc.).
 *
 * 拆分边界（modules/graph.js / modules/api.js / modules/constants.js）
 * 已经把渲染、网络、常量挪走，本文件只剩"UI state + 业务流程编排"。
 * ============================================================ */

import { INDUSTRIES } from "./constants.js";
import { MOUNT_SLUG, api, withMount } from "./api.js";
import {
  applyAmbientState,
  applyFilters,
  applyFocusState,
  applyIntentState,
  buildFocusSummary,
  clearHighlights,
  getCy,
  initCytoscape,
} from "./graph.js";

export function appState() {
  return {
    /* ---- data ---- */
    graph: { nodes: [], edges: [], me_id: null, weak_me_floor: 4 },
    mounts: [],            // [{slug, display_name, contact_count, locked, accent_color, ...}]
    mountSlug: MOUNT_SLUG, // active mount slug derived from URL; null on root
    mount: null,           // MountDTO of the current mount
    detail: null,
    paths: [],          // combined results, sorted by combined_score
    indirect: [],       // multi-hop introductions (path_kind == 'indirect')
    contacted: [],      // 1-hop (direct + weak merged), sorted by strength desc;
                        // backend still returns direct/weak separately for API
                        // compat, we union here because the binary 关系类型
                        // model has only "已联系 / 未联系", and "weak" is just
                        // strength=1 within "已联系"
    wishlist: [],       // any kind, but is_wishlist == true (curation chip)
    activePathKey: null, // which result row is currently highlighted on the graph
    intent: null,
    stats: null,
    introductions: [],

    /* ---- ui state ---- */
    query: "",
    searching: false,
    searchActive: false,
    viewMode: "ambient",
    focusedNodeId: null,
    focusSummary: null,
    ambientHint: "输入想找谁/想做什么以点亮路径，或悬停查看局部关系",
    hoverExitTimer: null,
    noLLM: false,
    showFilters: false,
    showStats: false,
    showHelp: false,
    showAdd: false,
    showIntros: false,
    twoPersonMode: false,
    pathStartInput: "",
    pathEndInput: "",
    pathStartFocused: false,
    pathEndFocused: false,
    pathMode: false,
    pathStart: null,
    pathEnd: null,
    pairLabels: null,
    toast: null,

    /* ---- filters ---- */
    filters: {
      industries: [], minStrength: 0, search: "",
    },

    /* ---- query history ---- */
    history: JSON.parse(localStorage.getItem("ls.history") || "[]"),

    /* ---- new contact form ---- */
    newPerson: {
      name: "", bio: "", notes: "",
      tags: "", skills: "", companies: "", cities: "", needs: "",
      strength_to_me: 3, relation_context: "", frequency: "yearly",
      embed: true,
    },

    /* ---- AI enrich state ---- */
    aiPreviewBusy: false,
    aiPreviewError: null,
    aiPreviewSummary: null,
    aiPersonBusy: null,        // detail.id while a per-person reparse is running
    showBatchEnrich: false,
    batchOnlyMissing: true,
    batchStarting: false,
    batchJob: null,            // last EnrichJobState
    _batchPollTimer: null,

    /* ---- NL relationship parse state ---- */
    showRelationParse: false,
    relationParseText: "",
    relationParseBusy: false,
    relationApplyBusy: false,
    relationParseError: null,
    relationProposals: [],     // [{a_id,a_name,b_id,b_name,strength,context,frequency,rationale,existing_edge,selected}]
    relationContext: {},       // {person_id: [RelationshipDTO]}
    relationUnknown: [],       // ["王某", ...]

    /* ---- relationships drawer (browse / edit) ---- */
    showRelationsDrawer: false,
    relationships: [],         // RelationshipDTO[]
    relationsTotal: 0,
    relationsLoading: false,
    relationsLoadingMore: false,
    relationsFilter: {
      q: "",
      min_strength: 0,
      include_me: true,
      sources: [],            // empty = no filter; otherwise subset of provenance
    },
    _relationsPageSize: 50,
    editingRelationId: null,
    editingRelation: null,     // shallow copy currently being edited
    editingRelationBusy: false,

    /* 一人一库（mount router）：解锁 token 仅在内存里，
       切 mount = 整页跳转 = token 自然丢失 = 必须再输密码 */
    locked: false,             // current mount has a password set
    unlockToken: null,         // string | null —— 仅当前 mount 有效
    showUnlockModal: false,
    unlockPassword: "",
    unlockError: null,
    unlockBusy: false,

    industriesList: INDUSTRIES,

    init() {
      window.app = this;
      this.newPerson = this.emptyPerson();
      this.bootstrap();
      this.bindShortcuts();
    },

    async bootstrap() {
      // Always fetch /api/mounts first so the owner-tab strip can render
      // even if the current mount happens to be locked. The mounts endpoint
      // is on the root app and never requires auth.
      try {
        const resp = await api("/api/mounts");
        this.mounts = resp.mounts || [];
        this.mount = this.mounts.find((m) => m.slug === this.mountSlug) || null;
        this.locked = Boolean(this.mount && this.mount.locked);
      } catch (e) {
        this.notify("加载网络列表失败：" + e.message, "error");
      }

      if (!this.mountSlug) {
        // Root URL `/` with multiple mounts: the user is on the picker
        // page, no further data fetch makes sense.
        return;
      }

      // Locked mount: pop the password modal first, only fetch data
      // after the user submits a valid password.
      if (this.locked) {
        try {
          await this.requestUnlock();
        } catch (e) {
          if (e?.message !== "cancelled") {
            this.notify("解锁失败：" + (e?.message || e), "error");
          }
          return;
        }
      } else {
        // Open mount: still need a token because the backend only skips
        // auth when web_password_hash is null (we mirror that on the
        // frontend by minting a permanent token via /api/unlock).
        try {
          const r = await api("/api/unlock", { method: "POST", body: { password: "" } });
          this.unlockToken = r.token;
        } catch (e) {
          this.notify("初始化失败：" + (e?.message || e), "error");
          return;
        }
      }

      await this.loadGraph();
      await this.loadStats();
    },

    /** Open the password modal and resolve when the user submits a
     *  correct password. Reject on cancel. */
    requestUnlock() {
      return new Promise((resolve, reject) => {
        this.unlockPassword = "";
        this.unlockError = null;
        this.unlockBusy = false;
        this._unlockDone = { resolve, reject };
        this.showUnlockModal = true;
        this.$nextTick(() => {
          const el = document.getElementById("mount-unlock-pw");
          if (el) el.focus();
        });
      });
    },

    openUnlockModal() {
      // Called by the api() helper when a request hits 401.
      // We don't reject the prior promise because there might not be one
      // (e.g. token expired mid-session).
      if (!this.showUnlockModal) {
        this.requestUnlock().catch(() => {});
      }
    },

    cancelUnlock() {
      this.showUnlockModal = false;
      const done = this._unlockDone;
      this._unlockDone = null;
      if (done) done.reject(new Error("cancelled"));
    },

    async submitUnlock() {
      this.unlockBusy = true;
      this.unlockError = null;
      try {
        const r = await fetch(withMount("/api/unlock"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password: this.unlockPassword || "" }),
        });
        const text = await r.text();
        if (!r.ok) {
          try {
            const j = JSON.parse(text);
            const d = j.detail;
            this.unlockError = typeof d === "object"
              ? (d.message || d.code || text)
              : (d || text);
          } catch (_) {
            this.unlockError = text;
          }
          return;
        }
        const data = JSON.parse(text);
        this.unlockToken = data.token;
        this.showUnlockModal = false;
        const done = this._unlockDone;
        this._unlockDone = null;
        if (done) done.resolve();
        // If this was a re-challenge (token expired mid-session), kick
        // off a graph reload so the UI recovers without a manual refresh.
        if (this.graph && (!this.graph.nodes || this.graph.nodes.length === 0)) {
          this.loadGraph().then(() => this.loadStats());
        }
      } catch (e) {
        this.unlockError = e.message || String(e);
      } finally {
        this.unlockBusy = false;
      }
    },

    /** Hard navigate to another mount. The full reload guarantees the
     *  in-memory unlock token is dropped — i.e. "切 tab 必重输". */
    switchMount(slug) {
      if (!slug || slug === this.mountSlug) return;
      window.location.assign(`/r/${slug}/`);
    },

    emptyPerson() {
      return {
        name: "", bio: "", notes: "",
        tags: "", skills: "", companies: "", cities: "", needs: "",
        strength_to_me: 3, relation_context: "", frequency: "yearly",
        embed: true,
      };
    },

    /* -------- shortcuts -------- */
    bindShortcuts() {
      document.addEventListener("keydown", (e) => {
        if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") {
          if (e.key === "Escape") e.target.blur();
          return;
        }
        if (e.key === "/") {
          e.preventDefault();
          document.getElementById("search-input").focus();
        } else if (e.key === "Escape") {
          this.clearSearch();
          this.clearDetail();
          this.exitTwoPerson();
          this.showHelp = false; this.showAdd = false; this.showIntros = false;
          this.showRelationParse = false;
        } else if (e.key === "?") {
          this.showHelp = !this.showHelp;
        } else if (e.key === "f" || e.key === "F") {
          this.toggleFullscreen();
        } else if (e.key === "n" || e.key === "N") {
          this.openAdd();
        }
      });
    },
    toggleFullscreen() {
      if (!document.fullscreenElement) document.documentElement.requestFullscreen();
      else document.exitFullscreen();
    },

    /* -------- data loading -------- */
    async loadGraph() {
      try {
        this.graph = await api("/api/graph");
        initCytoscape(this.graph);
        this.enterAmbientMode();
      } catch (e) {
        this.notify("加载图失败：" + e.message, "error");
      }
    },
    async loadStats() {
      try {
        this.stats = await api("/api/stats");
        this.$nextTick(() => this.renderChart());
      } catch (e) { /* silent */ }
    },
    renderChart() {
      if (!this.stats || !window.echarts) return;
      const el = document.getElementById("stats-chart");
      if (!el) return;
      const chart = window.echarts.init(el, null, { renderer: "canvas" });
      const data = Object.entries(this.stats.by_industry).map(([name, value]) => {
        const swatch = INDUSTRIES.find((x) => x[0] === name);
        return {
          name, value,
          itemStyle: {
            color: swatch ? swatch[1] : "#5c6068",
            borderColor: "#08090a",
            borderWidth: 2,
          },
        };
      });
      chart.setOption({
        backgroundColor: "transparent",
        tooltip: {
          trigger: "item",
          backgroundColor: "#16181b",
          borderColor: "rgba(255,255,255,0.08)",
          borderWidth: 1,
          textStyle: { color: "#f7f8f8", fontSize: 13, fontFamily: "Inter, system-ui", fontWeight: 510 },
          extraCssText: "box-shadow: 0 8px 28px rgba(0,0,0,0.4); border-radius: 6px;",
        },
        series: [{
          type: "pie",
          radius: ["52%", "80%"],
          avoidLabelOverlap: false,
          label: { show: false },
          labelLine: { show: false },
          data,
        }],
      });
      new ResizeObserver(() => chart.resize()).observe(el);
    },

    /* -------- search -------- */
    async runSearch() {
      const q = this.query.trim();
      if (!q) {
        this.notify("请先输入一句话描述你想找谁、想干什么，再按 Enter 或点击「查询」", "info", 3200);
        return;
      }
      this.searching = true;
      try {
        const resp = await api("/api/search", {
          method: "POST",
          body: { goal: q, top_k: 3, no_llm: this.noLLM },
        });
        this.intent = resp.intent_summary;
        this.paths = resp.results;
        // Tolerate older servers that only know `targets`.
        this.indirect = resp.indirect || resp.targets || [];
        // Merge backend's direct + weak into a single "已联系" bucket sorted
        // by combined_score desc — strength-1 weak ties naturally fall to
        // the bottom that way.
        this.contacted = [
          ...(resp.direct || []),
          ...(resp.weak || []),
        ].sort((a, b) => (b.combined_score || 0) - (a.combined_score || 0));
        this.wishlist = resp.wishlist || [];
        this.pathMode = false;
        this.pairLabels = null;
        const nResults = this.indirect.length + this.contacted.length;

        if (nResults === 0) {
          this.activePathKey = null;
          this.searchActive = false;
          this.focusSummary = null;
          this.focusedNodeId = null;
          this.enterAmbientMode();
          const summary = (this.intent || "").trim();
          const hint = summary
            ? `已理解需求：${summary}。库中暂无匹配路径，可换说法、开启 RAW，或补充联系人简介与标签。`
            : "库中暂无匹配路径，可换说法、开启 RAW，或补充联系人简介与标签。";
          this.notify(hint, "info", 5200);
          this.pushHistory(q);
          return;
        }

        this.activePathKey = null;
        this.searchActive = true;
        this.focusSummary = null;
        this.focusedNodeId = null;

        // Default highlight: ALL paths in the result panel, lightly.
        this.enterIntentMode(resp.highlighted_node_ids, resp.highlighted_edge_ids, {
          endpoints: this.paths.map((r) => r.target_id),
        });

        // Auto-focus the GLOBAL Top1 by combined_score, regardless of
        // bucket. Earlier this preferred `targets[0]` unconditionally,
        // which let any wishlist-flagged contact monopolise the highlight
        // even when a direct contact had much higher relevance. The new
        // rule: whichever person actually scored highest wins the focus,
        // and a wishlist chip on that row signals the curation status.
        const top = this.paths[0];
        if (top) {
          const key = this._rowKey(top);
          this.highlightPath(top, key);
        }
        this.pushHistory(q);
      } catch (e) {
        this.notify("搜索失败：" + e.message, "error");
      } finally {
        this.searching = false;
      }
    },
    clearSearch() {
      this.query = ""; this.intent = null;
      this.paths = []; this.indirect = []; this.contacted = [];
      this.wishlist = [];
      this.activePathKey = null;
      this.searchActive = false;
      this.pathMode = false;
      this.pairLabels = null;
      this.enterAmbientMode();
    },

    /* Stable row key used both by the result list (for `is-active` styling)
       and by autohighlight. The prefix mirrors the section the row lives in
       so clicking either the graph or the panel converges on the same key. */
    _rowKey(p) {
      const idx = this._rowIndex(p);
      // Two visible buckets only: indirect (未联系，多跳引荐) vs contacted
      // (已联系，1 跳，按 strength 排序). The legacy `path_kind=='weak'`
      // values from the backend collapse into the contacted bucket and use
      // the same `c{id}` key as strong direct contacts.
      if (p.path_kind === "indirect") return `t-${idx}-${p.target_id}`;
      return `c${p.target_id}`;
    },
    _rowIndex(p) {
      if (p.path_kind === "indirect") {
        return Math.max(0, this.indirect.findIndex((r) => r.target_id === p.target_id));
      }
      return 0;
    },

    /* Highlight a SINGLE path on the graph (clicked from results panel). */
    highlightPath(p, key) {
      this.activePathKey = key;
      this.searchActive = true;
      this.viewMode = "intent";
      const nodeIds = p.node_ids && p.node_ids.length > 0
        ? p.node_ids
        : p.path.map((s) => s.person_id);
      const edgeIds = p.edge_ids && p.edge_ids.length > 0
        ? p.edge_ids
        : (() => {
            const out = []; let prev = null;
            for (const nid of nodeIds) {
              if (prev !== null) {
                const lo = Math.min(prev, nid), hi = Math.max(prev, nid);
                out.push(`e_${lo}_${hi}`);
              }
              prev = nid;
            }
            return out;
          })();
      const endpoints = this.pathMode && nodeIds.length > 0
        ? [nodeIds[0], nodeIds[nodeIds.length - 1]]
        : [p.target_id];
      applyIntentState(nodeIds, edgeIds, {
        endpoints,
        fit: true,
      });
    },

    /* -------- query history -------- */
    pushHistory(q) {
      this.history = [q, ...this.history.filter((x) => x !== q)].slice(0, 20);
      localStorage.setItem("ls.history", JSON.stringify(this.history));
    },
    replay(q) { this.query = q; this.runSearch(); },

    /* -------- detail panel -------- */
    async handleNodeClick(node) {
      if (this.twoPersonMode) {
        if (!this.pathStart) {
          this.pathStart = node;
          this.pathStartInput = node.label;
        } else if (!this.pathEnd && node.id !== this.pathStart.id) {
          this.pathEnd = node;
          this.pathEndInput = node.label;
          await this.runTwoPersonPath();
        } else {
          this.pathStart = node;
          this.pathStartInput = node.label;
          this.pathEnd = null;
          this.pathEndInput = "";
        }
        return;
      }
      this.focusSummary = null;
      this.loadDetail(node.id);
    },
    handleNodeHover(node, nodeEle) {
      if (this.searchActive || this.twoPersonMode || this.detail) return;
      clearTimeout(this.hoverExitTimer);
      this.enterFocusMode(node, nodeEle);
    },
    /** Leaving the cytoscape node fires immediately; the 速览 card is
     *  fixed top-right, so the pointer must cross the canvas before
     *  reaching「打开档案」. Keep the card alive long enough, and let
     *  `onFocusPanelEnter` cancel the timer once the pointer hits the panel. */
    handleNodeExit() {
      if (this.searchActive || this.twoPersonMode || this.detail) return;
      clearTimeout(this.hoverExitTimer);
      this.hoverExitTimer = setTimeout(() => {
        if (!this.searchActive && !this.twoPersonMode && !this.detail) this.enterAmbientMode();
      }, 420);
    },

    onFocusPanelEnter() {
      if (this.searchActive || this.twoPersonMode || this.detail) return;
      clearTimeout(this.hoverExitTimer);
    },

    onFocusPanelLeave() {
      if (this.searchActive || this.twoPersonMode || this.detail) return;
      if (this.viewMode !== "focus") return;
      clearTimeout(this.hoverExitTimer);
      this.hoverExitTimer = setTimeout(() => {
        if (!this.searchActive && !this.twoPersonMode && !this.detail) this.enterAmbientMode();
      }, 160);
    },
    enterAmbientMode() {
      this.viewMode = "ambient";
      this.focusedNodeId = null;
      this.focusSummary = null;
      clearTimeout(this.hoverExitTimer);
      applyAmbientState();
    },
    enterFocusMode(node, nodeEle = null) {
      this.viewMode = "focus";
      this.focusedNodeId = String(node.id);
      this.focusSummary = applyFocusState(node.id) || buildFocusSummary(node, nodeEle);
    },
    enterIntentMode(nodeIds, edgeIds, options = {}) {
      this.viewMode = "intent";
      applyIntentState(nodeIds, edgeIds, options);
    },
    syncCurrentView() {
      const cy = getCy();
      if (!cy) return;
      if (this.searchActive && this.paths.length > 0) {
        const nodeIds = new Set();
        const edgeIds = new Set();
        this.paths.forEach((p) => {
          let prev = null;
          p.path.forEach((step) => {
            nodeIds.add(step.person_id);
            if (prev !== null) {
              const lo = Math.min(prev, step.person_id);
              const hi = Math.max(prev, step.person_id);
              edgeIds.add(`e_${lo}_${hi}`);
            }
            prev = step.person_id;
          });
        });
        this.enterIntentMode([...nodeIds], [...edgeIds], {
          endpoints: this.paths.map((p) => p.target_id),
          fit: false,
        });
        return;
      }
      if (this.viewMode === "focus" && this.focusedNodeId) {
        const raw = cy.getElementById(String(this.focusedNodeId))?.data("raw");
        if (raw) {
          this.focusSummary = applyFocusState(this.focusedNodeId);
          return;
        }
      }
      this.enterAmbientMode();
    },
    async loadDetail(id) {
      try {
        this.detail = await api(`/api/people/${id}`);
        const cy = getCy();
        if (cy) {
          cy.animate({ center: { eles: cy.getElementById(String(id)) }, duration: 400 });
        }
      } catch (e) {
        this.notify(e.message, "error");
      }
    },
    clearDetail() {
      this.detail = null;
      if (!this.searchActive) this.enterAmbientMode();
    },
    focusNode(id) { this.loadDetail(id); },
    nextStepText(pathResult) {
      if (pathResult && pathResult.next_step) return pathResult.next_step;
      const path = pathResult.path || [];
      if (this.pathMode) {
        if (path.length < 2) return "";
        const startName = path[0].name;
        const endName = pathResult.target_name;
        if (path.length === 2) {
          return `${startName} 与 ${endName} 直接相识，可一步打通。`;
        }
        const middles = path.slice(1, -1).map((s) => s.name).join(" → ");
        if (path.length === 3) {
          return `请 ${middles} 居间引荐 ${startName} ↔ ${endName}。`;
        }
        return `沿 ${middles} 链路居间引荐 ${startName} ↔ ${endName}。`;
      }
      if (path.length < 2) return "先查看资料，再决定是否联系。";
      const firstHop = path[1];
      if (path.length === 2) {
        return `下一步：直接联系 ${pathResult.target_name}。`;
      }
      if (path.length === 3) {
        return `下一步：找 ${firstHop.name}，请他引荐 ${pathResult.target_name}。`;
      }
      const chain = path.slice(1, -1).map((s) => s.name).join(" → ");
      return `下一步：从 ${firstHop.name} 起，沿 ${chain} 链路引荐到 ${pathResult.target_name}。`;
    },

    pathContextLine(pathResult) {
      const ctxs = (pathResult.path || [])
        .map((s) => s.relation_from_previous)
        .filter((c) => c && c.length > 0);
      if (ctxs.length === 0) return pathResult.rationale || "";
      return ctxs.join(" · ");
    },

    /* -------- two person path -------- */
    enterTwoPerson() {
      this.twoPersonMode = true; this.pathStart = null; this.pathEnd = null;
      this.pathStartInput = ""; this.pathEndInput = "";
      this.pathStartFocused = false; this.pathEndFocused = false;
      this.searchActive = false;
      this.focusSummary = null;
      this.viewMode = "ambient";
      clearHighlights(); this.notify("输入两个名字（带补全），或在图上依次点起点终点");
    },
    exitTwoPerson() {
      this.twoPersonMode = false; this.pathStart = null; this.pathEnd = null;
      this.pathStartInput = ""; this.pathEndInput = "";
      this.pathStartFocused = false; this.pathEndFocused = false;
      if (!this.searchActive) this.enterAmbientMode();
    },
    pairMatches(q) {
      const nodes = (this.graph && this.graph.nodes) || [];
      const raw = (q || "").trim().toLowerCase();
      if (!raw) {
        // 空查询：列前 50 个，避免一次性塞几百行 DOM；输入后再放开
        return nodes.slice(0, 50);
      }
      return nodes.filter((n) => (n.label || "").toLowerCase().includes(raw));
    },
    pickPathEndpoint(which, node) {
      if (which === "start") {
        this.pathStartInput = node.label;
        this.pathStartFocused = false;
      } else {
        this.pathEndInput = node.label;
        this.pathEndFocused = false;
      }
      this.resolvePathEndpoint(which);
    },
    hidePairSuggest(which) {
      // 延迟关闭，给 mousedown 选择留出时间
      setTimeout(() => {
        if (which === "start") this.pathStartFocused = false;
        else this.pathEndFocused = false;
      }, 120);
    },
    resolvePathEndpoint(which) {
      const raw = (which === "start" ? this.pathStartInput : this.pathEndInput).trim();
      if (!raw) {
        if (which === "start") this.pathStart = null;
        else this.pathEnd = null;
        return;
      }
      const nodes = (this.graph && this.graph.nodes) || [];
      const lc = raw.toLowerCase();
      const exact = nodes.filter((n) => (n.label || "").toLowerCase() === lc);
      let pick = exact[0];
      if (!pick) {
        pick = nodes.find((n) => (n.label || "").toLowerCase().includes(lc));
      }
      if (!pick) {
        this.notify(`找不到「${raw}」，请检查名字`, "error");
        return;
      }
      if (exact.length > 1) {
        this.notify(`「${raw}」有 ${exact.length} 人同名，已用 id=${pick.id}`, "info", 3500);
      }
      const other = which === "start" ? this.pathEnd : this.pathStart;
      if (other && other.id === pick.id) {
        this.notify("起点和终点不能是同一个人", "error");
        return;
      }
      const node = { id: pick.id, label: pick.label };
      if (which === "start") {
        this.pathStart = node;
        this.pathStartInput = pick.label;
      } else {
        this.pathEnd = node;
        this.pathEndInput = pick.label;
      }
      if (this.pathStart && this.pathEnd && this.pathStart.id !== this.pathEnd.id) {
        this.runTwoPersonPath();
      }
    },
    async runTwoPersonPath() {
      try {
        const resp = await api("/api/path", {
          method: "POST",
          body: {
            source_id: this.pathStart.id, target_id: this.pathEnd.id,
            max_paths: 5,
          },
        });
        if (resp.paths.length === 0) {
          this.notify(
            `${this.pathStart.label} 与 ${this.pathEnd.label} 之间在限定跳数内没有可行路径`,
            "error",
            4200,
          );
          return;
        }
        this.paths = resp.paths;
        this.indirect = resp.paths;
        this.contacted = [];
        this.wishlist = [];
        this.searchActive = true;
        this.pathMode = true;
        this.pairLabels = {
          start: this.pathStart.label,
          end: this.pathEnd.label,
        };
        this.intent = `${this.pathStart.label} → ${this.pathEnd.label}`;
        this.activePathKey = null;
        this.focusSummary = null;
        const nodes = new Set();
        const edges = new Set();
        resp.paths.forEach((p) => {
          let prev = null;
          p.path.forEach((s) => {
            nodes.add(s.person_id);
            if (prev !== null) {
              const lo = Math.min(prev, s.person_id);
              const hi = Math.max(prev, s.person_id);
              edges.add(`e_${lo}_${hi}`);
            }
            prev = s.person_id;
          });
        });
        this.enterIntentMode([...nodes], [...edges], {
          endpoints: [this.pathStart.id, this.pathEnd.id],
          fit: false,
        });
        const top = resp.paths[0];
        this.highlightPath(top, "t-0-" + top.target_id);
        this.twoPersonMode = false;
        this.pathStart = null;
        this.pathEnd = null;
        this.pathStartInput = "";
        this.pathEndInput = "";
      } catch (e) {
        this.notify(e.message, "error");
      }
    },

    /* -------- introductions -------- */
    async openIntros() {
      try {
        const resp = await api("/api/introductions");
        this.introductions = resp.suggestions;
        this.showIntros = true;
      } catch (e) {
        this.notify(e.message, "error");
      }
    },
    showIntroPair(provider_id, seeker_id) {
      const intro = (this.introductions || []).find(
        (s) => s.provider_id === provider_id && s.seeker_id === seeker_id,
      );
      const cy = getCy();
      const meNode = cy ? cy.nodes("[?is_me]").first() : null;
      const meId = meNode && meNode.length > 0 ? Number(meNode.id()) : null;
      const meName = meNode && meNode.length > 0 ? meNode.data("label") : "我";
      const providerName = intro ? intro.provider_name : `#${provider_id}`;
      const seekerName = intro ? intro.seeker_name : `#${seeker_id}`;
      const matched = intro && intro.matched_keyword ? intro.matched_keyword : "";
      const why = intro && intro.why ? intro.why : "";
      const path = meId != null
        ? [
            { person_id: provider_id, name: providerName },
            {
              person_id: meId, name: meName,
              relation_from_previous: "你直接认识",
            },
            {
              person_id: seeker_id, name: seekerName,
              relation_from_previous: matched ? `匹配「${matched}」` : "你直接认识",
            },
          ]
        : [
            { person_id: provider_id, name: providerName },
            {
              person_id: seeker_id, name: seekerName,
              relation_from_previous: matched ? `匹配「${matched}」` : "",
            },
          ];
      const nodeIds = path.map((s) => s.person_id);
      const edgeIds = [];
      for (let i = 1; i < nodeIds.length; i++) {
        const a = nodeIds[i - 1], b = nodeIds[i];
        edgeIds.push(`e_${Math.min(a, b)}_${Math.max(a, b)}`);
      }
      const nextStep = meId != null
        ? `由你居间引荐 ${providerName} ↔ ${seekerName}${
            matched ? `（匹配「${matched}」）` : ""
          }。`
        : `${providerName} ↔ ${seekerName}${
            matched ? ` 匹配「${matched}」` : ""
          }。`;
      const synthetic = {
        target_id: seeker_id,
        target_name: seekerName,
        path,
        node_ids: nodeIds,
        edge_ids: edgeIds,
        combined_score: 1.0,
        rationale: why,
        next_step: nextStep,
      };
      this.paths = [synthetic];
      this.indirect = [synthetic];
      this.contacted = [];
      this.wishlist = [];
      this.activePathKey = null;
      this.searchActive = true;
      this.pathMode = true;
      this.pairLabels = { start: providerName, end: seekerName };
      this.intent = `${providerName} → ${seekerName}（你撮合）`;
      this.focusSummary = null;
      this.showIntros = false;
      this.highlightPath(synthetic, "t-0-" + seeker_id);
    },

    /* -------- filters -------- */
    toggleIndustry(name) {
      const idx = this.filters.industries.indexOf(name);
      if (idx >= 0) this.filters.industries.splice(idx, 1);
      else this.filters.industries.push(name);
      applyFilters(this.filters);
    },
    setMinStrength(v) {
      this.filters.minStrength = +v;
      applyFilters(this.filters);
    },
    clearFilters() {
      this.filters = { industries: [], minStrength: 0, search: "" };
      applyFilters(this.filters);
    },

    /* -------- add contact -------- */
    openAdd() {
      this.newPerson = this.emptyPerson(); this.showAdd = true;
    },
    async submitAdd() {
      const p = this.newPerson;
      if (!p.name.trim()) { this.notify("请填写姓名", "error"); return; }
      const splitList = (s) => (s || "").split(/[;,；,]/).map((x) => x.trim()).filter(Boolean);
      try {
        await api("/api/people", {
          method: "POST",
          body: {
            name: p.name.trim(),
            bio: p.bio || null, notes: p.notes || null,
            tags: splitList(p.tags),
            skills: splitList(p.skills),
            companies: splitList(p.companies),
            cities: splitList(p.cities),
            needs: splitList(p.needs),
            strength_to_me: +p.strength_to_me,
            relation_context: p.relation_context || null,
            frequency: p.frequency,
            embed: p.embed,
          },
        });
        this.showAdd = false;
        this.notify(`已添加 ${p.name}`);
        await this.loadGraph(); await this.loadStats();
      } catch (e) {
        this.notify(e.message, "error");
      }
    },
    async deleteCurrent() {
      if (!this.detail) return;
      if (!confirm(`删除 ${this.detail.name}？此操作无法撤销。`)) return;
      try {
        await api(`/api/people/${this.detail.id}`, { method: "DELETE" });
        this.notify(`已删除 ${this.detail.name}`);
        this.detail = null;
        await this.loadGraph(); await this.loadStats();
      } catch (e) {
        this.notify(e.message, "error");
      }
    },

    /* -------- AI enrich helpers -------- */

    /** A: "AI 解析背景" button on the add-contact modal.
     *
     * Posts the in-flight form contents to /api/enrich/preview, then
     * MERGES the LLM proposals into the existing chip strings (we never
     * overwrite what the user typed by hand). UI feedback lives in
     * `aiPreviewBusy / aiPreviewError / aiPreviewSummary`.
     */
    async aiPreviewForNewPerson() {
      if (this.aiPreviewBusy) return;
      const p = this.newPerson;
      const splitList = (s) => (s || "").split(/[;,；,]/).map((x) => x.trim()).filter(Boolean);
      const rawTags = splitList(p.tags);
      const rawCities = splitList(p.cities);
      const knownCompanies = splitList(p.companies);
      const knownCities = rawCities.slice();
      const knownTags = rawTags.slice();

      this.aiPreviewBusy = true;
      this.aiPreviewError = null;
      this.aiPreviewSummary = null;
      try {
        const diff = await api("/api/enrich/preview", {
          method: "POST",
          body: {
            name: p.name || null,
            bio: p.bio || null,
            notes: p.notes || null,
            raw_tags: rawTags,
            raw_cities: rawCities,
            known_companies: knownCompanies,
            known_cities: knownCities,
            known_tags: knownTags,
          },
        });
        if (diff.error) {
          this.aiPreviewError = diff.error;
          return;
        }
        // Merge into the comma-separated string fields. We dedupe
        // case-sensitively because Chinese strings have no case anyway,
        // and we don't want to silently downcase company names.
        const merge = (current, additions) => {
          const have = new Set(splitList(current));
          const out = splitList(current);
          for (const a of additions || []) {
            if (a && !have.has(a)) {
              have.add(a);
              out.push(a);
            }
          }
          return out.join("; ");
        };
        p.companies = merge(p.companies, diff.add_companies);
        p.cities = merge(p.cities, diff.add_cities);
        // Titles + extra_tags both flow into the tags chip — they're
        // useful retrieval signal regardless of whether the model
        // labelled them job-titles or general descriptors.
        p.tags = merge(p.tags, [...(diff.add_titles || []), ...(diff.add_tags || [])]);

        const counts = [
          ["公司", (diff.add_companies || []).length],
          ["城市", (diff.add_cities || []).length],
          ["职位", (diff.add_titles || []).length],
          ["标签", (diff.add_tags || []).length],
        ].filter(([, n]) => n > 0).map(([k, n]) => `${k} +${n}`).join("，");
        this.aiPreviewSummary = counts ? `已回填：${counts}` : "AI 没有抽到新信息。";
      } catch (e) {
        this.aiPreviewError = e.message || "AI 解析失败";
      } finally {
        this.aiPreviewBusy = false;
      }
    },

    /** C: "AI 重新解析" button in the person detail panel.
     *
     * Server-side default is `only_missing=true`, but for an explicit
     * user action we set it to false so they can force a reparse even
     * if companies/cities already have values.
     */
    async aiReparseCurrent() {
      if (!this.detail || this.detail.is_me) return;
      const pid = this.detail.id;
      this.aiPersonBusy = pid;
      try {
        const updated = await api(`/api/enrich/person/${pid}?only_missing=false`, {
          method: "POST",
          body: {},
        });
        // Hot-swap the detail panel and refresh the graph payload so the
        // node tooltip / labels reflect the new fields.
        this.detail = updated;
        this.notify(`已用 AI 解析「${updated.name}」`);
        await this.loadGraph();
      } catch (e) {
        this.notify(`AI 解析失败：${e.message}`, "error");
      } finally {
        this.aiPersonBusy = null;
      }
    },

    /** D: "批量 AI 解析" button in the topbar. Opens the modal; doesn't
     *  auto-start so the user can read the privacy/scope copy first.
     */
    openBatchEnrich() {
      this.showBatchEnrich = true;
      // Re-attach polling if a job is still running from before this
      // modal was last closed.
      if (
        this.batchJob
        && (this.batchJob.status === "pending" || this.batchJob.status === "running")
        && !this._batchPollTimer
      ) {
        this._pollBatchJob();
      }
    },

    closeBatchEnrich() {
      this.showBatchEnrich = false;
      // We DON'T clear _batchPollTimer here — let it keep polling so the
      // notify() at completion still fires, and so the user reopens to
      // see the final state.
    },

    async startBatchEnrich() {
      if (this.batchStarting) return;
      this.batchStarting = true;
      try {
        const job = await api("/api/enrich/start", {
          method: "POST",
          body: { only_missing: this.batchOnlyMissing },
        });
        this.batchJob = job;
        if (this._batchPollTimer) {
          clearTimeout(this._batchPollTimer);
          this._batchPollTimer = null;
        }
        this._pollBatchJob();
      } catch (e) {
        this.notify(`启动失败：${e.message}`, "error");
      } finally {
        this.batchStarting = false;
      }
    },

    async _pollBatchJob() {
      if (!this.batchJob) return;
      const taskId = this.batchJob.task_id;
      try {
        const next = await api(`/api/enrich/status/${taskId}`);
        // Defend against a newer task being kicked off while we were
        // mid-poll.
        if (this.batchJob && this.batchJob.task_id === taskId) {
          this.batchJob = next;
          if (next.status === "done" || next.status === "error") {
            this._batchPollTimer = null;
            if (next.status === "done") {
              this.notify(`AI 解析完成：更新 ${next.touched} 条`);
              await this.loadGraph();
              await this.loadStats();
            } else {
              this.notify(`AI 解析失败：${next.error_message || "未知错误"}`, "error");
            }
            return;
          }
        }
      } catch (e) {
        this.notify(`进度获取失败：${e.message}`, "error");
        this._batchPollTimer = null;
        return;
      }
      this._batchPollTimer = setTimeout(() => this._pollBatchJob(), 2000);
    },

    batchProgressPct() {
      const j = this.batchJob;
      if (!j || !j.total) return 0;
      return Math.min(100, Math.round((j.processed / j.total) * 100));
    },

    batchStatusLabel(s) {
      return ({
        pending: "待启动",
        running: "解析中",
        done: "已完成",
        error: "失败",
      })[s] || s;
    },

    mountLabel(slug) {
      const m = (this.mounts || []).find((x) => x.slug === slug);
      return m ? m.display_name : (slug || "当前网络");
    },

    /* -------- NL relationship parse (modal) -------- */

    openRelationParse() {
      this.showRelationParse = true;
      this.relationParseError = null;
    },
    closeRelationParse() {
      this.showRelationParse = false;
    },
    resetRelationParse() {
      this.relationParseText = "";
      this.relationProposals = [];
      this.relationContext = {};
      this.relationUnknown = [];
      this.relationParseError = null;
    },

    selectedProposalCount() {
      return this.relationProposals.filter((p) => p.selected && p.strength).length;
    },

    toggleSelectAllProposals(on) {
      for (const p of this.relationProposals) {
        // never silently auto-select rows missing strength — user must
        // pick a value before applying. Toggle only the rows that are
        // actually applyable.
        if (on && p.strength) p.selected = true;
        else if (!on) p.selected = false;
      }
    },

    async parseRelations() {
      const text = (this.relationParseText || "").trim();
      if (!text || this.relationParseBusy) return;
      this.relationParseBusy = true;
      this.relationParseError = null;
      this.relationProposals = [];
      this.relationContext = {};
      this.relationUnknown = [];
      try {
        const resp = await api("/api/relationships/parse", {
          method: "POST",
          body: { text },
        });
        if (resp.error) {
          this.relationParseError = resp.error;
        }
        this.relationContext = resp.context_for || {};
        this.relationUnknown = resp.unknown_mentions || [];
        const proposals = (resp.proposals || []).map((p) => ({
          ...p,
          // Auto-select only when LLM gave a strength AND there isn't a
          // higher-priority existing edge that the user might want to
          // think twice about overwriting.
          selected: Boolean(
            p.strength
            && (!p.existing_edge || p.existing_edge.source !== "manual"),
          ),
        }));
        this.relationProposals = proposals;
        if (
          !this.relationParseError
          && proposals.length === 0
          && this.relationUnknown.length === 0
        ) {
          this.relationParseError = "AI 没解析出可入库的关系。试试更具体的描述。";
        }
      } catch (e) {
        this.relationParseError = e.message || "解析失败";
      } finally {
        this.relationParseBusy = false;
      }
    },

    async applySelectedRelations() {
      if (this.relationApplyBusy) return;
      const edges = this.relationProposals
        .filter((p) => p.selected && p.strength)
        .map((p) => ({
          a_id: p.a_id,
          b_id: p.b_id,
          strength: +p.strength,
          context: p.context || null,
          frequency: p.frequency || "yearly",
        }));
      if (edges.length === 0) {
        this.notify("请至少选一条且填好强度", "error");
        return;
      }
      this.relationApplyBusy = true;
      try {
        const resp = await api("/api/relationships/apply", {
          method: "POST",
          body: { edges },
        });
        this.notify(`已写入 ${resp.applied} 条`
          + (resp.skipped ? `（跳过 ${resp.skipped}）` : ""));
        this.closeRelationParse();
        this.resetRelationParse();
        await this.loadGraph();
        if (this.showRelationsDrawer) {
          await this.loadRelationships();
        }
      } catch (e) {
        this.notify(`入库失败：${e.message}`, "error");
      } finally {
        this.relationApplyBusy = false;
      }
    },

    /* -------- relationships drawer -------- */

    toggleRelationsDrawer() {
      this.showRelationsDrawer = !this.showRelationsDrawer;
      if (this.showRelationsDrawer && this.relationships.length === 0) {
        this.loadRelationships();
      }
    },

    toggleRelationSourceFilter(src) {
      const arr = this.relationsFilter.sources;
      const idx = arr.indexOf(src);
      if (idx >= 0) arr.splice(idx, 1);
      else arr.push(src);
      this.loadRelationships();
    },

    relationSourceLabel(src) {
      return ({
        manual: "手工",
        colleague_inferred: "同事推断",
        ai_inferred: "AI 推断",
      })[src] || src;
    },

    frequencyLabel(f) {
      return ({
        weekly: "每周",
        monthly: "每月",
        quarterly: "每季",
        yearly: "每年",
        rare: "极少",
      })[f] || f;
    },

    _relationsQueryString(offset) {
      const f = this.relationsFilter;
      const params = new URLSearchParams();
      if (f.q && f.q.trim()) params.set("q", f.q.trim());
      if (f.min_strength) params.set("min_strength", String(f.min_strength));
      if (!f.include_me) params.set("include_me", "false");
      if (f.sources && f.sources.length) {
        params.set("source", f.sources.join(","));
      }
      params.set("offset", String(offset));
      params.set("limit", String(this._relationsPageSize));
      return `/api/relationships?${params.toString()}`;
    },

    async loadRelationships() {
      this.relationsLoading = true;
      try {
        const resp = await api(this._relationsQueryString(0));
        this.relationships = resp.items || [];
        this.relationsTotal = resp.total || 0;
      } catch (e) {
        this.notify(`加载关系失败：${e.message}`, "error");
      } finally {
        this.relationsLoading = false;
      }
    },

    async loadMoreRelationships() {
      if (this.relationsLoadingMore) return;
      this.relationsLoadingMore = true;
      try {
        const resp = await api(this._relationsQueryString(this.relationships.length));
        this.relationships = this.relationships.concat(resp.items || []);
        this.relationsTotal = resp.total || this.relationsTotal;
      } catch (e) {
        this.notify(`加载更多失败：${e.message}`, "error");
      } finally {
        this.relationsLoadingMore = false;
      }
    },

    startEditRelation(r) {
      this.editingRelationId = r.id;
      this.editingRelation = {
        strength: r.strength,
        context: r.context || "",
        frequency: r.frequency,
      };
    },

    cancelEditRelation() {
      this.editingRelationId = null;
      this.editingRelation = null;
    },

    async saveEditRelation() {
      if (!this.editingRelationId || this.editingRelationBusy) return;
      const id = this.editingRelationId;
      this.editingRelationBusy = true;
      try {
        const updated = await api(`/api/relationships/${id}`, {
          method: "PATCH",
          body: {
            strength: this.editingRelation.strength,
            context: this.editingRelation.context || null,
            frequency: this.editingRelation.frequency,
          },
        });
        const idx = this.relationships.findIndex((x) => x.id === id);
        if (idx >= 0) this.relationships.splice(idx, 1, updated);
        this.cancelEditRelation();
        await this.loadGraph();
        this.notify("已更新");
      } catch (e) {
        this.notify(`保存失败：${e.message}`, "error");
      } finally {
        this.editingRelationBusy = false;
      }
    },

    async deleteRelation(r) {
      if (!confirm(`删除 ${r.a_name} ↔ ${r.b_name} 这条边？`)) return;
      try {
        await api(`/api/relationships/${r.id}`, { method: "DELETE" });
        this.relationships = this.relationships.filter((x) => x.id !== r.id);
        this.relationsTotal = Math.max(0, this.relationsTotal - 1);
        await this.loadGraph();
        this.notify("已删除");
      } catch (e) {
        this.notify(`删除失败：${e.message}`, "error");
      }
    },

    /* 简介里已有「公司 / 城市」时不再重复展示结构化 companies / cities 区块 */
    bioKeysFromBio(text) {
      const pairs = this.bioPairs(text);
      if (!pairs) return null;
      return new Set(pairs.map((p) => p.key));
    },
    detailShowExtraCompany(detail) {
      const keys = this.bioKeysFromBio(detail?.bio);
      if (!keys) return true;
      return !keys.has("公司");
    },
    detailShowExtraCity(detail) {
      const keys = this.bioKeysFromBio(detail?.bio);
      if (!keys) return true;
      return !keys.has("城市");
    },

    /* -------- bio formatting --------
     * bio 在导入时常是 "key：value · key：value · ..." 这种一行 KV 串。
     * 如果识别成 KV 序列就返回成对数组，给详情面板渲染成两列网格；
     * 否则返回 null，调用方按整段文本回退渲染。
     *
     * 判定标准：用 `·`（U+00B7 / U+30FB）切分后≥2 段，且其中每段都
     * 包含一个全角 / 半角冒号；冒号前后非空。
     */
    bioPairs(text) {
      if (!text || typeof text !== "string") return null;
      const segments = text
        .split(/[·・]/)               // both U+00B7 and U+30FB
        .map((s) => s.trim())
        .filter(Boolean);
      if (segments.length < 2) return null;
      const pairs = [];
      for (const seg of segments) {
        const m = seg.match(/^([^：:]{1,8})[：:]\s*(.+)$/);
        if (!m) return null;          // 任何一段不是 K:V 就放弃，避免误识别
        const key = m[1].trim();
        const value = m[2].trim();
        if (!key || !value) return null;
        pairs.push({ key, value });
      }
      return pairs;
    },

    /* -------- toast -------- */
    notify(msg, type = "info", durationMs = 2800) {
      this.toast = { msg, type };
      setTimeout(() => { if (this.toast && this.toast.msg === msg) this.toast = null; }, durationMs);
    },
  };
}
