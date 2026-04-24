# Lodestar 反馈系统设计稿

> **For Claude / 下一步：** 本文件是经过 brainstorming 收敛的**设计稿**，不是实施计划。落实前用 `superpowers:writing-plans` 技能生成任务拆解版实施计划。

**目标：** 让跑业务的销售同事能通过 WebUI 的「📝 反馈」按钮一键提交 bug / 需求，提交即打包全部复现上下文（db 快照、API 回放、前端状态）；开发把生成的 markdown 原样甩给 AI，AI 按其中的指引一次改对、不反复追问。

**架构：** 在现有 FastAPI + Alpine SPA 之上加一张 `feedback` 表和一个表单 modal；不动 Person / Relationship / 路径算法。

**技术栈：** FastAPI + SQLite + Alpine.js + Cytoscape.js（沿用现有栈）

---

## 角色与痛点

- **业务（销售同事）**：非技术、日常用飞书、每天跑客户。提 bug 只说结果不说过程，提需求只说愿望不说场景。
- **开发（repo owner）**：一人维护。当前靠微信转述 + 口头翻译，每提一个 bug 都要反复来回问"你搜了啥 / 看到啥 / 期望啥"，极耗时。
- **AI（Cursor 内 Claude）**：上下文是开发一次粘过来的一段话。当前常见失败：指代不清 / 不可复现 / 期望模糊 / bug-vs-设计之争 / 多问题打包 / 业务心智模型与代码模型不一致。

**核心设计目的**：把"业务反馈 → 开发翻译 → AI 执行"这条链路上的信息损耗全部消灭在**表单那一步**，让业务提交完即产生一份 AI 可直接消费的 md artifact。

---

## 架构总览

```
┌─────────────────┐   POST /api/feedback    ┌─────────────────────┐
│  WebUI Topbar   │ ──────────────────────> │  FastAPI Backend    │
│  📝 反馈 按钮    │   form + auto-capture   │                     │
└─────────────────┘                          │  1. 生成 ticket_id  │
        │                                    │  2. 反查 db snapshot│
        │                                    │  3. 落 feedback 表  │
        │                                    │  4. 渲染 md 文件    │
        v                                    │                     │
┌─────────────────┐                          └──────────┬──────────┘
│  Modal 表单     │                                     │
│  - 共用字段     │                                     v
│  - type 专属    │                          ┌─────────────────────┐
│  - 自动附环境   │                          │ SQLite `feedback` 表│
└─────────────────┘                          │ (SOT)               │
                                             │                     │
                                             │ docs/feedback/      │
                                             │   FB-20260423-0001  │
                                             │   .md (衍生)        │
                                             └─────────────────────┘
```

遵循 Lodestar 现有 "SQLite 是 SOT、文件是衍生" 范式。`feedback` 表与 `Person` / `Relationship` 平级，共用 mount 隔离（每个 mount 有自己的反馈 backlog）。

---

## 设计要点（分 8 节）

### § 1. 入口与鉴权

- WebUI topbar 增加「📝 反馈」按钮，与「批量 AI 解析」「关系抽屉」同列。
- 业务点击打开 modal 表单。
- 提交走 `POST /api/feedback`，沿用现有 mount 鉴权（需带 `X-Mount-Unlock` token）——提交者隐式绑定当前 mount。
- **不做**独立 `/feedback` 路由给业务自查，也**不做**业务的账号体系。提交一次性，反馈号用于飞书侧追踪。

### § 2. 表单 UX

**布局**（modal，宽 560px）：

```
┌─────────────────── 反馈 ───────────────────┐
│ ○ 🐛 报告 Bug        ○ 💡 提需求            │
├────────────────────────────────────────────┤
│ 标题 (10–40 字): [________________] 12/40  │
│                                            │
│ 涉及的人: [🔍 搜索联系人...] + 可多选芯片   │
│                                            │
│ ─── Bug 专属 ───                            │
│ 你想干什么:     [________________]          │
│ 你做了什么:     [步骤 1.\n步骤 2.\n...]     │
│ 看到了什么:     [________________]          │
│ 期望什么:       [________________]          │
│ 为什么这样期望: [________________] (可选)   │
│ 历史对比:       ○ 新需求 ○ 最近才坏 ○ 一直  │
│                                            │
│ ─── 需求专属（切换 type 后替换上面 Bug 段）─  │
│ 用户故事:       [当___的时候，我希望___]    │
│ 验收标准:       [- ___\n- ___]              │
│ 现在怎么凑合:   [________________] (可选)   │
│                                            │
│ 影响程度: ○ 🔥 影响正常使用 ○ ⚠️ 每天都遇 ○ 💭 │
│ 你是谁:   [姓名] [飞书账号/手机尾号]         │
│ 截图:     [拖拽或点击上传] (bug 强制 ≥1张) │
│                                            │
│ ℹ️ 提交时会一并打包 10 次最近操作的技术数据 │
│                              [取消][提交]   │
└────────────────────────────────────────────┘
```

