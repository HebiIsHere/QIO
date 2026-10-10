# R5 阶段二验收报告（D 独立验证）—— 上传终态 / 回答重复 / 附件复制阻塞

- 契约：\`docs/plans/2026-10-07-final-convergence.md\`（Lead 冻结）+ §3 验收清单
- 工作树：\`D:\qio-dev\qio-r5-d\`（分支 \`wt/r5-d\`，已 merge 集成分支 \`fix/process-upload-final-convergence\`）
- 验收人：D（只新增 \`backend/tests/test_r5_*\`、\`scripts/verify-r5-*\`、\`docs/verification-r5-*\`；**不改生产实现**）
- 口径：模型调用一律假 provider（本机扮演 SSE 厂商）；结论只说明「QIO 自己的链路对」，不证明任何真实厂商行为。

## 1. 阶段二实机取证：**15 / 15 通过**（EXIT=0）

跑法：\`powershell -ExecutionPolicy Bypass -File scripts/verify-r5-phase2.ps1\`
（假 provider → \`scripts/e2e_up.py\` 起 uvicorn:8734 + vite:5199 → msedge 无头 + Playwright）
产出：\`docs/verification-shots-r5-phase2/*.png\` + \`summary.json\`

### S1 上传失败/客户端暂停时的界面反馈（明确失败，不是无限转圈）

\`\`\`
[PASS] S1 上传写盘失败：界面在有限时间内给出明确失败与原因（不是无限转圈）
       :: {"composerText":"默认话题 Enter 发送 · Shift+Enter 换行 r5-上传失败.txt 28 B 文件不在原位 重新定位 重试 × 附件 路径 ↑",
           "shot":"r5-01-upload-failed.png"}
[PASS] S1 失败上传没有停在「准备中」的残留记录 :: {"rows":[["r5-上传失败.txt","missing"]]}
\`\`\`

即：写盘失败后界面在有限时间内给出**明确失败 + 「文件不在原位 / 重新定位 / 重试」**，不是无限「准备中」；
接口侧同一条记录也不会停在 \`prepared\`。

### S2 正式回答在 provider 结束前流式可见、且不重复（含未声明降级路径）

\`\`\`
[PASS] S2 回答在 provider 结束前已出现在正式回答容器（.message.assistant）
       :: {"observed":true,"answerCallStillOpen":true,"streamEndsBeforeSend":0,"shot":"r5-02-answer-live-before-provider-end.png"}
[PASS] S2 命中时刻过程区不含这段正式回答 :: {"process":"已完成\n耗时 154 毫秒\n运行中"}
[PASS] S2 完成后：同一答案全局只出现一次（回答区一份、过程区没有副本）
       :: {"occurrences":1,"inProcess":false,"shot":"r5-03-answer-after-complete.png"}
[PASS] S2 未发生「为同一答案再生成一次」的额外调用（调用台账 ≤ 脚本步数 2）
       :: {"requests":2,"kinds":["text","text"]}
[PASS] S2 未声明降级路径：正文一次性出现在回答容器、过程区不含它、全局一份
       :: {"inAnswer":true,"inProcess":false,"occurrences":1,"shot":"r5-04-undeclared-fallback.png"}
\`\`\`

- 声明路径（\`[[QIO:ANSWER]]\`）：正文在**回答调用仍未结束**时就出现在正式回答容器里（\`answerCallStillOpen=true\`），
  过程区**没有**这份文字，整页只出现一次，凭证路径只发生**2 次调用**（工作调用 + 声明回答）——没有「再生成一次」。
- 未声明降级路径：正文一次性出现在回答容器、过程区不含它、全局一份。

### S3 带附件重试（复制克隆）界面正常、内容正确

\`\`\`
[PASS] S3 第一轮失败后出现真实「重试」入口 :: {"chipReady":true,"retryAppeared":true,"shot":"r5-05-failed-with-retry.png"}
[PASS] S3 重试真的开了新轮，新轮附件是克隆（新 id、原文件已删除）且内容读得回来
       :: {"originalId":"att_00171b33e373","newIds":["att_4600e4e621de"],"state":"ready",
           "content":"R5 实机附件内容：只有这份副本里才有的标记 5b21","shot":"r5-06-after-retry.png"}
[PASS] S3 重试后这一轮界面正常（正式回答出现 + 最新一轮已完成、无失败提示）
       :: {"lastRegion":"已完成 耗时 101 毫秒"}
\`\`\`

### S4 回归

\`\`\`
[PASS] S4 运行中默认折叠：默认可见区没有历史抽屉 :: {"historyCount":0,"shot":"r5-07-running-default-collapsed.png"}
[PASS] S4 结束原因与耗时可见（已完成 + 耗时） :: {"tail":"… 已完成 ▸ 耗时 481 毫秒 1 次调用 …"}
[PASS] S4 内联审批：描述/独立说明/真实命令三者都在卡里
       :: {"cardText":"! 电脑操作审批 想运行一条 shell 命令 会执行命令 ◈ QIO 的说明 模型说明：… 会改变什么 … 这次会实际执行什么 命令 echo r5-phase2-ap…",
           "shot":"r5-08-inline-approval.png"}（拒绝后 r5-09-approval-rejected.png）
[PASS] S4 回归：刷新后历史附件行仍在，点「打开」→ GET /content 200
       :: {"rowCount":1,"contentStatus":200,"shot":"r5-10-history-attachment-open.png"}
\`\`\`

## 2. 自动化闸门（集成态，本机实跑）

| 闸门 | 结果 |
| --- | --- |
| D 的 R5 六个验收文件 | **36 passed / 0 failed**（\`test_r5_answer_duplication_verify\` 8、\`test_r5_upload_terminal_verify\` 8、\`test_r5_clone_thread_verify\` 2、\`test_r5_upload_real_http_verify\` 2、\`test_r4_answer_phase_verify\` 10、\`test_audit_stream_role_verify\` 6） |
| 后端全量 pytest | **EXIT=0**（同一棵树实跑，`pytest -q` 全绿） |
| D 的 R5 集合二次确认 | **36 passed / EXIT=0**（`pytest` 六文件一次跑完） |
| 前端 | **1148 passed**（134 files），\`vue-tsc --noEmit\` EXIT=0 |
| \`check_docs\` | 通过（30 个里程碑条目） |
| 实机取证 | **15 / 15**（§1） |

## 3. 逐项对照 plan §3

### 问题一：接收端与工作线程共同收敛

| 验收点 | 装置（\`tests/test_r5_upload_terminal_verify.py\` 等） | 结果 |
| --- | --- | --- |
| 建目录失败 + 请求体暂停 | 注入 \`Path.mkdir\` 失败 + asyncio.Event 闸门；断言请求有限时间内返回、闸门全程未开、已发送块数 ≤ 1、收尾干净 | **已验证** |
| 打开临时文件失败 + 暂停 | 注入 \`.part\` 打开失败（同上形状） | **已验证** |
| 写入中途失败 + 暂停 | 注入第 1 次写入 OSError(28) | **已验证** |
| 删除取消 + 暂停 | 暂停期间 \`DELETE /api/attachments/{id}\` | **已验证** |
| 请求取消 + 读取待决 | 取消 POST 任务后收敛 | **已验证** |
| 队列满 + 工作线程提前退出 | 客户端不停发 24 块 + 立即死掉的工作线程 | **已验证** |
| 失败与最后一块/成功提交的竞争 | 注入第 2 次写入失败 + 释放闸门后最后一块立刻到达；若 ready 必须大小完整 | **已验证** |
| 关键断言在客户端闸门仍关闭时 | 每条都用 \`assert not gate.is_set()\` + 闸门只在收尾时放行 | **已验证** |
| 至少一组走真实上传路由 | 全部走 httpx.ASGITransport（应用真实路由）；另有 \`test_r5_upload_real_http_verify.py\` 起**真 uvicorn** | **已验证** |
| 收尾检查（状态/活动任务/线程/目录文件） | \`_converged()\`：无 prepared、失败不提交 ready、无 \`.part\`、\`active_jobs()\` 为空；真实 HTTP 变体用独立连接轮询 | **已验证** |
| 同期其它 API 能推进 | 闸门关闭期间 \`GET /api/turns/queue\` 200（ASGI 与真实 HTTP 两个变体） | **已验证** |

### 问题二：内容角色协议（不重复生成、不重复显示）

| 验收点 | 装置 | 结果 |
| --- | --- | --- |
| 正式正文第一段后暂停，回答容器已显示 | \`test_r4_answer_phase_verify.py::test_declared_answer_streams_into_answer_area_before_call_ends\`（\`chunk_delay_ms=2500\` + 命中时本轮仍在跑） | **已验证** |
| 过程区没有这份完整答案的副本 | 同上文件 + \`test_r5_answer_duplication_verify.py\` | **已验证** |
| 工具列表非空时同样成立 | 工具轮说明留过程区、声明回答流式进回答区（两轮工具场景） | **已验证** |
| 不为同一答案再次生成正文（记录调用角色与次数） | 调用台账断言：2 轮工具 + 1 次声明回答 = **3 次调用**；实机 S2 为 2 次 | **已验证** |
| 工作调用直接给出完整答案的**当前反例** | \`test_working_call_direct_answer_is_not_generated_twice\`（阶段一红 → 集成后绿） | **已验证** |
| 合法直接回答 / 过程说明+工具+回答 | 两个文件各有用例 | **已验证** |
| 声明缺失（未声明降级） | 一次性交付回答区 + 过程区无副本（后端 + 实机 S2） | **已验证** |
| 声明非法/被拆成多分块/小写 | \`test_split_or_lowercase_declaration_still_recognised\`、\`test_illegal_declaration_falls_back_without_duplication\` | **已验证** |
| 迟到工具增量 | 320ms/1.2s 两档：不执行 + 可见 WARNING | **已验证** |
| 空回答 / 取消 / 断流 | 取消保留已显示文字、厂商 500 如实抛 \`ProviderError\` | **已验证** |
| 重连 / 历史恢复 | 由 round4 的 \`(delta_id, seq)\` 去重与校准用例 + 同一 delta_id 单调断言覆盖 | **已验证** |
| text 兼容档 | \`test_text_compat_adapter_delivers_answer_once\`（1 次调用、不重复） | **已验证** |
| 中文/长文本/代码块/表格 | 中文与长文本在多个用例中；**代码块/表格未单独构造** | **未验证** |

### 问题三：重试克隆的文件 I/O 异步化

| 验收点 | 装置 | 结果 |
| --- | --- | --- |
| 强制 \`os.link\` 抛 OSError → 真实进入 copyfile 退路 | \`test_r5_clone_thread_verify.py\`（monkeypatch \`os.link\`） | **已验证** |
| 复制内部线程闸门 + 记录实际执行线程（**不是事件循环线程**） | 闸门里记 \`threading.get_ident()\`；阶段一红（MainThread）→ 集成后绿 | **已验证** |
| 闸门仍关闭时其它 API / SSE 能推进 | loop 心跳计数 + 测试外观察线程比较（不挂死） | **已验证** |
| 完成后新轮与原轮都能读出正确内容 | 大小/sha256/首中末块抽查 + 两轮 \`/content\` | **已验证** |
| 原文件删除后仍通过 | 实机 S3（原文件删除后重试内容仍读出标记）+ 后端用例 | **已验证** |
| 复制途中失败/取消/并发重试/目标已删除/源副本缺失准确收尾 | 源副本缺失与跨话题/失效引用为**结构化拒绝**（round4 用例）；复制途中失败/取消/并发重试**未单独构造** | **部分未验证** |
| 接近 100,000,000 字节的真实文件 + 等号边界 | 恰好 100,000,000 字节 → \`kind=copy\`、size/sha256 一致、两轮可读、删除原文件后可读、无残留 | **已验证（含等号边界）** |
| 性能证据用线程身份与事件推进，不以毫秒阈值 | 全部用例以线程身份 + 心跳事件推进判定 | **已验证** |

## 4. 阶段一的原始 FAIL 行（保留作对照）

阶段一在基线 \`84f79a2\` 上的红证据（未修饰）：

\`\`\`
test_r5_answer_duplication_verify.py      2 passed / 6 failed
  AssertionError: ('完整答案在过程区留了副本（同一个答案显示两份）',
    {'process': ['结论：这个问题的答案是 42。', '结论：这个问题的答案是 42。'],
     'requests': [{'step':'text','tools':1,'msgs':2}, {'step':'text','tools':0,'msgs':3}]})
test_r5_clone_thread_verify.py            1 failed / 1 passed
  AssertionError: ('重试克隆的复制跑在**事件循环线程**上（基线缺陷：bind_for_turn 被 async 路由同步调用）',
    {'thread_ident': 140244, 'thread_name': 'MainThread', 'is_loop_thread': True, 'calls': 1})
test_r5_upload_terminal_verify.py         3 passed / 4 failed
  AssertionError: ('工作线程已经失败，客户端仍在暂停发送（闸门关闭），上传请求却不返回 —— 接收端没有观察作业终态',
    {'injection': 'mkdir', 'status': 'timeout', 'waited_s': 25.0,
     'state': {'states': ['prepared'], 'stuck_prepared': ['att_7f3962f2ba0e'], …}})
test_r4_answer_phase_verify.py / test_audit_stream_role_verify.py
  按 §1.1 改写后基线 7 passed / 9 failed（失败签名：final_content='[[QIO:ANSWER]]…'、
  ('同一个答案被生成了两次（调用台账）', ['tool_chunks','tool_chunks','text','text'])）
\`\`\`

阶段一当时的红灯现在全部转绿（§2）。阶段一还记录了一条**契约自相矛盾**（Lead 已修正 \`adacfcc\`）：
我按 §1.1 写的「工作调用直接给完整答案 → 只 1 次调用」曾因契约第 2/3 条冲突而红，修正后转绿。

## 5. 未验证与边界（如实）

1. **HTTP/1.1 chunked 上传时客户端读不到提前响应**：真实 uvicorn 变体实测 —— 客户端线程暂停发送请求体时，
   **服务端已经收敛**（无活动作业 / 无 \`.part\` / 无 \`prepared\`），但客户端**拿不到**服务端提前发出的错误响应，
   要等到自己的超时（实测 \`ReadError: [WinError 10053]\`）才结束。因此「请求在客户端暂停期间就返回」这条硬断言
   只在 ASGI 直连用例里断言（那里严格成立）；真实 HTTP 变体断言的是「服务端侧已收敛 + 放行后请求必须结束 + 收尾干净」。
   这是客户端库/HTTP 语义层面的边界，**未**在本轮改动范围内。
2. **强制 \`os.link\` 失败只能在进程内打桩**：实机（独立进程）无法注入；该路径由后端用例
   （\`test_r5_clone_thread_verify.py\`，线程闸门 + 记录执行线程）覆盖，实机 S3 走的是正常硬链接路径。
3. **代码块/表格**的回答角色用例未单独构造（中文/长文本已覆盖）。
4. **复制途中失败 / 取消 / 并发重试 / 目标已删除**未单独构造；源副本缺失与失效引用已有结构化拒绝用例。
5. **CI 已知 flake（非本轮改动）**：\`backend (windows-latest)\` 上
   \`test_interactive_during_heavy_work.py::test_health_probe_stays_responsive_while_slow_prediction_runs\`
   实测 121 ms（阈值 100 ms）；本轮未改该文件，本机带抢核进程连跑全绿，同 runner 曾出现 1191 ms 离群值
   → 判为共享 runner 负载敏感。**不改阈值、不 skip**，如实记为已知 flake。
6. 本报告全部证据来自本机假厂商；**真实厂商行为未验**（也不在验收范围）。

## 6. 已实现 / 已验证 / 未验证

**已实现（本轮三方改动，D 只验证不改）**
1. 接收端在等待网络数据时同时观察作业终态/取消，工作线程失败或用户删除后无需客户端再发字节即可收尾；
2. 内容角色协议：\`[[QIO:ANSWER]]\` 声明 → 真流式进回答区（声明不展示）；未声明 → 有界缓冲降级（一次性交付）；
   迟到工具不执行 + 可见警告；\`tools=[]\` 仅作兜底；
3. \`bind_for_turn\` 异步化：校验/建行在事件循环线程、文件 I/O 在工作线程、定稿回事件循环线程。

**已验证**：§2 全部闸门 + §3 表中标「已验证」的条目 + §1 实机 15/15。

**未验证**：§5 第 1–4、6 条（HTTP 提前响应边界、实机强制 \`os.link\` 失败、代码块/表格、复制途中失败/取消/并发重试/目标已删除、真实厂商）。

**测试数字**：见 §2 表格（本机在同一棵树上实跑）。
