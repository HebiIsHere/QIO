# 四项剩余问题：附件就绪放行 / 声明解析 / 长正文退路 / 失败原因保留 —— 共享契约（Lead 冻结，2026-10-08）

## §0 基线与复现证据

- 基线：`origin/fix/process-upload-final-convergence` @ `6507c649be9dac60229f706ef52dfefe54dd815c`（已 fetch 确认：远端 HEAD 就是它、之后无新提交；`origin/main` 未包含）。
- 开发分支：`fix/attachment-readiness-stream-boundaries`（worktree `D:\qio-dev\qio-r6`）；子 worktree `qio-r6-a|b|c|d`。
- 四处缺陷的源码位置（已逐条确认）：
  1. `api/server.py:1566→1571`（turns）与 `:1799→1804`（resend）：**先 `ctx.turns.submit(...)` 再 `await attachments.bind_for_turn(...)`**；bind 异步化后，等待复制会让出事件循环 → 模型可在附件就绪前启动。
  2. `core/loop.py:271`：`if len(probe) > MARKER_PROBE_CHARS(32)` → 判为 undeclared；**合法声明 + 长正文落在同一大分块时识别失败**，整段被当未声明（结束后连声明一起泄漏）。
  3. `core/loop.py:314`：未声明正文超过 `UNDECLARED_BUFFER_LIMIT(256*1024 字符)` 即改判 `interim` 放行到过程区；调用结束无工具时 `_call_answer_text` 为空 → `_maybe_fallback`（:1535）**再生成一次** → 2 次调用、两份显示。另：`len(str)` 是**字符数**，与注释「256 KB」不一致。
  4. `services/attachments.py:1682-1685` `_check`：`kind == copy` 且没有可读 `stored_path` → 一律 `missing` +「QIO 保存的副本文件已经不在了」；**把从未保存成功的 `failed` 上传的真实原因覆盖掉**。

## §1 冻结契约

### §1.1 问题一：附件就绪后才放行执行（B）

**生命周期：预留 → 准备 → 放行**（不得先入队再等附件）：
```python
# core/turn.py（TurnManager）
def reserve(self, message, topic_id, *, intent_id=None) -> TurnContext   # 分配 turn_id、建台账行；**不入队**、不发 TURN_START
def activate(self, ctx) -> None                                          # 附件就绪后放行：入队并唤醒 worker（此时才发 TURN_START）
def abandon(self, ctx) -> None                                           # 准备失败/取消：丢弃预留，不留队列项
```
- **FIFO 保持**：放行按**预留顺序**进行；后预留的即使先就绪也要等前面的放行（有界等待，避免乱序）。
- 路由顺序（turns 与 resend 都要改）：`precheck` → `reserve` → **await 准备/克隆/校验** → 失败：`abandon` + 结构化拒绝（**不入队**）；成功：`activate` → 返回回执（`bound_attachment_ids` / `rejected`）。
- **任一必需附件失败即明确拒绝**，绝不「先执行再取消」，也绝不静默缺附件执行。
- 副本必须**实际可读**（`stored_path` 存在且可打开）才算就绪；引用附件按既有可用性/变化规则处理。
- 准备期间：**不伪造模型已开始**（无 TURN_START、无模型调用、无工具执行）；前端沿用现有「发送中」状态并显示统一的**正在准备附件**提示（前端改动需先报 Lead），取消 = 中止该请求。
- 准备失败/取消后：不遗留可执行队列项、永久准备状态、无人认领副本、迟到 `ready` 提交。
- **resend 的 claim** 不能因附件准备失败被永久消耗：准备成功后才 claim+activate；失败 → 释放 claim。
- 客户端断开 / DELETE / 服务关闭发生在准备期间 → `abandon` + 清理本次克隆；台账行如实收尾（不得事后看起来像「被中断的一轮」）。
- 不用固定延时或轮询判断就绪；用明确的就绪事实。
- 不把复制搬回事件循环；不跨 `await` 长时间持有数据库事务或全局锁；准备期间其它 API/SSE/停止仍能推进。