**字段文案原则**：用业务能懂的大白话；placeholder 给具体示例（"示例：查『私募』，李四应排前 3 但没出来"）。

**字段校验**（前端软提示 + 后端硬校验）：

| 字段 | 约束 |
|---|---|
| 标题 | 10–40 字，超限/不足 → 提交按钮置灰 |
| 涉及的人 | autocomplete 必选 ≥1；不能手打字符串，只能从 `/api/graph.nodes` 里 pick `{id, name}` |
| Bug: 看到/期望 | 都必填 |
| 需求: 用户故事 | 正则 `/当.*的时候.*希望\|when.*then/i` 必须匹配 |
| 需求: 验收标准 | 识别 `- ` 或 `1.` 起头 bullet，至少 1 条 |
| 截图 | bug 强制 ≥1 张；base64 inline，≤5MB/张，≤3 张 |
| 影响程度 / 提交者 | 必填 |

**关于强校验的取舍**：门槛故意设得高。业务如果连"当___我希望___"都填不出，说明需求没想清楚，这时候拦住比放进来制造幻觉需求强。门槛放宽由后续根据真实反馈再调。

### § 3. 自动抓取机制

四条采集线，全部在前端收集后一并 POST 给后端：

**A. 前端实时态** —— Alpine store 里一键序列化：
```js
{
  mount_slug, view_mode, search_active, query,
  detail_person_id, active_path_key,
  direct_overrides: Object.keys(directOverrides),
  indirect_targets: indirect.map(r => r.target_id),
  contacted_targets: contacted.map(r => r.target_id),
}
```

**B. API 回放环形 buffer**（size=10） —— 注入 `modules/api.js` 的 `api()` wrapper：
```js
const apiTrace = [];
async function api(path, opts) {
  const t0 = Date.now();
  const resp = await fetch(...);
  const body = await resp.clone().json().catch(() => null);
  apiTrace.push({ ts: t0, path, method, req_body, status, resp_body: body });
  if (apiTrace.length > 10) apiTrace.shift();
  return body;
}
window.__getApiTrace = () => apiTrace;
```

**C. 前端错误 buffer**（size=20）：
```js
window.onerror = (msg, src, line, col, err) =>
  errors.push({ ts, msg, stack: err?.stack });
window.addEventListener('unhandledrejection',
  e => errors.push({ ts, reason: String(e.reason) }));
```

**D. 后端反查 db snapshot** —— 后端收到 `involved_person_ids` 后：
- 拉每人的 Person 完整行
- 拉每人的 Me-edge + 1 跳邻居 Relationship 行
- 跑一遍脱敏 scrubber（规则见 § 7）

这一步让 AI 直接拿到结构化事实（"董淑佳 (id=47) bio=教育·副校长 · Me-edge strength=3"），省一轮反查。

### § 4. 后端存储

**新表** —— 走现有 `lodestar.db.init_schema`：
```sql
CREATE TABLE feedback (
  id INTEGER PRIMARY KEY,
  ticket_id TEXT UNIQUE NOT NULL,              -- FB-YYYYMMDD-NNNN
  type TEXT CHECK(type IN ('bug','feature')) NOT NULL,
  status TEXT DEFAULT 'open'
    CHECK(status IN ('open','in_progress','done','wontfix')),
  title TEXT NOT NULL,
  submitter TEXT NOT NULL,
  severity TEXT,                               -- blocking | daily | nice
  payload_json TEXT NOT NULL,                  -- 完整表单 + 自动捕获
  md_path TEXT,                                -- docs/feedback/<slug>/FB-*.md
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  closed_at TIMESTAMP,
  closed_by TEXT,
  related_pr TEXT                              -- 预留
);
CREATE INDEX idx_feedback_status ON feedback(status, created_at DESC);
```

**提交处理流程**（`POST /api/feedback`）：
1. 服务端生成 `ticket_id = f"FB-{YYYYMMDD}-{当日序号:04d}"`（按 mount 各自计数）
2. 反查 db 拼 `db_snapshot`
3. INSERT 到 `feedback`
4. 渲染 md 到 `docs/feedback/{slug}/{ticket_id}.md`（按 mount 分子目录）
5. 返回 `{ticket_id, md_url}` 给前端，前端 toast "已提交 {ticket_id}，请把号发给技术同事"

