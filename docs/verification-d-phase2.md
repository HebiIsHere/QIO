# D 阶段二验证报告：集成分支（独立验证 + 真实链路 + 截图）

- 验证 worktree：`D:\qio-dev\qio-up-v`，分支 `wt/up-verify2`
- 起点：`8d52634` → 中途验证 `a9caae5` → **最终验证 `2d7c2b2`**（含 A 的 created_at 修复 d940fb3、B 的 d570e25、D 的 794f831、Lead 的附件边界脚本）
- 契约：`qio-up/docs/plans/2026-10-06-unified-process-attachments-streaming.md`
- 角色：验证方（D）。本文件只写「我跑了什么、真实输出是什么」，不转述实现方结论。
- 日期：2026-10-06

## 0. 对照 plan §6 的七组验收

| # | 验收组 | 结论 | 证据 |
| --- | --- | --- | --- |
| 1 | 统一过程（立即出现/安静/同阶段更新/自主转阶段/并行工具与晚到结果/无重复气泡/完成自动收起） | **已验证** | `TurnProcess.verify.test.ts` 5/5 绿；截图 01/02/03；阶段历史里两个阶段 + 工具卡可见 |
| 2 | 审批与异常（批准/拒绝/多项待审批/可恢复错误/失败/停止/中断/重连/旧历史） | **机制已验证（事件层）/ 界面未验证** | `scripts/verify-approval-probe.py` → **[PASS]**：真链路发出 APPROVAL_REQUIRED（payload 完整）；浏览器级审批入口未捕获（§3.4） |
| 3 | 流式（provider 未结束前已有非空回答；碎片组装；取消/错误/重复/恢复/全文校准不重复） | **已验证** | 真链路 `verify-sse-local`：回答比 provider 结束早 **643ms**；`test_streaming_contract_verify.py` 6/6 绿 |
| 4 | 耗时（未展开即见总耗时；五态；明细错误不抹掉总耗时） | **已验证** | `TurnTimingContract.verify.test.ts` **8/8 绿**（含新增「状态词只出现一次」回归断言）；截图 03 显示「已完成 · 耗时 X」折叠态 |
| 5 | 附件（<、=、> 100MB 边界；多文件/同名/失败/取消/移动/删除/变更/重定位/重启/不可读类型） | **已验证（后端）** | `test_attachments_contract_verify.py` 13/13 绿（含 **100_000_000 字节实跑复制**）；前端附件 UI 见 §4 |
| 6 | 性能与视觉（真实起应用 + 截图：窄窗口/长回答/代码块/历史展开/审批/附件准备） | **6/7 场景已验证** | `docs/verification-shots/`（01–05、07、08）；审批场景未捕获 |
| 7 | check_docs + 后端 pytest + 前端 vue-tsc/vitest | **全绿（HEAD 2d7c2b2）** | 后端 2117 收集 / 0 失败；前端 1024 用例全绿；vue-tsc exit 0；check_docs 通过（§0.5） |


## 0.5 最终复跑（HEAD 2d7c2b2，Lead 要求）

| 项 | 命令 | 真实数字 |
| --- | --- | --- |
| 后端全量 pytest | `cd backend; uv run --frozen --extra dev pytest -q` | **2117 collected / 0 failed** |
| 前端全量 vitest | `cd frontend; npx vitest run` | **1024 passed（0 failed）** |
| 前端类型检查 | `npx vue-tsc --noEmit` | **exit 0** |
| 文档一致性 | `python scripts/check_docs.py` | **通过（28 个里程碑条目）** |
| D 的 8 个 verify 文件 | 5 个后端 + 3 个前端 | **35/35 + 20/20 全绿** |

结论变化：

- **A 的 STAGE created_at 缺口已转绿**（25daf6f）：`test_stage_events_always_carry_created_at` 现在通过。
- **B 的重复文案已修并已复验**（d570e25）：真机阶段历史里耗时入口现在是「耗时 294 毫秒」（不再带状态词），
  完成态折叠行是「已完成 · 耗时 X」——状态词恰好一次。我为这条缺陷补了回归断言
  （`TurnTimingContract.verify.test.ts` 新增「折叠态状态词只出现一次」），它在 2d7c2b2 上通过。
