# D 独立验收（R4 三项剩余问题）· 阶段二：实机交互取证

- 验证方：子智能体 D（独立验收，**不改实现文件**）
- worktree：`D:\qio-dev\qio-r4-v`（分支 `wt/r4-verify`），已 merge `fix/process-stream-retry-upload`（`f75315c`）
- 链路：本机假厂商（真 SSE，`scripts/verify_stream_provider.py`）→ uvicorn 8734 → vite 5199 → msedge 无头 + Playwright
- 一键：`powershell -ExecutionPolicy Bypass -File scripts/verify-r4-phase2.ps1`
  产出：`docs/verification-shots-r4-phase2/*.png`（11 张）、`summary.json`（17 条断言 + 真实网络台账）
- **口径**：provider 是本机扮演的假厂商，结论只能读成「QIO 自己的链路对」，**不证明任何真实厂商行为**。

## 1. 实机结果：**20 / 20 通过**（§1.6 的缺陷已由 B + 后端权威路径修复并复验）

### 1.1 S1 正式回答流式 + 端到端时间线（Lead 点名）

```
[PASS] S1 正式回答在 provider 结束前出现在正式回答容器里（.message.assistant）
       :: {"observed":true,"answers":"…正式回答第一句。…","shot":"r4-01-answer-live-before-provider-end.png"}
[PASS] S1 命中时刻过程区不含这段正式回答 :: {"process":"已完成\n▸\n耗时 135 毫秒\n运行中"}
[PASS] S1 端到端时间线：首个正文显示时刻早于 provider 结束
       :: {"firstTextVisibleAtMs":1791323529947,"providerStreamEndAtMs":1791323531859,"leadMs":1912,
           "answerCallStillOpenAtObservation":true,"chunksSent":3,
           "timelineEvents":["stream_start","stream_end","stream_start","chunk_sent","chunk_sent","chunk_sent","stream_end"],
           "shot":"r4-02-answer-complete.png"}
[PASS] S1 完成后：正式回答只在回答容器里（过程区没有副本，全局一份） :: {"duplicateCount":1,"inProcess":false}
```

- 「首个正文显示时刻」是我在页面上**看到第一句文字的瞬间**（`waitFor` 命中 `.message.assistant` 含「正式回答第一句。」），
  「provider 结束」是假厂商 `/__timeline` 里**最后一次 `stream_start` 之后的 `stream_end`**；
  两者相差 **1912 ms**（Node 侧同一时钟），且命中时那次回答调用**还没有 stream_end**（`answerCallStillOpenAtObservation:true`）。
- 时间线里 3 个 `chunk_sent` 对应假厂商分 3 次发正文（每片间隔 700ms），说明是**边生成边显示**，不是结束后一次性出现。
- 截图：`r4-01-answer-live-before-provider-end.png`（流中，回答区已有第一句）、`r4-02-answer-complete.png`（完成后仍是同一份，无副本）。

### 1.2 S2 断流前后 DOM 对照

```
[PASS] S2 断流前：已显示的正式回答在 .message.assistant 里、过程区不含
[PASS] S2 断流：未到达的那一句一个字都没出现（真的断在中间）
[PASS] S2 断流后：文字仍在回答容器里、过程区仍不含它、全局只有一份
       :: {"duplicateCount":1,"inProcess":false,"shot":"r4-04-answer-after-abort.png"}
```

假厂商在第 1 片之后断流（`abort_after:1`）：断流前文字已在回答容器、断流后**仍在**且过程区始终不含它，
未到达的第二句没有出现。截图：`r4-03-answer-before-abort.png`、`r4-04-answer-after-abort.png`。

### 1.3 S3 带附件重试（真实界面「重试」入口）

```
[PASS] S3 第一轮失败后界面出现真实「重试」入口 :: {"chipReady":true,"hasRetry":true,"shot":"r4-05-failed-turn-with-retry.png"}
[PASS] S3 点「重试」真的开了新轮，且新轮附件是克隆（新 id、原文件已删除）
       :: {"originalId":"att_4d98199c2833","newIds":["att_7fe26f67949a"],"state":"ready","shot":"r4-06-after-retry.png"}
[PASS] S3 克隆副本仍能读出原附件内容（原文件已删除）
       :: {"content":"R4-阶段二附件内容：只有这份副本里才有的标记 7f3a\n","state":"ready"}
```

流程全在界面上：路径粘贴 → chip 变成「已保存副本」→ 发送（第一轮假厂商 500）→ 出现「重试」→
**删除原文件** → 点「重试」→ 新轮附件是**新 id 的克隆**，`GET /content` 读出的仍是原内容。
（「读取工具真的读出内容」这层由同名后端验收件 `test_r4_attachment_retry_verify.py` 用真实
`ReadAttachmentTool` 覆盖，本轮实机覆盖的是**界面入口**这一层。）

