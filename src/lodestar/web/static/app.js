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

/* ---------------------------------------- API helpers
 * 一人一库（mount router）：
 *   - 每个 mount 独立挂在 `/r/<slug>/` 下，自己一个 db、自己一个密码、
 *     自己一份 unlock_secret。
 *   - SPA 跑在某个 mount 的 sub-app 里，绝对不会跨 mount 调 API。
 *   - "切 tab 必重输"语义靠**整页跳转**实现：换 mount = window.location
 *     assign 到另一个 `/r/<slug>/`，浏览器丢掉所有 in-memory state，
 *     新页面 init 时重新跑 unlock flow，自然必须再输一次密码。
 *   - unlock token 只活在 window.app.unlockToken 里，不写 storage —
 *     刷新即丢、切 mount 即丢、关 tab 即丢。 */

/** Detect "/r/<slug>/" prefix from current URL. Returns "" for root SPA. */
function _detectMountPrefix() {
  const m = window.location.pathname.match(/^\/r\/([^/]+)\//);
  return m ? `/r/${m[1]}` : "";
}
function _detectMountSlug() {
  const m = window.location.pathname.match(/^\/r\/([^/]+)\//);
  return m ? m[1] : null;
}

const MOUNT_PREFIX = _detectMountPrefix();
const MOUNT_SLUG = _detectMountSlug();

/** Resolve a path: `/api/...` → `/r/<slug>/api/...`, root `/api/mounts`
 *  stays absolute. */
function withMount(path) {
  if (!path.startsWith("/api/")) return path;
  // `/api/mounts` is a root-level endpoint shared across all mounts.
  if (path === "/api/mounts" || path.startsWith("/api/mounts/")) return path;
  if (!MOUNT_PREFIX) return path;
  return MOUNT_PREFIX + path;
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const tok = window.app && window.app.unlockToken;
  if (tok) headers["X-Mount-Unlock"] = tok;
  const res = await fetch(withMount(path), {
    headers,
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text();
    if (res.status === 401) {
      try {
        const j = JSON.parse(text);
        const d = j.detail;
        const code = typeof d === "object" ? d.code : null;
        if (code === "mount_locked") {
          // Token expired or never issued for this mount — drop it
          // and re-challenge.
          if (window.app) {
            window.app.unlockToken = null;
            window.app.locked = true;
            window.app.openUnlockModal();
          }
        }
      } catch (_) { /* ignore */ }
    }
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
  /* 速览卡：不要整段 bio（会和详情「简介」网格撞脸），只给一行「我在图里是谁」。 */
  let headline = "";
  if (raw.is_me) {
    headline = "你的网络中心 · 从这里发起搜索";
  } else if (raw.companies && raw.companies.length && raw.cities && raw.cities.length) {
    headline = `${raw.companies[0]} · ${raw.cities[0]}`;
  } else if (raw.companies && raw.companies.length) {
    headline = raw.companies[0];
  } else if (raw.cities && raw.cities.length) {
    headline = raw.cities[0];
  } else if (raw.skills && raw.skills.length) {
    headline = raw.skills.slice(0, 2).join(" · ");
  } else if (raw.tags && raw.tags.length) {
    headline = raw.tags.slice(0, 2).join(" · ");
  } else {
    headline = "点下方打开完整档案";
  }

  let reason = "";
  if (raw.is_me) reason = "输入一句话目标，看推荐路径与引荐链";
  else if (raw.needs && raw.needs.length) reason = `可能在找：${raw.needs.slice(0, 2).join(" · ")}`;
  else if (raw.skills && raw.skills.length) reason = `可聊：${raw.skills.slice(0, 2).join(" · ")}`;
  else if (raw.tags && raw.tags.length) reason = `关联：${raw.tags.slice(0, 2).join(" · ")}`;
  else reason = "悬停速览 · 点按钮看结构化资料与关系";

  return {
    id: raw.id,
    name: raw.name || raw.label,
    industry: raw.industry || "其他",
    color: raw.color,
    strength: raw.strength_to_me || 0,
    headline,
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
  const meIdRaw = meNode.length > 0 ? meNode.first().id() : (window.app?.graph?.me_id != null ? window.app.graph.me_id : null);
  const meIdStr = meIdRaw != null ? String(meIdRaw) : null;
  const centerIdStr = String(center.id());
  const neighborNodes = center.neighborhood("node");
  const connectedEdges = center.connectedEdges();
  const visibleNodeIds = new Set([centerIdStr, ...neighborNodes.map((n) => String(n.id()))]);
  const visibleEdgeIds = new Set(connectedEdges.map((e) => String(e.id())));

  cy.batch(() => {
    resetGraphState();
    cy.nodes().forEach((n) => {
      if (n.style("display") === "none") return;
      const nid = String(n.id());
      if (nid === centerIdStr) n.addClass("focus-node label-visible");
      else if (visibleNodeIds.has(nid)) n.addClass("focus-neighbor label-visible");
      else n.addClass("ambient-muted label-hidden");
    });
    cy.edges().forEach((e) => {
      if (!e.data("is_me_edge")) return;
      const s = String(e.data("source"));
      const t = String(e.data("target"));
      const srcN = cy.getElementById(s);
      const tgtN = cy.getElementById(t);
      const epOk = srcN.style("display") !== "none" && tgtN.style("display") !== "none";
      if (!epOk) {
        e.style("display", "none");
        return;
      }
      let show = false;
      /* Hover/focus 中心是「我」时：仍画出所有 Me 边，但下面只对
         strength >= weak_me_floor 的边打高亮，否则弱边会像刺猬一样全白，
         和路径里「弱直连要引荐」的语义不一致。 */
      if (meIdStr && centerIdStr === meIdStr) show = epOk;
      else if (meIdStr) show = (s === meIdStr && t === centerIdStr) || (t === meIdStr && s === centerIdStr);
      e.style("display", show ? "element" : "none");
    });
    const floor = window.app?.graph?.weak_me_floor ?? 4;
    cy.edges().forEach((e) => {
      if (e.style("display") === "none") return;
      const eid = String(e.id());
      if (e.data("is_me_edge") && meIdStr && centerIdStr === meIdStr) {
        const str = e.data("strength") || 0;
        if (str >= floor && visibleEdgeIds.has(eid)) e.addClass("focus-edge");
        else e.addClass("ambient-muted");
        return;
      }
      if (visibleEdgeIds.has(eid)) e.addClass("focus-edge");
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
        // Auto-focus the input when the modal mounts.
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
        // optimistic in-place swap
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
