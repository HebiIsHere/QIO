# 独立验证报告 · 阶段一：基线反例复现（acc-f）

- 计划：docs/plans/2026-10-09-process-attachment-audit-consolidation.md（§二 契约 C1—C8、§三 文件所有权、§六 验收）
- 验证者：acc-f2（独立验证者；本阶段**不改任何产品实现**）
- worktree / 分支：D:\qio-dev\qio-acc-f @ wt/acc-f2，起点 12cc95a（= 基线 e3d1205 + 反例测试）
- 基线审计 SHA：e3d1205eafef28f37db33c0c43a48a32b102de1b
- 环境：Windows / PowerShell；后端 uv run --frozen（Python 3.11）；前端 vitest 4.1.10（jsdom）+ vue-tsc --noEmit
- 原始输出留档：docs/verification-shots-acc/acc-phase1-raw/backend-acc-f-baseline.txt、.../frontend-acc-f-baseline.txt

> 本文件是计划里对每个负责人承诺、但仓库中缺失的交付物（verification-acc-phase1.md）。
> 计划按负责人分 §A—§E；本报告按**问题项 F03/F05/F06/F07/F11/F12/F13** 组织，并在每项标注
> 计划负责人，便于回填计划 §一 状态表。

## 〇、Lead 冻结的契约裁定（本轮据此对齐断言，属契约对齐而非放宽）

1. **F11**：系统核对注记由后端以**独立字段** annotation（完整字符串，含 ANNOTATION_HEADER 与
   结论句）随 TURN_END 交付，final_annotation 为兼容别名；final_content 保持**纯正文**；前端在
   独立「系统事实」区域渲染。断言改为「正文唯一（BODY 恰好一次、不含表头）＋ 注释完整
   （表头与结论句）」。
2. **F06**：TURN_END 新增终态 incomplete（仅当 reason_code=incomplete_stream），actions 含
   retry；保留 status != completed、reason_code != none。
3. **F12**：排队轮取消必须有一条 TURN_END（cancelled / reason_code=user_stopped /
   stopped_by=user / 时长字段 / 落库）；可恢复动作由 resend **改判为 retry**。
   理由：/api/turns/{id}/resend 只接受 journal 记成 interrupted 的行（agent/api/server.py 的
   recoverable/claim），排队取消后 journal 落的是 cancelled，resend 必然 409 —— 那是死按钮；
   retry 走前端「重发该轮用户消息」（session.retryTurn → 普通发送接口），真实可用。
   active 取消路径保持既有 resend，既有 test_turn_timing_facts.py 的精确相等断言不动。

## 一、结论速览

| 项 | 计划负责人 | 验证层级 | 基线判定 | 基线通过/失败 |
| --- | --- | --- | --- | --- |
| F03 noopener 误判 / URL 过早撤销 | D | 前端服务函数（真实 window.open 语义 stub + 真实 URL API） | **仍成立** | 2 失败 |
| F05 排队改变活动 turn 归属 | C | 后端跨层（假 provider→adapter→loop→TurnManager→总线）+ 前端 store | **已有修复且反例通过** | 后端 1 通过、前端 1 通过 |
| F06 SSE 无结束标记仍算完成 | B | 后端跨层（真 HTTP + 真 SSE 假厂商端点） | **仍成立** | 1 失败 |
| F07 流式事件绕过脱敏（含跨分块） | B | 后端跨层（假 provider→adapter→AgentLoop→事件总线） | **仍成立** | 2 失败 |
| F11 附注记导致整段回答重复 | C（后端出口协同 B） | 后端跨层 + 前端 store 复放真实事件夹具 | **仍成立**；后端出口在冻结契约下亦红 | 后端 1 失败、前端 2 失败 |
| F12 排队轮取消无结束事实 | C | 后端跨层（假 provider→队列→取消→journal） | **仍成立** | 1 失败 |
| F13 列表内代码块/表格/嵌套压平 | C | 前端组件级（mount MarkdownContent） | **仍成立** | 3 失败 + 1 对照通过 |

汇总命令结果（对齐冻结契约后的**同一次**运行）：

- 后端：.FFFFF —— 5 failed, 1 passed，退出码 1。
- 前端：Test Files 3 failed | 1 passed；Tests 7 failed | 2 passed，退出码 1。
- 前端类型检查：npx vue-tsc --noEmit 退出码 0（无输出）。

## 二、逐项记录

### F03 — noopener 下 window.open 返回 null 被当成失败、blob URL 过早 revoke

