/* ============================================================
 * Lodestar SPA — main controller
 *
 * Architecture:
 *   - Cytoscape for the network graph (vanilla, no Alpine binding)
 *   - Alpine.js for reactive UI panels around it
 *   - All state in window.app for easy debugging
 * ============================================================ */

const ME_COLOR = "#f7f8f8";
const DIM_COLOR = "rgba(208, 214, 224, 0.10)";
const EDGE_COLOR = "rgba(255, 255, 255, 0.10)";
const ACCENT = "#9ca4ae";
const ACCENT_SOFT = "rgba(208, 214, 224, 0.42)";
const PATH_EDGE = "#d0d6e0";
const AMBIENT_LABEL_COUNT = 5;

const INDUSTRIES = [
  ["投资金融", "#5f756f"],
  ["技术研发", "#6d6a7e"],
  ["政府国资", "#8f8365"],
  ["销售渠道", "#8b6f73"],
  ["创业老板", "#8f765e"],
  ["学术研究", "#5f6f82"],
  ["医疗大健康", "#5e7568"],
  ["制造地产", "#636b78"],
  ["其他", "#747882"],
  ["我", "#8f98a6"],
];

/* ---------------------------------------- API helpers */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

/* ---------------------------------------- Cytoscape setup */
let cy = null;
function buildGraph(payload) {
  const meId = payload.me_id != null ? String(payload.me_id) : null;
  const elements = [];
  for (const n of payload.nodes) {
    elements.push({
      group: "nodes",
      data: {
        id: String(n.id),
        label: n.label,
        color: n.color,
        glow: n.glow,
        size: n.size,
        industry: n.industry,
        is_me: n.is_me,
        strength_to_me: n.strength_to_me,
        always_label: n.is_me || (n.strength_to_me || 0) >= 5,
        raw: n,
      },
    });
  }
  for (const e of payload.edges) {
    const src = String(e.source);
    const tgt = String(e.target);
    const touchesMe = Boolean(meId && (src === meId || tgt === meId));
    elements.push({
      group: "edges",
      data: {
        id: e.id,
        source: src,
        target: tgt,
        strength: e.strength,
        width: 0.8 + e.strength * 0.5,
        is_me_edge: touchesMe,
      },
    });
  }
  return elements;
}

function resetGraphState() {
  if (!cy) return;
  cy.elements().removeClass(
    "highlight dim endpoint ambient-muted focus-node focus-neighbor focus-edge intent-node intent-edge label-visible label-hidden"
  );
}

function collectAmbientLabelIds() {
  if (!cy) return new Set();
  const visibleNodes = cy.nodes().filter((n) => n.style("display") !== "none");
  const topNodes = visibleNodes
    .filter((n) => !n.data("is_me"))
    .sort((a, b) => {
      const strengthDelta = (b.data("strength_to_me") || 0) - (a.data("strength_to_me") || 0);
      if (strengthDelta !== 0) return strengthDelta;
      return (b.data("size") || 0) - (a.data("size") || 0);
    })
    .slice(0, AMBIENT_LABEL_COUNT);

  const ids = new Set(topNodes.map((n) => n.id()));
  visibleNodes.forEach((n) => {
    if (n.data("always_label")) ids.add(n.id());
  });
  return ids;
}

function hideMeEdgesAmbient() {
  if (!cy) return;
  cy.edges().forEach((e) => {
    if (!e.data("is_me_edge")) return;
    e.style("display", "none");
  });
}

function applyAmbientState() {
  if (!cy) return;
  const labelIds = collectAmbientLabelIds();
  cy.batch(() => {
    resetGraphState();
    cy.nodes().forEach((n) => {
      if (n.style("display") === "none") return;
      if (labelIds.has(n.id())) n.addClass("label-visible");
      else n.addClass("label-hidden");
    });
    hideMeEdgesAmbient();
  });
}

function buildFocusSummary(raw, nodeEle) {
  const neighbors = nodeEle ? nodeEle.neighborhood("node") : [];
  const neighborCount = nodeEle ? neighbors.length : 0;
  const snippet = raw.bio
    || (raw.skills && raw.skills.length ? `擅长 ${raw.skills.slice(0, 2).join(" / ")}` : "")
    || (raw.tags && raw.tags.length ? `关键词：${raw.tags.slice(0, 3).join(" / ")}` : "")
    || (raw.companies && raw.companies.length ? raw.companies.slice(0, 2).join(" · ") : "")
    || "点击查看完整资料";

  let reason = "点击可展开完整资料";
  if (raw.is_me) reason = "从这里开始，输入目标后点亮路径";
  else if (raw.needs && raw.needs.length) reason = `他现在可能在找：${raw.needs.slice(0, 2).join(" / ")}`;
  else if (raw.skills && raw.skills.length) reason = `你可以和他聊：${raw.skills.slice(0, 2).join(" / ")}`;
  else if (raw.tags && raw.tags.length) reason = `这个人和 ${raw.tags.slice(0, 2).join(" / ")} 有关`;

  return {
    id: raw.id,
    name: raw.name || raw.label,
    industry: raw.industry || "其他",
    color: raw.color,
    strength: raw.strength_to_me || 0,
    snippet,
    reason,
    neighborCount,
  };
}

