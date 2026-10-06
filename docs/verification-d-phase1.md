# D 阶段一验证报告：独立验证 + 耗时口径取证（基线 ee6bbff）

- worktree：`D:\qio-dev\qio-up-d`，分支 `wt/up-d`，基线 `ee6bbff1841b869be8b2e17dd85dc434b496b839`
- 契约：`qio-up/docs/plans/2026-10-06-unified-process-attachments-streaming.md`（§1 阶段协议、§2 流式、§3 耗时、§4 附件）
- 角色：验证方（D）。本文件只写「我跑过什么、真实输出是什么」，不转述实现方结论。
- 日期：2026-10-06

## 0. 一句话结论

A/B/C 的实现**尚未合并**，所以按契约写好的验证用例在基线上**大量为红**，这正是「现状不满足契约」的证据。
本阶段已交付：验证用例（后端 34 条 / 前端 19 条）、真 SSE 假厂商端点（自检 PASS）、耗时口径取证脚本（已跑出真实数字）、阶段二 e2e 驱动脚本（**未运行**）。

| 类别 | 结果 |
| --- | --- |
| 后端验证用例 | 34 条：**31 红 / 3 绿**（红=契约未实现） |
| 前端验证用例 | 19 条：**12 红 / 7 绿** |
| 前端类型检查 npx vue-tsc --noEmit | exit 0（含新增验证用例） |
| 假厂商端点自检 | **PASS**（流式分片/工具碎片/断流/500/时间线/脱敏） |
| 耗时口径取证 | 已跑出真实数字（见 §4） |
| 端到端（真起应用 + 截图） | **未运行**（阶段二） |
| 本地 SSE 冒烟（假厂商 + 真 uvicorn + 真 SSE，无前端/截图） | **已跑通 harness**；契约项 2 红（见 §2.5） |

说明：本文件里的红/绿条数是 **2026-10-06 在基线 ee6bbff 上的取证快照**，不是长期状态；实现合并后这些数字必须重跑、以重跑结果为准（命令见各节）。

## 1. 已实现（D 交付的验证资产）

新增文件（均在我的写范围内；未改 A/B/C 名下实现文件）：

| 文件 | 作用 |
| --- | --- |
| `backend/tests/test_stage_protocol_verify.py` | 阶段协议：STAGE 事件字段/op 语义/index 单调/stage_id/工具归属/截断脱敏/持久化 raw.stage/模型不能伪造状态 |
| `backend/tests/test_streaming_contract_verify.py` | 流式：BaseAdapter.stream 默认面、真 SSE 路径（回答先于 provider 结束）、工具参数碎片组装、断流保留已确认文本、text 档不假装流式 |
| `backend/tests/test_timing_contract_verify.py` | 耗时：TURN_END 四个权威字段、排队与执行时长分开、并行分项 vs 总耗时取证、缺失!=0、residual 归零 |
| `backend/tests/test_attachments_contract_verify.py` | 附件：<、=、>100_000_000 边界、多文件同名、缺失/变化/重定位、删除不动原文件、read_attachment 真实内容与分段、不可读类型诚实说明 |
| `backend/tests/test_migrations_append_only_verify.py` | 历史迁移只追加（基线 25 条迁移的规范化 sha256 冻结为 `bce312cf92d7a3743b5fbe04c6e15a87b4b770e4132b21af343965eaca7d4f6a`）、attachments 表结构 |
| `frontend/src/stores/__tests__/streamingDeltas.verify.test.ts` | 事件→store：正式回答真流式、累计快照、seq 去重按 delta_id、答案/过程改判不重复、final_content 只校准、取消保留文本 |
| `frontend/src/components/__tests__/TurnProcess.verify.test.ts` | 统一过程区 DOM：立即出现/安静/同阶段更新/自主转阶段/并行工具与晚到结果/无重复/完成自动收起 |
| `frontend/src/components/__tests__/TurnTimingContract.verify.test.ts` | 耗时 DOM + composable 五态：未展开即见总耗时、不显示「读取中」、一轮一个入口、明细失败不抹掉总耗时、缺失!=0 |
| `scripts/verify_stream_provider.py` | 支持真 SSE 分片、工具参数碎片、中途断流的假厂商端点（控制面 /__log /__timeline /__script） |
| `scripts/verify-timing-accounting.ps1` | 耗时口径取证：跑契约用例 + 打印 TIMING_EVIDENCE 真实数字与结论 |
| `scripts/verify_sse_capture.py` | 真实 SSE 链路取证：记录每个事件墙钟，判断「回答是否先于 provider 结束」 |
| `scripts/verify-e2e.ps1` | 阶段二驱动：provider → e2e_up → 建假凭据 → SSE 取证 → msedge 无头截图 |