- **触发条件**：真实浏览器在 features 含 noopener 时 window.open **返回 null**（这是正常成功）。
  反例以该语义 stub window.open，调用真实服务函数 openExternal 与 openAttachment。
- **反例文件**：frontend/src/services/__tests__/acc_f_03_external_open.test.ts
- **证据命令**：
      cd frontend
      npx vitest run src/services/__tests__/acc_f_03_external_open.test.ts
- **基线实际结果（原始摘要）**：
  - openExternal 如实返回成功：
    AssertionError: expected false to be true // Object.is equality（第 75 行，expect(ok).toBe(true)）
  - 真实 noopener 语义下 openAttachment 报告已打开且不同步 revoke：
    AssertionError: expected 'download' to be 'view'（第 85 行，用户点「打开」却退化成下载）
- **判定**：**仍成立**。基线把 noopener 的 null 当成「打开失败」，进而 revokeObjectURL 并退化成
  下载；两项反例都如实红。

### F05 — 排队改变活动 turn 归属

- **触发条件**：A 正在流式执行（假 provider 在 hold_after 处被 asyncio.Event 卡住），此时提交 B、C，
  B/C 进入排队；随后放行 A。
- **反例文件**：
  - backend/tests/test_acc_f_05_turn_ownership.py（跨层）
  - frontend/src/stores/__tests__/acc_f_05_active_turn_ownership.test.ts（store 级回归守卫）
- **证据命令**：
      cd backend
      uv run --frozen pytest -q -p no:warnings tests/test_acc_f_05_turn_ownership.py
      cd ../frontend
      npx vitest run src/stores/__tests__/acc_f_05_active_turn_ownership.test.ts
- **基线实际结果**：两条都通过。
  - 后端：只有 A 的 TURN_START/ASSISTANT；/api/turns/queue 的 running 是 A、queued 是 [B, C]；
    每个 turn 恰好一次 TURN_START/TURN_END；B_start > A_end、C_start > B_start（FIFO）。
  - 前端：markTurnQueued(B/C) 不改 activeTurnId；排队 turn 的 ASSISTANT 不进主对话；
    TURN_START 是唯一允许设 active 的入口。
- **判定**：**已有修复且反例通过**。前端侧缺陷已在 2b204d7（fix(qio): turn state contract,
  cancellation targets, usage semantics, long-run performance；已核实是基线 e3d1205 的祖先）修复；
  两个用例保留为回归守卫，不制造假红灯。
- 说明：装置有效性由该用例自身保证（若 provider hold 未生效，第 112 行会以「装置失效」失败）。

### F06 — SSE 无结束标记直接 EOF 仍被算作「正常完成」

- **触发条件**：本机假厂商端点（scripts/verify_stream_provider.py，**真 HTTP + 真 SSE**）以
  abort_after=2 在发出两片正文后**干净关闭连接但不给 finish_reason**；adapter 得到
  finish_reason=None 的 Completion。
- **反例文件**：backend/tests/test_acc_f_06_incomplete_stream.py
- **证据命令**：
      cd backend
      uv run --frozen pytest -q -p no:warnings tests/test_acc_f_06_incomplete_stream.py
- **基线实际结果（原始摘要）**：
  AssertionError: ('EOF 缺 finish_reason = 不完整结束，不得标成正常完成', {'status': 'completed', 'reason_code': 'none'})
  —— 已确认正文「前两句。第二句。」保留、后缀未凭空出现（① 通过），但 ②③ 红。
- **判定**：**仍成立**。已按冻结契约在 ①②③ 之上**加严**：必须 status == "incomplete"、
  reason_code == "incomplete_stream"、actions 含 retry；原两条断言原样保留。

### F07 — 流式事件绕过脱敏（完整出现 / 跨分块切开）

- **触发条件**：测试内生成**随机合成敏感值**（accf07-<uuid4 hex>，无密钥形状，只有登记表能识别），
  经 agent.trace.redact.register_secret 登记后清理；两种位置：
  - 完整出现：一整段正文里包含完整敏感值；
  - 跨分块切开：敏感值被 SSE 分片从中间切开，前一片已能触发一次累计快照发布。
  断言只输出布尔与索引，**失败信息不含原值**（_contains_secret 助手；fixture 不带敏感值参数）。
- **反例文件**：backend/tests/test_acc_f_07_stream_redaction.py
- **证据命令**：
      cd backend
      uv run --frozen pytest -q -p no:warnings tests/test_acc_f_07_stream_redaction.py