function applyFocusState(nodeId) {
  if (!cy) return null;
  const center = cy.getElementById(String(nodeId));
  if (!center || center.length === 0) {
    applyAmbientState();
    return null;
  }
  const meNode = cy.nodes("[?is_me]");
  const meId = meNode.length > 0 ? meNode.first().id() : (window.app?.graph?.me_id != null ? String(window.app.graph.me_id) : null);
  const neighborNodes = center.neighborhood("node");
  const connectedEdges = center.connectedEdges();
  const visibleNodeIds = new Set([center.id(), ...neighborNodes.map((n) => n.id())]);
  const visibleEdgeIds = new Set(connectedEdges.map((e) => e.id()));

  cy.batch(() => {
    resetGraphState();
    cy.nodes().forEach((n) => {
      if (n.style("display") === "none") return;
      if (n.id() === center.id()) n.addClass("focus-node label-visible");
      else if (visibleNodeIds.has(n.id())) n.addClass("focus-neighbor label-visible");
      else n.addClass("ambient-muted label-hidden");
    });
    cy.edges().forEach((e) => {
      if (!e.data("is_me_edge")) return;
      const s = e.data("source");
      const t = e.data("target");
      const srcN = cy.getElementById(s);
      const tgtN = cy.getElementById(t);
      const epOk = srcN.style("display") !== "none" && tgtN.style("display") !== "none";
      if (!epOk) {
        e.style("display", "none");
        return;
      }
      let show = false;
      if (meId && center.id() === meId) show = true;
      else if (meId) show = (s === meId && t === center.id()) || (t === meId && s === center.id());
      e.style("display", show ? "element" : "none");
    });
    cy.edges().forEach((e) => {
      if (e.style("display") === "none") return;
      if (visibleEdgeIds.has(e.id())) e.addClass("focus-edge");
      else e.addClass("ambient-muted");
    });
  });
  return buildFocusSummary(center.data("raw"), center);
}

function applyIntentState(nodeIds, edgeIds, options = {}) {
  if (!cy) return;
  const nodeSet = new Set(nodeIds.map(String));
  const edgeSet = new Set(edgeIds);

  cy.batch(() => {
    resetGraphState();
    cy.edges().forEach((e) => {
      if (!e.data("is_me_edge")) return;
      const srcN = cy.getElementById(e.data("source"));
      const tgtN = cy.getElementById(e.data("target"));
      const epOk = srcN.style("display") !== "none" && tgtN.style("display") !== "none";
      const onPath = edgeSet.has(e.id());
      e.style("display", epOk && onPath ? "element" : "none");
    });
    cy.nodes().forEach((n) => {
      if (n.style("display") === "none") return;
      if (nodeSet.has(n.id())) n.addClass("intent-node label-visible");
      else n.addClass("ambient-muted label-hidden");
    });
    cy.edges().forEach((e) => {
      if (e.style("display") === "none") return;
      if (edgeSet.has(e.id())) e.addClass("intent-edge");
      else e.addClass("ambient-muted");
    });
    if (options.endpoints) {
      options.endpoints.forEach((id) => {
        const n = cy.getElementById(String(id));
        if (n) n.addClass("endpoint");
      });
    }
  });

  if (nodeIds.length > 0 && options.fit !== false) {
    const targets = cy.nodes().filter((n) => nodeSet.has(n.id()));
    cy.animate({ fit: { eles: targets, padding: 120 }, duration: 600, easing: "ease-out-cubic" });
  }
}