## 2. 已验证（真实命令 + 真实输出）

### 2.1 假厂商端点自检

~~~text
cd backend
uv run --frozen python ..\scripts\verify_stream_provider.py --selftest
-> exit=0
[PASS] verify_stream_provider 自检通过：流式分片/工具碎片/断流/500/时间线/脱敏
~~~

（端点自身不做任何真实厂商校验；它只证明 QIO 侧链路能否被这个假端点驱动。）

### 2.2 后端验证用例（基线，应当红）

~~~text
cd backend
uv run --frozen --extra dev pytest tests/test_stage_protocol_verify.py tests/test_streaming_contract_verify.py tests/test_timing_contract_verify.py tests/test_attachments_contract_verify.py tests/test_migrations_append_only_verify.py -q --tb=no
-> 31 FAILED，3 passed（收集 34 条）
~~~

逐文件红绿：

| 文件 | 红 / 绿 | 现状事实（来自断言消息，不是推测） |
| --- | --- | --- |
| test_migrations_append_only_verify.py | 1 / 1 | 历史迁移摘要一致（绿）；`attachments` 表不存在（红） |
| test_stage_protocol_verify.py | 9 / 0 | `agent.core.stage` 不存在；一轮里 0 条 STAGE 事件 |
| test_streaming_contract_verify.py | 6 / 0 | `BaseAdapter` 无 `stream`；整轮没有任何非空 ASSISTANT 增量 |
| test_timing_contract_verify.py | 2 / 2 | TURN_END 实际键 `['error','final_content','instance_id','iterations','revision','status','tokens','tool_calls','turn_id']`；口径取证 2 条绿 |
| test_attachments_contract_verify.py | 13 / 0 | `POST /api/attachments` 返回 404 |

代表性失败原文：

~~~text
AssertionError: 契约 §3：TURN_END 缺耗时字段 ['duration_ms', 'queue_ms', 'started_at', 'ended_at']；
实际键=['error', 'final_content', 'instance_id', 'iterations', 'revision', 'status', 'tokens', 'tool_calls', 'turn_id']

AssertionError: 契约 §1.3：一轮里出现阶段就必须发 STAGE 事件
assert []

AssertionError: 契约 §2：守卫窗口是有界的，不能等整段响应结束才播放
assert None is not None

AssertionError: (404, '{"detail":"Not Found"}')   # POST /api/attachments
~~~

### 2.3 前端验证用例（基线，应当红）

~~~text
cd frontend
npx vitest run src/stores/__tests__/streamingDeltas.verify.test.ts src/components/__tests__/TurnProcess.verify.test.ts src/components/__tests__/TurnTimingContract.verify.test.ts
-> Test Files 3 failed (3)，Tests 12 failed | 7 passed (19)
~~~

代表性失败原文（逐条对应验收清单里的「现状问题」）：

| 契约点 | 真实断言输出 |
| --- | --- |
| 折叠态永远「读取中」/ 未展开没有总耗时 | `未展开必须看得见总耗时: expected '耗时读取中这次没有留下耗时记录' to match /耗时[\s\S]{0,8}1\.5/` |
| 一轮多个耗时入口 | `契约 §3：过程区与耗时面板只保留一个入口: expected 2 to be less than or equal to 1` |
| 正式回答无增量（被当过程） | `expected +0 to be 1`（没有任何非 interim 的助手消息） |
| seq 无去重、回退会覆盖文字 | `expected [ '已经确认' ] to deeply equal [ '已经确认的一段话' ]` |
| 同轮二次 interim/delta 互相覆盖 | `不同的 delta_id 不得互相覆盖: expected [ '第二次模型的正式回答' ] to include '第一次模型说的话'` |
| 守卫放行后改判（答案→过程） | `守卫放行之后、改判之前，这段文字属于正式回答: expected false to be true` |
| 统一过程区不存在 | `契约 §1.5：一轮必须有一个可识别的过程区（锚点 [data-test="turn-process"]）: expected null not to be null` |
| 耗时五态（composable） | 5 条绿：idle/loading/ready/missing/error 与「旧记录 totalMs=null 不是 0」 |


### 2.5 本地 SSE 冒烟（真 uvicorn + 真 SSE + 假厂商；无前端/截图）

