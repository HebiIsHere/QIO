# 中断消息恢复入口（第二阶段 · Agent B）验收记录

分支：`wt/p2-interrupted-ui`（基于 main `926a8e1`）
日期：2026-10-02
相关文件：`frontend/src/services/api.ts`、`frontend/src/stores/session.ts`、
`frontend/src/components/InterruptedTurnEntry.vue`、`frontend/src/views/ConversationView.vue`。

## 1. 缺陷与分类

| 项 | 分类 | 依据 |
| --- | --- | --- |
| 后端 `GET /api/runtime/state.interrupted_turns` + `POST /api/turns/{id}/resend` + `/dismiss` + `storage/turn_journal.py` + 迁移 25 | **已解决（未改）** | 三个端点都在，且真实运行验证过（见第 3 节） |
| 前端 `api.ts` 只有 `interrupted_approvals`，没有 `interrupted_turns` / resend / dismiss | **仍存在 → 本次修复** | `grep -n interrupted frontend/src` 只命中 `api.ts:385` 的 interrupted_approvals 与 ApprovalEntry 的文案 |
| 界面没有任何恢复入口 | **仍存在 → 本次修复** | 修复前 `grep -rn "resend\|dismiss" frontend/src` 无命中 |
| **不自动重发**（重启后 queued/running → interrupted，是否继续由用户确认） | **设计限制（保留）** | 入口只在用户点「继续发送这条」时才调 resend；组件里没有任何自动调用 |

## 2. 改了什么

* `api.ts`：`InterruptedTurn` 类型、runtime state 的 `interrupted_turns` 字段、
  `resendInterruptedTurn` / `dismissInterruptedTurn`。
* `session.ts`：`interruptedTurns` 状态；`resumeInterruptedTurn` /
  `dismissInterruptedTurn` / `dismissAllInterruptedTurns`；单条在飞时守卫（同一条不会发两次）；
  409 → 重新拉权威状态 + 可读回执；网络失败保留入口。
* `InterruptedTurnEntry.vue`（新）：常驻一行入口（不是弹窗、不 autofocus、默认收起），
  展开后逐条：原文（视觉两行截断 + 省略号，DOM 保留全文）、被中断时间、后端给的原因、
  继续 / 忽略；多条时给「全部忽略（N 条）」。
  根条件是 `count || notice`：409 之后列表会被后端清空，若整块一起消失，用户点完看不到任何结果。
* `ConversationView.vue`：挂载入口；顶部固定提示条的让位改成按**实测高度**计算
  （ResizeObserver），不再写死 52px。

## 3. 真实验证（真实后端 + 生产构建 + 无头 Chrome）

台账行是**直接写 sqlite 构造的**（不是真的排队过的对话）；其余每一步都走真实 HTTP / 真实界面。

后端（真实 uvicorn，重启路径）：

* 先让后端建库 → 塞入 `queued` / `running` / `completed` / `cancelled` /
  `notify=1` / 已处理过 6 行 → **重启后端** → 启动时 `interrupt_stale` 把 queued/running 标成 interrupted：
  `ids == ['turn_queued', 'turn_running']`，原文、`reason_text`、时间都在。
* `completed` / `cancelled` / `notify=1` / 已处理过的 **都不出现**。
* `POST /api/turns/turn_queued/resend` → 200 + 新 turn_id；再点一次 → **409**；
  `completed` / `cancelled` / 已处理过的 resend → 409。
* `POST /api/turns/turn_running/dismiss` → 200，入口变空，再次 dismiss → 409；
  台账里原文仍在（忽略 ≠ 删数据）。

界面（生产构建 + 真实后端）：

* 入口文本：「↺ 有 2 条消息在上次退出时没有执行」，`aria-label` 说清「展开可以继续发送或者忽略」，
  `aria-expanded=false`；全页没有「有新任务」这类模糊措辞。
* **不抢焦点**：入口出现时、展开后，`document.activeElement` 都还是 `#composer-input`。
* 展开：`role=region`、`aria-label=上次退出时没有执行的消息`；两条各带原文、`今天 17:49`、原因、
  「继续发送这条」「忽略」；多条时出现「全部忽略（2 条）」。
* **继续**：连点三次只发出 **1 个** `POST /turns/ui_ok1/resend`；回执「已经按原话题重新排队，这一轮马上开始」；
  入口从 2 条变 1 条。
* **409（真实）**：先绕开界面直接 `POST /dismiss` 把某条处理掉，再点界面上的「继续发送这条」→
  真实 409 → 显示「这条已经被处理过了（可能已在别处继续、或已被忽略），入口已按后端最新状态刷新」，
  列表同步刷新（这条不是静默失败）。
* **忽略** → 「已忽略这一条（原文仍然保留在记录里）」，入口消失。
* **Esc**（焦点在入口上，真实按键）：面板收起、入口保留、`aria-expanded` 回到 false、焦点不丢。
* **focus-visible**（真实 Tab）：入口与面板里的两个按钮都 `:focus-visible=true`，
  描边 `2px rgb(176,19,106)`（`--focus-ring`）。
* **让位计算**：收起时 slot 高 53px → `--qio-top-notes-offset: 113px`；
  展开后 198px → 258px（= 52 横幅 + 实测高度 + 8），ResizeObserver 生效。

## 4. 本次发现的后端问题（未改，已发 CROSS-ROUTE REQUEST）

`storage/turn_journal.py`：

* `unfinished()`（界面入口的来源）排除了 `notify = 1`（系统通知轮不算用户的消息）；
* `recoverable()`（resend / dismiss 的准入判断）**只**看 `status = interrupted AND recovered_at IS NULL`，
  **没有**排除 `notify = 1`。

实测：对一条 `notify=1` 的 interrupted 行直接 `POST /api/turns/<id>/resend` → **200 且真的重新提交**。
界面不会拿到这类 turn_id，所以正常操作不可达；但两个函数对同一概念的口径不一致，
属于「同一权威定义有两份」的隐患（重复执行一条系统消息）。

## 5. Not Verified（诚实标注）

* 真机上「应用重启后入口出现」这一步没有跑完整安装包（本机已有安装在 `D:\QIO`，不覆盖）；
  这里用真实后端 + 真实前端 + 真实重启（后端进程重启）验证，**不是**安装包级验证。
* 「全部忽略」只有单元测试（真实后端那轮用的是单条 dismiss）。
* 窄窗口 / 长原文的视觉复核（截断是否总是可见）只在 1440×900 的默认视口下看过。
