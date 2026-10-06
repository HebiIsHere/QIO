# D 独立验收（R4 三项剩余问题）· 阶段二：实机交互取证

- 验证方：子智能体 D（独立验收，**不改实现文件**）
- worktree：`D:\qio-dev\qio-r4-v`（分支 `wt/r4-verify`），已 merge `fix/process-stream-retry-upload`（`f75315c`）
- 链路：本机假厂商（真 SSE，`scripts/verify_stream_provider.py`）→ uvicorn 8734 → vite 5199 → msedge 无头 + Playwright
- 一键：`powershell -ExecutionPolicy Bypass -File scripts/verify-r4-phase2.ps1`
  产出：`docs/verification-shots-r4-phase2/*.png`（11 张）、`summary.json`（17 条断言 + 真实网络台账）
- **口径**：provider 是本机扮演的假厂商，结论只能读成「QIO 自己的链路对」，**不证明任何真实厂商行为**。

## 1. 实机结果：16 / 17 通过（1 条未验证，见 §1.6）

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

### 1.6 S6「刷新后重试」= **未验证**（前置条件没复现，如实记红）

```
[FAIL] S6 刷新（历史恢复）之后，失败那一轮的「重试」入口仍然可用
       :: {"retryBeforeRefresh":false,"processRegionsBeforeRefresh":0,
           "processRegionsAfterRefresh":0,"retryAfterRefresh":false,
           "shot":"r4-11-after-refresh-retry-entry.png"}
```

**不能据此说「刷新会丢入口」**：这一轮里 `retryBeforeRefresh:false`、`processRegionsBeforeRefresh:0` ——
**刷新之前**那条失败轮的过程区/重试入口就没有出现在 DOM 里，属于我的场景前置条件没复现
（S3 在同一台机器上证明了刚失败时「重试」入口是有的：`hasRetry:true`）。
所以这条的结论只能是：**「刷新后重试」实机路径未验证**（需要先让它稳定复现「失败 + 入口可见」再断言）。
后端 API 级那条 `test_retry_after_history_refresh_uses_message_attachment_ids` 仍是 skip（装置受限），
两者一致：这条路径**未验证**，不当作通过。

## 2. 闸门（阶段二 worktree 实跑）

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| D 的 6 个 R4/审计文件 | `$env:PYTHONPATH='src'; .venv\Scripts\python.exe -m pytest tests/test_r4_*.py tests/test_audit_{stream_role,attachment_binding,turn_end_facts}_verify.py -q` | **EXIT=0：45 passed / 1 skipped**（`...................s..........................`） |
| 后端全量 pytest | 同上，不带文件参数 | 与实机取证**并行**跑时 EXIT=1：唯一失败是 `tests/test_cmd_tools.py::test_proc_list_ok - TimeoutError`（进程列表工具超时）；**单独重跑 EXIT=0** → 判为负载抖动（当时 16 核被 vite/msedge/实机取证占满），不是本次三项问题相关的回归。集成分支上 Lead 复跑为 EXIT=0。 |
| 前端全量 vitest | `cd frontend; npx vitest run` | **Test Files … Tests 1140 passed (1140)**，EXIT=0 |
| 前端类型检查 | `npx vue-tsc --noEmit` | **EXIT=0** |
| 文档一致性 | `python scripts/check_docs.py` | **EXIT=0**（文档一致性检查通过，29 个里程碑条目） |
| 实机取证 | `powershell -File scripts/verify-r4-phase2.ps1` | **16 / 17**（唯一红 = §1.6 未验证项） |

## 3. 逐项对照 plan §3

| plan §3 | 证据 | 结论 |
| --- | --- | --- |
| 问题一：回答区在 provider 结束前已有字 / 过程区无副本 / interim=false 且在调用结束前 / 前端渲染在正式回答容器 | §1.1（leadMs=1912ms、answerCallStillOpenAtObservation=true、3 片 700ms）、§1.2（断流前后）；后端事件层 `test_r4_answer_phase_verify.py` + `test_audit_stream_role_verify.py`（含「一轮请求台账：tools=[] 确实发起」诊断） | **已验证**（后端事件层 + 实机界面层） |
| 问题二：界面重试 → 新轮读出原附件内容 / 原文件删除后仍可读 / 原轮历史归属不变 / 结构化拒绝 | §1.3；后端 `test_r4_attachment_retry_verify.py`（克隆 + 真实读取工具 + 跨话题/已绑他轮 409 + 引用型 missing + resend） | **已验证**；「刷新后重试」子路径未验证（§1.6） |
| 问题三：三类受控失败 + 队列超容 + 状态/临时文件/线程收敛 + 取消/断开 + 同期 API | §1.4（界面层）；后端 `test_r4_upload_convergence_verify.py`（写入途中/建目录/开临时文件/取消/断开/队列超容/DB 跨线程探针）+ C 的 `test_attachment_upload_convergence.py` | **已验证** |
| 回归：默认折叠、历史附件打开、结束原因与耗时 | §1.5 + 阶段一 `test_audit_turn_end_facts_verify.py`（45 passed 里含它） | **已验证** |

## 4. 已实现 / 已验证 / 未验证

**已实现（D 名下资产）**：`scripts/verify-r4-phase2.ps1`、`scripts/verify-r4-phase2-shots.mjs`、
`docs/verification-shots-r4-phase2/`（11 张截图 + summary.json）、阶段一的三个 `test_r4_*` 与两个改写过的老文件、本文件。

**已验证**：（除上面这条负载抖动外）§2 的闸门数字；§1.1–§1.5 的实机行为（回答流式 + 时间线、断流前后 DOM、
带附件重试（含原文件删除）、上传失败界面反馈、默认折叠与历史附件打开）。

**未验证（不当作通过）**：
1. **刷新后重试（实机）** —— §1.6，前置条件未复现；后端 API 级同名用例仍是 skip。
2. **真实厂商**（OpenAI/Anthropic/兼容档）—— 全程假厂商，结论不外推。
3. **断流后的自动重连**（网络层重连）—— 本轮只覆盖「断流后已显示文字不丢」。
4. **前端在窄窗口/大量历史下的视觉**（滚动、代码块溢出）—— 本轮截图固定 1440×900。
5. **上传失败的具体错误文案在多语言/长路径下的表现**；**100MB 等号边界的真实上传**。
6. **Tauri 桌面壳内的重新定位**（浏览器内无原生选择器；阶段一已用最小桩覆盖过链路）。

**阶段一的原始 FAIL 行保留**：见 `docs/verification-r4-phase1.md` 第 1–5 节（26 红基线）与 §6/§7（合并后转绿差值）。
