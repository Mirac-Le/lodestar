"""Render the contact network as an interactive HTML graph.

Visual encoding:
- node color    = inferred industry bucket (muted, low-saturation palette)
- node size     = relationship strength to "me" (degree-weighted fallback)
- edge width    = relationship strength (1-5 → thicker lines)
- emphasis = nodes/edges on a recommended path are slightly stronger

Pyvis sits on top of vis-network. Path highlights use soft contrast, not neon.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from pathlib import Path

from pyvis.network import Network

from lodestar.db.repository import Repository
from lodestar.models import PathResult, Person, Relationship

# --------------------------------------------------------------------- palette
# (label, keyword list, fill color, border/glow accent — desaturated)
INDUSTRY_BUCKETS: list[tuple[str, list[str], str, str]] = [
    (
        "投资金融",
        [
            "私募",
            "公募",
            "基金",
            "投资",
            "券商",
            "银行",
            "信托",
            "FOF",
            "资管",
            "投行",
            "投顾",
            "金融",
            "理财",
            "财富",
        ],
        "#5f756f",
        "#5f756f",
    ),
    (
        "技术研发",
        [
            "IT",
            "技术",
            "工程师",
            "算法",
            "开发",
            "AI",
            "数据",
            "互联网",
            "芯片",
            "半导体",
            "软件",
            "码农",
        ],
        "#6d6a7e",
        "#6d6a7e",
    ),
    (
        "政府国资",
        ["政府", "国资", "处长", "科长", "局", "委", "事业单位", "公务员", "国企"],
        "#8f8365",
        "#8f8365",
    ),
    (
        "销售渠道",
        ["销售", "客户", "渠道", "BD", "市场", "营销", "经理", "分公司"],
        "#8b6f73",
        "#8b6f73",
    ),
    (
        "创业老板",
        ["老板", "创业", "私营", "合伙人", "CEO", "创始人", "董事", "总经理", "总裁"],
        "#8f765e",
        "#8f765e",
    ),
    ("学术研究", ["研究", "教授", "博士", "院士", "学者", "学术"], "#5f6f82", "#5f6f82"),
    ("医疗大健康", ["医院", "医疗", "药", "医师", "护士", "器械", "生物"], "#5e7568", "#5e7568"),
    ("制造地产", ["制造", "工厂", "产业", "建筑", "地产", "建材"], "#636b78", "#636b78"),
]
DEFAULT_BUCKET = ("其他", "#747882", "#747882")
ME_COLOR = "#f7f8f8"
ME_GLOW = "#9ca4ae"


def infer_industry(person: Person) -> tuple[str, str, str]:
    """Return (label, color, glow_color) for the most likely industry."""
    haystack = " ".join(
        [*person.tags, *person.companies, person.bio or "", person.notes or ""]
    ).lower()
    for label, keywords, color, glow in INDUSTRY_BUCKETS:
        for kw in keywords:
            if kw.lower() in haystack:
                return label, color, glow
    return DEFAULT_BUCKET[0], DEFAULT_BUCKET[1], DEFAULT_BUCKET[2]


def _format_tooltip(person: Person, industry: str, strength_to_me: int | None) -> str:
    """HTML tooltip shown on node hover."""
    rows = [
        f"<b style='color:#f7f8f8;font-size:14px'>{html.escape(person.name)}</b>",
        f"<span style='color:#8b919c'>{html.escape(industry)}</span>",
    ]
    if strength_to_me is not None:
        bars = "█" * strength_to_me + "░" * (5 - strength_to_me)
        rows.append(f"<span style='color:#a89b78'>可信度 {bars}</span>")
    if person.bio:
        rows.append(f"<i>{html.escape(person.bio[:120])}</i>")
    if person.companies:
        rows.append("公司: " + html.escape(", ".join(person.companies[:3])))
    if person.tags:
        rows.append("标签: " + html.escape(", ".join(person.tags[:5])))
    if person.skills:
        rows.append("技能: " + html.escape(", ".join(person.skills[:5])))
    if person.needs:
        rows.append(
            "<span style='color:#a8827a'>需求: "
            + html.escape(", ".join(person.needs[:3]))
            + "</span>"
        )
    return "<br>".join(rows)


class GraphExporter:
    """Build an interactive HTML graph from the repository."""

    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    def export(
        self,
        output: Path,
        highlighted: Iterable[PathResult] | None = None,
        title: str = "Lodestar — Network",
    ) -> Path:
        people = self._repo.list_people()
        rels = self._repo.list_relationships()
        me = self._repo.get_me()
        if me is None or me.id is None:
            raise RuntimeError("No 'me' record. Run `lodestar init` first.")

        people_by_id: dict[int, Person] = {p.id: p for p in people if p.id}
        people_by_id[me.id] = me

        strength_to_me = _strength_to_me_map(rels, me.id)

        highlighted_list: list[PathResult] = list(highlighted or [])
        path_node_ids, path_edge_ids = _collect_path_elements(highlighted_list)

        net = Network(
            height="100vh",
            width="100%",
            bgcolor="#08090a",
            font_color="#f7f8f8",
            directed=False,
            notebook=False,
            cdn_resources="remote",
        )
        _apply_options(net)

        for person in people_by_id.values():
            assert person.id is not None
            self._add_node(
                net,
                person,
                strength_to_me=strength_to_me.get(person.id),
                is_me=person.is_me,
                on_path=person.id in path_node_ids,
            )

        for rel in rels:
            edge_key = _edge_key(rel.source_id, rel.target_id)
            on_path = edge_key in path_edge_ids
            self._add_edge(net, rel, on_path=on_path)

        output.parent.mkdir(parents=True, exist_ok=True)
        net.write_html(str(output), notebook=False, open_browser=False)
        _post_process_html(output, title=title, highlighted=highlighted_list)
        return output

    def _add_node(
        self,
        net: Network,
        person: Person,
        strength_to_me: int | None,
        is_me: bool,
        on_path: bool,
    ) -> None:
        assert person.id is not None
        if is_me:
            industry_label = "我"
            color = ME_COLOR
            glow = ME_GLOW
            base_size = 38
        else:
            industry_label, color, glow = infer_industry(person)
            base_size = 14 + (strength_to_me or 1) * 4

        size = base_size + (10 if on_path else 0)
        border_color = glow if on_path else "#2e3036"
        shadow = {
            "enabled": True,
            "color": glow if on_path else "#000000",
            "size": 28 if on_path else 8,
            "x": 0,
            "y": 0,
        }

        net.add_node(
            person.id,
            label=person.name,
            title=_format_tooltip(person, industry_label, strength_to_me),
            color={
                "background": color,
                "border": border_color,
                "highlight": {"background": glow, "border": "#ffffff"},
                "hover": {"background": glow, "border": "#ffffff"},
            },
            size=size,
            borderWidth=3 if on_path else 1,
            shadow=shadow,
            font={
                "color": "#ffffff" if on_path or is_me else "#c0cce0",
                "size": 16 if (on_path or is_me) else 11,
                "face": "system-ui, -apple-system, sans-serif",
                "strokeWidth": 3,
                "strokeColor": "#08090a",
            },
            mass=2.5 if is_me else 1.0,
        )

    def _add_edge(self, net: Network, rel: Relationship, on_path: bool) -> None:
        width = 1.5 + rel.strength * 1.2
        if on_path:
            color = "#aeb6c2"
            width *= 1.8
        else:
            # subtle dim by strength
            alpha = 0.12 + rel.strength * 0.06
            color = f"rgba(130, 138, 150, {alpha:.2f})"

        net.add_edge(
            rel.source_id,
            rel.target_id,
            width=width,
            color=color,
            title=(rel.context or f"strength {rel.strength}"),
            shadow={"enabled": on_path, "color": "#8e96a3", "size": 10, "x": 0, "y": 0},
            smooth={"type": "continuous", "roundness": 0.2},
        )


# ----------------------------------------------------------------- internals
def _strength_to_me_map(rels: list[Relationship], me_id: int) -> dict[int, int]:
    """Max strength of any direct edge between 'me' and each person."""
    out: dict[int, int] = {}
    for r in rels:
        if r.source_id == me_id:
            other = r.target_id
        elif r.target_id == me_id:
            other = r.source_id
        else:
            continue
        out[other] = max(out.get(other, 0), r.strength)
    return out


def _edge_key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


def _collect_path_elements(
    paths: list[PathResult],
) -> tuple[set[int], set[tuple[int, int]]]:
    nodes: set[int] = set()
    edges: set[tuple[int, int]] = set()
    for r in paths:
        prev: int | None = None
        for step in r.path:
            nodes.add(step.person_id)
            if prev is not None:
                edges.add(_edge_key(prev, step.person_id))
            prev = step.person_id
    return nodes, edges


def _apply_options(net: Network) -> None:
    """Vis-network physics + interaction tuning."""
    net.set_options(
        """
        {
          "physics": {
            "enabled": true,
            "solver": "forceAtlas2Based",
            "forceAtlas2Based": {
              "gravitationalConstant": -90,
              "centralGravity": 0.012,
              "springLength": 140,
              "springConstant": 0.05,
              "damping": 0.6,
              "avoidOverlap": 0.6
            },
            "stabilization": { "enabled": true, "iterations": 220, "fit": true },
            "minVelocity": 0.4
          },
          "interaction": {
            "hover": true,
            "tooltipDelay": 120,
            "navigationButtons": true,
            "keyboard": true,
            "zoomView": true
          },
          "edges": { "smooth": { "type": "continuous" } },
          "nodes": { "shape": "dot" }
        }
        """
    )


# ----------------------------------------------------------------- post-process
def _post_process_html(path: Path, title: str, highlighted: list[PathResult]) -> None:
    """Inject custom CSS, an info panel, and a search box on top of pyvis HTML."""
    html_text = path.read_text(encoding="utf-8")

    custom_head = f"""
