# 取消确认 / 附件就绪 / 暂存故障交付 / 等待者收尾 —— 共享契约（Lead 冻结，2026-10-08）

## §0 基线与复现证据

- 基线：`origin/fix/attachment-readiness-stream-boundaries` @ `966e2fce7ab5964ed3e4bf9ae061fb86207d89c4`（已 fetch：远端 HEAD 就是它、之后无新提交；`origin/main` **不含**本轮成果，故从该修复分支起，不从 main 重开发）。
- 开发分支：`fix/preparation-cancel-and-spill-recovery`（worktree `D:\qio-dev\qio-r7`）；子 worktree `qio-r7-a|b|c|d`。
- 四处缺陷源码位置（逐条确认）：
  1. `frontend/src/components/Composer.vue` 的 `cancelPreparing()` 只做 `AbortController.abort()`；`api/server.py` 的 turns/resend 在 `await` 附件绑定后**直接 `activate()`**，没有把**真实 HTTP 断连**落实为取消。
  2. `services/attachments.py:1293`：`_reject_reason` 的 `att.state not in (STATE_PREPARED, STATE_READY, STATE_CHANGED)` —— **prepared 被当作可就绪**；`bind_for_turn`（:1137）对普通未绑定附件只写归属，不等首次后台复制、也不验证副本可读。
  3. `core/answer_buffer.py:210`：`collect()` 捕获暂存读取 `OSError` 后**只记日志**，继续返回内存前缀；`_take_buffered()` 在 `collect()` 之前取 `truncation_reason`，之后立刻 `discard()` —— 故障事实既没进事件也没进轮次状态。
  4. `core/turn.py:360`：`abandon()` **先** `self._futures.pop(ctx.turn_id, None)`，**再** `_resolve()`（内部再 pop，已找不到）→ 等待者永远不完成。

## §1 冻结契约

### §1.1 问题一：准备阶段的**可确认取消**（A）

**选择：请求标识 + 显式取消协议 + 服务端断连监测的组合**（理由：abort 只是客户端行为，不能当后端证据；而只靠断连无法覆盖「用户点中止但连接还没断」与「要给出确认」）。

- **准备标识**：`POST /api/turns` 与 `POST /api/turns/{id}/resend` 接受请求头 `X-QIO-Prepare-Id`（前端发起时生成 UUID）。服务端在 `reserve` 后登记 `prepare_id → turn_id`，并在 `activate`/`abandon` 时注销。
- **取消端点（幂等）**：`POST /api/turns/prepare/{prepare_id}/cancel` → 返回：
  - `{"ok": true, "cancelled": true, "turn_id": ...}`：该预留已被放弃（不入队、不调模型）；
  - `{"ok": true, "cancelled": false, "already_started": true, "turn_id": ...}`：**已经放行/开始** → 前端必须走**既有停止流程**并**如实**显示「已受理，已按停止取消」，**不得**再宣称「没有发送」；
  - `{"ok": true, "unknown": true}`：未知/已过期标识（幂等，不报错）。
- **服务端断连也是取消**：准备期间检测到请求断开（ASGI 层面）→ 按同一契约 `abandon` + 清理本次克隆；不依赖客户端再发任何字节。
- **取消确认之后**：本轮不得放行、不调用模型、不执行工具；**迟到的复制成功不得重新启动本轮**（沿用 `ClonePlan.cancelled` 代次校验，并在 `activate` 前复核取消标记）。
- **竞态必须覆盖**：取消 vs 登记 / 绑定中 / 复制完成边界 / `activate` 同时发生。
- **重复取消幂等**；只取消本请求，不影响其他排队或运行中的轮次；准备期间 API、SSE、取消操作持续可用。
- **收尾**：本次克隆行、临时文件、无人认领副本、台账如实收尾；**原轮副本不得删除**；草稿与附件的既有体验保留。
- **线程**：取消 asyncio 等待**不等于**终止已运行的工作线程 —— 必须处理线程迟到结果（取消标记 + 行状态复核）。
- **前端（A 负责接线）**：`cancelPreparing()` 改为「先调取消端点，以后端**确认为准**」；确认前显示准确状态（「正在中止…」），确认失败保留可理解原因与可用操作，**不提前宣称成功取消**；`already_started` 分支走 `stopActiveTurn()` 并如实说明。