- **审批组按有界尝试收口**：见 §3.4（机制在事件层已验证，浏览器入口未捕获）。
## 1. 真实命令与真实输出（首次跑，HEAD a9caae5；最终 HEAD 2d7c2b2 的数字见 §0.5）

### 1.1 真实链路 SSE 取证（阶段二核心）

~~~text
cd D:\qio-dev\qio-up-v
powershell -ExecutionPolicy Bypass -File scripts/verify-sse-local.ps1
-> provider_ready=True / backend_ready=True pid=82628
-> credential: key_id=key_... verify_ok=True verify_state=verified
-> [PASS] 本地 SSE 冒烟全通过
~~~

关键数字（`docs/verification-e2e-stream-local.json` / `docs/verification-e2e-stream.json`）：

~~~json
{
  "turn_id": "turn_87e855774af2",
  "trace_adapter_modes": ["native"],
  "provider_requests": 2, "stream_requests": 1, "plain_requests": 1,
  "provider_chunks": 20,
  "sse_events": 23, "assistant_events": 14,
  "first_answer_lead_ms": 643,
  "first_answer_latency_ms": 474,
  "turn_end_status": "completed",
  "failed": []
}
~~~

全部检查项：QIO 请求了 stream=true ✓、provider 真分片（20 片）✓、SSE 收到 14 条非空正式回答增量 ✓、
回答先于 provider 结束 ✓、累计快照单调不回退 ✓、TURN_END.final_content 与 provider 文本一致（100 字符）✓、
完整回答只交付一次（收尾快照不算重复）✓、收尾快照是最后一条且交付全文 ✓。

### 1.2 端到端（真起应用 + 截图）

~~~text
powershell -ExecutionPolicy Bypass -File scripts/verify-e2e.ps1
-> SSE 取证 failed: []
-> [PASS] 统一过程区：运行中截图
-> [PASS] 完成自动收起截图
-> [PASS] 长回答与代码块截图
-> [PASS] 阶段历史展开截图   （region: 2 个阶段 · 2 次调用，两个工具卡与失败原因都在）
-> [PASS] 窄窗口截图
-> [FAIL] 审批内联截图       （inline=false, modal=false —— 见 §3.4）
-> [PASS] 附件准备中截图 / 附件登记完成截图
~~~

### 1.3 八个 verify 文件（集成分支）

~~~text
cd backend
uv run --frozen --extra dev pytest tests/test_stage_protocol_verify.py tests/test_streaming_contract_verify.py tests/test_timing_contract_verify.py tests/test_attachments_contract_verify.py tests/test_migrations_append_only_verify.py -q
-> 35 passed / 1 failed（36 条）

cd frontend
npx vitest run src/stores/__tests__/streamingDeltas.verify.test.ts src/components/__tests__/TurnProcess.verify.test.ts src/components/__tests__/TurnTimingContract.verify.test.ts
-> Test Files 3 passed (3)，Tests 19 passed (19)
~~~

唯一一条红：`test_stage_events_always_carry_created_at`（§3.1）。

### 1.4 全量闸门

~~~text
cd backend;  uv run --frozen --extra dev pytest -q   -> 2116 tests collected，1 failed（就是上面那条）
cd frontend; npx vitest run                          -> Test Files 113 passed (113)，Tests 1023 passed (1023)
cd frontend; npx vue-tsc --noEmit                    -> exit 0
python scripts/check_docs.py                         -> 文档一致性检查通过（27 个里程碑条目）
~~~

## 2. 阶段一「未验证」清单的结转