### §1.2 问题二：增量前缀解析（A）

- 判定只依赖**控制前缀**：`_probe` 只保存「识别声明所需的部分」，最多 `len(ANSWER_MARKER)+2` 个字符；**任何超出部分都是正文**，不得再参与判定。
- 删除 `len(probe) > MARKER_PROBE_CHARS` 这条判据（它把「累计收到的正文长度」当成「声明是否有效」的证据）。
- 匹配成功后，**同一分块里剩下的正文立即交给回答流**（实时发布，不整段等待）。
- **分块无关性（核心不变量）**：同一字节/文本序列，无论怎样拆分或合并，**角色判定、最终正文、声明隐藏、控制流语义完全一致**。
- 规则保持不变：大小写不敏感；声明后可跟 LF 或 CRLF；声明被拆到多个分块要能拼出来；空分块忽略；非法（不在开头/重复/被改坏）按未声明处理且**原文照实保留**。
- 保持 `delta_id`、`seq`、累计快照、去重、`TURN_END` 校准；不靠提高 32 的上限、不对前端做字符串替换、不删正文里的普通字面内容来掩盖泄漏。

### §1.3 问题三：未声明长正文的角色待定退路（A）

- **缓冲上限只能管理资源，不能决定角色**：超过上限**不再**改判 `interim`，也**不构成**「有工具调用」或「整轮没有回答内容」的证据。
- 退路：**有界内存 + 临时磁盘暂存**：
  - `UNDECLARED_MEMORY_LIMIT = 256 * 1024`（**统一按 UTF-8 字节计量**；契约、实现、测试同一单位，并纠正旧注释）；
  - 超出后写入暂存文件（`<data_dir>/tmp/` 下，按 `delta_id` 命名），**追加写在工作线程**，事件循环只记账；
  - 硬上限 `UNDECLARED_SPILL_LIMIT`（默认 64 MiB）：达到即**如实报告**（可见 WARNING + 截断事实写进交付内容/轮次警告），**不得无界增长、偷偷丢字、擅自换角色或重写答案**。
- 调用结束时的归属：
  - **无工具调用** → 该正文（内存 + 暂存）**一次性**交付正式回答区（同 `delta_id`、`{interim:false, streaming:false}`），**不重新生成、不先显示在过程区**；
  - **有工具调用** → 按过程规则**按序完整**放行到过程区（说明不丢字）；
  - 合规声明路径**继续真实流式**（不得因本轮改动退化）。
- `_maybe_fallback` **只在确实没有可交付回答内容时**触发；暂存中的正文属于「已经生成的内容」。
- 清理：取消 / 断流 / 服务关闭 / 重启 → 删除暂存文件、不留待决任务；启动时清理陈旧暂存文件。
- 不通过扩大阈值、降低 provider 输出上限、截断测试输入、或只给测试正文补声明来「修复」。

### §1.4 问题四：失败原因保留（C）

- `_check` 必须区分两个事实：**(a) 保存失败、从未产生有效副本** vs **(b) 曾成功保存、后来副本丢失**。
- 冻结规则：
  - `failed` **在 GET / list / history / payload 可用性检查下是粘性的** —— 不得自动改成 `missing` 或 `ready`，原始 `error` 不得被通用文案覆盖；
  - 只有**显式重试**成功、或**可验证的恢复**（记录的 `stored_path` 存在且 `sha256` 与登记值一致）才可转 `ready`；
  - 曾成功保存后丢失的副本（`ready → 文件消失`）继续如实显示 `missing`（行为不变）；
  - `prepared` / `cancelled` 的既有行为不变。
- **可用操作按 kind 区分**：copy + 从未保存成功 → **重试**（不提供「重新定位」）；reference + failed/missing/changed → **重新定位**；浏览器字节上传（无 `source_path`）→ 明确**不能自行从原地址恢复**（提供重试/重新上传），界面不得暗示 QIO 能自己找回。
- 原因必须是**用户可见**的（重新打开界面仍能看到），不能只留在服务端日志；日志/事件继续脱敏。
- 需要新字段时**只追加迁移**；不为本轮引入无关 schema。

