/* ============================================================
 * Lodestar SPA — entry module.
 *
 * 自 2026-04 重构后，本文件只负责「装配」：
 *   - 把 modules/state.js 暴露的 appState() 工厂挂到 window，
 *     让 index.html 里的 `x-data="appState()"` 能解析到。
 *
 * 实际职责拆分：
 *   - modules/constants.js  视觉/领域常量（颜色 / INDUSTRIES）
 *   - modules/api.js        mount-aware fetch + unlock token 处理
 *   - modules/graph.js      cytoscape 渲染 + ambient/focus/intent 三态
 *   - modules/state.js      Alpine 数据 + 业务流程（unlock / 搜索 /
 *                           AI enrich / 关系编辑 / ...）
 *
 * Alpine 与 Cytoscape / ECharts 都是经典 IIFE 库，挂在 window 上；
 * 我们走 ESM 但在浏览器里照常使用 `<script src="...">` 加载它们，
 * 在模块代码中通过 `window.cytoscape` / `window.echarts` 访问。
 * ============================================================ */

import { appState } from "./modules/state.js";

window.appState = appState;

/* ---- Frontend error buffer ----
 * 反馈表单提交时一并打包，帮 AI 定位"业务没看见但控制台红了"的错误。
 * 20 条足够覆盖一次会话；大于此值环形丢弃最老的。 */
const ERR_MAX = 20;
window.__errBuffer = [];
window.addEventListener("error", (e) => {
  window.__errBuffer.push({
    ts: new Date().toISOString(),
    msg: e.message,
    stack: e.error && e.error.stack ? e.error.stack : null,
  });
  if (window.__errBuffer.length > ERR_MAX) window.__errBuffer.shift();
});
window.addEventListener("unhandledrejection", (e) => {
  window.__errBuffer.push({
    ts: new Date().toISOString(),
    reason: String(e.reason),
    stack: e.reason && e.reason.stack ? e.reason.stack : null,
  });
  if (window.__errBuffer.length > ERR_MAX) window.__errBuffer.shift();
});
