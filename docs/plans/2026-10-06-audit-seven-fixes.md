# 统一过程区 / 流式输出 / 附件 专项修复（审计七项）

- 修复基线：`feat/unified-process-attachments-streaming` @ `e428bb94e1934c53a92c7138ba427f02b882664d`
  （用户给的 `e428bb94…427f2b88…` 有一处笔误，实际 SHA 以本次 fetch 为准；该功能**未**合入 main）
- 修复分支：`fix/unified-process-audit`（worktree `D:\qio-dev\qio-fix`），不合并 main、不覆盖其他在研分支
- 本文是共享契约：字段、语义、文件归属以本文为准；子智能体只改自己名下的文件

---

## 0. 七项问题的当前复现结果（已逐条在源码中确认）

| # | 复现证据（当前代码） | 结论 |
| --- | --- | --- |
| 1 | `TurnProcess.vue:264-274`：内联审批只取 `approvalIntent(payload)` 一行 + `approvalCapabilities`；`:287-295` `claimInline` 让全局入口/弹窗让位 → 真实命令/路径/参数/scope、独立 explanation、授权与预算设置全部没有入口 | 成立 |
| 2 | `core/loop.py` `_AssistantStream`：`GUARD_MS=300` 到期即判 answer；`note_tool_call()` 在 role=="answer" 时把文字**移回** interim（`loop.py:165-174`）；测试 `test_late_tool_call_moves_text_from_answer_to_interim` 把这个移动写成正确行为 | 成立 |
| 3 | `api/server.py:1233` `raw_ids = body.get("attachment_ids") or []` → 显式空列表变成 falsy → `bind_for_turn` 走兜底把「该话题下所有未绑定附件」绑上（`services/attachments.py:779-806`） | 成立 |
| 4 | `TurnProcess.vue:76-85` `watch(running)` → `setProcessExpanded(..., true)`：运行中自动展开抽屉，旧阶段/旧说明直接可见；`:343` 当前阶段**逐项**渲染 `MessageItem` 工具卡 | 成立 |
| 5 | 历史附件只有标签：`AttachmentChip.vue`/`MessageItem.vue` 无「打开副本」入口；`relocateAttachment` 在前端定义了但无调用点；无 `/content` 下载/查看链路 | 成立 |
| 6 | `api/server.py:1122-1153`：上传 `payload = await request.body()` 先**整包读入内存**（Content-Length 缺失即无上限），再在事件循环里同步写盘 + 算 sha256；relocate 同样可能同步复制 | 成立 |
| 7 | `TurnProcess.vue` 失败/停止只显示状态词 + 工具计数；`TurnFacts` 有 `reason?` 字段（session.ts:361）但无来源、无展示、无操作入口 | 成立 |

---

## 1. 共享契约（先冻结，再并行）

### 1.1 输出角色（问题 2 —— 本轮的**核心裁决**）

**删除 300ms 守卫与「answer → interim 移动」这个例外**（含其测试与文档表述）。新规则：

1. 一次模型调用的正文增量**先进入过程区**（当前阶段的「生成中」说明），实时到达、实时显示 —— 这就是「边生成边显示」。
2. **唯一可靠的正式回答判据**：该次调用**结束且没有任何工具调用**。此时这段文字成为本轮正式回答，**原样提升**到正式回答区（同一 `delta_id`、同一份文字、不重打、不重复），随后由 `TURN_END.final_content` 校准。
3. 调用结束时**有**工具调用 → 该段文字是过程说明，留在过程区（它是该阶段历次说明之一）。**永不移动**已进入答案区的文字。
4. **显式回答阶段（补充路径，零/一次额外调用）**：工具阶段收尾的那次调用**没有产出任何正文**时，循环再发**一次不带工具**的调用专门产出正式回答；这次调用天然满足「role 可靠」，其正文从**第一个增量起**就进正式回答区（真流式）。成本如实记录：每轮最多一次额外调用，只在必要时发生。
5. 判据里**不得**出现：经过多少时间、文案像不像答案、暂未收到工具增量、某 kind 变化。
6. 保持不变：累计快照 + `delta_id`/`seq` 单调去重、取消/失败保留已确认文本、断线重连与历史恢复、`TURN_END.final_content` 只校准、不支持流式的 provider 如实提示 `streaming:false`。
7. **实现口径（2026-10-06 Lead 确认）**：正文增量**一开始就**以 `interim=true` 实时发布（进过程区）；调用结束且无工具调用 → **同一 `delta_id`** 发 `{interim:false, streaming:false, content=累计全文}` 原样提升；有工具调用 → 留在过程区，阶段就位后用**同一 `delta_id`** 补发带 `stage_id`/`call_ids` 的累计快照。
   - 前端必须允许 `interim: true → false` 的**单向提升**（现状 session.ts 写着「interim 一旦为 true 就不再回正文区」要改）；**正式回答 → 过程区永远不允许**。
   - 事件里**显式 `stage_id=null` 时不得回退到「到达时的当前阶段」**：先按未归属渲染，带 `stage_id` 的快照到达后就地归位（同一消息，不新增）；正式回答永不挂阶段。