~~~text
cd D:\qio-dev\qio-up-d
powershell -ExecutionPolicy Bypass -File scripts/verify-sse-local.ps1
-> provider_ready=True / backend_ready=True
-> credential: key_id=key_aad04311aef1 verify_ok=True verify_state=verified
-> capture_exit=1（基线应为红）
~~~

真实取证（完整 JSON：docs/verification-e2e-stream-local.json）：

~~~json
{
  "turn_id": "turn_002bd1ffd62f",
  "provider_chunks": 0,          // QIO 全程没有向 provider 请求流式（没有 stream=true）
  "sse_events": 6,
  "turn_end_status": "completed",
  "checks": [
    {"name": "provider 真的分片发送", "ok": false, "detail": {"chunks": 0, "stream_end": false}},
    {"name": "SSE 收到非空正式回答增量", "ok": false, "detail": {"assistant_events": 0, "answer_events": 0}},
    {"name": "TURN_END 的最终全文与 provider 发出的完全一致", "ok": true, "detail": {"final_len": 100, "provider_len": 100}},
    {"name": "SSE 全流里没有出现重复的完整回答", "ok": true}
  ],
  "failed": ["provider 真的分片发送", "SSE 收到非空正式回答增量"]
}
~~~

这条取证说明两件事：

1. **现状（基线）**：真链路上 SSE 里**一条非空 ASSISTANT 增量都没有**，整段回答只在 TURN_END 的 final_content 里出现一次 —— 与 §2.3 的 store 层红证据互相印证。
2. **阶段二的 harness 已经跑通**：假厂商 + 真 uvicorn + 假凭据（@@verify_state=verified@@）+ 真 SSE 采集 + 与 provider 时间线对比，全链路可用；阶段二只需把期望从「应当红」换成「应当绿」。

假厂商端点在本机跑了真 HTTP + 真 SSE；这不证明任何真实厂商行为。


同一轮**全量**前端套件（含既有用例）也跑过，确认新增验证用例没有污染既有行为：

~~~text
cd frontend
npx vitest run
-> Test Files 3 failed | 102 passed (105)，Tests 12 failed | 947 passed (959)
   （3 个失败文件就是我新增的 3 个 verify 文件）
~~~

### 2.4 前端类型检查

~~~text
cd frontend
npx vue-tsc --noEmit
-> exit=0（无输出）
~~~

## 3. 统一过程区：DOM 锚点（验证方声明，已同步 Lead 与 B）

契约 §1.5/§3 没有冻结 DOM，DOM 级验证必须锚定可测点。我声明的**最小锚点**：

- `[data-test="turn-process"]`：一轮一个过程区根元素（次选：组件 `TurnProcess` 且根 class 含 turn-process）
- `[data-test="turn-process-status"]`：状态行（系统事实：受理中/运行中/等待确认/已停止/已完成 + 耗时）
- `[data-test="turn-process-duration"]`：总耗时（未展开也要有）
- `[data-test="turn-process-history"]`：历史体（收起时 v-if 或 v-show 不可见）
- `[data-test="turn-process-toggle"]`：展开开关
- 一轮只保留一个耗时入口；MessageItem 不再单独渲染 interim 气泡（同一段文字全 DOM 只出现一次）

Lead 已确认把这条作为验收前置。**若 B 用了别的锚点名**，按 Lead 裁决以 B 的为准、我改测试（不改实现文件）。

测试环境适配说明：jsdom 没有布局，@tanstack/vue-virtual 不会渲染任何轮次，所以组件用例里把它替换成「全部渲染」的桩；这是环境适配，没有放宽任何契约断言。

## 4. 耗时口径取证（可复现命令 + 真实数字）

~~~text
cd D:\qio-dev\qio-up-d
powershell -ExecutionPolicy Bypass -File scripts/verify-timing-accounting.ps1
~~~

真实输出（2026-10-06，本机；数值每次运行有几十毫秒抖动）：

~~~json
TIMING_EVIDENCE {"after_turn_ms": 0, "all_spans_raw_sum_ms": 710, "duration_ms": 379,
 "model_wait_ms": 0, "queue_wait_ms": 5, "residual_ms": 0, "tool_run_count": 3,
 "tool_runs_sum_ms": 836, "tool_wait_wall_ms": 292, "top_level_sum_ms": 380,
 "turn_id": "turn_f3734ffcf113"}
~~~

口径结论（同一轮 turn，三个并发安全工具各睡 250ms）：

