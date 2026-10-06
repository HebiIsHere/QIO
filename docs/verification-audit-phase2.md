# D 独立验收（阶段二）：集成实现复跑 + 实机交互取证

- 验证方：子智能体 D（独立验证，**不改实现文件**）
- worktree：`D:\qio-dev\qio-fix-v`（分支 `wt/fix-verify2`），集成分支内容 `fix/unified-process-audit`
- 基线口径：A/B/C/D 已全部合并；本文件只报**实测**结果，不引用实现方结论
- 假厂商：`scripts/verify_stream_provider.py`（本机扮演，真 SSE 分片）。结论只能读成
  「QIO 自己的链路对」，**不证明任何真实厂商**。
- 新增验证资产（D 名下）：
  - `scripts/verify-audit-phase2.ps1`：一键起假厂商 + 后端 + 前端 + 建凭据 + 实机取证 + 收尾
  - `scripts/verify-audit-phase2-shots.mjs`：Playwright（channel=msedge，无头）驱动真实页面
  - `docs/verification-shots-phase2/*.png` + `phase2-summary.json`（每条断言 + 真实网络台账）

## 0. 闸门（全量）

| 项目 | 命令（在 qio-fix-v 里跑） | 结果 |
| --- | --- | --- |
| 后端全量 pytest | `cd backend; $env:PYTHONPATH='src'; .venv\Scripts\python.exe -m pytest -q` | **EXIT=0**（100%，0 失败；离线 venv 必须用 PYTHONPATH=src，否则依赖 subprocess 的用例假红） |
| 后端 D 审计用例（5 文件） | 同上，指定 `tests/test_audit_*_verify.py` | **EXIT=0，26 条全绿**（stream 7 / binding 6 / io 4 / content 4 / turn_end 5） |
| 前端全量 vitest | `cd frontend; npx vitest run` | **Test Files 127 passed (127) / Tests 1117 passed (1117)**（Duration 83.86s） |
| 前端类型检查 | `npx vue-tsc --noEmit` | **EXIT=0** |
| 文档一致性 | `backend\.venv\Scripts\python.exe scripts\check_docs.py` | **EXIT=0**（文档一致性检查通过） |
| 实机取证 | `powershell -File scripts/verify-audit-phase2.ps1` | **30 / 32 条断言通过**；2 条失败是**同一个真实缺陷**（见 §3） |

后端 D 审计用例在阶段一曾是 44 红 / 24 绿守卫的基线；合并后**全部转绿**（EXIT=0），
阶段一的红证据与本次绿结果是同一条断言链的前后两端，见
`docs/verification-audit-phase1.md`。

## 1. 实机交互取证（真应用：假厂商 → uvicorn 8734 → vite 5199 → msedge 无头 + Playwright）

链路与操作都在真页面上完成；下面每条的 `[PASS]` 行都是脚本原始输出（截图见
`docs/verification-shots-phase2/`）。脚本先跑一轮**预热**：首次使用凭据会有一次
**非流式**调用（provider 台账实证 `{step:text, stream:false, msgs:1}`），会吃掉 FIFO 脚本第一步。

### S1 默认折叠（plan §1.5）

```
[PASS] S1 两个阶段都到达（当前阶段名可见） :: {"region":"运行中\n已完成 2 次调用\n▸\n2 个阶段 · 2 次调用\n核对实现\n\n第二阶段正在慢慢生成一段\n\n▸\n本阶段 1 次调用"}
[PASS] S1 运行中默认不展开历史（默认可见区没有历史抽屉） :: {"historyCount":0,"shot":"p2-01-running-collapsed.png"}
[PASS] S1 默认可见区有：当前阶段名 + 最新说明 + 一行工具摘要
[PASS] S1 默认可见区没有旧阶段说明与逐项工具卡 :: {"stageTools":0,"toolCards":0,"hasOldNote":false}
[PASS] S1 展开后能回看旧阶段说明（整轮历史抽屉） :: {"region":"...▾\n2 个阶段 · 2 次调用\n核对实现\n...1\n读取仓库结构\n进行中..."}
[PASS] S1 本阶段明细（逐项工具卡）展开后可见 :: {"stageText":"✓\n读取文件\n01:02:10\n▸\n输出\n# 阶段二取样文件 B"}
```

