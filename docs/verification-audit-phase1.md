# D 独立验收（阶段一）：审计七项的**红证据**与验收资产

- 验证方：子智能体 D（独立验证，不改实现文件）
- worktree：`D:\qio-dev\qio-fix-d`（分支 `wt/fix-d`）
- 基线：`e428bb9`（审计七项修复基线）+ `ff7a355`（plan 契约）
- 提交：`a69cc53`（后端验收用例）；本文件的同批提交含前端用例与脚本
- 立场：**断言只依据产品规则**（`docs/plans/2026-10-06-audit-seven-fixes.md` §0/§1/§3），
  不依据实现方事后说明；修复前必须**真的变红**，红证据保留命令与原始输出。
- 模型调用一律 fake/mock（`scripts/verify_stream_provider.py` 假厂商端点、`FakeStreamAdapter`、
  自定义假 adapter），不联网、不需要真实 Key，输出里不含任何密钥原文。

## 0. 证据层级（本文件严格区分，不把低层证据说成 E2E）

| 层级 | 含义 | 本文件里的哪些 |
| --- | --- | --- |
| **事件层** | 真 HTTP + 真 SSE 假厂商（`verify_stream_provider.py`）或真 AppContext/HTTP API 驱动，断言事件载荷与后端事实 | `test_audit_stream_role_verify.py`（native 路径）、`test_audit_attachment_*_verify.py`、`test_audit_turn_end_facts_verify.py` |
| **DOM 层** | 真挂载 Vue 组件（@vue/test-utils + jsdom），断言用户能看到什么 | `frontend/src/**/*.audit.verify.test.ts`（6 个文件） |
| **真实端到端** | 实际启动后端 + vite + 假厂商，浏览器里点 | **本阶段未做**（阶段二：msedge 无头 + Playwright 截图） |

## 1. 红证据（基线 e428bb9，未做任何修复）

一键复跑（本机 pwsh 未在 PATH 上时直接在 PowerShell 里用 `&` 调用）：

```
cd D:\qio-dev\qio-fix-d; & .\scripts\verify-audit-phase1.ps1
→ backend exit=1  vitest exit=1  vue-tsc exit=0
→ Test Files  6 failed (6) / Tests  15 failed | 7 passed (22)
→ 有红：符合预期（修复前）/ 需要排查（修复后）
```


### 后端（20 红 / 5 绿守卫）

命令：

```
cd D:\qio-dev\qio-fix-d\backend
uv run --frozen --extra dev pytest tests/test_audit_stream_role_verify.py tests/test_audit_attachment_binding_verify.py tests/test_audit_attachment_io_verify.py tests/test_audit_attachment_content_verify.py tests/test_audit_turn_end_facts_verify.py -q --tb=line
```

结果：**20 failed, 5 passed**（20 条 FAILED 全部逐条列出，见下）。

#### 问题 2 · 输出角色（事件层，真 SSE 假厂商）

- `E   Failed: 文字从答案区被移回过程区（plan §1.1 第 3 条：永不移动已进入答案区的文字）：delta_id=dl_late_too_1 在第 0 条已判为正式回答，第 2 条又变成 interim=true，content='我先说明一下。这一步马上要调用工具。'`
- `E   AssertionError: provider 结束之前过程区一直是空的（答案被守卫窗口憋到 300ms 之后才出现，或者直接落在正式回答区）—— 这不是边生成边显示`
- `E   AssertionError: provider 结束前过程区没有任何 interim=true 的文字（正文没有实时到达）`
- `E   AssertionError: 正文增量必须先进过程区（interim=true）`
- `E   AssertionError: ('工具阶段收尾的调用没有任何正文时，必须再发一次调用专门产出正式回答（plan §1.1 第 4 条）', [3, 3])`
- `E   AssertionError: ('调用结束且无工具调用 → role_evidence 必须是 call_closed_without_tools', [('直接回答。', None), ('直接回答。', None)])`

覆盖：工具增量晚于正文 **320ms / 1200ms** 两档、纯回答的实时可见性、提升只发一次且为累计全文、
工具阶段零正文时的 `tools=[]` 补充调用（含「每轮最多一次」）、`role_evidence`。

#### 问题 3 · 附件显式绑定（事件层，真 HTTP API）

