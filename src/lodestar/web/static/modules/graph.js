/* ============================================================
 * Cytoscape rendering + three view modes (ambient / focus / intent)
 * + display filters.
 *
 * The Alpine state layer never touches cytoscape internals directly:
 *   - to read the cy instance (for centering / lookups), use getCy()
 *   - to trigger a state transition, call apply{Ambient,Focus,Intent}State
 *   - to apply chip / strength / search filters, call applyFilters
 *
 * Graph callbacks (mouseover/out/tap) talk back to Alpine via the
 * shared `window.app` reference, which Alpine sets in its `init()`.
 * ============================================================ */

import {
  ME_COLOR,
  ACCENT,
  EDGE_COLOR,
  PATH_EDGE,
  AMBIENT_LABEL_COUNT,
} from "./constants.js";

let cy = null;

/** Return the live cytoscape instance, or null before init / after destroy. */
export function getCy() {
  return cy;
}

export function buildGraph(payload) {
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

export function applyAmbientState() {
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

export function buildFocusSummary(raw, nodeEle) {
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

export function applyFocusState(nodeId) {
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

  /* Hover/focus 中心是「我」时：保留 Me 节点圆形高亮，但不要画出任何
     放射状的 Me 边——之前对几十～上百个联系人全亮 me-edge 视觉上像刺猬，
     用户多次反馈不想要。其他节点统一 ambient-muted，等用户进入具体目标
     或路径时再点亮链路。 */
  if (meIdStr && centerIdStr === meIdStr) {
    cy.batch(() => {
      resetGraphState();
      cy.nodes().forEach((n) => {
        if (n.style("display") === "none") return;
        if (String(n.id()) === centerIdStr) n.addClass("focus-node label-visible");
        else n.addClass("ambient-muted label-hidden");
      });
      cy.edges().forEach((e) => {
        if (e.data("is_me_edge")) {
          e.style("display", "none");
        } else {
          e.addClass("ambient-muted");
        }
      });
    });
    return buildFocusSummary(center.data("raw"), center);
  }

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
      const show = meIdStr ? ((s === meIdStr && t === centerIdStr) || (t === meIdStr && s === centerIdStr)) : false;
      e.style("display", show ? "element" : "none");
    });
    cy.edges().forEach((e) => {
      if (e.style("display") === "none") return;
      const eid = String(e.id());
      if (visibleEdgeIds.has(eid)) e.addClass("focus-edge");
      else e.addClass("ambient-muted");
    });
  });
  return buildFocusSummary(center.data("raw"), center);
}

export function applyIntentState(nodeIds, edgeIds, options = {}) {
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

export function initCytoscape(payload) {
  if (cy) cy.destroy();
  cy = window.cytoscape({
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
        style: { "text-opacity": 1 },
      },
      {
        selector: "node.label-hidden",
        style: { "text-opacity": 0 },
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
        style: { display: "none" },
      },
      {
        selector: "edge.ambient-muted",
        style: { "opacity": 0.03 },
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

export function clearHighlights() {
  if (!cy) return;
  applyAmbientState();
}

/** Back-compat alias retained for any caller still using the old name. */
export function applyHighlight(nodeIds, edgeIds, options = {}) {
  applyIntentState(nodeIds, edgeIds, options);
}

export function applyFilters(filters) {
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
        const q = filters.search.trim().toLowerCase();
        if (q) {
          const hay = [
            raw.label,
            raw.bio || "",
            raw.notes || "",
            ...(raw.tags || []),
            ...(raw.companies || []),
            ...(raw.cities || []),
            ...(raw.skills || []),
            ...(raw.needs || []),
          ]
            .join(" ")
            .toLowerCase();
          // 支持多空格分词：任一 token 命中即显示（AND）
          const tokens = q.split(/\s+/).filter(Boolean);
          const ok =
            tokens.length === 0
              ? true
              : tokens.every((t) => hay.includes(t));
          if (!ok) visible = false;
        }
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