截图：`p2-01-running-collapsed.png`（运行中默认折叠：状态行 + 当前阶段名「核对实现」+ 最新说明 +
一行工具摘要「2 个阶段 · 2 次调用」，**没有**旧说明与逐项工具卡）、
`p2-02-history-expanded.png`（点开整轮历史 → 旧阶段「读取仓库结构」出现）、
`p2-02b-stage-detail.png`（点开「本阶段明细」→ 逐项 fs_read 卡与输出出现）。

### S2 正式回答稳定性（plan §1.1）

```
[PASS] S2 纯回答在 provider 结束前已经可见（出现在过程区） :: {"region":"运行中\n▸\n\n第一句。","timeline":["stream_start","chunk_sent"]}
[PASS] S2 提升到答案区：全局只出现一次，过程区不再留副本 :: {"inAnswer":true,"count":1,"region":"已完成\n耗时 3.1 秒"}
[PASS] S2 工具轮的说明在工具增量之前就已可见（过程区）
[PASS] S2 迟到工具增量不移动答案区文字（工具说明只在过程区、全局一份、答案区没有） :: {"count":1,"inAnswers":false,"expanded":true}
[PASS] S2 正式回答仍然完整到达答案区
```

关键点：过程区里出现「第一句。」时，provider 时间线**只有** `stream_start/chunk_sent`（没有
`stream_end`）→ 「provider 结束前已可见」是实测的；迟到 1.2s 的工具增量之后，那段过程说明
**仍只在过程区**、全局一份、答案区里没有它，正式回答照常到达答案区。

截图：`p2-03-answer-live-in-process.png`、`p2-04-answer-promoted.png`、
`p2-05-late-tool-process-text.png`、`p2-06-late-tool-no-move.png`。

### S3 内联审批真实点击（plan §1.3）

```
[PASS] S3 内联审批卡出现 :: {"payloadKeys":["action","cmd","risk","description","explanation","access","capabilities","detail","scope"]}
[PASS] S3 内联卡能看到「描述 / 独立说明 / 真实命令」三者（都在卡里，不用另开弹窗）
       :: {"inlineFacts":[["描述","想运行一条 shell 命令"],
                          ["独立说明","模型说明：为了验证审批链路，我要在受限子进程里跑一条无害的回显命令。"],
                          ["真实命令","echo qio-phase2-approval"]],"visibleInline":["描述","独立说明","真实命令"]}
[PASS] S3 「查看完整信息」打开原弹窗（弹窗接管，同一审批只有一套有效按钮） :: {"shot":"p2-08-approval-modal.png"}
[PASS] S3 点「允许」真的发出应答并生效（POST /api/approvals/{id}/respond 200 + 审批清空）
[PASS] S3 允许之后这一轮继续到正式回答（工具真的执行了）
[PASS] S3 点「拒绝」真的生效（审批被应答、命令不执行）
```

拦截到的真实请求（`phase2-summary.json` 的 `net`）：

```
POST http://127.0.0.1:8734/api/approvals/appr_.../respond 200   （允许）
POST http://127.0.0.1:8734/api/approvals/appr_.../respond 200   （拒绝）
```

截图：`p2-07-inline-approval.png`（内联卡：描述 / 「QIO 的说明」= 模型独立说明 / 「命令 echo
qio-phase2-approval」/ 允许·拒绝·查看完整信息）、`p2-08-approval-modal.png`（弹窗接管，内联卡显示
「这次确认已经在完整窗口中打开」）、`p2-09/10`（允许前后）、`p2-11-approval-rejected.png`（拒绝后
工具不执行，后端日志 `tool run_shell failed: 命令执行未获批准：你点了拒绝`）。

### S4 历史附件：打开副本（真机点通了） + **发现一个真实缺陷**

```
[PASS] S4 附件 chip 进入 ready（发送闸门放行）
[PASS] S4 真实 UI 把附件发出去并在消息里出现附件行（锚点带 id/kind/state）
       :: {"chip":true,"liveItemCount":1,"attId":"att_...","kind":"copy","state":"ready"}
[PASS] S4 点「打开」真的取到 QIO 副本（GET /content 200） :: {"status":200,"contentLength":"79","hasOpen":true}
[FAIL] S4 刷新后（真实历史加载）附件行仍在 —— 历史附件才有可用的打开入口
       :: {"histCount":0,"historyMessageKeys":["id","role","content","content_type","created_at","raw","turn_id"],
           "attachmentIdsInHistory":0,
           "apiNote":"GET /api/session/messages 不返回 attachments 字段（只在实时消息里由前端本地带上）"}
[FAIL] S4 引用失效后历史里出现重新定位入口（真机点击）
       :: {"blocked":"刷新后的历史消息没有附件行（同上一条缺陷）→ 重新定位入口在真实历史里渲染不出来","bigItemCount":0}
```

