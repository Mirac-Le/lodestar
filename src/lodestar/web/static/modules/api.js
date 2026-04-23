/* ============================================================
 * Mount-aware fetch client.
 *
 * 一人一库（mount router）：
 *   - 每个 mount 独立挂在 `/r/<slug>/` 下，自己一个 db、自己一个密码、
 *     自己一份 unlock_secret。
 *   - SPA 跑在某个 mount 的 sub-app 里，绝对不会跨 mount 调 API。
 *   - "切 tab 必重输"语义靠**整页跳转**实现：换 mount = window.location
 *     assign 到另一个 `/r/<slug>/`，浏览器丢掉所有 in-memory state，
 *     新页面 init 时重新跑 unlock flow，自然必须再输一次密码。
 *   - unlock token 只活在 window.app.unlockToken 里，不写 storage —
 *     刷新即丢、切 mount 即丢、关 tab 即丢。
 * ============================================================ */

/** Detect "/r/<slug>/" prefix from current URL. Returns "" for root SPA. */
function detectMountPrefix() {
  const m = window.location.pathname.match(/^\/r\/([^/]+)\//);
  return m ? `/r/${m[1]}` : "";
}
function detectMountSlug() {
  const m = window.location.pathname.match(/^\/r\/([^/]+)\//);
  return m ? m[1] : null;
}

export const MOUNT_PREFIX = detectMountPrefix();
export const MOUNT_SLUG = detectMountSlug();

/** Resolve a path: `/api/...` → `/r/<slug>/api/...`, root `/api/mounts`
 *  stays absolute. */
export function withMount(path) {
  if (!path.startsWith("/api/")) return path;
  if (path === "/api/mounts" || path.startsWith("/api/mounts/")) return path;
  if (!MOUNT_PREFIX) return path;
  return MOUNT_PREFIX + path;
}

/* ---- API trace ring buffer ----
 * 反馈表单打开时，从这里读取最近 10 次请求上下文，一并 POST
 * 给 /api/feedback。保留整段 req_body + resp_body 让 AI 能精确
 * 复现服务端返回了什么。size=10 是拍脑袋值，业务一次反馈通常只
 * 关心最后几次操作，10 足够且不让 md 过长。 */
const API_TRACE_MAX = 10;
const __apiTrace = [];
if (typeof window !== "undefined") {
  window.__getApiTrace = () => __apiTrace.slice();
}

/** Thin fetch wrapper that:
 *   - prefixes `/api/...` with the current mount slug
 *   - attaches `X-Mount-Unlock` from window.app.unlockToken when present
 *   - on 401 + `mount_locked`, drops the cached token and reopens the
 *     unlock modal so the user can re-authenticate without a refresh.
 *   - records every call into an in-memory ring buffer for feedback
 *     auto-capture (accessible via window.__getApiTrace()). */
export async function api(path, opts = {}) {
  const ts = new Date().toISOString();
  const method = (opts.method || "GET").toUpperCase();
  const reqBody = opts.body ?? null;
  let status = 0;
  let respBody = null;
  try {
    const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    const tok = window.app && window.app.unlockToken;
    if (tok) headers["X-Mount-Unlock"] = tok;
    const res = await fetch(withMount(path), {
      headers,
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    status = res.status;
    const text = await res.text();
    try { respBody = text ? JSON.parse(text) : null; }
    catch { respBody = text ? text.slice(0, 500) : null; }
    if (!res.ok) {
      if (res.status === 401) {
        try {
          const j = typeof respBody === "object" && respBody !== null
            ? respBody
            : JSON.parse(text);
          const d = j.detail;
          const code = typeof d === "object" ? d.code : null;
          if (code === "mount_locked") {
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
    return respBody;
  } finally {
    __apiTrace.push({
      ts, path, method,
      req_body: reqBody, status, resp_body: respBody,
    });
    if (__apiTrace.length > API_TRACE_MAX) __apiTrace.shift();
  }
}
