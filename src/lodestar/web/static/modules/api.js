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

/** Thin fetch wrapper that:
 *   - prefixes `/api/...` with the current mount slug
 *   - attaches `X-Mount-Unlock` from window.app.unlockToken when present
 *   - on 401 + `mount_locked`, drops the cached token and reopens the
 *     unlock modal so the user can re-authenticate without a refresh. */
export async function api(path, opts = {}) {
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