- `E   AssertionError: ('attachment_ids 出现且为空 = 显式声明「没有附件」，不得兜底绑定话题下未绑定附件', ['att_431bfc04964c'])`
- `E   AssertionError: 后端不得把附件绑到这一轮`（模型上下文 / 历史消息里不得出现未发送的附件）
- `E   AssertionError: ('已被别的 turn 绑定的附件不得重复绑定到新轮（归属只能有一个）', {'first': 'turn_0593308e9240', 'second': 'turn_2eb44c48e086', 'now': 'turn_2eb44c48e086'})`

#### 问题 5 · 历史附件打开（事件层，真 HTTP API）

- `E   Failed: 契约 §1.6：缺 GET /api/attachments/{id}/content（历史附件没有可用的打开链路）`（3 条用例）

#### 问题 6 · 附件后台化 / 有界接收（事件层，httpx.ASGITransport 同事件循环实测）

- `E   AssertionError: ('服务器把整个请求体都读完了（没有 Content-Length 时无字节上限）：客户端已发 105000000 字节',)`
- `E   AssertionError: 上传期间事件循环被同步文件 I/O 占住的总时长 301 ms（最大单次停顿 142 ms）：期间其它请求/SSE/停止都会卡住`
- `E   AssertionError: 重定位期间事件循环被同步复制占住的总时长 312 ms（最大单次停顿 111 ms）`

（另有一条**绿守卫**：附件服务的数据库访问始终在同一个线程 → 修复方案不得把整个 service
方法丢进工作线程而重新引入 CI 抓到的共享 sqlite 并发缺陷。）

#### 问题 7 · 轮次结束事实（事件层，真 HTTP API + 真应用关闭路径）

- `E   AssertionError: ('TURN_END 必须带 reason_code（plan §1.2）', {'status': 'failed', 'keys': ['duration_ms', 'ended_at', 'error', 'final_content', 'instance_id', 'queue_ms', ...]})`（provider 失败）
- `E   AssertionError: ('TURN_END 必须带 reason_code（plan §1.2）', {'status': 'cancelled', ...})`（用户停止）
- `E   AssertionError: ('TURN_END 必须带 reason_code（plan §1.2）', {'status': 'completed', 'keys': [..., 'iterations', ...]})`（可恢复工具错误）
- 程序中断用例走真实应用关闭（TestClient 退出 = lifespan shutdown），TURN_END 到达但同样缺字段。

### 前端（DOM 层，15 红 / 7 绿守卫）

命令：

```
cd D:\qio-dev\qio-fix-d\frontend
npx vitest run src/components/__tests__/ProcessDefaultVisibility.audit.verify.test.ts src/components/__tests__/ApprovalFacts.audit.verify.test.ts src/components/__tests__/AnswerRoleStability.audit.verify.test.ts src/components/__tests__/HistoryAttachmentOpen.audit.verify.test.ts src/stores/__tests__/TurnEndFacts.audit.verify.test.ts src/stores/__tests__/SendAttachmentIds.audit.verify.test.ts
```

结果：**Test Files 6 failed (6) / Tests 15 failed | 7 passed (22)**（另有按新契约改写的
`TurnProcess.verify.test.ts`：2 红 / 3 绿，见下）。

- 问题 1（审批）：
  `→ 模型说明必须单独保留（不能只显示描述）: expected '!需要你确认的操作运行测试命令执行本机命令 · 只读本轮工作目录 允许  …' to contain '模型说明：这一步要跑单元测试'`
  → 内联卡只渲染 `approvalIntent` 一行 + 能力清单：**实际命令、独立说明、路径/参数/风险/预算都没有入口**。
- 问题 2（渲染稳定性）：
  `→ 任何情况下都不允许「正式回答 → 过程区」的移动：文字必须留在答案区`
  （迟到的同一 delta_id `interim=true` 事件把答案文字搬回过程区）；
  `→ 提升之后必须显示在正式回答区`（`interim=false` 的提升事件不会把消息提升回答案区）。
- 问题 3（前端一律发送字段）：
  `→ 空列表也必须出现在请求体里：后端要靠「字段存在」区分「显式没有附件」与「旧客户端没给」`
  `→ 发送路径必须把这个字段传到底: expected undefined to deeply equal []`