1. **总耗时 duration_ms = 379ms**，顶层阶段合计 380ms，`+residual 0ms` ≈ duration —— 顶层阶段铺满时间轴（允许 1ms 级取整差）。
2. **各工具自身耗时之和 836ms ≈ 批次墙钟 292ms 的 2.9 倍**：并行分项**不能**求和冒充总耗时。界面必须用 `duration_ms` 当总耗时，分项只作占比说明。
3. **全部 span 原始合计 710ms > duration 379ms**：嵌套细分会重复计入，原始合计不是总耗时。
4. **排队（queue_wait 5ms）不计进 duration**；trace 的 `phases.notes.queue_wait_ms` 与 TURN_END 的 `queue_ms` 应同源（后者基线还没有，见 §2.2）。
5. **after_turn 0ms**：这一轮结束后没有后台整理；有整理时必须单独说明、不计入总耗时。
6. 一句话：`duration_ms（墙钟） = 顶层阶段合计 + residual`，分项只作解释；缺失 != 0，不为负。

## 5. 未验证（诚实清单）

1. **A/B/C 的实现是否满足契约：未验证。** 未合并，全部相关用例为红（§2.2/§2.3）；合并后必须在集成分支重跑。
2. **浏览器/前端链路的端到端与截图：未运行。** `scripts/verify-e2e.ps1`（含 vite + msedge 截图）没有跑过。已跑过的是 §2.5 的 `scripts/verify-sse-local.ps1`（真 uvicorn + 真 SSE + 假厂商，无前端），它在**基线**上证明了 harness 可用；因此「回答先于 provider 结束」这条在**流式实现合并前仍未在任何链路验证过**（进程内用例、真 SSE 冒烟都还是红的）。
3. **截图：未做。** msedge 可用（`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`，版本 `154.0.4258.53`），端口 8734/5199/8798 当前空闲，但阶段一没有起应用。
4. **附件前端 UI（Composer 的附件入口、AttachmentChip 文案）：未验证。** 前端附件服务层的函数名/入参契约未冻结，写单测等于写死实现名；改为阶段二在 e2e 里验（含 100MB 边界的真实复制耗时）。
5. **100MB 边界只写了用例、没跑过。** `test_file_exactly_at_threshold_is_copied` 用稀疏文件造 100,000,000 字节，真正复制 100MB 的时间/磁盘必须等 C 的实现在阶段二实跑确认。
6. **迁移「只追加」只校验了历史前缀摘要**：能抓到改历史迁移，但抓不到「新增迁移与历史迁移语义重复」这类问题（attachments 表结构另有单独用例）。
7. **事件总线合并键 (type, turn_id, delta_id) 的后端行为未经我独立验证**：我验证的是前端「不同 delta_id 不互相覆盖」的可观察后果；A 侧合并键细化需要 `api/bus.py` 的背压/合并用例（阶段二补）。
8. **审批与异常路径（批准/拒绝/多项待审批/可恢复错误/失败/停止/中断/重连/旧历史）在统一过程区里的呈现**：阶段一未覆盖（需要 B 的组件与 A 的事件同时就位），阶段二补。

## 6. 阶段二计划（Lead 通知后在集成分支执行）

1. 集成后先跑：`uv run --frozen --extra dev pytest` 全量 + `npx vitest run` 全量 + `npx vue-tsc --noEmit`。
2. 跑本文件 §2 的五个 verify 文件与三个 verify test：把红变绿的部分逐条核实，**没有变绿的必须指出是锚点缺失还是实现缺失**。
3. 先 `powershell -File scripts/verify-sse-local.ps1`（快，后端 + SSE，基线已跑通 harness），再 `powershell -File scripts/verify-e2e.ps1`：真 provider + 真 uvicorn + 真 vite + 真 SSE 取证 + msedge 无头截图（宽 1440x900 / 窄 480x900），产出 `docs/verification-e2e-stream.json` 与截图。
4. 补 §5 的 8 条未验证项；出阶段二报告，仍按「已实现/已验证/未验证」分开写。

## 7. 跨模块提醒（不改别人的文件，只报告）

- 契约 §3 要求 TURN_END 增补 `duration_ms/queue_ms/started_at/ended_at`：当前 `_emit_turn_end`（`backend/src/agent/core/turn.py`）只发 status/final_content/error/usage。TurnContext 已有 `accepted_perf/started_perf`，trace 台账已有 `duration_ms` 与 `phases.notes.queue_wait_ms`，数据源不缺。
- 前端耗时 UI 现在把 `TurnTimingPanel` 挂在**每条** `turnId` 的助手消息上（`MessageItem.vue:343`），同一轮会渲染多个入口；折叠态在没有明细时显示「读取中」而不是总耗时。
- `docs/status.md` 与 `docs/architecture.md` 由 Lead 收口，D 不写。