**SOT 一致性**：md 是**只读衍生品**。手工改 md 不回流；状态变更走 `PATCH /api/feedback/{ticket_id}`（MVP 可暂缺，直接 UPDATE db 也行），后端改完 db 后自动重渲染 md。

### § 5. Ticket md 样例

这是整个系统最终产出、也是 AI 直接消费的 artifact。示例文件 `docs/feedback/me/FB-20260423-0001.md`：

````markdown
---
ticket_id: FB-20260423-0001
type: bug
status: open
severity: daily
submitter: 王磊（飞书 @wanglei，尾号 8823）
created_at: 2026-04-23 14:32:11
mount_slug: me
frontend_version: 20260423-weak-direct-fallback
backend_git_sha: a1b2c3d
---

> **@assistant 处理指引（给 AI 看）**
>
> 这是一份业务同事通过 WebUI 反馈按钮提交的 bug，下面所有信息已由系统
> 自动打包，无需再问人。请按这个顺序处理：
> 1. 读「涉及的人 / db 快照」理解数据状态
> 2. 读「实际 vs 期望 / 验收标准」确认修复目标
> 3. 读「API 回放」对照代码路径，定位出 bug 的函数
> 4. 若「业务的期望」与「代码现行设计」冲突（例：算法是故意这么设计），
>    **先停下来和开发（repo owner）确认是改代码还是改 UI 提示**，
>    不要擅自改算法
> 5. 写测试 → 改代码 → 跑 `uv run pytest` → 报告

---

## 🐛 标题
查「帮孩子上海上学」时董淑佳没出现在引荐列表

## 涉及的人
- 董淑佳 (id=47)
- 王昌尧 (id=23)

## 你想干什么
帮一个客户解决孩子在上海上学的问题，找圈内有资源的人

## 你做了什么
1. 在搜索框输入「帮孩子上海上学」
2. 按回车
3. 看结果列表

## 看到了什么（实际）
结果列表只有 3 个人，董淑佳不在里面。
需要引荐区：王昌尧、李四、赵五。
已联系区：空。

## 期望什么
董淑佳应该出现在「需要引荐」或「已联系」任意一处。她是我客户里
最懂上海教育的人，她儿子就在上海读国际学校。

## 为什么这样期望
董淑佳的简介里写了「上海教育资源」。她女儿在 UWC 读书，这条我写在
notes 里了。

## 历史对比
✅ 以前能用，最近才坏的（大概 2–3 天内）

## 影响程度
⚠️ 每天都遇到，靠绕能过

## 截图
![实际结果](./attachments/FB-20260423-0001-01.png)

---

## 🔧 自动打包的技术数据

### 前端状态（提交时）
```json
{
  "mount_slug": "me",
  "view_mode": "intent",
  "search_active": true,
  "query": "帮孩子上海上学",
  "detail_person_id": null,
  "active_path_key": "t-0-23",
  "direct_overrides": [],
  "indirect_targets": [23, 99, 104],
  "contacted_targets": []
}
```

### db 快照（涉及联系人 + 1 跳邻居，已脱敏）

**董淑佳 (id=47)**
- bio: `行业：教育 · 职务：副校长 · 城市：上海 · 合作价值：4/5`
- tags: `["上海教育资源", "国际学校", "家长圈"]`
- Me → 董淑佳: strength=3, frequency=yearly, context="老同事介绍"
- 1 跳邻居: 王昌尧 (strength=5), 李四 (strength=2)

**王昌尧 (id=23)**
- bio: `行业：投资金融 · 职务：合伙人 · 城市：上海 · 合作价值：5/5`
- Me → 王昌尧: strength=5

### API 回放（最近 10 次请求）
```json
[
  {
    "ts": "2026-04-23T14:32:05.112Z",
    "method": "POST",
    "path": "/api/search",
    "req_body": {"goal": "帮孩子上海上学", "top_k": 3, "no_llm": false},
    "status": 200,
    "resp_body": {
      "intent_summary": "解决上海学区/择校问题",
      "indirect": [{"target_id": 23, "combined_score": 0.42}, ...],
      "contacted": [],
      "wishlist": []
    }
  }
]
```
👉 关键观察（v1.1 由后端自动标注）：董淑佳 (id=47) 完全没出现在任何
一桶里，不是排序问题，是召回问题。

### 前端错误 buffer
（空，最近 20 条无 console error）

### 浏览器 / 视口
- UA: `Mozilla/5.0 ... Chrome/131`
- Viewport: `1920x1080`
````

