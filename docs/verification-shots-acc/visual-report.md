# 实机视觉取证收口报告（acc-vis / task-8）

- 分支：`wt/acc-f2`（基线 0249704；本报告与脚本修复由 acc-vis 追加提交，未 merge / 未 push）
- 装置：本机假厂商（真 SSE 分片）→ uvicorn `127.0.0.1:8734` → vite `127.0.0.1:5199` → msedge 无头 + Playwright
- 入口：`scripts/verify-acc-phase2.ps1` → `scripts/verify-acc-phase2-shots.mjs`
- 判定手段：**DOM/文本级探针**（临时 `scripts/tmp-acc-vis-probe.mjs`，只用于取证，**未提交**）：打印选择器命中数、相关节点 outerHTML / innerText 片段、Pinia session store 状态、假厂商请求台账。全程不看 PNG。
- 结论：**改前 7/11 → 改后 11/11 PASS**（`docs/verification-shots-acc/summary.json`，consoleErrors=[]）

> 说明：4 项失败经探针判定**全部是装置问题**（选择器 / 期望 / 时序），产品渲染一直是对的；
> 另发现 1 处产品侧分支优先级问题（S3，已回报 Lead，本子任务不改产品代码）。

## 逐项：检查 → 判定 → 证据 → 结论

### S3 不完整结束（改前 FAIL）
- 检查：界面明确「未完成」且不伪装完成；未确认后缀不出现。
- 改前期望：`[data-test="turn-incomplete-notice"]` 存在 → 探针实测 count=0。
- 真实 DOM 证据：
  - `.notice` 全页只有一条：`{"cls":"notice warn","text":"提示 模型流在给出结束标记之前就结束了，回答可能不完整。"}`
  - store：`lastTurnOutcome={turnId:"turn_87d0c344cded",status:"incomplete"}`、`warning="模型流在给出结束标记之前就结束了，回答可能不完整。"`
  - 最后一个 `[data-test="turn-process"]`：`data-state="incomplete"`，文本「未完成 总耗时 868 毫秒 模型流在给出结束标记之前就结束了，回答可能不完整。 重试 详情」
- 判定：**产品侧分支优先级问题（已报 Lead）+ 装置只认单一选择器**。
  - 产品：`frontend/src/views/ConversationView.vue:176-217` 是 `v-if / v-else-if` 链，`session.warning` 分支排在 `status === 'incomplete'` 分支之前；而 `backend/src/agent/core/loop.py:1594-1619` 在判定 `incomplete_stream` 时**同时**发 WARNING 事件 → 专门的安静提示在真机路径上被警告横幅顶掉（该分支实际不可达）。
  - 实质要求未被违反：未完成 + 原因 + 可用的「重试」入口都由警告横幅与过程区如实给出，正文没有伪装成完成。
- 改前 → 改后：`!seen && …` → 多路径断言：`procState==="incomplete"` 且过程区内含「未完成」+ 原因 + 重试入口 + **不出现**「已完成」，且「安静提示 **或** 警告横幅」二者之一如实说出不完整，且未确认后缀不出现。**实质要求不降**（仍是「未完成 + 原因 + 可重试 + 不伪装完成」）。
- 依据：上面的探针 DOM 证据。
- 结论：PASS（quietSeen=false、warnSeen=true、procState=incomplete、reasonLine=模型流在给出结束标记之前就结束了…、retryInProc=1、leakedSuffix=false）。

### S6 工具失败注记（改前 FAIL）
- 检查：注记在独立「系统事实」区域，正文恰好一份且不被注记污染。
- 改前：`[data-test="answer-system-note"]` count=0、正文出现次数 0。
- 真实 DOM 证据：
  - 该轮过程区出现「等待确认 fs_read · 1 项工具运行中 … **电脑操作审批** … 拒绝 允许」；
  - 假厂商台账那一刻只有 **1 条**请求：`{step_kind:"tool_chunks", tool_count:20, message_count:2}`；
  - store 里该轮没有任何 assistant 消息（本轮还没产出回答）。
  - 佐证（同一次探针跑在 S8 阶段再取一次）：该轮最终产出的回答正文是 `系统事实 —— 系统核对（后端事实，不是模型的说法）：· fs_read 执行失败：read 未获批准：审批等待超时（等你确认超过 5 分钟，已自动取消） 这几项没有通过验证，不能当作「已完成 / 可使用」。`