「打开副本」在**实时消息**上是真机点通的（`p2-12-attachment-live.png` → 点「打开」→
`GET /api/attachments/att_.../content` **200 / 79 字节**，见 `p2-13-attachment-open.png`）。
但**刷新（= 真实历史加载）之后附件行消失**：见 §3 缺陷。

### S5 失败入口与可用操作（plan §1.2）

```
[PASS] S5 失败状态如实显示 :: {"state":"failed","region":"已失败\n耗时 1.5 秒\n\nProviderInternalError: InternalServerError: Error code: 500 - {'error': {'message': 'phase2-fake-provider-boom', 'type': 'verify'}}\n\n重试\n详情"}
[PASS] S5 失败原因（系统事实）出现在界面上
[PASS] S5 失败后有可用操作入口（重试/重发）
[PASS] S5 点「重试」真的又跑了一轮并拿到回答 :: {"doneState":"ready","tail":"...重试之后这一轮真的跑通了。"}
```

截图：`p2-16-turn-failed.png`（已失败 + 原因 + 重试）、`p2-17-retry-succeeded.png`（重试后拿到回答）。
另外实测到：**被用户拒绝的命令**会在正式回答后追加系统核对注记
（`—— 系统核对（后端事实，不是模型的说法）： run_shell 执行失败：命令执行未获批准：你点了拒绝`），
这正是「可恢复工具错误 ≠ 整轮失败」的真机形态。

## 2. 逐项对照 plan §3 的七组验收

| plan §3 | 阶段一（事件层/DOM 层） | 阶段二（实机） | 结论 |
| --- | --- | --- | --- |
| 1 延迟工具增量（300ms/1s/正文之后）不移字 | 7 条事件层用例（真 SSE）+ 3 条 DOM 用例 | S2 全部通过（1.2s 迟到增量、全局一份、答案区不动） | **已验证** |
| 2 审批三者可见 / 查看完整信息 / 允拒生效 | 4 条 DOM 用例 | S3 全部通过（真机点击 + respond 200 + 拒绝不执行） | **已验证** |
| 3 附加不发送 → 各处都没有 | 6 条 API 用例 + 3 条前端用例 | （未覆盖：本轮实机未构造「附加不发送」场景） | 事件层/DOM 层已验证，实机未验 |
| 4 默认折叠（首轮/转阶段/并行/错误/完成/重连） | 5 条 DOM 用例 + 5 条事件驱动用例 | S1 全部通过（真机运行中折叠 + 展开回看） | **已验证** |
| 5 历史附件打开 / 重新定位 | 6 条 DOM 用例 + 4 条后端用例 | S4：打开真机通过；**历史（刷新后）打开与重定位被缺陷阻断** | 部分验证（见 §3） |
| 6 后台化（有界接收 + 主循环不被占住） | 4 条后端用例（最大单次停顿 ≤120ms + 探针推进） | （未在实机上量；测试层已覆盖） | 事件层已验证 |
| 7 结束事实（失败/停止/中断/旧历史） | 5 条后端用例 + 5 条前端用例 | S5 全部通过（失败原因 + 重试真的再跑一轮） | **已验证** |

## 3. 发现的真实缺陷（交 Lead 转负责人）

**历史消息不携带附件元数据 → 刷新后附件行消失，历史里的「打开副本 / 重新定位」入口渲染不出来。**

证据（全部可复现）：

1. 实机：通过真实 UI（路径 → 添加 → 发送）带附件发一条消息，消息行出现
   `[data-test="message-attachment"][data-id=att_...][data-kind=copy][data-state=ready]`，
   点「打开」→ `GET /api/attachments/att_.../content` **200 / 79 字节**（真机点通）。
2. 刷新页面（走真实历史加载）后：同样的消息**没有**附件行（`histCount: 0`），
   也没有重新定位入口。
3. 后端接口证据：`GET /api/session/messages`（与 `/api/session/context` 同源）返回的每条消息
   字段是 `["id","role","content","content_type","created_at","raw","turn_id"]` —— **没有
   `attachments`**；实现里的查询只取这些列（`agent/services/app.py::session_messages_page`）。