- **基线实际结果（原始摘要）**：
  - 完整出现：AssertionError: ('完整出现：ASSISTANT 累计快照泄漏了已登记敏感值', {'events': 2, 'leaked_indices': [0, 1]})
  - 跨分块：AssertionError: ('跨分块切开：累计快照泄漏了完整敏感值', {'events': 3, 'leaked_indices': [1, 2]})
- **判定**：**仍成立**。ASSISTANT 载荷直接发布累计原文，登记表未被消费 → 原值与「半个敏感值」
  都会上线（尾部缓冲未实现）。

### F11 — 工具失败 + 系统注记导致整段回答重复

- **触发条件**：一轮里工具 acc_f_failing_tool 失败 → 后端事实台账产生系统注记（ANNOTATION_HEADER +
  结论句）→ 旧形态把它拼进 TURN_END.final_content。后端出口用真实链路捕获事件并写出前端复放夹具
  docs/acc/acc-f-f11-events.json；前端用真实 Pinia store 复放该夹具。
- **反例文件**：
  - backend/tests/test_acc_f_11_annotation_final_answer.py（后端出口）
  - frontend/src/stores/__tests__/acc_f_11_final_answer_annotation.test.ts（跨层复放）
- **证据命令**：
      cd backend
      uv run --frozen pytest -q -p no:warnings tests/test_acc_f_11_annotation_final_answer.py
      cd ../frontend
      npx vitest run src/stores/__tests__/acc_f_11_final_answer_annotation.test.ts