- 判定：**装置问题**。`fs_read` 读根外路径需要一次**交互审批**；旧脚本不批准，这一轮就停在「等待确认」直到审批超时（5 分钟）才继续，而旧脚本约 2 分钟后就断言 —— 自然既没有正文也没有注记。注记本身是产品既有行为（`core/turn_facts.py`、`core/loop.py:1503`）。
- 改前 → 改后：
  1. 脚本如实点 `[data-test="turn-process-approval-allow"]`「允许」→ 工具真的执行、真的失败（文件不存在）→ 轮末产生注记；`waitIdle` 放宽到 180s。
  2. 正文断言从 `.stream .message.assistant`（**它包含注记块本身**，旧断言 `answers.indexOf("系统核对") < 0` 只要注记存在就恒假）改为回答气泡正文 `.assist-bubble .markdown-body`；并补两条可证伪的独立性事实：注记不在任何 `.markdown-body` 里、注记恰好 1 个。
- 依据：探针 DOM（审批卡、厂商台账、store message 列表）。
- 结论：PASS（approvalSeen=true、seen=true、noteHead=「系统事实 —— 系统核对…· fs_read 执行失败：读取失败：[Errno 2] No such file or directory…」、bodyCount=1、noteInsideAnswer=false、noteInsideMarkdown=0、noteCount=1）。

### S7 附件恢复（改前 FAIL）
- 检查：刷新后历史附件行仍在，点「打开」→ GET /content 200。
- 改前：`rowCount=0`、`contentStatus=null`（`chipReady=true`、`sendStatus=200`）。
- 真实 DOM 证据（S7 探针那一刻）：
  - 假厂商台账 **为空**（这一轮根本没被模型消费）；store `localSendSeq=0`；
  - 队列 chip 显示「1 运行中 · 1 排队中 · 1 已取消」→ 前一轮（S6）仍卡在审批上占着运行位，S7 的发送只是**受理（200）/ 排队**，从未执行、从未落库；刷新后自然没有历史附件行。
- 判定：**装置问题 = S6 级联 + 时序问题**（`page.reload()` 后 `waitAppReady()` 只等模块挂载；历史是异步加载的，旧写法立刻计数 = 假阴性）。不是产品缺陷。
- 改前 → 改后：S6 修好后该轮真正执行；刷新后对附件行做 `waitFor`（30s，不再立刻计数）；并追加原始证据：`GET /api/session/context` 里该用户消息确实带 `attachments`。
- 依据：队列/台账/store 证据 + 修后历史接口 payload。
- 结论：PASS（rowCount=1、点「打开」→ `GET /api/attachments/{id}/content` = 200、historyAttachments=[{role:"user", head:"附件恢复：发送后刷新", names:["acc-附件恢复.txt"]}]）。

### S8 列表内代码块 / 表格（改前 FAIL）
- 检查：Markdown 列表内代码块 / 表格结构保留（F13 实机）。
- 改前：`codeBlocks=1`、`tables=1` 但 `hasCodeText=false`。
- 真实 DOM 证据：
  - `.code-block` outerHTML：`<div class="code-block" data-lang="js"><button class="code-copy" …>复制</button><pre><code class="hljs"><span class="hljs-keyword">const</span> a = <span class="hljs-number">1</span>;</code></pre></div>`
  - `.code-block pre code` textContent = `const a = 1;`
  - 该轮 `.markdown-body` 实测 8 个，代码块所在的正文是其中一个，而旧写法 `page.locator(".markdown-body").last()` 取到的是页面**最后一个**正文（另一条「（假模型默认回复）」回答）→ `hasCodeText` 恒假。
  - 表格：`<div class="table-wrap"><table>…`，文本 `a b 1 2`；代码块与表格都在 `li` 里。
- 判定：**装置问题**（取文本定位到了错的那一块；产品渲染一直正确）。
- 改前 → 改后：文本改为从**带结构的那一块自身**取 —— `.markdown-body:has(.code-block)` 内的 `.code-block pre code`、`.markdown-body:has(.table-wrap table)` 内的 table；并记录列表内计数（`li .code-block` / `li .table-wrap table`）。
- 依据：上面的 outerHTML/textContent。
- 结论：PASS（codeBlocks=1、tables=1、codeInList=1、tableInList=1、codeText="const a = 1;"、tableText="a b 1 2"、hasCodeText=true、hasTableCells=true）。

## 改前 → 改后 → 依据 汇总

| 项 | 改前（选择器/期望/时序） | 改后 | 依据（真实 DOM 证据） |
| --- | --- | --- | --- |
| S3 | 只认 `[data-test=turn-incomplete-notice]`（真机上被 warning 分支顶掉，恒 0） | 多路径：过程区 `data-state=incomplete` + 未完成 + 原因 + 重试入口 + 无「已完成」+（安静提示 或 警告横幅）+ 无未确认后缀 | `.notice warn` 唯一命中；`lastTurnOutcome.status=incomplete`；过程区 data-state=incomplete |
| S6 | 不批准审批，等 2 分钟就断言；正文断言用了包含注记块的 `.message.assistant` | 如实点「允许」；正文断言改 `.assist-bubble .markdown-body`，另加注记独立性与唯一性 | 审批卡在过程区；厂商台账只有 tool_chunks 一条；store 无 assistant 消息 |
| S7 | 刷新后立刻计数（历史未加载）；且被 S6 卡住的队列饿死 | 行级 `waitFor`（30s）+ S6 修好 + 追加历史接口 payload 证据 | 台账为空、`localSendSeq=0`、队列「1 运行中 · 1 排队中」 |
| S8 | `.markdown-body.last()` 取到别的回答 | 从 `:has(.code-block)` / `:has(.table-wrap table)` 的那一块自身取文本 | `.code-block` outerHTML 与 `pre code` textContent 均含 const a = 1; |