<title>{html.escape(title)}</title>
<style>
  html, body {{ margin: 0; padding: 0; background: #08090a;
                font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
                color: #f7f8f8; overflow: hidden; }}
  #mynetwork {{ position: fixed !important; inset: 0; height: 100vh !important;
                width: 100vw !important;
                background: radial-gradient(ellipse 80% 65% at 50% 38%,
                  rgba(255,255,255,0.03) 0%, transparent 55%), #08090a; }}
  .card {{ display: none; }}
  .ls-header {{ position: fixed; top: 18px; left: 24px; z-index: 100;
                font-weight: 600; font-size: 17px; letter-spacing: -0.02em;
                color: #eceef2; display: flex; align-items: center; gap: 8px; }}
  .ls-header::before {{ content: ""; width: 7px; height: 7px; border-radius: 50%;
                        background: #9ca4ae; flex-shrink: 0; }}
  .ls-sub    {{ position: fixed; top: 44px; left: 24px; z-index: 100;
                font-size: 12px; color: #868b96; }}
  .ls-search {{ position: fixed; top: 18px; right: 24px; z-index: 100;
                background: rgba(22, 22, 26, 0.88); border: 1px solid rgba(255,255,255,0.08);
                border-radius: 10px; padding: 8px 14px;
                color: #f7f8f8; font-size: 14px; outline: none;
                width: 240px;
                backdrop-filter: blur(12px);
                box-shadow: 0 8px 28px rgba(0, 0, 0, 0.35); }}
  .ls-search:focus {{ border-color: rgba(200, 206, 216, 0.45);
                      box-shadow: 0 0 0 3px rgba(156, 164, 174, 0.15); }}
  .ls-panel {{ position: fixed; bottom: 24px; left: 24px; z-index: 100;
               background: rgba(22, 22, 26, 0.92); border: 1px solid rgba(255,255,255,0.08);
               border-radius: 12px; padding: 14px 18px; max-width: 360px;
               max-height: 50vh; overflow-y: auto;
               backdrop-filter: blur(12px);
               box-shadow: 0 12px 40px rgba(0, 0, 0, 0.45); }}
  .ls-panel h3 {{ margin: 0 0 10px 0; font-size: 12px;
                  color: #b4bac6; letter-spacing: 0.06em; text-transform: uppercase;
                  font-weight: 600; }}
  .ls-panel .row {{ display: flex; align-items: baseline;
                    gap: 8px; padding: 6px 0; border-top: 1px solid rgba(255,255,255,0.06);
                    font-size: 13px; }}
  .ls-panel .row:first-of-type {{ border-top: 0; }}
  .ls-panel .rank {{ color: #9ca4ae; font-weight: 600; min-width: 20px;
                    font-variant-numeric: tabular-nums; }}
  .ls-panel .name {{ color: #eceef2; font-weight: 600; }}
  .ls-panel .why  {{ color: #868b96; font-size: 11px; flex: 1; text-align: right; }}
  .ls-legend {{ position: fixed; bottom: 24px; right: 24px; z-index: 100;
                background: rgba(22, 22, 26, 0.92); border: 1px solid rgba(255,255,255,0.08);
                border-radius: 12px; padding: 12px 16px;
                font-size: 12px; backdrop-filter: blur(12px); }}
  .ls-legend .swatch {{ display: inline-block; width: 10px; height: 10px;
                        border-radius: 50%; margin-right: 6px;
                        vertical-align: middle; opacity: 0.9; }}
  .ls-legend .item {{ padding: 3px 0; color: #b4bac6; }}
  .vis-navigation .vis-button {{ background-color: rgba(22, 22, 26, 0.75) !important;
                                 border: 1px solid rgba(255,255,255,0.08) !important;
                                 border-radius: 8px !important; }}
</style>
"""

    panel_html = _build_side_panel(highlighted)
    legend_html = _build_legend()
    overlay = f"""
<div class="ls-header">Lodestar</div>
<div class="ls-sub">{html.escape(title)}</div>
<input class="ls-search" placeholder="🔍  搜索人名…" oninput="lsFilter(this.value)" />
{panel_html}
{legend_html}
<script>
function lsFilter(q) {{
  if (typeof network === 'undefined' || typeof nodes === 'undefined') return;
  q = (q || '').trim().toLowerCase();
  const ids = nodes.getIds();
  if (!q) {{
    nodes.update(ids.map(id => ({{ id, opacity: 1 }})));
    return;
  }}
  const matched = [];
  ids.forEach(id => {{
    const n = nodes.get(id);
    const hit = n.label && n.label.toLowerCase().includes(q);
    if (hit) matched.push(id);
    nodes.update({{ id, opacity: hit ? 1 : 0.18 }});
  }});
  if (matched.length) {{ network.focus(matched[0], {{ scale: 1.5, animation: true }}); }}
}}
</script>
"""

    if "</head>" in html_text:
        html_text = html_text.replace("</head>", custom_head + "</head>", 1)
    if "</body>" in html_text:
        html_text = html_text.replace("</body>", overlay + "</body>", 1)
    else:
        html_text += custom_head + overlay
    path.write_text(html_text, encoding="utf-8")


def _build_side_panel(paths: list[PathResult]) -> str:
    if not paths:
        return ""
    rows = []
    for i, r in enumerate(paths[:8], start=1):
        path_str = " → ".join(s.name for s in r.path)
        rows.append(
            f"""<div class="row">
                  <span class="rank">#{i}</span>
                  <div>
                    <div class="name">{html.escape(r.target.name)}</div>
                    <div style="color:#868b96;font-size:11px">{html.escape(path_str)}</div>
                  </div>
                  <span class="why">{r.combined_score:.2f}</span>
                </div>"""
        )
    return f"""<div class="ls-panel"><h3>推荐路径</h3>{"".join(rows)}</div>"""


def _build_legend() -> str:
    items = [(label, color) for label, _, color, _ in INDUSTRY_BUCKETS]
    items.append(("我", ME_GLOW))
    rows = "".join(
        f'<div class="item"><span class="swatch" style="background:{c};color:{c}"></span>{html.escape(name)}</div>'
        for name, c in items
    )
    return f'<div class="ls-legend"><h3 style="margin:0 0 6px 0;color:#b4bac6;font-size:11px;letter-spacing:0.08em;font-weight:600">行业</h3>{rows}</div>'