function initCytoscape(payload) {
  if (cy) cy.destroy();
  cy = cytoscape({
    container: document.getElementById("cy"),
    elements: buildGraph(payload),
    style: [
      {
        selector: "node",
        style: {
          "background-color": "data(color)",
          "background-opacity": 0.42,
          "label": "data(label)",
          "color": "#d8dde6",
          "font-size": 14,
          "font-family": "Inter, system-ui, sans-serif",
          "font-weight": 510,
          "text-outline-color": "#08090a",
          "text-outline-width": 2.5,
          "text-opacity": 0,
          "text-margin-y": 6,
          "text-valign": "bottom",
          "text-halign": "center",
          "border-width": 1,
          "border-color": "rgba(255,255,255,0.05)",
          "width": "data(size)",
          "height": "data(size)",
          "transition-property": "background-color, border-color, border-width, opacity, width, height, text-opacity",
          "transition-duration": "0.2s",
        },
      },
      {
        selector: "node[?is_me]",
        style: {
          "background-color": ME_COLOR,
          "background-opacity": 0.96,
          "border-color": ACCENT,
          "border-width": 1.5,
          "font-size": 16,
          "color": "#08090a",
          "font-weight": 590,
          "text-opacity": 1,
        },
      },
      {
        selector: "node.label-visible",
        style: {
          "text-opacity": 1,
        },
      },
      {
        selector: "node.label-hidden",
        style: {
          "text-opacity": 0,
        },
      },
      {
        selector: "node:selected",
        style: {
          "border-width": 1.5,
          "border-color": "rgba(255,255,255,0.7)",
        },
      },
      {
        selector: "node.ambient-muted",
        style: {
          "opacity": 0.12,
          "background-opacity": 0.18,
        },
      },
      {
        selector: "node.focus-node",
        style: {
          "opacity": 1,
          "background-opacity": 0.95,
          "border-color": "rgba(228, 231, 237, 0.55)",
          "border-width": 1.5,
          "color": "#f7f8f8",
          "shadow-color": "#000000",
          "shadow-blur": 6,
          "shadow-opacity": 0.32,
          "z-index": 999,
        },
      },
      {
        selector: "node.focus-neighbor",
        style: {
          "opacity": 0.78,
          "background-opacity": 0.66,
          "border-color": "rgba(255,255,255,0.14)",
          "border-width": 1.2,
          "color": "#d0d6e0",
          "z-index": 998,
        },
      },
      {
        selector: "node.intent-node",
        style: {
          "opacity": 1,
          "background-opacity": 0.95,
          "border-color": "rgba(247, 248, 248, 0.6)",
          "border-width": 1.5,
          "color": "#f7f8f8",
          "shadow-color": "#000000",
          "shadow-blur": 8,
          "shadow-opacity": 0.32,
          "z-index": 999,
        },
      },
      {
        selector: "node.endpoint",
        style: {
          "border-color": "rgba(247, 248, 248, 0.7)",
          "border-width": 1.5,
          "color": "#f7f8f8",
          "shadow-color": "#000000",
          "shadow-blur": 6,
          "shadow-opacity": 0.36,
        },
      },
      {
        selector: "edge",
        style: {
          "width": "data(width)",
          "line-color": EDGE_COLOR,
          "curve-style": "bezier",
          "target-arrow-shape": "none",
          "transition-property": "line-color, width, opacity",
          "transition-duration": "0.2s",
          "opacity": 0.18,
        },
      },
      {
        selector: "edge[?is_me_edge]",
        style: {
          display: "none",
        },
      },
      {
        selector: "edge.ambient-muted",
        style: {
          "opacity": 0.03,
        },
      },
      {
        selector: "edge.focus-edge",
        style: {
          "line-color": "rgba(208, 214, 224, 0.55)",
          "width": 1.6,
          "opacity": 0.7,
          "z-index": 999,
        },
      },
      {
        selector: "edge.intent-edge",
        style: {
          "line-color": PATH_EDGE,
          "width": 1.8,
          "opacity": 0.95,
          "z-index": 999,
        },
      },
    ],
    layout: {
      name: "cose",
      idealEdgeLength: 100,
      nodeOverlap: 20,
      refresh: 20,
      fit: true,
      padding: 60,
      randomize: false,
      componentSpacing: 100,
      nodeRepulsion: 8000,
      edgeElasticity: 100,
      nestingFactor: 5,
      gravity: 60,
      numIter: 1200,
      animate: "end",
      animationDuration: 800,
    },
    minZoom: 0.2,
    maxZoom: 4,
    wheelSensitivity: 0.2,
  });

  cy.on("mouseover", "node", (e) => {
    window.app?.handleNodeHover(e.target.data("raw"), e.target);
  });
  cy.on("mouseout", "node", () => {
    window.app?.handleNodeExit();
  });

  cy.on("tap", "node", (e) => {
    const data = e.target.data("raw");
    window.app.handleNodeClick(data);
  });
  cy.on("tap", (e) => {
    if (e.target === cy) window.app.clearDetail();
  });
}

function clearHighlights() {
  if (!cy) return;
  applyAmbientState();
}

function applyHighlight(nodeIds, edgeIds, options = {}) {
  applyIntentState(nodeIds, edgeIds, options);
}