## §2 分工与写入范围（互不重叠）

| 智能体 | 独占写入 |
| --- | --- |
| **A** | `backend/src/agent/core/loop.py`、`adapters/*.py`、新增缓冲/暂存模块、`backend/tests/{test_streaming_*,test_loop,test_stage_protocol*}.py` |
| **B** | `backend/src/agent/core/turn.py`、`backend/src/agent/api/server.py` 的 **turns/resend 路由段**、orchestrator 接线（`services/app.py` 相关处）、其实现测试 |
| **C** | `backend/src/agent/services/attachments.py`、其实现测试（`test_attachments_*.py`）；**涉及前端/其它文件先报 Lead** |
| **D** | 只新增 `backend/tests/test_r6_*_verify.py`、`scripts/verify-r6-*`、`docs/verification-r6-*.md`；**不改生产实现** |
| **Lead** | `docs/*`、集成、路由与共享文件冲突裁决、最终复验 |

- B 需要改 attachments 服务时**先提接口需求**，由 C 或 Lead 实施；A/B/C 不得修改 D 的验证文件。
- 纪律：**先写能复现问题的反例（修复前必须红）再改实现**；模型调用一律 fake/mock provider；临时数据库/临时文件；不得出现密钥原文；数据库只追加迁移；只在自己的 worktree 提交，不 push、不合 main。

## §3 验收（D 独立执行，Lead 亲自复跑四条反例）

**问题一**（真实 HTTP/ASGI 路由 + 真实 TurnManager/TurnOrchestrator + 假 provider）：复制闸门关闭时**模型调用数必须为 0、工具执行数 0**；释放后新轮 `read_attachment` 读出正确内容才允许后续回答；复制失败 → 模型始终 0 次调用 + 准确原因；准备期间取消/删除附件/服务关闭 → 释放磁盘闸门后**仍不能**重新开始执行；多附件最后一个未就绪、排队期间准备失败、resend 准备失败后**再次恢复**；同期 API/SSE 能推进；源轮历史副本与归属正确。**关键断言在磁盘闸门仍关闭时**。

**问题二**：固定一份含声明的正文，覆盖：声明与全部正文同一大分块；声明内部**每个位置**拆分；声明单独一块 + 其后整个正文一块；一字符一块；空块；确定性随机分块；大小写、LF/CRLF、中文、代码块、表格、长英文；整段响应、流式响应、取消、断流。断言**相同最终正文、相同角色、无声明泄漏、无重复生成**；provider 在首段正文后暂停时正式回答容器已可见、过程区无副本；**至少一组经 NativeAdapter + 本地假 SSE provider**。

**问题三**：以**实际阈值**为依据，覆盖阈值前 / 等号 / 阈值后 / 明显超过；ASCII 与中文分别检查**字符数与 UTF-8 字节数**；一整块大正文与多个小块结果一致；未声明长回答无工具 → **只一次调用**、完整内容一次交付、过程区无副本；长说明后有真实工具调用 → 工具执行正确且说明完整；暂存创建/写入失败、取消、断流与清理；内存有界、事件循环与同期 API/SSE 可推进。同时验证问题二的合法声明长正文**仍真流式**。

**问题四**：真实上传路由注入建目录/打开/写入/权限/空间错误 → HTTP 错误、原始数据库状态、首次 GET、重复 GET、列表、历史 payload、重新打开后的原因**一致**；失败记录保持 `failed` 且原始原因未被通用 `missing` 文案覆盖；已成功副本后来删除仍转 `missing`；重试成功准确改 `ready`、再次失败保留**新的**失败原因；`prepared/cancelled` 不回退；浏览器无原路径与本地路径附件各自操作真实可用。若涉及前端，**实际起应用截图**证明原因与按钮正确（不能只凭「不是准备中」判通过）。