- 问题 4（默认折叠）：
  `→ 契约 §1.5：运行中默认**不展开**历史（默认可见区不得出现旧阶段与逐项工具卡）: expected true to be false`
  `→ 运行中默认折叠：历史抽屉不该进 DOM`（4 条用例：首轮 / 两阶段切换+同阶段三说明+并行工具 / 可恢复错误 / 完成+重连）
- 问题 5（历史附件入口）：
  `→ 历史里的副本附件必须有可用的打开入口（不能只有一个标签）: expected undefined to be truthy`
  `→ 引用型附件必须提供「重新定位」入口（前端 relocateAttachment 目前没有任何调用点）: expected undefined to be truthy`
- 问题 7（结束事实）：
  `→ 必须记下 reason_code: expected undefined to be 'provider_error' // Object.is equality`
  `→ expected 'undefined' to contain 'A 轮失败了'`（原因按 turn 归属）
  `→ expected undefined to be 'user_stopped' // Object.is equality`
  `→ expected undefined to be 'none' // Object.is equality`
  （旧历史用例是绿守卫：**不得伪造原因**。）

#### 旧验证资产按新契约改写（Lead 2026-10-06 裁决）

`frontend/src/components/__tests__/TurnProcess.verify.test.ts`（D 上一轮的验证资产）里
「运行中过程区默认展开」的断言**已被产品规则取代**（不是实现变了）：plan §1.5 最终版 =
运行中默认不展开历史。已按新契约改写并**保留强度**：

- 必须可见：状态行（含**一行**工具摘要）、当前阶段名、最新一条说明；
- 必须不可见：旧阶段的历次说明、同一阶段更早的说明、**逐项工具卡**（收起态 DOM 里
  `.tool-card` 数量为 0）；
- 展开之后仍然完整可回看，且每段文字全局只出现一次。

命令与结果：

```
cd D:\qio-dev\qio-fix-d\frontend
npx vitest run src/components/__tests__/TurnProcess.verify.test.ts --reporter=verbose
→ × 工具一开跑就出现过程区，没有阶段也要显示真实系统状态
   → 运行中默认不展开历史（plan §1.5 最终版取代了「运行中默认展开」）: expected true to be false
→ × 阶段说明就地更新；op=next 后当前阶段换成新阶段，各文字只出现一次
   → 运行中默认不展开历史（plan §1.5 最终版取代了「运行中默认展开」）: expected true to be false
→ ✓ 同一内容只出现一次：interim 不再单独成泡
→ ✓ 并行工具与晚到结果：晚到的结果只收口自己的卡
→ ✓ 完成后过程区收起，保留简短状态 + 总耗时；回答在下方
```

（该文件的「运行中工具行必须看得见」这条**旧**断言本身与 §1.5 冲突，已改为
「状态行必须给出一行工具摘要 + 逐项工具卡不得出现在默认可见区」。）

#### 流式 store 层：废止「答案 → 过程」方向（§1.1 最终版）

`frontend/src/stores/__tests__/streamingDeltas.verify.test.ts` 里
「守卫放行后把同一段文字**移到**过程区」是旧规则，§1.1 已**永久废止该方向**。已改写为：

- 正文一开始就以 `interim=true` 在**过程区**实时可见；
- 调用结束且无工具调用 → **同一 delta_id** 用 `{interim:false, streaming:false}` 原样**提升**
  到答案区（全局只有一份，不重打、不重复）；
- 工具轮正文留在过程区；**答案区文字不因后来的工具增量消失或转移**；
- 原有强度全部保留：**顺序去重**（回退/重复 seq 丢弃）、**累计快照**（就地更新、不回退、
  不追加第二条气泡）、不同 delta_id 互不覆盖、TURN_END 只校准、取消保留已确认文本。

```
npx vitest run src/stores/__tests__/streamingDeltas.verify.test.ts --reporter=verbose
→ × 无工具调用：同一 delta_id 用 {interim:false, streaming:false} 原样提升到答案区
   → 提升之后必须是正式回答: expected true to be false
→ × §1.1 废止的方向：迟到的 interim=true 不得把答案区文字搬回过程区
   → 文字必须留在答案区，不得被搬回过程区: expected false to be true
→ ✓ 正文一开始就在过程区实时可见（不是等 provider 结束才出现）
→ ✓ 工具轮：正文留在过程区；答案区文字不因后来的工具增量消失或转移
→ ✓ provider 还没结束：正式回答已非空、处于 streaming、且不是「过程」
→ ✓ 累计快照就地更新：不追加第二个气泡、不回退
→ ✓ 回退 / 重复 seq 必须丢弃（重连补发不得让文字倒退或重复）
→ ✓ 不同 delta_id 互不覆盖：seq 去重必须按 delta_id 各算各的
→ ✓ TURN_END.final_content 只做校准：替换不追加、全文只出现一次
→ ✓ 取消：已确认文本保留、不当成最终答案、状态如实
```