| 阶段一未验证项 | 现在 |
| --- | --- |
| A/B/C 实现是否满足契约 | **已验证**（8 个 verify 文件 55 条：54 绿 + 1 条实现缺口） |
| 浏览器链路 e2e 与截图 | **6/7 已验证**（审批场景未捕获） |
| 「回答先于 provider 结束」在真链路 | **已验证**（早 643ms） |
| 100MB 边界真实复制 | **已验证**（=100_000_000 实跑 copy、> 实跑 reference） |
| 附件前端 UI | 部分：附件「准备中/已保存」截图与文案已验证；完整交互（多文件、复制失败、取消、移动、删除、变更、重定位）**未验证** |
| 审批与异常在过程区的呈现 | **未验证**（见 §3.4） |
| bus 合并键 (type, turn_id, delta_id) 后端行为 | **未验证**（前端可观察后果已验；A 侧背压/合并用例我没跑） |

## 3. 发现

### 3.1 实现缺口（A 正在修）：STAGE 收口事件 created_at 为 null

~~~text
FAILED tests/test_stage_protocol_verify.py::test_stage_events_always_carry_created_at
AssertionError: ('契约 §1.3：每个 STAGE 事件都要带 created_at',
  [{'turn_id': 'turn_d065eb1130c6', 'stage_id': 'st_d065eb11_1', 'index': 1, 'status': 'done',
    'op': 'end', 'narrative_id': None, 'text': '', 'created_at': None, ...}])
~~~

契约 §1.3 把 created_at 写成时间字符串；实测只有**系统收口事件**（op=end，没有落库行）为 null，
其余事件（start/update/next）都带时间戳。前端排序有兜底，影响小；Lead 已确认派给 A 修。**已在 25daf6f 修复并合并（d940fb3）：2d7c2b2 上这条用例通过。**

### 3.2 UI 缺陷（B）：状态行重复「已完成」

截图 `03-process-collapsed-complete.png` 折叠态实际显示：

~~~text
● 已完成 · 已完成 · 耗时 2.6 秒
~~~

原因：过程区状态词（已完成）+ `TurnTimingPanel` 折叠文案自身也以「已完成 · 耗时 X」开头。
同一行出现两次「已完成」。我的 verify 用例只断言「耗时 + 总耗时可见」，没有断言「状态词不重复」，
所以单测没拦住；这是**视觉检查才发现的**。B 已在 d570e25 修复并合并（2d7c2b2）：
耗时入口不再拼状态词，完成态折叠行只剩一处「已完成」。我为这条缺陷补了回归断言
（`TurnTimingContract.verify.test.ts` 新增用例：折叠态过程区 text() 里状态词恰好 1 次、耗时入口不含状态词），
它在 2d7c2b2 上通过；真机截图 02 里耗时入口显示为「耗时 294 毫秒」。

### 3.3 我的 harness 缺陷（已修）：旧进程占端口造成「假绿/假红」

第一次跑 `verify-sse-local.ps1` 时结果全是「没有流式」（provider_chunks=0、assistant_events=0），
而进程内用真 NativeAdapter 的同一路径是流式的。定位结果：

- 阶段一冒烟留下的 uvicorn（**基线代码**）仍在监听 8734（17:16 启动，pid 83980）；
- 我的清理只杀了 cmd.exe 包装进程，python 子进程活了下来；
- 新后端起不来，/api/health 由**旧进程**应答 → 脚本判定 backend_ready=true，实际测的是旧代码。

已修（两处）：脚本启动前做**端口预检**（占用即报错并指出 pid），收尾用 taskkill /PID <pid> /T /F 杀进程树。
修完后同一条命令全绿。这条教训对 Lead 的闸门同样适用：**端口被占用时的「就绪」是假的**。

### 3.4 审批组：机制已在事件层验证，浏览器入口未捕获

**有界尝试的结论（Lead 要求）**：

1. **机制存在（已验证，事件层）**：`scripts/verify-approval-probe.py` 自己起假厂商 + uvicorn + 真 SSE，
   脚本化一次 `run_shell`（参数 `cmd`），实测：

~~~text
sse_event_types: ['TURN_START','TURN_QUEUE','TURN_QUEUE','CAPABILITY','ANCHOR','TOOL_START','APPROVAL_REQUIRED']
APPROVAL_REQUIRED count: 1
approval payload: {"approval_id":"appr_...","kind":"computer","payload":{"action":"run_shell",
  "cmd":"echo qio-approval-probe","risk":"high","description":"想运行一条 shell 命令",
  "capabilities":["联网：否","读取文件：否","写入文件：否","启动进程：是","使用凭据：无","副作用：destructive"],"scope":"once"}}
