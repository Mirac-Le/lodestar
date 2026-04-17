# DESIGN.md

> Lodestar 的 UI 取自两个公开设计系统的共性：
> **Linear**（信息架构与字号节律）+ **Vercel**（黑白克制与无装饰）。
> 不复刻品牌色；颜色一律走中性灰阶，让网络图谱本身做视觉中心。

---

## 1. Visual Theme & Atmosphere

- **气质**：editorial dark，工具向，**克制 / 精确 / 安静**。
- **不要**：霓虹、彩色光晕、装饰性渐变、CRT 噪点、浮夸阴影。
- **要**：单一中性灰板、线条优先、阴影只用在 overlay/modal。
- **图谱是主角**，UI 壳子（topbar、面板、按钮）应当看不太见，靠层级与对齐组织信息。

## 2. Color Palette

| Token | Hex | Role |
| --- | --- | --- |
| `--bg-base` | `#08090A` | 全局画布（Linear 标志性近黑） |
| `--bg-elevated` | `#101113` | 面板 / 模态背景 |
| `--bg-raised` | `#16181B` | 二级容器 |
| `--bg-overlay` | `rgba(255,255,255,0.025)` | hover / 选中态填充 |
| `--bg-overlay-2` | `rgba(255,255,255,0.05)` | active / pressed |
| `--border` | `rgba(255,255,255,0.06)` | 默认 1px |
| `--border-strong` | `rgba(255,255,255,0.10)` | hover / focus |
| `--border-focus` | `rgba(228,231,237,0.42)` | 输入聚焦 |
| `--text` | `#F7F8F8` | 标题 / 主要 |
| `--text-secondary` | `#D0D6E0` | 次要 |
| `--text-muted` | `#8A8F98` | 标签 / 注释 |
| `--text-faint` | `#5C6068` | 占位 / 说明 |
| `--brand` | `#FFFFFF` | Vercel 风格 primary（白底深字） |
| `--accent` | `#9CA4AE` | 中性强调（dot、滑块） |
| `--good` | `#7D9A86` | 技能 tag |
| `--warn` | `#B89A6E` | 需求 tag |
| `--danger` | `#C17A74` | 危险动作 |

行业色（节点用）保持低饱和：`#5F756F` / `#6D6A7E` / `#8F8365` / `#8B6F73` / `#8F765E` / `#5F6F82` / `#5E7568` / `#636B78` / `#747882`。

## 3. Typography

- **字族**：Inter Variable，启用 `cv01, ss03`；中文回落 PingFang / HarmonyOS Sans。
- **基线**：14 px / line-height 1.5 / `letter-spacing: -0.012em`。
- **字重**：400（正文）/ 510（强调）/ 590（标题）。
- **数字与代码**：JetBrains Mono / `tabular-nums`，`letter-spacing: 0`。
- **小标签（panel `<h3>`、字段 label）**：11 px、`text-transform: uppercase`、`letter-spacing: 0.08em`、`font-weight: 600`，**不要超过 0.1em**（Linear 约束）。

| Role | Size | Weight | Tracking |
| --- | --- | --- | --- |
| Display (modal h2) | 18px | 590 | -0.018em |
| Brand | 14px | 590 | -0.018em |
| Body | 14px | 400 | -0.012em |
| Panel title | 11px | 600 | 0.08em UPPER |
| Field label | 11px | 600 | 0.08em UPPER |
| Caption | 12px | 400 | 0 |
| Mono / number | 12-13px | 510 | 0 |

## 4. Components

### Buttons (Vercel discipline)

- 高 **32 px**，padding `0 12`，radius **6**。
- 默认：`background: transparent`、1px `--border`、`--text-secondary` 文本。Hover 加 `--bg-overlay` + `--border-strong`，**不变色**。
- `.primary`：白底 `#FFFFFF` + `#08090A` 文本（Vercel signature）。Hover `#E8EBF0`。
- `.danger`：仅 hover 时切换到 danger 色，默认依然中性。
- 图标按钮 `.icon-only`：32×32，无背景，仅 hover 显边框。