### 1.4 S4 上传写盘失败：界面明确失败，不是无限转圈

```
[PASS] S4 写盘失败：界面在有限时间内给出明确失败与原因（不是无限转圈）
       :: {"composerText":"默认话题 … r4-写盘失败.txt 28 B 文件不在原位 重新定位 重试 × 附件 路径 ↑",
           "shot":"r4-07-upload-failed.png"}
[PASS] S4 写盘失败：没有停在「准备中」的残留记录 :: {"rows":[["r4-写盘失败.txt","missing"]]}
```

制错方式：把复制目标的**月目录**临时换成同名文件（`<data>/attachments/<年>/<月>`），
mkdir 必失败；收尾还原，不影响后续场景。界面给出「文件不在原位 + 重新定位 / 重试」，
不再显示「准备中」，后端也没有停在 `prepared` 的残留记录。

### 1.5 S5 回归

```
[PASS] S5 运行中默认折叠：默认可见区没有历史抽屉 :: {"historyCount":0,"shot":"r4-08-running-default-collapsed.png"}
[PASS] S5 回归：刷新后历史附件行仍在，点「打开」→ GET /content 200 :: {"rowCount":1,"contentStatus":200,"shot":"r4-09-history-attachment-open.png"}
```

### 1.6 S6「刷新后重试」= 曾确认的产品缺陷 → **已修复并复验通过**

**稳定前置后的最终结果（17/18，唯一红 = 这条）**：

```
[PASS] S6 前置：这一轮以 provider 错误失败并出现真实「重试」入口
       :: {"retryBeforeRefresh":true,"processRegionsBeforeRefresh":2,"leftoverChipsBeforeSend":0,
           "composerText":"默认话题 Enter 发送 · Shift+Enter 换行 附件 路径 ↑"}
[FAIL] S6 刷新（历史恢复）之后：结束原因/过程区仍在，且「重试」入口仍在
       :: {"retryBeforeRefresh":true,"processRegionsBeforeRefresh":2,
           "processRegionsAfterRefresh":1,          ← 失败轮的过程区没有从历史恢复
           "retryAfterRefresh":false,               ← 刷新后「重试」入口消失
           "netTail":[{"method":"POST","status":200,"url":"…/api/turns"}×3],
           "shot":"r4-11-after-refresh-retry-entry.png"}
```

**这是一条真缺陷（不是装置问题）**：前置已按 Lead 要求做成稳定——
① 带附件的一轮以模拟 provider 错误失败（界面出现真实「重试」）→ ②确认入口可见
（`retryBeforeRefresh:true`、过程区 2 个）→ ③再刷新 → ④刷新后过程区只剩 1 个、**「重试」入口消失**。

早期那一轮我没有下这个结论是对的：当时前置没复现（`retryBeforeRefresh:false`），
根因是我自己的场景卫生问题——S4 失败上传留下的 chip 挡住了发送闸门
（输入区如实显示「这些附件没有准备好，不能当作发送成功：「r4-写盘失败.txt」文件不在原位」），
于是那一轮根本没发出去。修掉场景卫生（用产品自己的 chip「×」移除）之后前置稳定，缺陷随之复现。

**影响**：失败/中断的一轮在**刷新（历史恢复）之后失去「重试」入口**，用户只能重写；
这与 round3 `TURN_END` facts/actions 在历史恢复后的可用性同源。已交 B。

**修复来源（两层）**：B 的前端在同一浏览器刷新后从本机留痕恢复事实；**后端权威路径**
`TURN_END` 的 `reason_code/reason/stopped_by/actions` 落进 `turn_journal`（迁移 28），
并随 `/api/session/context`、`/api/session/messages`（分页）与 `/api/runtime/state`（RESYNC）
以 `turn_facts` 下发（旧记录不伪造）。

**复验（同一套稳定前置，20/20 全绿）**：