### §1.2 问题四：放弃预留时先兑现等待者（A）

- `abandon()` 必须**先**兑现该 turn 的等待者（按既有约定返回失败/取消结果），**再**清结果表；`_resolve` 保持幂等。
- 重复 `abandon` / `cancel` / `shutdown` 幂等；**已终态轮次不被迟到 abandon 改写成另一终态**。
- 单个等待者超时或取消**不得**取消共享完成 future，也不影响其他等待者（`wait` 已用 `shield`，不要退回）。
- 放弃后，后续**已就绪**的预留仍能正常推进（FIFO 链不断）。
- 验收必须证明**等待调用真的返回**；「管理器里已无该 Future」或「状态已 cancelled」都**不算**通过。

### §1.3 问题二：唯一的执行就绪条件（B）

**执行就绪（冻结）= 该轮所需每个附件都满足**：
- `copy`：`state == ready` **且**副本**实际存在且可打开**（`stored_path` 可读、大小与登记一致）；
- `reference`：按既有可用性/变化规则（`changed` 走既有允许路径；`missing`/`failed` 拒绝）；
- **`prepared` 一律不就绪**：要么**等待正在进行的首次准备任务完成**，要么**结构化拒绝**（`attachment_not_ready` + 人话原因 + 可用操作=重试）。

- **覆盖所有路径**：首次登记后直接发送、普通发送、**旧客户端缺 `attachment_ids` 字段**的兜底绑定、重试克隆、resend、排队中。
- **不启动第二份重复复制**：复用已有准备任务（事件/await，**不用固定延时或轮询猜完成**）。
- **显式空附件列表**仍表示「不带附件」（不因此拒绝）。
- 任一必需附件未就绪或失败 → **不静默缺附件执行**。
- 准备等待**有界**：单请求等待上限（`PREPARE_WAIT_MS`，默认与 `ACTIVATION_ORDER_TIMEOUT` 同量级）到点结构化拒绝；FIFO 不被无限阻塞。
- 准备期间失败 / 取消 / 删除 / 重新定位 / 服务关闭 → 准确收尾；**释放磁盘闸门也不得复活已取消轮次**。
- 线程与锁：不把复制搬回事件循环；不跨 `await` 长期持有数据库事务或全局锁；DB 动作仍在既定线程。
- 保留：重试仅依赖**保存副本**（源文件已删仍可用）与 resend 失败后**再次恢复**的能力。

### §1.4 问题三：暂存读取故障的准确交付（C）

**冻结结果契约**：`AnswerBuffer.collect()` 返回结构化结果：
```python
@dataclass(frozen=True)
class BufferOutcome:
    text: str
    complete: bool
    kind: str      # complete | limit | spill_create | spill_write | spill_read
    reason: str | None   # 人话原因（过 redact）
```
- **三类必须区分**：正常完整交付 / **硬上限截断**（`limit`）/ **暂存创建·写入·读取故障**（`spill_*`）；读取故障**不得**被描述成「正文超过上限」。
- 读取失败 / 文件消失 / 内容不完整：**保留已确认可交付的内容**，并**明确告诉用户回答未完整保存/读取**。
- **事实传递时机**：`collect()` **之后**、`discard()`/缓冲清理**之前**，把故障写入①流上的**可见事件**（沿用既有事件契约，WARNING，脱敏）②轮次结果/警告。**只写日志不算交付**。
- 不得用校准事件（TURN_END/final_content）覆盖不完整事实；**不重新调用模型**伪造找回原文；不把缺失正文当完整回答。
- 正常路径（含正常超阈值 ASCII/中文）**不得**产生任何警告，仍完整交付、只 1 次调用；合法声明长正文仍**真流式**。
- 有界缓冲 + 工作线程 I/O 保持；成功 / 故障 / 取消 / 断流后临时文件都收敛。