事件层不变（`ASSISTANT{content, interim, streaming, delta_id, seq, stage_id, call_ids}`）：`interim=true` 表示「过程区文字」，`interim=false` 表示「正式回答」。新增可选字段 `role_evidence: "call_closed_without_tools" | "tool_free_call"`，仅用于取证与测试断言。

### 1.2 轮次结束事实（问题 7）

`TURN_END.data` 增补（全部来自系统事实，脱敏后写入）：
```jsonc
{
  "reason_code": "provider_error" | "internal_error" | "credential_unavailable" | "tool_failed"
               | "budget" | "no_progress" | "guard_halt" | "user_stopped" | "interrupted" | "none",
  "reason": "一句话人话原因（≤200 字，已过 redact）",
  "stopped_by": "user" | "system" | null,
  "actions": ["retry" | "resend"]   // 只列**当前确实可用**的操作，见下表
}
```
- 可恢复的单次工具错误**不等于**整轮失败；`status` 语义不变（`completed|failed|cancelled|unavailable`）。
- `actions` 映射（2026-10-06 Lead 裁决：宁缺毋滥，**只给确实可执行的**）：

| reason_code | actions | 依据 |
| --- | --- | --- |
| `provider_error` / `internal_error` / `tool_failed` | `["retry"]` | 前端用现有发送接口重发该轮用户消息（新开一轮）；B 必须提供入口，否则 A 必须去掉该 action |
| `user_stopped` / `interrupted` | `["resend"]` | 后端既有 `POST /api/turns/{id}/resend`（仅 journal 记成 interrupted 的可重发） |
| `budget` / `no_progress` / `guard_halt` | `[]` | 重发同样的请求会再次停下，不给会再次失败的按钮；原因写清楚即可 |
| `credential_unavailable` | `[]` | 真正入口是「设置 → 凭据」，写在 reason 文案里 |
| `none` | `[]` | — |
- 前端把 `reason/reason_code/actions` 记进 `TurnFacts`（按 `turn_id`），历史分页与 RESYNC 快照同样带回。
- 旧记录没有这些字段 → 不伪造原因，只显示原有状态词。

### 1.3 审批事实（问题 1）

- 抽出**共用**的 `ApprovalFacts.vue`（或等价的共享整理函数 `approvalFacts(payload) → {description, explanation, access, capabilities, scope, detail, command, path, params, risk, budget}`），ApprovalModal 与内联卡**共用同一份**，禁止两套显示规则。
- 内联卡固定展示：说明（模型 explanation 与系统 description **分别**保留）、真实操作事实（命令/路径/工具参数/授权对象/范围/风险）、验证信息（有则显示）、必要的授权与预算设置入口；长技术明细折叠但入口明确。
- 提供「查看完整信息」入口打开原弹窗；**任一时刻同一 approval_id 只有一套有效按钮**（内联与弹窗按 id 互斥，复用既有 claim 机制）。
- 保留：`approval_id` 绑定、队列、过期、拒绝、失败重试、防重复提交。

### 1.4 附件显式绑定（问题 3）

- 请求体里 `attachment_ids` 的**存在性**即语义：**出现**（含 `[]`）= 显式，**只绑这些**，空列表 = 没有附件；**缺字段**才走旧客户端兜底。
- 前端一律发送该字段（即使为空）。
- 待发附件与「话题 + 草稿」绑定并在组件重建/刷新后可**可见恢复**（或明确清空）；用户看到的附件 == 发送的附件；切话题不串。
- 绑定前校验：目标附件存在、属于当前话题/无归属、状态有效（`ready`/`prepared` 可绑，`failed/cancelled/missing` 不绑）；已被别的 turn 绑定的 id 不再重复绑定。

### 1.5 默认折叠与工具摘要（问题 4）

- **运行中默认不展开历史**：`watch(running)` 不再 `setProcessExpanded(true)`。默认可见区 = 状态行 + **当前阶段名 + 最新一条说明** + **一行工具摘要**（`正在读取文件 · 2 项工具运行中`）。
- 历史（旧阶段、旧说明、逐项工具记录）**默认收起**，展开后完整可回看；当前阶段的展开状态与历史/工具展开状态**分开管理**。
- 逐项工具卡不再出现在默认可见区；工具参数/结果/耗时/失败详情在展开后查看。
- 完成/失败/停止自动收起；用户手动展开或正在阅读时不被普通状态更新打断（保留既有保护）。
- 受保护不变量：阶段与工具的 `stage_id`/`call_id` 归属、持久化、旧历史兼容、设计令牌。

### 1.6 附件打开与重定位（问题 5）