function applyFilters(filters) {
  if (!cy) return;
  cy.batch(() => {
    cy.nodes().forEach((n) => {
      const raw = n.data("raw");
      if (!raw) return;
      let visible = true;
      if (filters.industries.length > 0 && !raw.is_me) {
        if (!filters.industries.includes(raw.industry)) visible = false;
      }
      if (filters.minStrength > 0 && !raw.is_me) {
        const s = raw.strength_to_me || 0;
        if (s < filters.minStrength) visible = false;
      }
      if (filters.search) {
        const q = filters.search.toLowerCase();
        const hay = [
          raw.label, raw.bio || "", ...(raw.tags || []),
          ...(raw.companies || []), ...(raw.cities || []),
          ...(raw.skills || []), ...(raw.needs || []),
        ].join(" ").toLowerCase();
        if (!hay.includes(q)) visible = false;
      }
      n.style("display", visible ? "element" : "none");
    });
    cy.edges().forEach((e) => {
      const src = cy.getElementById(e.data("source"));
      const tgt = cy.getElementById(e.data("target"));
      const endpointsVisible = src.style("display") !== "none" && tgt.style("display") !== "none";
      if (e.data("is_me_edge")) {
        e.style("display", "none");
        return;
      }
      e.style("display", endpointsVisible ? "element" : "none");
    });
  });
  window.app?.syncCurrentView();
}

/* ---------------------------------------- Alpine app */
function appState() {
  return {
    /* ---- data ---- */
    graph: { nodes: [], edges: [], me_id: null },
    detail: null,
    paths: [],          // legacy combined results
    targets: [],        // multi-hop introductions (path_kind == 'target')
    direct: [],         // 1-hop strong (path_kind == 'direct')
    weak: [],           // 1-hop weak (path_kind == 'weak')
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
    ambientHint: "输入目标以点亮路径，或悬停查看局部关系",
    hoverExitTimer: null,
    noLLM: false,
    showFilters: false,
    showStats: false,
    showHelp: false,
    showAdd: false,
    showIntros: false,
    twoPersonMode: false,
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

    industriesList: INDUSTRIES,

    init() {
      window.app = this;
      this.newPerson = this.emptyPerson();
      this.loadGraph();
      this.loadStats();
      this.bindShortcuts();
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
      const chart = echarts.init(el, null, { renderer: "canvas" });
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
        this.notify("请先输入一句目标，再按 Enter 或点击「查询」", "info", 3200);
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
        this.targets = resp.targets || [];
        this.direct = resp.direct || [];
        this.weak = resp.weak || [];
        this.pathMode = false;
        this.pairLabels = null;
        const nResults =
          this.targets.length + this.direct.length + this.weak.length;

        if (nResults === 0) {
          this.activePathKey = null;
          this.searchActive = false;
          this.focusSummary = null;
          this.focusedNodeId = null;
          this.enterAmbientMode();
          const summary = (this.intent || "").trim();
          const hint = summary
            ? `已理解目标：${summary}。库中暂无匹配路径，可换说法、开启 RAW，或补充联系人简介与标签。`
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

        // If we have any target-type result, auto-focus the top target so
        // the user immediately sees the killer use case (multi-hop intro).
        if (this.targets.length > 0) {
          const top = this.targets[0];
          this.highlightPath(top, 't-0-' + top.target_id);
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
      this.paths = []; this.targets = []; this.direct = []; this.weak = [];
      this.activePathKey = null;
      this.searchActive = false;
      this.pathMode = false;
      this.pairLabels = null;
      this.enterAmbientMode();
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
        } else if (!this.pathEnd && node.id !== this.pathStart.id) {
          this.pathEnd = node;
          await this.runTwoPersonPath();
        } else {
          this.pathStart = node; this.pathEnd = null;
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
    handleNodeExit() {
      if (this.searchActive || this.twoPersonMode || this.detail) return;
      clearTimeout(this.hoverExitTimer);
      this.hoverExitTimer = setTimeout(() => {
        if (!this.searchActive && !this.twoPersonMode && !this.detail) this.enterAmbientMode();
      }, 70);
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
        cy.animate({ center: { eles: cy.getElementById(String(id)) }, duration: 400 });
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
      this.searchActive = false;
      this.focusSummary = null;
      this.viewMode = "ambient";
      clearHighlights(); this.notify("点击起点和终点");
    },
    exitTwoPerson() {
      this.twoPersonMode = false; this.pathStart = null; this.pathEnd = null;
      if (!this.searchActive) this.enterAmbientMode();
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
        this.targets = resp.paths;
        this.direct = [];
        this.weak = [];
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
      this.targets = [synthetic];
      this.direct = [];
      this.weak = [];
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

    /* -------- toast -------- */
    notify(msg, type = "info", durationMs = 2800) {
      this.toast = { msg, type };
      setTimeout(() => { if (this.toast && this.toast.msg === msg) this.toast = null; }, durationMs);
    },
  };
}
