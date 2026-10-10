# 独立验证报告 · 阶段二：R1—R7 修复复验与实机取证（fb-E）

- 计划：docs/plans/2026-10-10-final-boundaries-r1-r7.md（冻结契约 K1—K3）
- 验证者：fb-e（复用成员 acc-f2；只新增验证资产，不改产品实现）
- worktree / 分支：D:\qio-dev\qio-acc-f @ wt/fb-e
- 本阶段起点：wt/fb-e @ 0471d87（阶段一反例）→ merge 集成分支 **4454f10**（本地 merge 提交 **b9acdd1**）
- 环境：Windows / PowerShell；后端 uv run --frozen（Python 3.11 / pytest 9.1.1）；前端 vitest 4.1.10（jsdom）+ vue-tsc；实机 msedge 无头 + Playwright
- 原始输出：docs/verification-shots-fb/phase2-raw/*.txt；实机截图与 summary.json：docs/verification-shots-fb/

## 一、结论速览（R1—R7 旧基线红 → 合并 4454f10 后绿）

| 项 | 旧基线（9e53736） | 修复后（b9acdd1 = 4454f10） | 证据 |
| --- | --- | --- | --- |
| R1 旧定位任务破坏最新定位 | 红：最新定位被同名 .part 打成 failed、元数据指向 C 而磁盘仍是 A | **绿**（2 passed，含串行定位守卫） | backend/tests/test_fb_e_r1_relocate_part_window.py |
| R2 集合提交缺整组复核 | 红：已删除成员仍被回执报成 bound | **绿**（2 passed，含无交错守卫） | backend/tests/test_fb_e_r2_set_release_delete.py |
| R3① 准备中移除被旧 poll 复活 | 红：移除后 chip 被 poll 结果加回 | **绿**（2 passed：poll 用例 + 恢复在途用例） | frontend/src/components/__tests__/fb_e_r3_poll_removed.test.ts |
| R3② 恢复写回复活已删 ID | 红：服务整表回写把已删 ID 写回持久化 | **绿**（1 passed，按 K1.6 生产契约对齐后） | frontend/src/services/__tests__/fb_e_r3_restore_tombstone.test.ts |
| R4 选择器等待期丢发起话题 | 红：prepareAttachment 收到 topicId=B（期望 A） | **绿** | frontend/src/components/__tests__/fb_e_r4_picker_topic.test.ts |
| R5 历史注记丢失 | 红：raw.annotation 形态刷新后注记消失 | **绿**（2 passed，含 legacy 内联守卫） | frontend/src/stores/__tests__/fb_e_r5_history_annotation.test.ts |
| R6 回答身份仍按文字相似度 | 红：同身份 12→13 出两条、长→短被忽略（4 条） | **绿**（4 passed） | frontend/src/stores/__tests__/fb_e_r6_answer_identity.test.ts |
| R7 活动取消给死按钮 resend | 红：actions=[resend] 且 resend→409（已知限制复核） | **绿**（2 passed：actions==[retry]、动作可用性） | backend/tests/test_fb_e_r7_cancel_action.py |

**复跑命令与结果（本轮实测）**

- 后端：cd backend; uv run --frozen pytest -q -p no:warnings --tb=short tests/test_fb_e_r1_*.py tests/test_fb_e_r2_*.py tests/test_fb_e_r7_*.py tests/test_fb_e_combo_boundaries.py
  → **8 passed**，退出码 0。
- 前端：cd frontend; npx vitest run <fb_e_r3①,fb_e_r3②,fb_e_r4,fb_e_r5,fb_e_r6,fb_e_combo_topic_ops,fb_e_combo_history_answer>
  → **Test Files 7 passed (7)；Tests 13 passed (13)**，退出码 0。

## 二、逐项说明（修复后验的是「用户可见不变量」）

- **R1**：最新一次定位必须收敛到 ready，磁盘字节必须等于最新来源、sha256 对应它，且不留 .part；旧的在飞任务不得让新定位失败。合并后三条断言全绿（另有串行定位守卫，证明断言可达）。
- **R2**：集合回执里的每个 ID 在收敛时刻都必须仍然存在且归属本轮；成员在等待期间被删除时不得以「成功」收尾。合并后绿（另有「无删除交错 → 整组成功」守卫）。
- **R3①**：移除必须进 tombstone；任何在途轮询结果不得把它 upsert 回来（列表与持久化都不得复活）。
- **R3②**：**按 K1.6 生产契约对齐**（见 §七），断言「tombstone 候选连核对请求都不发」「补丁模式服务不写持久化」「Composer 合并不得复活恢复期间被移除的条目」，全部绿。
- **R4**：发起时刻的 topic 必须被捕获；选择器返回后不得再读 currentTopicId 决定归属；结果只落发起话题 A，不进 B。
- **R5**：历史转换必须恢复 raw.annotation（字段优先、legacy 内联兜底），系统事实区域独立渲染、注记与正文各恰好一次。
- **R6**：有 answer_id 只更新该身份那条、不新建；无 answer_id 校准该 turn 最后一条已发布的正式回答；缩短/非前缀改写必须生效；禁止全文/前缀比较。
- **R7**：活动取消（user_stopped）动作表只给 retry；「动作表列出的动作必须真的可用」——合并后 resend 不再出现，retry 语义由前端发送接口创建新轮（后端 /api/turns 接受 retry_of_turn_id）。

## 三、跨模块组合（本轮新增）

| 组合 | 文件 | 场景 | 结果 |
| --- | --- | --- | --- |
| R1×R2 | backend/tests/test_fb_e_combo_boundaries.py | 集合提交（a 绑定 + b 重试克隆被闸门卡住）与对 b 的重新定位同时在途，期间删除 a；两个 I/O 闸门放行后各自收敛 | **2 passed** |
| R7 | 同上 | 活动取消 → actions==[retry] → 用 retry_of_turn_id 经既有发送接口创建新轮并跑完 → resend 对 cancelled 行仍 409（准入不变，只是不再展示） | 含在上面的 2 passed 内 |
| R3×R4 | frontend/src/components/__tests__/fb_e_combo_topic_ops.test.ts | A 的路径选择在途 → 切到 B → B 新增并移除附件 → A 的选择结果只落 A、不进 B 的 UI → B 的恢复补丁（含已移除 id）被移除水位拦下 | **1 passed** |
| R5×R6 | frontend/src/stores/__tests__/fb_e_combo_history_answer.test.ts | 同身份校准 12→13 + 独立注记：一条回答、正文 13、注记与正文各恰好一次；历史 raw.annotation 恢复后仍渲染系统事实区域 | **2 passed** |

## 四、实机取证（Playwright + msedge 无头 + 假厂商 SSE）

命令：powershell -ExecutionPolicy Bypass -File scripts/verify-fb-phase2.ps1（ProviderPort 8809；干净 data dir 与干净端口后运行）

结果：**7 / 7 PASS**（summary.json 的 results 全部 ok=true），截图 7 张：

| 场景 | 结论 | 截图 |
| --- | --- | --- |
| S1 活动取消后的按钮 | UI 给「重试」按钮 1 个、resend 按钮 0 个；点「重试」真的跑出新的一轮（文本命中） | fb-01-cancelled-retry-action.png、fb-02-after-retry-click.png |
| S2 附件移除后不复活 | 移除后列表与刷新后都不再出现（持久化不含它） | fb-03-attachment-removed-persisted.png |
| S3 注记历史恢复 | 刷新后系统事实区域仍在（含 read_attachment 失败原因），正文恰好一次 | fb-04-annotation-after-reload.png |
| S4 校准后唯一正文 | 一轮结束后正式回答正文在页面上恰好一份 | fb-05-single-answer.png |
| S5 窄窗口与代码块 | 420px 宽无横向溢出（overflowPx=0），代码块内部自行滚动 | fb-06-wide-1440.png、fb-07-narrow-420.png |

装置说明（避免把装置问题当产品问题）：实机脚本对每个场景使用**本次运行唯一**的标记串，因为话题历史会累积，同名文本会让 waitFor/计数误判；S1 特意**不等空闲**（必须在运行中取消），并在 UI 停止按钮不可用时以 API 取消兜底；S2/S5 改为等待目标元素真正出现/消失；S3 改用不需要审批的必失败工具 read_attachment（缺 id），此前用 fs_read 会触发审批等待超时，属于装置选择问题。

## 五、全量回归与文档（本轮实测）

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量（第 1 次） | cd backend; uv run --frozen pytest -q -p no:warnings --tb=short | 退出码 1：2 failed —— tests/test_attachment_upload_convergence.py::test_request_cancellation_converges（残留 .part）、::test_shutdown_during_commit_leaves_no_orphan_copy（残留副本） |
| 后端该文件单跑（连续 2 次） | uv run --frozen pytest -q tests/test_attachment_upload_convergence.py | 两次都 **19 passed / 退出码 0** |
| 后端全量（第 2 次） | 同上全量命令 | **退出码 0**（无 FAILED） |
| 前端类型检查 | npx vue-tsc --noEmit | **退出码 0** |
| 前端全量 | npm test | **Test Files 170 passed (170)；Tests 1373 passed (1373)**，退出码 0 |
| 文档一致性 | python scripts/check_docs.py | 「文档一致性检查通过（35 个里程碑条目）」，退出码 0 |

**关于第 1 次全量的两条失败（如实登记，未自行修改产品）**：只在同一次全量运行中出现，单跑连续两次绿、第二次全量也绿 —— 判断为**负载/时序敏感**，不是稳定回归。两条失败留下的都是 fb-a 的 R1 修复引入的**带随机后缀的 .part / 已提交副本**（例：att_....36b6345d.part），说明上传取消与「提交竞态」清理路径在特定交织下可能漏清 — 已在汇报里附最小证据点交 Lead 裁定是否加固（我不改产品实现）。

## 六、未实测项（明确边界）

- 真实厂商端点（OpenAI / Anthropic 实网行为、网络抖动、鉴权失败矩阵）：**未测**；全部用本机假厂商（真 HTTP + 真 SSE）。
- Windows / Tauri 原生壳：原生文件选择器、拖入路径、原生中止提示、安装包：**未测**（实机只用 msedge 无头浏览器）。
- 真实浏览器矩阵（Chrome / Firefox / Safari / 移动端）与多显示器 / 高 DPI 缩放：**未测**（单机 msedge 无头 1440×900 与 420×820）。
- 长时间运行 / 大文件（接近 100MB 副本阈值）在实机页面上的表现：**未测**。
- 互动板（interactive）等其它开发线：按计划不在本轮范围，未触碰。

## 七、为验证新增/调整的文件（只动验证资产）

新增：

- backend/tests/test_fb_e_combo_boundaries.py（跨模块组合 R1×R2 + R7）
- frontend/src/components/__tests__/fb_e_combo_topic_ops.test.ts（R3×R4）
- frontend/src/stores/__tests__/fb_e_combo_history_answer.test.ts（R5×R6）
- scripts/verify-fb-phase2.ps1、scripts/verify-fb-phase2-shots.mjs
- docs/verification-shots-fb/**（截图、summary.json、phase2-raw 原始输出）

调整（阶段二按契约对齐，非放宽）：

- frontend/src/components/__tests__/fb_e_r3_poll_removed.test.ts：在原「poll 在途移除」之外，新增一条 Composer 级「恢复在途移除」断言（恢复补丁仍带着被移除 id 时必须被移除水位拦下）。
- frontend/src/services/__tests__/fb_e_r3_restore_tombstone.test.ts：**由旧调用形状改为 K1.6 生产契约形状** —— 生产路径是 patch 模式（服务不写持久化、由 Composer 合并），已移除候选连核对请求都不发。旧断言测的是 legacy 兼容分支，在新契约下不再代表生产行为；改用例后覆盖的仍是同一个用户可见不变量（已移除的附件绝不能被恢复结果复活），且新增「补丁原样回显 revision」「服务不写持久化」两条更严格的断言。

关于 R6 与既有用例的冲突：frontend/src/stores/__tests__/acc_c_final_answer.test.ts 中「同一 turn 内真正不同的多条回答仍然保留」编码的是**旧契约**，与 K2.2 直接冲突；该文件归 fb-c，已由 fb-c 按 K2 更新 —— 本报告中不作为回归看待（已核对合并后该文件通过）。

## 八、结论

R1—R7 全部：旧基线红 → 合并 4454f10 后绿；跨模块组合（R1×R2、R3×R4、R5×R6、R7）全绿；实机取证 7/7；我的全量复跑：后端第 2 次退出码 0、前端 170 文件 / 1373 用例全通过、vue-tsc 退出码 0、check_docs 通过。唯一需 Lead 裁定的是第 1 次全量出现的两条负载/时序敏感失败（证据与最小现象见 §五）。