4. 前端证据：`frontend/src/stores/session.ts::_historyMessage` 明确读 `m.attachments`
   （`historyAttachmentRefs(m.attachments)`），前端没有任何地方按 turn 去补拉附件
   （`listAttachments` 只被 Composer 用）。
5. 后端测试没有覆盖这条（`tests/test_session_pagination.py` 里没有 attachment 相关断言），
   所以合入门禁全绿也发现不了。

影响：问题 5（历史附件可打开/可重定位）在**实时那一轮**可用，但用户**刷新/重进**后就看不到
附件行 —— 而「历史附件」正是这一条要解决的问题；§1.6「引用失效时提供重新定位入口」在真实
历史里同样渲染不出来。

可能的修法（二选一，交负责人定）：后端 `session_messages_page` 附带该轮/该消息的附件元数据
（与 `attachments.payload` 同形状，注意别把 stored_path 这类内部字段全吐给前端），
或前端在历史加载后按 `turn_id` / `message_id` 批量取一次 `/api/attachments`。

## 4. 已实现 / 已验证 / 未验证（诚实清单）

**已实现（D 名下新增资产，未改任何实现文件）**

- `scripts/verify-audit-phase2.ps1`、`scripts/verify-audit-phase2-shots.mjs`
- `docs/verification-shots-phase2/`（截图 + `phase2-summary.json`：每条断言 + 真实网络台账）
- 本文件 `docs/verification-audit-phase2.md`

**已验证**

- 全量闸门：后端 pytest EXIT=0、D 审计 26 条全绿、前端 127 文件 / 1117 用例全绿、
  `vue-tsc` EXIT=0、`check_docs` EXIT=0（数字均为本次实跑）。
- 实机（真应用 + 真点击 + 真网络）：默认折叠与展开回看、正式回答在 provider 结束前可见、
  迟到工具增量不移字、内联审批三者可见 + 查看完整信息弹窗接管 + 允许/拒绝真的应答生效、
  失败原因与重试真的再跑一轮 —— 共 30/32 条断言通过。
- 打开副本真机点通（`GET /content` 200 / 79 字节）。

**未验证（不得当作通过）**

1. **历史附件（刷新后）的打开与重新定位** —— 被 §3 缺陷阻断，实机不可用（本文件已如实记红）。
2. **浏览器里的重新定位真实链路** —— 浏览器没有原生选择器（`pickLocalPath` 返回 null，属 Tauri
   桌面能力）；实机只验证了「点了不假装成功」。真实 relocate 需要桌面壳或直接打
   `POST /api/attachments/{id}/relocate`（后端/DOM 层已覆盖）。
3. **真实厂商**（OpenAI/Anthropic/兼容档）—— 本轮全程使用本机假厂商，不证明任何真实厂商行为；
   Anthropic 线路格式仍只在 loop + StreamDelta 层覆盖。
4. **问题 6 的实机表现**（上传/重定位期间事件循环）—— 只在后端测试层用同事件循环实测
   （最大单次停顿 14/15ms），没有在真机上并发探测。
5. **问题 3 的实机场景**（附加不发送 → 切话题/刷新/失败重试）—— 只有事件层 + DOM 层证据。
6. **窄窗口/滚动/代码块溢出等视觉检查** —— 本轮截图固定在 1440×900；阶段一有窄窗口截图，
   本轮未重跑。
7. **100MB 等号边界的真实上传** —— 仍未跑（阈值判定由后端用例覆盖）。
8. **多用户/并发下的事件时序** —— 未做压力与并发下的实机验证。

后续步骤建议：先把 §3 修掉（这是唯一让阶段二实机「历史附件」无法通过的项），
再补 1–2 个实机场景（附加不发送、窄窗口），其余未验证项按需安排。

## 5. 复跑方式

```
# 1) 闸门
cd D:\qio-dev\qio-fix-v\backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest -q
cd D:\qio-dev\qio-fix-v\frontend; npx vitest run; npx vue-tsc --noEmit
cd D:\qio-dev\qio-fix-v; $env:PYTHONPATH='backend\src'; backend\.venv\Scripts\python.exe scripts\check_docs.py

# 2) 实机取证（自己起假厂商 + 后端 + 前端，跑完自己收）
powershell -ExecutionPolicy Bypass -File scripts/verify-audit-phase2.ps1
# 产出：docs/verification-shots-phase2/*.png、phase2-summary.json；退出码非 0 = 有失败项
```