```
[PASS] S6 前置：这一轮以 provider 错误失败并出现真实「重试」入口
       :: {"retryBeforeRefresh":true,"processRegionsBeforeRefresh":7,"leftoverChipsBeforeSend":0}
[PASS] S6 刷新（历史恢复）之后：结束原因/过程区仍在，且「重试」入口仍在
       :: {"processRegionsBeforeRefresh":7,"processRegionsAfterRefresh":7,"retryAfterRefresh":true,
           "shot":"r4-11-after-refresh-retry-entry.png"}
[PASS] S6 清掉 localStorage 再刷新：失败轮的事实仍从后端历史恢复（过程区 + 重试入口）
       :: {"processRegionsAfterWipe":7,"retryAfterWipe":true,
           "shot":"r4-12-after-localstorage-wipe.png",
           "note":"证明后端权威路径真的生效，而不是只靠前端留痕"}
[PASS] S6 刷新后点「重试」：新轮附件是克隆（新 id、原文件已删除）且内容读得回来
       :: {"originalId":"att_a2ec40581501","newIds":["att_59a04401ecbb","att_ca2bac8f96c2"],
           "state":"ready","content":"R4-阶段二附件内容：只有这份副本里才有的标记 7f3a",
           "shot":"r4-13-retry-after-refresh-result.png"}
```

第 3 条是**清掉 localStorage / sessionStorage 之后**的断言（模拟换设备/清存储）：
事实仍然从后端历史恢复 —— 这一条专门用来排除「只靠前端本地留痕」的假通过。

## 2. 闸门（阶段二 worktree 实跑）

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| D 的 6 个 R4/审计文件 | `$env:PYTHONPATH='src'; .venv\Scripts\python.exe -m pytest tests/test_r4_*.py tests/test_audit_{stream_role,attachment_binding,turn_end_facts}_verify.py -q` | **EXIT=0：45 passed / 1 skipped**（`...................s..........................`） |
| 后端全量 pytest | 同上，不带文件参数 | 与实机取证**并行**跑时 EXIT=1：唯一失败是 `tests/test_cmd_tools.py::test_proc_list_ok - TimeoutError`（进程列表工具超时）；**单独重跑 EXIT=0** → 判为负载抖动（当时 16 核被 vite/msedge/实机取证占满），不是本次三项问题相关的回归。集成分支上 Lead 复跑为 EXIT=0。 |
| 前端全量 vitest | `cd frontend; npx vitest run` | **Test Files … Tests 1140 passed (1140)**，EXIT=0 |
| 前端类型检查 | `npx vue-tsc --noEmit` | **EXIT=0** |
| 文档一致性 | `python scripts/check_docs.py` | **EXIT=0**（文档一致性检查通过，29 个里程碑条目） |
| 实机取证 | `powershell -File scripts/verify-r4-phase2.ps1` | **20 / 20**（含 §1.6 刷新后重试 + 清 localStorage 变体） |
| 事件循环停顿口径（本文件之外，D 的 round3 验收件） | `tests/test_audit_attachment_io_verify.py` | 改为**按环境地板标定**（见 §5）：max_stall ≤ max(120ms, 3×floor)，并新增「400 ms 同步阻塞必须被判红」的鉴别力自证 |

## 3. 逐项对照 plan §3

| plan §3 | 证据 | 结论 |
| --- | --- | --- |
| 问题一：回答区在 provider 结束前已有字 / 过程区无副本 / interim=false 且在调用结束前 / 前端渲染在正式回答容器 | §1.1（leadMs=1912ms、answerCallStillOpenAtObservation=true、3 片 700ms）、§1.2（断流前后）；后端事件层 `test_r4_answer_phase_verify.py` + `test_audit_stream_role_verify.py`（含「一轮请求台账：tools=[] 确实发起」诊断） | **已验证**（后端事件层 + 实机界面层） |
| 问题二：界面重试 → 新轮读出原附件内容 / 原文件删除后仍可读 / 原轮历史归属不变 / 结构化拒绝 / **刷新后重试（含清 localStorage）** | §1.3 + §1.6（复验绿）；后端 `test_r4_attachment_retry_verify.py`（克隆 + 真实读取工具 + 跨话题/已绑他轮 409 + 引用型 missing + resend） | **已验证** |
| 问题三：三类受控失败 + 队列超容 + 状态/临时文件/线程收敛 + 取消/断开 + 同期 API | §1.4（界面层）；后端 `test_r4_upload_convergence_verify.py`（写入途中/建目录/开临时文件/取消/断开/队列超容/DB 跨线程探针）+ C 的 `test_attachment_upload_convergence.py` | **已验证** |
| 回归：默认折叠、历史附件打开、结束原因与耗时 | §1.5 + 阶段一 `test_audit_turn_end_facts_verify.py`（45 passed 里含它） | **已验证** |

## 4. 已实现 / 已验证 / 未验证

**已实现（D 名下资产）**：`scripts/verify-r4-phase2.ps1`、`scripts/verify-r4-phase2-shots.mjs`、
`docs/verification-shots-r4-phase2/`（11 张截图 + summary.json）、阶段一的三个 `test_r4_*` 与两个改写过的老文件、本文件。