#### 历史附件 DOM 锚点（B 提供）+ 真实点击

`HistoryAttachmentOpen.audit.verify.test.ts` 已改用 B 的锚点断言**真实可点击**：
`[data-test="message-attachments"]` 行容器、`[data-test="message-attachment"]`
单附件（带 `data-id`/`data-kind`/`data-state`）、`[data-test="attachment-notice"]` 反馈、
`.attach-note` 无元数据兜底；打开入口必须真的触发 C 的
`GET /api/attachments/{id}/content`（断言 URL + Authorization + GET），重新定位入口点下去
必须有可观察反馈（选择器 / 路径输入 / 提示 / `/relocate` 请求），不能是装饰按钮。

```
npx vitest run src/components/__tests__/HistoryAttachmentOpen.audit.verify.test.ts --reporter=verbose
→ × 副本附件：真实可点，并且真的去取 /api/attachments/{id}/content（带认证头）
   → 每个附件都要有 [data-test="message-attachment"] 锚点: expected 0 to be greater than 0
→ × 引用型附件：真实可点的重新定位入口，点下去必须有可观察反馈   → 同上（缺锚点）
→ × 状态诚实：missing 的附件不得被当成可以打开（状态可机器检查） → 同上（缺锚点）
→ ✓ 只有 id 没有元数据：如实说「元数据未加载」，不编造文件名
```

#### eventBufferOverflow：超时上限放宽（负载问题，不是功能回归）

`frontend/src/stores/__tests__/eventBufferOverflow.verify.test.ts` 在**整套并行跑**时
曾顶掉 30s 上限（单跑约 7–10s）。已在文件头写明口径并把上限放宽到负载下也够用：
压力用例（2 万条）90s、其余两条 60s。**功能断言一个字都没放宽**
（上限必须存在 / 溢出必须标记重新同步 / 无溢出时一条不丢）。

实测（本次同批并行跑，机器同时在跑别的验收套件）：

```
→ ✓ 同步期间灌入 2 万条事件：缓存必须有界，不能无限增长     49517ms
→ ✓ 溢出时必须标记「需要重新同步」，不能静默丢事件           11579ms
→ ✓ 溢出之后不得宣称已同步（resyncState 不能停在 normal）    14123ms
→ ✓ 没有溢出时：缓存事件按到达顺序补放，一条都不丢              19ms
```

### 集成实现复跑（临时 detached worktree @ `fix/unified-process-audit` `9ddf017`）

Lead 报告集成分支上我的验收有 9 红，其中 2 类属**测试侧**（实现是对的）。已修：

1. **测试隔离**：过程区展开状态在**模块级 store**（键 = 话题|轮|阶段）且会持久化，
   同一文件前面的用例调过 `openProcessHistory` → `manual=true`，后面同一 `turn_1` 的用例
   就被「用户手动开合过不再自动改」保护住（该保护是契约要求，不能删）。
   → `TurnProcess.verify.test.ts` 与 `ProcessDefaultVisibility.audit.verify.test.ts` 的
   `beforeEach` 现在都调用 `resetProcessState()` + 清 `localStorage`。
   **没有**放宽「完成之后过程区保持折叠」这条断言；反而把它拆成两条更强的：
   「用户没碰过 → 完成前后都折叠」+「用户手动展开过 → 完成时不收走（保护阅读）」。
2. **引用型重定位的 fixture 用错状态**：健康引用（`ready`）本来就没有重新定位需求。
   → 重新定位断言改用 `missing` 与 `changed`，并**新增**一条正向覆盖：
   健康的引用型必须显示「引用本地文件」+「不保证内容仍然存在」，且**不**出现假的重新定位入口。
   → 另加一条：`pickLocalPath` 返回 null（无选择器/用户取消）时**不得**提交重定位、
   不得宣称成功。