**md 设计关键点**：
1. **Frontmatter 机读** → 脚本/AI 可 grep 状态
2. **开头「给 AI 的 prompt 段」** → 直接指引 AI 流程，尤其 "bug/设计之争时先停下问人" 的 guardrail
3. **业务描述部分用人话** → 业务自己能读懂
4. **技术数据在下半段** → 业务看不到也不关心；AI 看起来是结构化 JSON
5. **👉 关键观察**（v1.1）→ 由后端自动标注"召回 vs 排序问题"这类初步诊断，给 AI 起点

### § 6. 协作工作流

```
业务 ──[WebUI 点反馈]──> toast: "FB-20260423-0001 已提交，请发给王工"
 │
 │  [飞书发消息：@王工 FB-20260423-0001 搜索漏人]
 v
开发 ──[Cursor 打开 docs/feedback/me/FB-20260423-0001.md]──
 │   [复制 md 全文粘贴到 Cursor 对话]
 │   [加一句：「修这个 bug，遵循 md 里的 @assistant 指引」]
 v
 AI ──[按 md 流程：读数据 → 读期望 → 读 API → 诊断 → 若 bug/设计之争先问开发]──
 │   [写测试 → 改代码 → 跑测试 → 汇报]
 v
开发 ──[review → 合并 → PATCH status=done related_pr=#42]──
 v
业务 ──[飞书通知「FB-20260423-0001 已修复」]（v1.1 webhook）
```

### § 7. 隐私 / 脱敏策略

对内部工具而言，PII 边界要想清但不过度工程化：

| 字段 | 处理 |
|---|---|
| 姓名 | **保留**（业务日常就用姓名交流） |
| 手机号 | 保留后 4 位：`138****8888` |
| 身份证号 | 全 redacted：`[REDACTED_ID]` |
| 银行卡号 | 全 redacted：`[REDACTED_CARD]` |
| 邮箱 | mask 中段：`w***@gmail.com` |
| 投资金额 / 敏感财务 | 保留但加 `⚠️` 前缀让 reviewer 留意 |
| bio / notes 自由文本 | 跑正则 scrubber，匹配 18 位连续数字 / 身份证校验位等，命中替换占位符 |

**实现位置**：`src/lodestar/web/feedback.py` 里新建 `scrub(text: str) -> str`。后续通用的脱敏可 extract 到 `lodestar.privacy`。

**git 提交策略**：
- `docs/feedback/` 进 `.gitignore`，**默认不提交**
- 开发本地拥有，需要分享时手动粘贴 md 全文给 AI
- 未来若需归档为项目历史，走手工审 + 移到 `docs/feedback-archive/` 的流程，不自动化

### § 8. MVP 范围与迭代路线

**MVP（这次交付）—— 确保核心流程跑通：**

- [ ] feedback 表 migration（加到 `lodestar.db.init_schema`）
- [ ] `POST /api/feedback` 端点（受 mount 鉴权）
- [ ] 前端 topbar「📝 反馈」按钮 + modal 表单
- [ ] 表单字段校验（前端软 + 后端硬）
- [ ] API 回放 ring buffer 注入 `modules/api.js`
- [ ] 前端错误 buffer
- [ ] 后端反查 db snapshot + 脱敏 scrubber
- [ ] md 渲染 + 文件落盘（按 mount 分子目录）
- [ ] 截图上传（base64 inline）
- [ ] 冒烟测试：提交 1 条 bug + 1 条 feature，断言 md 结构正确

**不在 MVP：**
- ❌ `/feedback` 列表视图（攒几份样本后再做）
- ❌ 飞书 webhook 状态通知
- ❌ 后端对 API 回放做自动诊断（"👉 关键观察"段）
- ❌ `PATCH /api/feedback` 状态流转 API（MVP 手工 UPDATE db 即可）
- ❌ feedback 列表的筛选、搜索、批量操作

**v1.1（攒 5–10 份真实反馈后）：**
- 列表视图 + 状态流转 API + 简单筛选
- 根据真实反馈数据校准字段设计：业务是否有大量留白、自动抓的数据是否真够用、脱敏规则是否过严或过松

**永远不加（反 YAGNI）：**
- 业务账号体系、权限模型
- 业务自助查看自己提过的反馈列表（避免 PII 交叉暴露）

---

## 成功标准

- 业务能在 2 分钟内填完一份反馈（不包含截图时间）
- 开发从收到 ticket 号到甩给 AI ≤ 30 秒（复制 md → 粘贴 → 加一句话）
- AI 拿到 md 后**首次修改**就能命中根因的比率 ≥ 70%（靠后续 v1.1 的真实数据验证，MVP 阶段定性观察）
- 反馈"bug/设计之争"时，AI **停下来问开发**而不是擅自改算法

---

## 下一步

用 `superpowers:writing-plans` 技能把本设计稿拆成 task-by-task 的实施计划，保存到 `docs/plans/2026-04-23-feedback-system-plan.md`。