**已验证**：（除上面这条负载抖动外）§2 的闸门数字；§1.1–§1.5 的实机行为（回答流式 + 时间线、断流前后 DOM、
带附件重试（含原文件删除）、上传失败界面反馈、默认折叠与历史附件打开）。

**未验证（不当作通过）**：
0. （原第 1 条「刷新后重试未验证/缺陷」已于 §1.6 修复并复验通过，本条移出未验证清单。）
1. **后端 API 级同名用例 `test_retry_after_history_refresh_uses_message_attachment_ids` 仍是 skip** ——
   装置受限（API 级夹具没有可跑通的 provider，历史里没有附件行）；该路径由实机 §1.6 覆盖。
2. **真实厂商**（OpenAI/Anthropic/兼容档）—— 全程假厂商，结论不外推。
3. **断流后的自动重连**（网络层重连）—— 本轮只覆盖「断流后已显示文字不丢」。
4. **前端在窄窗口/大量历史下的视觉**（滚动、代码块溢出）—— 本轮截图固定 1440×900。
5. **上传失败的具体错误文案在多语言/长路径下的表现**；**100MB 等号边界的真实上传**。
6. **Tauri 桌面壳内的重新定位**（浏览器内无原生选择器；阶段一已用最小桩覆盖过链路）。

**阶段一的原始 FAIL 行保留**：见 `docs/verification-r4-phase1.md` 第 1–5 节（26 红基线）与 §6/§7（合并后转绿差值）。

## 5. 事件循环停顿口径：**确定性判据为主**（墙钟降级为 smoke）

> 本节记录两轮收口：先按环境地板标定（§5.0），再按 Lead 裁决换成**确定性判据**（§5.1，当前口径）。

### 5.0 第一轮：按环境地板标定（已被 §5.1 取代为主判据）

**CI 证据（run 37542503098，commit `36d7468`）**：两个 job 红，都是同一类阈值测试 ——

```
backend (py3.11)      tests/test_audit_attachment_io_verify.py::test_upload_does_not_block_the_event_loop
                      AssertionError: 上传期间事件循环被一次同步文件 I/O 占住 407 ms（累计 417 ms） [阈值 120ms]
backend (windows-latest) tests/test_interactive_during_heavy_work.py::test_health_probe_stays_responsive_while_slow_prediction_runs
                      AssertionError: 慢推理期间事件循环被占住 103 ms [阈值 100ms]   ← 既有测试，不是本轮改的
```

**两条互相独立**的「事件循环停顿阈值」测试在同一次运行里同时越线（其中一条只超 3 ms）→
指向 **2 vCPU runner 被抢占**，不是代码同步阻塞。旁证：C 的压力实验里，同一台机器在极端压力下
**1 KB 请求**的地板就有 91–100 ms；实现侧事件循环只做 6–9 条行级 sqlite 语句与几次 stat。

**改法（D 的 `test_audit_attachment_io_verify.py`，不是放宽功能要求）**：

1. 同一次运行内先测**对照地板** `floor_ms`：同样的心跳/探针机制跑一次 **1 KB 上传**；
2. 硬指标改为 `max_stall ≤ max(120ms, 3 × floor_ms)`，**保留**「工作期间探针完成数 ≥3 且
   探针延迟 ≤ 同一上限」；
3. `floor_ms` / 上限 / `max_stall` / 累计值 / 探针统计**全部打印**（诊断不参与判定）；
4. **地板很低的机器上仍然是 120 ms 硬线**（本机实跑：地板 8 ms → 上限 120 ms）。

**鉴别力自证**（口径不是放宽）——同一次运行先量地板，再在事件循环上放一个 400 ms 同步阻塞：

```
[诊断] 对照地板（1 KB 上传）：环境地板 0 ms；本次上限 120 ms；最大单次停顿 8 ms；累计 blocked 3 ms；探针请求 5 次（工作窗口内 5 次，最大延迟 9 ms）
[诊断] 对照地板（1 KB 上传）：环境地板 0 ms；本次上限 120 ms；最大单次停顿 8 ms；累计 blocked 3 ms；探针请求 5 次（工作窗口内 5 次，最大延迟 9 ms）
[诊断] 鉴别力对照（事件循环上 400 ms 同步阻塞）：环境地板 8 ms；本次上限 120 ms；最大单次停顿 402 ms；累计 blocked 397 ms；探针请求 0 次
[诊断] 鉴别力自证通过：400 ms 同步阻塞被判定越线（实测 402 ms > 上限 120 ms）
```