复跑还暴露并修掉 3 处我自己的 fixture 假设错误（同样是我这边的问题，不是实现）：
- 逐项工具卡在集成实现里位于「本阶段明细」**独立开关**后面（`[data-test="turn-process-stage-toggle"]`
  → `[data-test="turn-process-stage-tools"]`），不是整轮历史抽屉里 → 断言按新锚点改写
  （默认收起 = 默认可见区看不到；点开才看得到）；
- 重新定位断言改为**确定性**：桩掉原生选择器返回一个真实路径 → 断言真的发出
  `POST /api/attachments/{id}/relocate`（含认证头），而不是靠「点下去有没有反应」猜。

集成实现上的复跑（把本 worktree 的这批测试文件覆盖到临时 detached worktree 后运行；
临时 worktree 已删除，本 worktree 未因此产生任何提交）：

```
cd <临时 detached worktree>/frontend
npx vitest run src/components/__tests__/TurnProcess.verify.test.ts \
  src/components/__tests__/HistoryAttachmentOpen.audit.verify.test.ts \
  src/components/__tests__/ProcessDefaultVisibility.audit.verify.test.ts \
  src/stores/__tests__/streamingDeltas.verify.test.ts \
  src/stores/__tests__/eventBufferOverflow.verify.test.ts
→ ✓ TurnProcess.verify.test.ts (5 tests)
→ ✓ HistoryAttachmentOpen.audit.verify.test.ts (6 tests)
→ ✓ ProcessDefaultVisibility.audit.verify.test.ts (5 tests)
→ ✓ streamingDeltas.verify.test.ts (10 tests)
→ ✓ eventBufferOverflow.verify.test.ts (4 tests) 12873ms（并行；2 万条那条 9306ms）
→ Test Files  5 passed (5) / Tests  30 passed (30)
```

同一批文件在**本 worktree（基线实现 e428bb9）**上仍然是红的，符合预期
（实现修复不在这个 worktree 里）：

```
cd D:\qio-dev\qio-fix-d\frontend
npx vitest run src/components/__tests__/TurnProcess.verify.test.ts src/components/__tests__/HistoryAttachmentOpen.audit.verify.test.ts
→ Test Files  2 failed (2) / Tests  7 failed | 4 passed (11)
npx vue-tsc --noEmit → EXIT=0
```

### 类型检查

```
cd D:\qio-dev\qio-fix-d\frontend; npx vue-tsc --noEmit   → EXIT=0
```

## 2. 绿守卫（现在就是绿的，修完必须还是绿的）

1. 缺 `attachment_ids` 字段 → 旧客户端兜底绑定仍然生效（后端）。
2. 显式给一个 id → 不会顺手绑定别的未绑定附件；给一个不存在的 id → 不会退回兜底（后端）。
3. 附件服务的数据库访问始终在同一个线程（后端，防 CI 复发的共享 sqlite 并发缺陷）。
4. `/api/attachments/{任意路径}` 一律拒绝（后端；端点上线后必须仍然拒绝）。
5. 审批：允许/拒绝按 `approval_id` 应答；多条排队各显示自己的事实；非当前轮的审批不内联（前端）。
6. 提升之后到来的工具/阶段事件不改变答案区内容（前端）。
7. 旧记录没有 reason/actions → 不伪造（前端）。
8. missing 状态附件不得被当成可以打开（前端）。
9. 纯文字消息的本地消息里没有附件（前端）。

## 3. 已实现 / 已验证 / 未验证（诚实清单）

**已实现（本阶段交付，均为新增验收资产，未改任何实现文件）**

- `backend/tests/test_audit_stream_role_verify.py`（7 条）
- `backend/tests/test_audit_attachment_binding_verify.py`（6 条）
- `backend/tests/test_audit_attachment_io_verify.py`（4 条）
- `backend/tests/test_audit_attachment_content_verify.py`（4 条）
- `backend/tests/test_audit_turn_end_facts_verify.py`（4 条）
- `frontend/src/components/__tests__/ProcessDefaultVisibility.audit.verify.test.ts`（4 条）
- `frontend/src/components/__tests__/ApprovalFacts.audit.verify.test.ts`（4 条）
- `frontend/src/components/__tests__/AnswerRoleStability.audit.verify.test.ts`（3 条）
- `frontend/src/components/__tests__/HistoryAttachmentOpen.audit.verify.test.ts`（3 条）
- `frontend/src/stores/__tests__/TurnEndFacts.audit.verify.test.ts`（5 条）
- `frontend/src/stores/__tests__/SendAttachmentIds.audit.verify.test.ts`（3 条）
- `frontend/src/components/__tests__/TurnProcess.verify.test.ts`（D 上一轮资产，按 §1.5 最终版
  改写「运行中默认展开」的旧断言；事件驱动 + 真挂载）