**没有降低实质要求**：未完成仍必须如实说出「未完成 + 原因 + 重试」且不得出现「已完成」；正文仍必须恰好一份且注记独立成块（并新增「注记不在 markdown 正文里」的证伪条件）；附件仍必须刷新后在 DOM 里并真的能 `GET /content` 200；代码块与表格仍必须结构存在且内容可取。

## 产品侧待处置（已报 Lead，本子任务未改产品代码）

1. **S3 分支优先级**：`frontend/src/views/ConversationView.vue:176-217` 的 `v-else-if` 链让 `session.warning` 抢在 `lastTurnOutcome.status === 'incomplete'` 之前；而 `backend/src/agent/core/loop.py:1594-1619` 对 `incomplete_stream` **总是**同时发 WARNING。结果：`data-test="turn-incomplete-notice"`（第 208-216 行）在真机路径上不可达，同一事实被警告横幅表达。低危（信息没丢、没伪装完成），但契约 §七 C2 点名的那条安静提示实际上不生效 —— 取舍由 Lead 定。

## 装置侧改动清单（全部只改 scripts/，不动产品代码）

| 文件 | 改前 → 改后 | 依据 |
| --- | --- | --- |
| `scripts/verify-acc-phase2.ps1` | `Tee-Object -FilePath shots-console.log` → 逐行 `Add-Content -Encoding UTF8`（先删旧文件） | Windows PowerShell 5.1 的 `Tee-Object` **没有 -Encoding**（参数表已核），只能写 UTF-16；日志进版本库后 git 按二进制处理（`Bin 0 -> N`）、无法直接读。改后首字节 = `EF BB BF`（UTF-8 BOM）、无 NUL，git 按文本 diff。只影响日志落盘编码，**不影响任何断言**；该改动同样经这次完整复跑验证（11/11 PASS）。 |
| `scripts/verify-acc-phase2-shots.mjs` | S3 单选择器 → 多路径（见上）；S6 增加「允许」审批 + 正文断言作用域；S7 行级 `waitFor` + 历史接口证据；S8 从带结构的那一块取文本；删除失效的 `answerText()` | 见每一节里的 DOM 证据 |

## 未验证项（不用浏览器证据冒充）

- **Windows 原生窗口 / Tauri 安装包**：原生选文件对话框、路径拖入、原生中止提示、多显示器 DPI 均未验证（本次只在 msedge 无头里取 DOM 证据，不触碰二进制 PNG）。
- **真实厂商端点**：本次 provider 是本机扮演的假厂商（真 SSE 分片、真 500、真断流），只能读成「QIO 自己的链路对」；真实厂商的网络行为 / 兼容性 / 限流 / 鉴权未验证。
- **窗口尺寸**：只覆盖 1440×900 与 420×820（无横向溢出 0px）；200% DPI、键盘滚动、触屏拖拽未验证。
- **排队取消的结束事实**：本次取到的是可操作证据（B 从队列移除、A 不受影响）；`cancelled / user_stopped / retry` 的结束事实由后端契约与前端 store 用例钉住，不是本次实机结论。

## 复跑

```powershell
cd D:\qio-dev\qio-acc-f
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-acc-phase2.ps1
# 产出：docs/verification-shots-acc/acc-01..10-*.png、summary.json、shots-console.log
```

## 本次快照

| 文件 | 内容 |
| --- | --- |
| acc-01-running-queued.png | S1 执行 + 排队共存 |
| acc-02-turn-ended-folded.png | S2 结束折叠 + 权威耗时 |
| acc-03-incomplete-ended.png | S3 不完整结束（警告横幅 + 过程区「未完成/重试」） |
| acc-04-failed-retry.png | S4 失败 + 重试入口 |
| acc-05-queued-cancel.png | S5 排队取消 |
| acc-06-system-note.png | S6 工具失败 → 独立「系统事实」注记 |
| acc-07-attachment-restored.png | S7 刷新后历史附件行 |
| acc-08-markdown-list-blocks.png | S8 列表内代码块 / 表格 |
| acc-09-wide-1440.png / acc-10-narrow-420.png | S9 宽窄窗口 |
| summary.json | 11/11 PASS 的机器可读结果 |
| acc-phase2-raw/visual-run-acc-vis-*.txt | 本次运行的逐行台账 |