### Inputs / Search

- 高 **32 px**，radius 6，1px border。
- Focus：`--border-focus` + `0 0 0 3px rgba(228,231,237,0.10)`（极淡 ring，不要彩色）。
- Search 顶栏 36 px，圆角 8，最大宽 560，居中 flex。

### Panels

- `background: var(--bg-elevated)`、1px `--border`、radius **10**。
- `backdrop-filter: blur(12px)`（不要 24+）。
- 阴影只用 `--shadow-md`（一档），不叠多层。
- `<h3>`：上方 padding 14，下方 10 + 1px 分隔线；右侧放计数 chip（mono 11px `--text-muted`）。
- 内边距 14。

### Lists / Path rows

- 行高 **40 px**（顶端贴 8、底 8），1px 顶部分隔，hover 给 `rgba(255,255,255,0.025)`。
- 序号用 mono / `tabular-nums` / `--text-muted`。
- 主名 14px / 510，副信息 12px mono / muted。

### Tags / Chips

- 高 22，padding `2 8`，radius 4。
- 默认：透明 + `--border` + `--text-secondary`。
- Active：`--bg-overlay-2` + `--border-strong` + `--text`。
- 颜色 tag（skill/need）：仅描边和文字带 `--good` / `--warn`，背景做 8% alpha。

### Toolbar

- 高 **48 px**（Linear 标准），左 18 / 右 18，gap 8。
- 背景：`rgba(8,9,10,0.92)` 实色，**底部 1px `--border`**；不要顶部到底的 fade gradient。
- 品牌：6×6 圆点 `--accent` + `Lodestar` 14px / 590，不要全大写。

### Modal & overlay

- 背景：`rgba(0,0,0,0.7)` + blur 8，`--bg-elevated` 卡片，radius 12。
- 阴影 `--shadow-lg`，仅此一处用大阴影。

## 5. Layout / Spacing

- **基准 4 px**：gap/padding 用 4、6、8、12、14、18、24。
- 顶栏高 48；面板距 topbar **18 px**；面板四边距 18。
- 卡片内 section 间距 12；分组间距 14。
- 圆角等级：4（chip / kbd） / 6（button / input） / 10（panel） / 12（modal）。

## 6. Depth & Elevation

| Layer | Treatment |
| --- | --- |
| Base canvas | 仅一处极淡径向 `rgba(255,255,255,0.03)`，无 dot grid |
| Inline (button/chip) | **无阴影**，仅 1px border |
| Floating panel | 1px border + `0 10px 40px rgba(0,0,0,0.38)` |
| Modal / overlay | `0 22px 64px rgba(0,0,0,0.45)` + 1px strong border |
| Focus ring | `0 0 0 3px rgba(228,231,237,0.10)` 中性 |

## 7. Do's & Don'ts

✅ 所有强调用「字重 / 描边 / 位置」表达，颜色保持中性。
✅ Hover 改 background + border，不改色相。
✅ 数字和路径串始终 mono + `tabular-nums`。
✅ 同一界面只允许 **一个** 实色 primary 按钮（白底）。

❌ 不用紫/青/品红 accent；不用 `text-shadow` 任何颜色发光。
❌ 不在按钮、芯片、行上加 `box-shadow`。
❌ `letter-spacing` 不超 `0.1em`；不用 1.6em 这种夸张轨距。
❌ `backdrop-filter` 不超 16 px；不叠 saturate(160%)。
❌ 不在边缘加 dot-grid / 扫描线 / 噪点。

## 8. Agent Prompt Snippet

> Build it like Linear's app: 14px Inter with `cv01, ss03`, `-0.012em` tracking.
> Surfaces `#08090A → #101113 → #16181B`. Borders `rgba(255,255,255,0.06-0.10)`.
> Buttons 32px, transparent + 1px border by default; primary is white on `#08090A`.
> No coloured shadows; no gradients on chrome; no neon. Numbers in JetBrains Mono.