- `frontend/src/stores/__tests__/streamingDeltas.verify.test.ts`（D 上一轮资产，按 §1.1 最终版
  改写「答案 → 过程」的废止方向，新增提升与不转移断言）
- `frontend/src/components/__tests__/HistoryAttachmentOpen.audit.verify.test.ts`（改用 B 的 DOM
  锚点，断言真实点击 + 触发 C 的 `/content` 接口）
- `frontend/src/stores/__tests__/eventBufferOverflow.verify.test.ts`（只放宽超时上限并写明负载口径，
  功能断言不变）
- `scripts/verify-audit-phase1.ps1`（一键复跑本文件的所有命令）

**已验证**

- 上述 **35 条**用例在基线 e428bb9 上**真的变红**，且红在**产品规则**上（不是语法/导入/mock 写错）：
  红点覆盖问题 1/2/3/4/5/6/7 七项；原始输出见 §1。
- 按新契约改写的两处旧验证资产同样**真的变红**：`TurnProcess.verify.test.ts` **2 红 / 3 绿**、
  `streamingDeltas.verify.test.ts` **2 红 / 5 绿**（旧断言分别被 §1.5 / §1.1 最终版取代），
  且 `HistoryAttachmentOpen.audit.verify.test.ts` 改用 B 的锚点后为 **3 红 / 1 绿**。
- 合计：**40 条红 / 24 条绿守卫**（后端 20 红 / 5 绿；前端 20 红 / 19 绿）。
- `eventBufferOverflow.verify.test.ts` 4 条全绿（只放宽了超时上限，功能断言未动）。
- 每条用例失败信息都写明「缺什么」，没有静默跳过（无 try/catch 吞断言、无 `--passWithNoTests`）。
- `npx vue-tsc --noEmit` 在加入这些用例后仍然通过（EXIT=0）。
- 附加约束：模型调用全部为假 adapter / 假厂商端点；没有联网、没有真实 Key；
  日志与断言输出中不含任何密钥原文。

**未验证（不得当作已通过）**

- **删除原文件后再打开**（问题 5 的后端一半）只在「副本仍在、原文件已删」这一种情形的**用例形态**
  上就绪；失败/变化/同名文件/重启历史/类型不支持等组合尚未逐条取证。
- **引用型在 UI 里真实重定位**（原生选择器 → relocate → 状态回落）未做真机验证。
- **100MB 等号边界的真实上传**只覆盖了「无 Content-Length 超限拒绝」与 60/90MB 的
  事件循环占用；等号边界本身未跑（`test_attachments_contract_verify.py` 覆盖了阈值判定）。
- **Anthropic / 兼容档**只在 loop + StreamDelta 层覆盖（假 adapter）；**没有**用真实
  Anthropic 线路格式的假端点验证。
- **实际启动应用**（后端 + vite + 假厂商 + Playwright/msedge 截图）全部未做 → 阶段二。
- 「审批**必须实际触发一次前端审批**」（§3 第 2 条）目前只有事件载荷 + DOM 断言，
  没有真机点击；阶段二补。
- 前端「待发附件与话题/草稿绑定、组件重建后可见恢复」的 UI 语义未取证（当前只验到
  「一律发送字段」与「本地消息不伪造附件」）。

## 4. 阶段二计划（Lead 集成后）

1. 在集成分支复跑本文件全部用例 + 全量 `pytest` / `vitest` / `vue-tsc`。
2. 实际启动应用（后端 + vite + `scripts/e2e_fake_provider.py`），用 Playwright/msedge 截图取证：
   默认折叠、正式回答稳定性、内联审批（含真实点击）、历史附件打开与重新定位、失败入口。
3. 把阶段二截图与操作记录补进本文件（新增 `docs/verification-audit-phase2.md`），
   并保留每张截图对应的命令与时间。
