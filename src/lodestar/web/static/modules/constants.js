/* ============================================================
 * Visual + domain constants shared by graph rendering and Alpine UI.
 *
 * Kept in one tiny ESM module so both the cytoscape layer
 * (modules/graph.js) and the Alpine state layer (modules/state.js)
 * can pull a single source of truth without duplicating literals.
 * ============================================================ */

export const ME_COLOR = "#f7f8f8";
export const DIM_COLOR = "rgba(208, 214, 224, 0.10)";
export const EDGE_COLOR = "rgba(255, 255, 255, 0.10)";
export const ACCENT = "#9ca4ae";
export const ACCENT_SOFT = "rgba(208, 214, 224, 0.42)";
export const PATH_EDGE = "#d0d6e0";
export const AMBIENT_LABEL_COUNT = 5;

/* Industry chip palette — order is also the display order in the
   industry filter strip in the topbar. The "我" entry is reserved for
   the Me node and never shows up as a togglable chip. */
export const INDUSTRIES = [
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