[PASS] 真链路发出了 APPROVAL_REQUIRED（机制存在）
~~~

   注意：这条探针前两版是**我的脚本 bug**，不是实现问题 —— (a) `run_shell` 的参数是 `cmd`，我写成 `command`，
   工具直接报「cmd 必填」；(b) 假厂商脚本是 FIFO，凭据验证会先吃掉若干步，轮到本轮只剩文本回复。两处都修掉后才得到上面的结果。

2. **浏览器级审批入口未捕获（未验证）**：Playwright 驱动真实页面（发送 `run_shell` 轮次、失焦输入框、
   查 `[data-test="turn-process-approval"]` / 审批模态 / 「确认·待办·审批」入口按钮）都没有出现审批 UI。
   **我没有把根因定性**（候选：`autoOpen` 的焦点门控、inline claim 要求队列头、`kind=computer` 的路由），
   只记录事实。界面机制由 B 的 DOM 级测试覆盖（approvalInlineGating / approvalRouting / ApprovalDefer / ApprovalModal 全绿）。

3. 因此本组口径：**事件层已验证；浏览器级截图未验证**。

## 4. 未验证（诚实清单）

1. **审批与异常组**在真实界面上的呈现：事件层已验（APPROVAL_REQUIRED 到达），**浏览器入口未捕获**；批准/拒绝/多项待审批/可恢复错误/失败/停止/中断/重连/旧历史的完整 UI 路径未逐一验证。
2. **附件前端完整交互**：多文件、复制失败、取消、移动、删除、变更、重定位在 UI 上的表现未逐一验证（后端 13 条契约已验）。
3. **bus 合并键 (type, turn_id, delta_id) 的后端背压/合并行为**：未跑 A 侧用例。
4. **真实厂商**：不适用——本报告全部基于本机假厂商端点，只证明 QIO 自己的链路。
5. **长时/并发压力**：未做（同一时刻多轮、断网重连窗口内的流式合并）。

## 5. 截图清单（`docs/verification-shots/`）

| 文件 | 场景 | 结果 |
| --- | --- | --- |
| 01-process-running.png | 统一过程区运行中 | PASS |
| 02-stage-history.png | 阶段历史展开（2 阶段 + 工具卡 + 失败原因） | PASS |
| 03-process-collapsed-complete.png | 完成自动收起（保留状态 + 总耗时） | PASS（**2d7c2b2 重截**：折叠行「已完成 · 耗时 X」状态词只出现一次） |
| 04-long-answer-codeblock.png | 长回答 + 代码块 + 表格 | PASS |
| 05-narrow.png | 窄窗口 480x900（阶段历史 + 长错误换行不溢出） | PASS |
| 06-approval-inline.png | 审批内联 | **未捕获**（§3.4） |
| 07-attachment-preparing.png | 附件准备中 | PASS |
| 08-attachment-ready.png | 附件登记完成 | PASS |
| shots-summary.json | 每个场景的 ok/失败原因 | — |

注：截图里顶部有「还没设置完 QIO…」横幅，是 harness 只标记了 onboarding「已看」没有走完引导所致，不是产品缺陷。

## 6. 复现步骤

~~~text
cd D:\qio-dev\qio-up-v
powershell -ExecutionPolicy Bypass -File scripts/verify-sse-local.ps1
powershell -ExecutionPolicy Bypass -File scripts/verify-e2e.ps1
cd backend;  uv run --frozen --extra dev pytest tests/test_stage_protocol_verify.py tests/test_streaming_contract_verify.py tests/test_timing_contract_verify.py tests/test_attachments_contract_verify.py tests/test_migrations_append_only_verify.py -q
cd frontend; npx vitest run src/stores/__tests__/streamingDeltas.verify.test.ts src/components/__tests__/TurnProcess.verify.test.ts src/components/__tests__/TurnTimingContract.verify.test.ts
~~~

前置：8734 / 5199 / 8798 三个端口必须空闲（脚本会预检并报出占用者 pid）。
