"""Render a feedback submission into a markdown ticket file.

生成的 md 结构：
  1. YAML frontmatter（机读）
  2. @assistant 处理指引段（给 AI 看）
  3. 业务填的内容（人类可读）
  4. 自动捕获的技术数据（AI 直接消费）

不使用 Jinja2，纯 f-string 拼接——依赖小、可读性好、diff 友好。
"""

from __future__ import annotations

import json
from typing import Any

_PROMPT_HEADER = """\
> **@assistant 处理指引（给 AI 看）**
>
> 这是一份业务同事通过 WebUI 反馈按钮提交的 {kind}，下面所有信息
> 已由系统自动打包，无需再问人。请按这个顺序处理：
> 1. 读「涉及的人 / db 快照」理解数据状态
> 2. 读「实际 vs 期望 / 验收标准」确认修复目标
> 3. 读「API 回放」对照代码路径，定位根因函数
> 4. 若「业务的期望」与「代码现行设计」冲突（例：算法是故意这样），
>    **先停下来和开发（repo owner）确认**是改代码还是改 UI 提示，
>    不要擅自改算法
> 5. 写测试 → 改代码 → 跑 `uv run pytest` → 报告
"""

_HISTORY_LABEL = {
    "new": "🆕 新需求 / 第一次用这个功能",
    "recent": "✅ 以前能用，最近才坏的",
    "always": "❌ 一直就是这样",
}

_SEVERITY_LABEL = {
    "blocking": "🔥 阻塞签单",
    "daily": "⚠️ 每天都遇到，靠绕能过",
    "nice": "💭 没这个也行，有了更好",
}


def render_ticket_md(payload: dict[str, Any]) -> str:
    """Render the full markdown body for a feedback ticket."""
    ticket_id = payload["ticket_id"]
    type_ = payload["type"]
    form = payload["form"]
    auto = payload["auto_capture"]
    snapshot = payload.get("db_snapshot", [])
    screenshots = payload.get("screenshots", [])

    frontmatter = (
        "---\n"
        f"ticket_id: {ticket_id}\n"
        f"type: {type_}\n"
        f"status: {payload.get('status', 'open')}\n"
        f"severity: {payload.get('severity', '')}\n"
        f"submitter: {payload['submitter']}\n"
        f"created_at: {payload.get('created_at', '')}\n"
        f"mount_slug: {auto['mount_slug']}\n"
        f"frontend_version: {auto['frontend_version']}\n"
        "---\n\n"
    )

    prompt = _PROMPT_HEADER.format(
        kind=("bug" if type_ == "bug" else "需求"),
    ) + "\n---\n\n"

    body = _render_bug_body(form) if type_ == "bug" else _render_feature_body(form)

    involved = _render_involved(snapshot)

    severity = _SEVERITY_LABEL.get(payload.get("severity", ""), "")
    impact = f"## 影响程度\n{severity}\n\n" if severity else ""

    # 截图部分（md 里只写文件引用，真实 base64 由 endpoint 落盘到 attachments/）
    screenshots_md = ""
    if screenshots:
        screenshots_md = "## 截图\n" + "\n".join(
            f"![{s['filename']}](./attachments/{s['filename']})"
            for s in screenshots
        ) + "\n\n"

    tech = _render_tech_data(auto, snapshot)

    return "".join([
        frontmatter,
        prompt,
        involved,
        body,
        impact,
        screenshots_md,
        "---\n\n## 🔧 自动打包的技术数据\n\n",
        tech,
    ])


def _render_bug_body(f: dict[str, Any]) -> str:
    history = _HISTORY_LABEL.get(f.get("history", ""), f.get("history", ""))
    why = f"## 为什么这样期望\n{f['why_expected']}\n\n" if f.get("why_expected") else ""
    return (
        f"## 🐛 标题\n{f['title']}\n\n"
        f"## 你想干什么\n{f['want_to_do']}\n\n"
        f"## 你做了什么\n{f['did']}\n\n"
        f"## 看到了什么（实际）\n{f['actual']}\n\n"
        f"## 期望什么\n{f['expected']}\n\n"
        f"{why}"
        f"## 历史对比\n{history}\n\n"
    )


def _render_feature_body(f: dict[str, Any]) -> str:
    acceptance = "\n".join(f["acceptance"])
    workaround = (
        f"## 现在你是怎么凑合的\n{f['workaround']}\n\n"
        if f.get("workaround") else ""
    )
    return (
        f"## 💡 标题\n{f['title']}\n\n"
        f"## 用户故事\n{f['user_story']}\n\n"
        f"## 验收标准\n{acceptance}\n\n"
        f"{workaround}"
    )


def _render_involved(snapshot: list[dict[str, Any]]) -> str:
    if not snapshot:
        return ""
    lines = ["## 涉及的人"]
    for entry in snapshot:
        p = entry.get("person")
        if p is None:
            lines.append(f"- ⚠️ 联系人 id={entry.get('missing_id')} 已不存在")
        else:
            lines.append(f"- {p['name']} (id={p['id']})")
    return "\n".join(lines) + "\n\n"


def _render_tech_data(
    auto: dict[str, Any],
    snapshot: list[dict[str, Any]],
) -> str:
    state_json = json.dumps(
        {k: auto[k] for k in (
            "mount_slug", "view_mode", "search_active", "query",
            "detail_person_id", "active_path_key", "direct_overrides",
            "indirect_targets", "contacted_targets",
        )},
        ensure_ascii=False, indent=2,
    )
    api_json = json.dumps(auto.get("api_trace", []),
                          ensure_ascii=False, indent=2)
    err_json = json.dumps(auto.get("error_buffer", []),
                          ensure_ascii=False, indent=2)

    parts = [
        "### 前端状态（提交时）\n```json\n",
        state_json, "\n```\n\n",
        "### db 快照（涉及联系人 + 1 跳邻居，已脱敏）\n\n",
    ]
    for entry in snapshot:
        p = entry.get("person")
        if p is None:
            continue
        me_edge = entry.get("me_edge")
        parts.append(f"**{p['name']} (id={p['id']})**\n")
        if p.get("bio"):
            parts.append(f"- bio: `{p['bio']}`\n")
        if p.get("tags"):
            parts.append(f"- tags: `{json.dumps(p['tags'], ensure_ascii=False)}`\n")
        if p.get("notes"):
            parts.append(f"- notes: `{p['notes']}`\n")
        if me_edge:
            parts.append(
                f"- Me → {p['name']}: strength={me_edge['strength']}, "
                f"frequency={me_edge.get('frequency')}, "
                f"context={me_edge.get('context')!r}\n"
            )
        for n in entry.get("neighbors", []):
            parts.append(
                f"- 1 跳邻居: {n['name']} (strength={n['strength']}, "
                f"frequency={n.get('frequency')})\n"
            )
        parts.append("\n")

    parts.extend([
        "### API 回放（最近 10 次请求）\n```json\n",
        api_json, "\n```\n\n",
        "### 前端错误 buffer\n```json\n",
        err_json, "\n```\n\n",
        f"### 浏览器 / 视口\n- UA: `{auto['user_agent']}`\n"
        f"- Viewport: `{auto['viewport']}`\n",
    ])
    return "".join(parts)
