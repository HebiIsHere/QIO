# D 阶段二验证报告：集成分支（独立验证 + 真实链路 + 截图）

- 验证 worktree：`D:\qio-dev\qio-up-v`，分支 `wt/up-verify2`
- 起点：`8d52634`（Lead 集成提交）→ 已 fast-forward 到 `a9caae5`（含 A 的 loop 修复）
- 契约：`qio-up/docs/plans/2026-10-06-unified-process-attachments-streaming.md`
- 角色：验证方（D）。本文件只写「我跑了什么、真实输出是什么」，不转述实现方结论。
- 日期：2026-10-06

## 0. 对照 plan §6 的七组验收

| # | 验收组 | 结论 | 证据 |
| --- | --- | --- | --- |
| 1 | 统一过程（立即出现/安静/同阶段更新/自主转阶段/并行工具与晚到结果/无重复气泡/完成自动收起） | **已验证** | `TurnProcess.verify.test.ts` 5/5 绿；截图 01/02/03；阶段历史里两个阶段 + 工具卡可见 |
| 2 | 审批与异常（批准/拒绝/多项待审批/可恢复错误/失败/停止/中断/重连/旧历史） | **部分验证** | DOM 锚点与 store 行为在单测里绿；**真机审批截图未能触发**（见 §3.4）—— 这一组整体按「未验证」计 |
| 3 | 流式（provider 未结束前已有非空回答；碎片组装；取消/错误/重复/恢复/全文校准不重复） | **已验证** | 真链路 `verify-sse-local`：回答比 provider 结束早 **643ms**；`test_streaming_contract_verify.py` 6/6 绿 |
| 4 | 耗时（未展开即见总耗时；五态；明细错误不抹掉总耗时） | **已验证** | `TurnTimingContract.verify.test.ts` 7/7 绿；截图 03 显示「已完成 · 耗时 2.6 秒」折叠态 |
| 5 | 附件（<、=、> 100MB 边界；多文件/同名/失败/取消/移动/删除/变更/重定位/重启/不可读类型） | **已验证（后端）** | `test_attachments_contract_verify.py` 13/13 绿（含 **100_000_000 字节实跑复制**）；前端附件 UI 见 §4 |
| 6 | 性能与视觉（真实起应用 + 截图：窄窗口/长回答/代码块/历史展开/审批/附件准备） | **6/7 场景已验证** | `docs/verification-shots/`（01–05、07、08）；审批场景未捕获 |
| 7 | check_docs + 后端 pytest + 前端 vue-tsc/vitest | **1 条红** | 后端 2116 收集 / 1 失败（A 的 STAGE created_at，见 §3.1）；前端 113 文件 1023 用例全绿；vue-tsc exit 0；check_docs 通过 |

## 1. 真实命令与真实输出

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
其余事件（start/update/next）都带时间戳。前端排序有兜底，影响小；Lead 已确认派给 A 修，修完这条应变绿。

### 3.2 UI 缺陷（B）：状态行重复「已完成」

截图 `03-process-collapsed-complete.png` 折叠态实际显示：

~~~text
● 已完成 · 已完成 · 耗时 2.6 秒
~~~

原因：过程区状态词（已完成）+ `TurnTimingPanel` 折叠文案自身也以「已完成 · 耗时 X」开头。
同一行出现两次「已完成」。我的 verify 用例只断言「耗时 + 总耗时可见」，没有断言「状态词不重复」，
所以单测没拦住；这是**视觉检查才发现的**。建议 B 二选一：状态词与耗时文案合并，或耗时文案去掉状态前缀。

### 3.3 我的 harness 缺陷（已修）：旧进程占端口造成「假绿/假红」

第一次跑 `verify-sse-local.ps1` 时结果全是「没有流式」（provider_chunks=0、assistant_events=0），
而进程内用真 NativeAdapter 的同一路径是流式的。定位结果：

- 阶段一冒烟留下的 uvicorn（**基线代码**）仍在监听 8734（17:16 启动，pid 83980）；
- 我的清理只杀了 cmd.exe 包装进程，python 子进程活了下来；
- 新后端起不来，/api/health 由**旧进程**应答 → 脚本判定 backend_ready=true，实际测的是旧代码。

已修（两处）：脚本启动前做**端口预检**（占用即报错并指出 pid），收尾用 taskkill /PID <pid> /T /F 杀进程树。
修完后同一条命令全绿。这条教训对 Lead 的闸门同样适用：**端口被占用时的「就绪」是假的**。

### 3.4 未能捕获：审批内联截图

run_shell（cmd_tools.py:153 无条件请求审批）与 fs_write（工作区外应 approve）两种触发方式，
在默认权限模式下都没出现可截图的审批：`[data-test="turn-process-approval"]` 与审批模态（role=dialog 含「拒绝」）都不存在。
我的驱动方式（Playwright 填输入框 → 发送 → 等锚点）与工具参数都核对过（run_shell 的参数是 cmd，已修正）。
**未定位到根因**：可能是默认权限模式下判定为 deny（直接拒绝而非请求批准）、审批路由（inline claim）条件、
或工具执行前置校验。按诚实原则记为**未验证**，并把真实失败输出留在这里。

## 4. 未验证（诚实清单）

1. **审批与异常组**（批准/拒绝/多项待审批/可恢复错误/失败/停止/中断/重连/旧历史）在真实界面上的呈现：截图未捕获；只有单测与 DOM 锚点层面的证据。
2. **附件前端完整交互**：多文件、复制失败、取消、移动、删除、变更、重定位在 UI 上的表现未逐一验证（后端 13 条契约已验）。
3. **bus 合并键 (type, turn_id, delta_id) 的后端背压/合并行为**：未跑 A 侧用例。
4. **真实厂商**：不适用——本报告全部基于本机假厂商端点，只证明 QIO 自己的链路。
5. **长时/并发压力**：未做（同一时刻多轮、断网重连窗口内的流式合并）。

## 5. 截图清单（`docs/verification-shots/`）

| 文件 | 场景 | 结果 |
| --- | --- | --- |
| 01-process-running.png | 统一过程区运行中 | PASS |
| 02-stage-history.png | 阶段历史展开（2 阶段 + 工具卡 + 失败原因） | PASS |
| 03-process-collapsed-complete.png | 完成自动收起（保留状态 + 总耗时） | PASS（含 §3.2 的重复文案） |
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