## §2 分工与写入范围（互不重叠）

| 智能体 | 任务 | 独占写入 |
| --- | --- | --- |
| **A**（`fix-c-attachments`） | 问题 1、4 | `backend/src/agent/core/turn.py`、`backend/src/agent/api/server.py` 的 **turns/resend/取消路由**、`backend/tests/{test_r6_attachment_readiness,test_preparation_cancel_*}.py`、**前端取消接线**（`frontend/src/components/Composer.vue`、`frontend/src/services/{api,attachments}.ts`、`frontend/src/stores/session.ts`）及其前端测试 |
| **B**（`fix-b-process-ui`） | 问题 2 | `backend/src/agent/services/attachments.py`（+ 必要的准备任务协调）、`backend/tests/test_attachments_*.py`、`test_attachment_explicit_binding.py` |
| **C**（`fix-a-runtime`） | 问题 3 | `backend/src/agent/core/answer_buffer.py`、`backend/src/agent/core/loop.py`、`backend/tests/{test_streaming_*,test_loop}.py` |
| **D**（`fix-d-audit-verify`） | 独立复现与验收 | 只新增 `backend/tests/test_r7_*_verify.py`、`scripts/verify-r7-*`、`docs/verification-r7-*.md` |
| **Lead** | 契约、`docs/*`、集成、最终复验、`api/server.py` 冲突裁决 |

- **A 需要 B 的附件取消/等待接口** → 先与 Lead 约定接口，再由 B 实现；**B 需要改 `api/server.py`** → 先报 Lead，由 A 接线。
- 纪律：**先写能复现问题的反例（修复前必须红）再改实现**；模型调用一律 fake/mock 或本地假 provider；临时数据库/临时文件；不得出现密钥原文；只追加迁移；只在自己的 worktree 提交，不 push、不合 main。

## §3 验收（D 独立执行，Lead 亲自复跑四项）

**问题一**：**真 uvicorn + 真客户端 TCP 断连**，复制内部闸门控时序 → 释放后**模型 0 次、工具 0 次、无 TURN_START、无可执行队列残留**；**实际浏览器点击「中止」**核对界面提示、服务端终态与模型调用数（页面显示成功中止后不得又开始运行）；覆盖取消先于登记、取消发生在复制完成边界、重复取消、已放行时的停止行为。**不得**用取消测试端 ASGI Task、直接调 `abandon`、或只断言 `signal.aborted` 代替。
**问题二**：首次准备闸门关闭时经**真实发送接口**验证模型/工具 0 次、无 TURN_START；释放后才执行且真实 `read_attachment` 读到正确内容；覆盖显式附件列表、旧客户端缺字段、多附件最后一个未就绪、显式空列表、准备失败、取消、删除、不可读副本；重试/恢复路径同样复验（源文件已删仍可用保存副本）；**断言在闸门仍关闭时**。
**问题三**：以实际 256 KiB UTF-8 阈值触发暂存，经**真实 AgentLoop** 注入读取 `OSError` 与文件消失 → 断言**用户可见的不完整说明 + 原因 + 正式回答内容 + 最终结果一致**（**只有日志不算通过**）；覆盖创建失败、写入失败、读取失败与正常硬上限（原因不得混淆）；正常超阈值 ASCII/中文仍完整且只 1 次调用；合法声明长正文仍真流式；取消/断流后临时文件收敛。
**问题四**：一个与**多个**已开始等待的调用，证明 `abandon` 后**都及时得到一致结果**；覆盖超时后仍在等的其他调用、取消一个等待者、重复收尾、服务关闭。