即：修复前那种「整包读 body / 在事件循环里同步写盘 + 算 sha256」在这套口径下**仍然红**
（`test_criterion_still_catches_synchronous_blocking` 把这条自证固化成了常驻用例，
CI 上再红时会同时打出所有线程栈）。

**同类环境抖动（不在本轮改动范围）**：既有测试 `test_interactive_during_heavy_work.py`
（100 ms 硬线）在同一次 CI 运行里实测 103 ms 越线 —— 属于同一类 runner 抢占抖动，
本报告只记录事实，不改它的口径（它的口径与归属由 Lead 决定）。

### 5.1 第二轮（当前口径）：确定性判据 = 「事件循环线程上没有文件 I/O 与哈希」

**为什么换（第二次 CI 证据）**：地板标定没解决问题。CI run 37546042900：

```
tests/test_audit_attachment_io_verify.py::test_relocate_does_not_block_the_event_loop
重定位期间事件循环被一次同步复制占住 443 ms（本次上限 120 ms = max(120, 3×地板 15)，累计 639 ms）
```

同一次运行**地板只有 15 ms、停顿 443 ms（30× 地板）** → 更像一次**真的长阻塞**，不是整机被抢占。
**推断（不是已证实）**：事件循环线程上的 **sqlite COMMIT（fsync）** 在 CI 虚拟磁盘上偶发几百毫秒 ——
线程纪律要求 DB 只能在事件循环线程访问，所以它天然在环上；工作线程的复制已经不进环了。
墙钟阈值在这里既不可靠（同一份代码 CI 两次：407 ms 上传 / 443 ms 重定位）也不精确
（分不清是我们的文件 I/O 还是 DB 提交）。

**新口径（写入 `test_audit_attachment_io_verify.py` 文件头）**：

1. **主判据（确定性）**：整段上传/重定位期间，**事件循环线程上不得发生任何文件 I/O 与哈希**。
   monkeypatch 覆盖 `builtins.open` / `Path.read_bytes|write_bytes` / `shutil.copy*|disk_usage` /
   `hashlib.sha256(...).update`，记录**调用线程**；事件循环线程上出现任一条即失败，
   并打印是哪一条、哪个调用点（修复前「事件循环里同步写盘 + 算 sha256」**必然**命中）。
2. **活性判据（保留）**：工作期间探针请求确实在推进（窗口内完成 ≥3 次）。
3. **墙钟降级为 smoke + 诊断**：宽松上限 `SMOKE_MAX_STALL_MS = 1000 ms`，`floor/max_stall/累计/探针`
   全部打印；文件头写明墙钟受环境与 sqlite fsync 影响、**不作为主判据**。

**本机实跑（原始诊断）**：

```
[诊断] 上传（2 × 99 MB）：环境地板 9 ms；本次上限 120 ms；最大单次停顿 15 ms；累计 blocked 98 ms；探针 36 次（窗口内 36 次，最大延迟 6 ms）
[诊断] 上传档确定性判据：违规 0 条；忽略（只读、非数据目录）0 条
[诊断] 重定位（3 × 90 MB）：环境地板 7 ms；本次上限 120 ms；最大单次停顿 17 ms；累计 blocked 217 ms；探针 73 次（窗口内 73 次，最大延迟 11 ms）
[诊断] 重定位档确定性判据：违规 0 条；忽略（只读、非数据目录）0 条
```

**鉴别力自证（两个对照组，常驻用例）**：

```
[诊断] 事件循环线程上出现 builtins.open：mode=wb path=…\loop-thread-sync.bin（test_audit_attachment_io_verify.py:551 in _work）
[诊断] 事件循环线程上出现 hashlib.sha256.update：1000000 bytes（test_audit_attachment_io_verify.py:554 in _work）
[诊断] 对照组 A（事件循环上同步写盘+哈希）：违规 2 条；忽略 0 条
[诊断] 对照组 B（同样工作只放在工作线程）：违规 0 条；忽略 0 条
[诊断] 鉴别力对照（事件循环上 400 ms 同步阻塞）：环境地板 10 ms；最大单次停顿 401 ms；探针 0 次
[诊断] 鉴别力自证通过：400 ms 同步阻塞被判定越线（实测 401 ms > 上限 120 ms）
```

- 对照组 A：事件循环上同步写盘 + 哈希 → **被抓 2 条**（含文件名与调用点）；
- 对照组 B：同样的工作只放工作线程 → **0 违规**（判据只看事件循环线程，不误伤正确做法）；
- 另有一个 400 ms 同步阻塞的 smoke 口径自证（墙钟仍能抓 1s 级）。

**闸门**：D 的 7 个文件（含本文件）`EXIT=0，52 passed / 1 skipped`。