- 新增 `GET /api/attachments/{id}/content`：**只读 QIO 管理的副本**（`kind=copy` 且 `state=ready`），按 id 取路径，**绝不接受任意路径**；沿用本地 API 认证与文件名安全。
- 浏览器：前端 `fetch` 该接口（带认证头）→ Blob → `下载/查看`；不新增无认证的裸链接。
- 桌面：**原生打开**走 Tauri 命令 + `shell:open`（capability 已允许），但对可执行/脚本类扩展名**不自动执行**：改为「在文件夹中显示」并明确说明原因。
- 引用型（>100,000,000 字节）：历史里提供**重新定位**入口（原生选择器 → `POST /api/attachments/{id}/relocate`），重新校验大小/方式/状态；`missing/changed/failed` 如实显示并且刷新后仍能检查。

### 1.7 附件后台化（问题 6）

- 上传：用 `request.stream()` **有界分块**接收（无 Content-Length 也强制上限），落临时文件 + 哈希都在**工作线程**；事件循环线程只提交状态。
- 重新定位/复制：同样「工作线程只做文件 I/O，事件循环线程落库」。
- **不得**把整个 service 方法丢进线程（会重新引入上一轮 CI 抓到的共享 sqlite 连接并发缺陷）。
- 取消之后不得再提交为 `ready`；保持临时文件 + 改名提交 + 失败重试 + 复制期间变化处理。

---

## 2. 分工与文件归属（各自独立 worktree）

| 智能体 | worktree / 分支 | 独占文件 |
| --- | --- | --- |
| **A** | `qio-fix-a` / `wt/fix-a` | `backend/src/agent/core/{loop.py,turn.py}`、`backend/src/agent/adapters/*`、`backend/tests/test_streaming_*.py`、`test_stage_*.py`、`test_turn_timing_facts.py` |
| **B** | `qio-fix-b` / `wt/fix-b` | `frontend/src/components/{TurnProcess.vue,ApprovalModal.vue,ApprovalEntry.vue,ApprovalFacts.vue(新),MessageItem.vue,MessageStream.vue}`、`stores/{approvals.ts,session.ts,events.ts,turnProcess.ts}`、`composables/useTurnTiming.ts`、`frontend/src/**/__tests__/*` |
| **C** | `qio-fix-c` / `wt/fix-c` | `backend/src/agent/api/server.py`（附件路由 + 上传/重定位）、`services/attachments.py`、`tools/attachment_tools.py`、`frontend/src/components/{Composer.vue,AttachmentChip.vue}`、`frontend/src/services/{attachments.ts,api.ts}`、`frontend/src-tauri/**`、`backend/tests/test_attachment*.py` |
| **D** | `qio-fix-d` / `wt/fix-d` | 新增独立验收用例（`backend/tests/test_audit_*_verify.py`、`frontend/src/**/*.audit.verify.test.ts`）、`docs/verification-audit-*.md`、`scripts/verify-audit-*`；**不改实现文件** |

- 共享文件 `api/server.py` 由 **C** 统一负责；A/B/D 不改。
- 集成、契约收口、`docs/status.md`、`docs/architecture.md`、最终报告由 **Lead** 负责。
- 每个子智能体：先写**能复现问题的回归用例**（修复前红），再改实现；提交到自己的分支（不 push）。

## 3. 回归与验收计划

1. **问题 2**：延迟 300ms / 1s 后才出现的工具增量、正文之后才出现的工具调用 → 断言**没有任何文字从答案区移走**；纯回答在 provider 结束前已开始显示；原生 / Anthropic / 兼容档三条路径；事件时序 + 实际渲染证据。
2. **问题 1**：构造「简短描述 + 实际命令 + 独立说明」的审批 → 三者都有查看入口；覆盖工具执行/电脑操作/凭据/工具创建、多项排队、拒绝、过期、重连、非当前轮；**必须实际触发一次前端审批**（事件层 payload 不算）。
3. **问题 3**：附加不发送 → 重建组件/刷新 → 发纯文字：后端、模型上下文、历史消息都不得有该附件；覆盖空列表、缺字段、多草稿、切话题、失败重试、已有归属 id。
4. **问题 4**：首轮运行、两次阶段切换、同阶段三次说明、并行工具、可恢复错误、完成、重连；默认可见区**不得**出现旧说明或逐项工具卡。
5. **问题 5**：实际点击历史副本打开；删除原文件后再打开；移动引用文件后在 UI 重新定位；同名文件、重启历史、失效位置、类型不支持。
6. **问题 6**：受控慢 I/O 下上传/重定位期间其它 API、SSE、停止仍可推进；数据库无跨线程访问；**实跑 100MB 等号边界**并记录数据/时间/状态。
7. **问题 7**：provider 失败、工具可恢复错误、用户停止、程序中断、旧历史；原因属于正确轮次、操作确实生效、已知耗时不受明细失败影响。
8. 全量：后端 pytest、前端 vitest + vue-tsc、`check_docs`、CI（远端）；**实际启动应用**检查默认折叠、正式回答稳定性、审批、历史附件打开/重定位、失败入口，并提供截图与操作证据。