- **基线实际结果（原始摘要）**：
  - 前端（旧夹具，注释内嵌 final_content）：AssertionError:
    ["====正文====\n这是这一轮唯一的正式回答。","====正文====\n这是这一轮唯一的正式回答。"]: expected 2 to be 1
    —— 同一段正式回答出现**两条**消息（applyFinalAnswer 以「全文是否相等」判断同一次回答，不等就
    再追加一条）。
  - 后端（冻结契约对齐后）：AssertionError: ('系统注释不得再拼进 final_content（冻结契约：注释走独立
    字段，正文保持纯净）', ...) —— 基线 TURN_END 的字段是
    ['actions','error','final_content','reason','reason_code','status','stopped_by','turn_id']，
    **没有** annotation 字段，注记内嵌在 final_content 末尾。
- **判定**：**仍成立**（前端重复两条）；后端出口在新契约下亦红（注记尚未成为独立字段）。
- **透明说明**：本项对齐前的旧断言（final_content = body + 注记）在基线上**是绿的**（第一次运行
  F05 通过 / F06、F07×2、F12 红 / F11 绿）。对齐冻结契约后 F11 后端转为红，这是**新契约的反例**，
  不是把绿改成红来凑数。夹具 docs/acc/acc-f-f11-events.json 目前仍是旧形态（final_content 内嵌
  注记），阶段二后端修复通过后由该测试自动重写为新形态（annotation 字段），前端复放用例随后据此
  验证。

### F12 — 排队轮被取消后没有结束事实

- **触发条件**：A 流式执行中提交 B（排队）→ POST /api/turns/{B}/cancel；随后放行 A。
- **反例文件**：backend/tests/test_acc_f_12_queued_cancel_facts.py
- **证据命令**：
      cd backend
      uv run --frozen pytest -q -p no:warnings tests/test_acc_f_12_queued_cancel_facts.py
- **基线实际结果（原始摘要）**：
  AssertionError: ('排队轮被取消后没有 TURN_END（前端拿不到结束事实，刷新后也恢复不了）',
  {'turn_b': 'turn_1145ff10cd51', 'events': ['TURN_START', 'CAPABILITY', 'ANCHOR', 'TURN_QUEUE',
  'TURN_QUEUE', 'ASSISTANT', ...]}) —— A 的断言（继续完成、回答归 A、取消 B 不结束 A）全部通过；
  缺的就是 B 的 TURN_END 与落库事实。
- **判定**：**仍成立**。本项断言已按 Lead 裁定把可恢复动作由 resend 改为 retry（理由见上文 〇.3），
  其余断言（status=cancelled / reason_code=user_stopped / stopped_by=user / 时长与队列字段 /
  turn_journal 落库）保持不变。

### F13 — 列表内的代码块 / 嵌套列表 / 表格被压平

- **触发条件**：给真实组件 MarkdownContent 传入「列表项里含块级结构」的 Markdown：嵌套列表、围栏
  代码块、表格；另有「列表外」对照组。
- **反例文件**：frontend/src/components/__tests__/acc_f_13_markdown_lists.test.ts
- **证据命令**：
      cd frontend
      npx vitest run src/components/__tests__/acc_f_13_markdown_lists.test.ts
- **基线实际结果（原始摘要）**：
  - 嵌套列表：找不到 ul ul；
  - 代码块：<div class="markdown-body"><ul><li>步骤：</li></ul></div>: expected false to be true
    —— 代码块内容整个丢失；
  - 表格：<div class="markdown-body"><ul><li>表格：ab12</li></ul></div>: expected false to be true
    —— 表格被压成纯文本；
  - 对照「列表外的代码块与表格本来就能正常渲染」：**通过**（证明选择器 .code-block /
    .table-wrap table 有效，红不是装置问题）。
- **判定**：**仍成立**（3 红 + 1 对照绿）。基线 listItem 用 renderInline 渲染子节点，块级节点落到
  默认分支。

## 三、验证层级与模拟边界（诚实声明）

| 维度 | 本轮实际覆盖 | 未覆盖（阶段二补） |
| --- | --- | --- |
| 模型 | 全部 fake/mock provider：FakeStreamAdapter（进程内脚本）、scripts/verify_stream_provider.py（本机 **真 HTTP + 真 SSE**，虚拟密钥 sk-accf-fake-0006，abort_after 模拟缺 finish_reason 的干净关闭） | 真实厂商端点（OpenAI/Anthropic 实网） |
| 链路 | 后端 adapter → AgentLoop → TurnManager → 事件总线（含 API 层 /api/turns、队列、取消、journal） | 真实 uvicorn 进程 + 前端页面的端到端 |
| 前端 | 组件级（@vue/test-utils + jsdom mount）、store 级（真实 Pinia）；事件夹具来自后端真实链路 | 真实浏览器（msedge 无头）+ 真实应用装配、视觉截图 |
| 桌面 | 无 | Windows/Tauri 原生壳、安装包 |
| 密钥 | 只用合成/假值；F07 失败信息与断言刻意不打印敏感原文 | — |

结论：阶段一的红灯是**基线产品行为**的红灯（断言全部来自真实服务函数 / 真实跨层事件链 / 真实组件
挂载），不是装置故障；但**不能**据此宣称实机（真实浏览器、真实厂商、Tauri）表现，这部分留给阶段二
用 Playwright + 假厂商 SSE + 无头 msedge 取证。

## 四、本阶段为验证而做的改动（仅测试资产，无产品代码）

| 文件 | 改动 |
| --- | --- |
| backend/tests/test_acc_f_06_incomplete_stream.py | 冻结契约加严：status == incomplete、reason_code == incomplete_stream、actions 含 retry（保留原有 != completed / != none） |
| backend/tests/test_acc_f_11_annotation_final_answer.py | 改为独立注释字段契约：final_content 纯正文（以 BODY 开头、不含表头、BODY 恰好一次）；annotation（兼容 final_annotation）完整含表头与结论句；ASSISTANT 里正文恰好一次且无表头 |
| backend/tests/test_acc_f_12_queued_cancel_facts.py | 按裁定把可恢复动作断言由 resend 改为 retry（其余不变） |
| frontend/src/stores/__tests__/acc_f_11_final_answer_annotation.test.ts | 改为「拆分语义」断言（同一段正文一条消息、正文恰好 BODY、注记完整、BODY 恰好一次），不依赖 store 内部字段名 |

未改动的反例：test_acc_f_05_turn_ownership.py、test_acc_f_07_stream_redaction.py、
acc_f_03_external_open.test.ts、acc_f_05_active_turn_ownership.test.ts、acc_f_13_markdown_lists.test.ts。

## 五、留给阶段二

- 合并集成分支新 HEAD 后复跑本仓全部 test_acc_f_*、test_acc_b_*、test_acc_e_*、test_r8_* 相关回归与
  前端 acc_f_*/acc_c_*/acc_d_* + 全量 vitest + vue-tsc。
- 跨层组合：A 流式 + B 排队 + 工具失败注记 + B 取消 + 不完整结束 / 正常结束，断言归属、正文唯一、
  脱敏（合成敏感值跨分块）、结束事实与历史恢复；正常 / 异常路径分开跑。
- 实机视觉（假厂商 SSE → uvicorn → vite dev → 无头 msedge，Playwright）：执行+排队共存、结束折叠、
  失败/取消、附件恢复、Markdown 列表内代码/表格、窄窗口，输出到 docs/verification-shots-acc/ +
  summary.json。
- 逐项 F01—F24 的最终判定写入 docs/verification-acc-phase2.md，并明确未实测项。
