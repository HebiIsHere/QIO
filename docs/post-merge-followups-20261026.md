# 合并后遗留问题修复（P91）— 2026-10-26

> 修复分支：fix/post-merge-followups-20261026（未合入 main、未发布）。
> 基线：origin/fix/post-merge-integration-cleanup-20261025 = 373fda40bf904ea0c715d8560fda71c58a6bc051。
> 状态：进行中（收口时回填结论、SHA、CI 运行号）。

## 0. 给用户的说明（非技术）

**修复了什么（用户可见）**

1. **切了话题再确认，续作不会跑错话题**：确认、恢复、重试会使用那次提交原本保存的话题；读不到原话题时明确提示原因（例如「原提交话题无法确认，未开始执行」），不会悄悄用你当前切到的话题顶替，也不会重复执行已完成的操作。
2. **旧审批恢复后不再走错接口**：旧格式审批会明确标成「旧审批」，继续用旧的 `/respond` 回答；恢复出来的旧审批只读显示「未执行/中断待核对」，不会被当成新审批去 `/decide`。
3. **预算修改与实际执行一致**：只有真正支持改预算的旧路径（子智能体创建等）可以编辑，并且执行用你批准的值；不支持修改的记录只读展示；后端收到不支持的修改会明确拒绝，不会出现「界面能改、执行却用旧值」。
4. **批准之后能看见真实结果**：旧 `/respond` 路径现在也会在工具真实执行完后记录「已执行成功/执行失败」并写入历史；「已批准」与「已执行成功」严格分开。断线重连、页面回到前台时会主动补读审批状态与历史，按审批 id 合并不串项，执行不再永远停在「执行中」；读取失败会显示原因并可重试。
5. **子任务通知不再丢**：超过 200 条不再静默丢弃最早的通知；通知先持久化，投递成功才记为已送达，失败会保留并可重试，重启后继续投递未送达的通知；同一任务不会重复通知（跨进程崩溃的极小窗口仍是至少一次，见边界）。
6. **文档与许可证概括更正**：上一阶段的「进行中」结论改为已完成并区分历史阶段；许可证概括按实际第三方组件更正（含弱 copyleft 的 MPL-2.0 与作为可选项的 LGPL 等）。

**怎么验证的**：每一项都先在修复前写出会失败的回归用例（基线红），修复后转绿；再做定向测试、独立验收（F）、一次本地全量、一次完整 CI；真实界面补了 480 窄窗与断线重连等证据（见 §4）。

**仍存在/未验证**：普通聊天续作仍回退当前话题（下一轮）；v2 审批结果路径未改；通知跨崩溃仍是「至少一次」；真实桌面壳、安装形态、真实模型链路、POSIX 本轮没有新证据（沿用上一轮证据并如实标注）；迁移只进不退。详见 §6。

## 1. 基线与分支

| 项目 | 值 |
| --- | --- |
| 仓库 | HebiIsHere/QIO |
| 开工 fetch 的上游分支 | fix/post-merge-integration-cleanup-20261025 = 373fda40 |
| origin/main | 1b03541e（未前进；本轮不碰 main） |
| 修复分支 | fix/post-merge-followups-20261026 |
| 受测代码 SHA | 8cb18828（集成分支 tip；本地全量/完整 CI 的受测 SHA） |
| 最终 HEAD | 本文件所在提交（docs-only 回填；可用 git rev-parse HEAD 核对，与受测 SHA 的差异仅为 docs） |

## 2. 工作流与文件 owner

| 工作流 | 分支 | worktree | 主要交付 |
| --- | --- | --- | --- |
| A 话题归属 | fix/fu2-topic-attribution | D:\qio-dev\fixq2-a | 确认/恢复/重试原话题优先、无法确认明确失败 |
| B 审批后端 | fix/fu2-approval-backend | D:\qio-dev\fixq2-b | legacy 身份、/respond 路由、执行结果持久化历史、overrides 语义 |
| C 审批前端 | fix/fu2-approval-frontend | D:\qio-dev\fixq2-c | 重连补读合并、结果闭环展示、预算只读/可改一致 |
| D 通知投递 | fix/fu2-notification-delivery | D:\qio-dev\fixq2-d | 持久化队列、可靠投递、重启恢复、去掉 200 静默丢弃 |
| E 文档许可 | docs/fu2-docs-license | D:\qio-dev\fixq2-e | 阶段状态更正、P91 记录、许可证概括、证据 |
| F 独立验收 | verify/fu2-acceptance | D:\qio-dev\fixq2-f | 基线反例 + 候选独立验收 |
| Lead | fix/post-merge-followups-20261026 | D:\qio-dev\fixq2-lead | 契约冻结、集成、全量、CI、本文档 |

## 3. 五项修复逐项

### 3.1 互动续作的话题归属

**问题**：确认/恢复/重试启动续作 Turn 时，`_start_intent_turn` 把界面当前选中话题传给 `start_interactive_turn`，而后者只在 `topic_id` 为空时才读原提交绑定 → 切换话题后确认会把续作写进新话题；没有绑定时还会用当前话题兜底起 Turn。
**反例（基线红）**：新回归 17 例中 13 红——原提交 `topicId=topic-saved` 时直接调用与真实 POST /confirm 都拿到 `topic-current`；无绑定/topicId 为空时 `started=True`（期望明确失败）。证据 `_fixpm2/evidence/A/red-baseline-topic-attribution.log`。
**修复**：`submission.resolve_resume_topic` 只认 `binding.resumeState.topicId`，缺失/为空/读取失败返回 `code=topic_unresolved`；`start_interactive_turn` 在 claim/提交/写绑定之前强制解析，失败不启动/不写绑定/不回退当前话题；API 不再传当前话题并补可读 code/reason；前端 `intentDisplay.summarizeConfirm` 消费 `started=false/startError/turnStart.reason` 显示真实原因。
**调用点核对**：startup_recovery 与 turn_orchestrator 两处都能从意图拿到带 topicId 的 submission；无 binding 时只 release_retry/skipped（最多 3 次 exhausted，无死循环）、不崩溃、不起 Turn、不重复执行（已补 4 条测试，2 条基线红）。
**验证（绿）**：新用例 17 passed；相邻 11 文件 147 passed；相关端点 123 passed/6 skipped；既有 `test_p3_d_intent_resume_retry.py` 仅改 1 条过期断言（调用方话题 → 原提交话题），未放宽。
**未解决**：`resolve_chat_binding` 的普通聊天续作仍回退 current_topic（超范围，后续事项）；历史「succeeded 但绑定缺失」数据自动重试会显式失败（不写错话题、不重复执行）。
### 3.2 旧审批恢复与预算

**问题**：旧 `request()` 审批落库行 state 为空，读取时 `state_of` 把空 state 按 status 映射（pending→waiting），恢复又把旧行标成 `interrupted_unverified`；旧审批因此被当成 v2 生命周期、前端误调 `/decide`。旧 `/respond` 只把决定交给等待中的工具，工具真实执行后没有任何地方写结果——已批准升不到已执行成功/失败。不支持 overrides 的路径收到 overrides 被静默忽略。
**反例（基线红）**：F 验收 3 红（无 legacy 标识/state 伪造 waiting、/decide 错误语义、恢复后 status 非独立事实）；预算 2 红（不支持 overrides 的旧路径与 v2 经 /respond 都静默接受）。B 红证据 12 failed/3 passed。
**修复（后端）**：`row_to_dict` 对 legacy 输出 `legacy=true/state=null/status` 原值；恢复落 `status=interrupted_legacy`、state 保持 NULL，保留 turn/session/digest/created/expires；`/decide` 对 legacy 409 `legacy_approval_use_respond`，已中断 legacy 再 /respond 409 `legacy_approval_not_executable`，已消费仍 404；`tools/approval_results.py` 在 ToolRegistry 执行边界用 ContextVar 统一收口 legacy 真实执行结果（history executed/failed + APPROVAL_RESULT executed/failed，不重复执行），维护审批与护栏/预算「继续/停止」分别按真实执行/决定事实收口（后者不写 executed）。预算：`overrides_supported` 顶层布尔，仅 tool_create+subagent 与 tool_execution+code_boundary 端到端支持，其余带 overrides 400 `overrides_unsupported` 且不消费。
**修复（前端）**：legacy 只走 /respond、不补 state；中断 legacy 只读；结果面板区分旧审批 approved 与 executed/failed；预算可编辑 ⇔ 旧 tool_create 子智能体且非 v2 且 overridesSupported!==false，v2 只读且绝不带 overrides；后端 400 显示真实原因并转只读。
**验证**：B 新回归 19 passed、两批定向 384 passed；C 红 17 failed/5 passed → 绿（FU2-C 40、审批相关 272、更大定向 233、vue-tsc 0）；集成分支复跑 B 审批 33 passed。
**未解决**：v2 store 生命周期结果仍走原 store.finish（未改）；legacy approved 但工具未执行完时历史停在 approved（界面明说等待执行，不伪造成功）。
### 3.3 审批结果闭环

**问题**：SSE 断线/RESYNC/回前台时前端只恢复运行时快照（`restoreRuntimeState`），从不补读审批 pending/history；断线期间审批已完成（executed/failed），本机记录仍停在「执行中」；历史读取失败没有可见错误；旧审批 approved 与 executed 也没有区分展示。
**反例（基线红）**：F 验收 3 红（restoreRuntimeState 一次都没调用 getApprovalHistory/getPendingApprovals；断线前 executing 重连后仍 executing；读失败无可见错误）。C 红 17 failed/5 passed。
**修复**：唯一入口 `approvals.refreshApprovalsAndHistory`，在 SSE onopen（首次+重连）、RESYNC 完成、窗口回前台触发；按 approval_id 合并——pending 等待项保留，history/事件终态收口本机 approved/executing；不新增重复行、不串项、不因补读重复提交；pending/history 读取失败分别可见且可重试，不伪装空集合；结果面板区分旧审批 approved（等待执行）与 executed/failed，中断 legacy 只读展示。
**验证**：C 新用例转绿（FU2-C 40）；审批相关定向 272 passed；更大定向 233 passed；`vue-tsc --noEmit` exit 0；集成分支上 B 契约联调通过。证据 `_fixpm2/evidence/C/green-*.log`、`final-*.log`。
**未解决**：跨崩溃 exactly-once 不在本轮（通知侧见 §3.4）；前端只依赖后端字段，无独立状态机。
### 3.4 子任务通知容量与可靠投递

**问题**：`SubtaskNotificationQueue` 是纯内存 deque，超过 200 条时 `popleft` 静默丢弃最早通知；`enqueue` 即调 `TaskManager.mark_notified`（把入队当交付成功）；重启后未送达通知全丢，take 后崩溃也丢。
**反例（基线红）**：D 新回归 22 例中 21 failed（250 条入队后 pending_count=200、最早 50 条被丢弃、入队即 mark_notified、无迁移/表、重启为空）；F 独立复现 4 红。证据 `_fixpm2/evidence/D/red-baseline.txt`。
**修复**：只追加迁移 71 建 `subtask_notifications(task_id PK, payload, origin, created_at, delivered_at, attempts, last_error, last_attempt_at)` + 待投递索引；72 为补偿入口（70 全部语句 + 71 DDL + 4 个可修复列 ADD COLUMN），`SCHEMA_VERSION = COMPENSATION_VERSION = 72`，`REQUIRED_OBJECTS` 同步表/列/索引。队列改 DB 模式：`INSERT OR IGNORE` 幂等去重、>200 只告警不丢弃、`take` 记 attempts/last_attempt_at、**只有 `mark_delivered` 写 delivered_at（唯一交付判据）**、`mark_failed` 保留 last_error 待重试、重启加载未送达。`app.py`：pump 在 `turns.submit` 受理成功后才 mark_delivered，失败 mark_failed + 延迟重试（10s），`resume_subtask_notifications`/启动 prime 补投；投递用确定性 `request_id=subtask-notify:<task_id>` + `lookup_request` 查重。
**验证**：新回归 22 passed；通知/子任务/迁移/应用定向 141 passed；5 个未显式迁移的 AppContext 测试 35 passed；既有迁移升级测试按授权把 `==70` 改为 `== SCHEMA_VERSION` 后 17 passed（未跳过/放宽/删用例）。F 调整后的独立通知验收 6 passed（250 条不丢、失败保留可重试、重启续投不重投、跨重启去重）。
**未解决/边界（如实）**：跨崩溃仍为 at-least-once——`turns.submit` 与写 `delivered_at` 无共享事务，若 submit 已受理、写交付前进程被杀，重启可能重投一次（同进程内 request_id 查重已压掉；正常失败重试路径不重复）；延迟重试为单次 10s 调度，无指数退避/最大次数；启动 prime 在 `AppContext.__init__` 末尾触发。
### 3.5 文档与许可证

**问题**：`docs/post-merge-integration-cleanup-20261025.md` 仍写「状态：进行中」；`docs/compliance.md` 把依赖概括为「全是 MIT/Apache/BSD/ISC/OFL 宽松许可、没有 copyleft 传染项」，与实际清单不符。
**修复**：旧文档第 5 行改为 P90 阶段已完成并新增 §12 P91 阶段（历史阶段与当前状态分开）；`docs/status.md` 新增 2026-10-26 最后核对行与 P91 里程碑（partial → 收口回填）；`docs/compliance.md` 按实际组件更正：含 MPL-2.0（7 条，文件级弱 copyleft，含 MPL-2.0 AND MIT）、LGPL-2.1-or-later 仅作 MIT/Apache-2.0 并列可选项（2 条 r-efi），其余 MIT/Apache/BSD/ISC/Zlib/Unlicense/CC0/MIT-0/0BSD/OFL/PSF/Unicode-3.0/BlueOak/CDLA/LLVM-exception 等，未发现 GPL/AGPL 单项；随包资产按实际 4 条（CPython 运行时、python-build-standalone、bge-small-zh-v1.5、Noto Serif SC）更正；补充「未改依赖不重新生成声明」与「生成输入 sha256 受换行符影响、CRLF 检出重跑只会改 3 行哈希、不得提交」的边界（该陷阱本轮已复现并撤销，未提交假变更）。
**验证**：`scripts/licenses/check_third_party.py` 通过（锁文件组件 914/声明 918/许可证 42 种，`--selftest` 7/7）；`scripts/check_docs.py` 通过（100 个里程碑，P91 解析成功）；未改依赖/锁文件/生成物。
**边界**：本轮无新的真机桌面/安装形态/真实模型证据（沿用 P90 证据并按未验证列出，见 §6）。

## 4. 验证台账

### 4.1 反例（基线红，先复现再修复）

| 项 | 基线 373fda40 红证据 | 修复后 |
| --- | --- | --- |
| 话题归属 | A：新回归 17 例中 13 红（原提交话题被当前话题覆盖；无绑定仍起 Turn）；F 独立 3 红（直接调用 + 真实 POST /confirm） | 全绿 |
| 旧审批恢复 | B：12 failed/3 passed（legacy 无标识、state 伪造 waiting、/decide 语义错、恢复 status 非独立事实）；F 独立 3 红 | 全绿 |
| 预算 overrides | F 独立 2 红（不支持 overrides 的旧路径与 v2 经 /respond 静默接受）；B 红含 overrides 用例 | 全绿 |
| 审批结果闭环 | C：4 文件 17 failed/5 passed（restoreRuntimeState 不补读、断线后仍 executing、读失败无错误）；F 独立 3 红 | 全绿 |
| 通知容量/投递 | D：22 例 21 failed（250 条丢最早 50、入队即 mark_notified、无迁移/表、重启丢失）；F 独立 4 红 | 全绿 |

证据目录：`_fixpm2/evidence/{A,B,C,D,F}/`（red-* 日志；F 的 `baseline-repro.md` 与 `baseline/item*.log`）。
### 4.2 定向验证（开发期，非全量）

| 工作流 | 定向范围 | 结果 |
| --- | --- | --- |
| A 话题归属 | 新回归 + 相邻 11 文件 + 相关端点 11 文件 | 17 passed；147 passed；123 passed/6 skipped |
| B 审批后端 | 新回归 + 审批/registry 18 文件 + loop/维护 11 文件 + 合并两批 | 19 passed；185 passed；86 passed；384 passed |
| C 审批前端 | FU2-C 文件 + 审批相关 + 更大定向 + vue-tsc | 40 passed；272 passed；233 passed；tsc 0 |
| D 通知投递 | 新测试 + 子任务/通知/迁移/应用定向 + 无显式迁移的 AppContext 测试 | 22 passed；141 passed；35 passed；迁移升级 17 passed |
| E 文档许可 | check_docs.py/--selftest、licenses check/--selftest | 通过（100 里程碑；914/918/42 种；selftest 7/7） |
| F 独立验收 | 基线五项反例 + 候选复跑（反例/保真/真实界面） | 基线 15 红断言/4 保真通过 → 候选：反例 21/21 绿、保真 6/6、回归 101+81 passed、vue-tsc 0、480 真实界面 4/4（见 §4.5 与 ACCEPTANCE.md） |
| Lead 集成 | 每次合并后定向 + 集成后 A/D 回归、B 审批、vue-tsc/vite build | 全部通过（日志 `_fixpm2/evidence/lead-*`） |
### 4.3 本地全量（仅一次）

- 候选：**8cb18828**（集成分支 tip；F 的验收测试在 8cb18828 之上以独立文件合入，产品代码不变，最终树 c2a47f48）。
- **后端全量**：`cd backend && uv run --frozen pytest -q`（601 个测试文件、收集 6013 项；在后端树 744546b7 上跑，该树在 8cb18828→c2a47f48 之间产品代码未变；F 的 5 个验收测试文件在其后合入，另行定向跑通见下）→ **exit 0**（无 failed/error；日志尾部为既有 ResourceWarning/PytestUnraisableException 噪音；`_fixpm2/evidence/lead-full-backend-e030e570.log`）。
- **前端全量**：`cd frontend && npx vitest run --maxWorkers=4` → **452 files / 4025 tests 全部通过（exit 0）**，无失败/跳过；`npx vue-tsc --noEmit` exit 0；`npx vite build` 成功（9.07s，dist 未提交）。
- **文档/许可/门禁**：`check_docs.py`（100 里程碑）与 `--selftest`、`check_third_party.py`（914/918/42 种）与 `--selftest`、`release_gate.py --selftest`、`verify_workflow_contract.py`（113 断言）全部通过。
- **离线评估**：`uv run --frozen python -m agent.eval.run` 在候选与基线 373fda40 各跑一次，输出逐字节相同（`_fixpm2/evidence/eval-*.json`）。
- **F 验收测试在最终树**：backend 5 文件（话题/旧审批/预算/保真/通知）20 passed；frontend `fu2-acceptance` 4 passed。
### 4.4 完整 CI（仅一次）与定向复验
<收口回填：运行号、SHA、逐 job>
### 4.5 真实界面/桌面证据

- **F 独立真实界面（480x600，隔离后端 8842 + vite 5292 + 独立数据目录 + fake/mock 模型）**：4/4 PASS、consoleErrors=0、无横向溢出：0 待办+历史可从 ⑤ 读到真实持久历史；reload/新 SSE connect 后仍可回看（重连补读）；「已批准」显示等待执行（无预算编辑、无“可修改后批准”、无允许按钮、不说已完成）；对已不存在的审批点击决定时显示真实 404 原因、不假装成功。证据 `_fixpm2/evidence/F/ui/`（4 张截图 + JSON）。
- **C 真实后端契约探针**（端口 8899、临时数据目录）：pending/history/runtime 形状与前端一致；legacy /decide → 409 `legacy_approval_use_respond`；/decide + overrides → 400 `overrides_unsupported`；已中断 legacy /respond → 409 `legacy_approval_not_executable`。
- **真机桌面/安装形态**：本轮未重新构建桌面壳、未打包、未做安装/卸载验证；真机桌面证据仍为 P90 的 `_fixpm/evidence/D/f07`（本轮未改壳侧代码），按未验证列出。真实模型链路未验证（全部 fake/mock）。
- **LIMITED（F 记录）**：通知的 AppContext pump/启动补投/request_id 查重只做代码核对 + 队列级真实状态机验证，未构造完整 AppContext 端到端；界面“断线重连”用 reload/新 connect 模拟、“决定失败”用 404 注入，未做真实网络故障注入；跨崩溃 at-least-once 窗口按 D 说明保留。

## 5. 保留机制与保护清单

- 三次确认（required/confirmed_stage 由后端推导与 CAS）、期限（expires_at 不续期）、CAS（revision/expected_revision 不变）、单次执行（claim CAS 只有一个执行者）、执行前重核（operation_digest/预检查）：本轮未改语义，B 的旧路径收尾只写结果不重跑工具；既有 approval/lifecycle/restart 测试全绿。
- 旧 `/respond` 接口保留并继续服务旧 request 审批（绑定 turn/session/digest 校验、单次消费不变）；旧 `/demo/advance` 与 pendingDecision 撤回路径未动。
- 历史数据库迁移 1..70 未改写；只追加 71（通知表）与 72（补偿入口），空库/旧版本 70 库升级、对象缺失补偿均有测试。
- 未恢复已删除的旧界面/旧组件；未改版本号与三套锁文件；旧审批 UI 仍只走统一审批体系。
- 已删除的旧界面不复活；`sdfRings`、测试 support/harness、归档模块 `archive.py` 未动。

## 6. 未解决与未验证

- **话题归属**：`submission.resolve_chat_binding` 里普通聊天续作仍会回退 current_topic（本轮只修确认/恢复/重试/启动恢复/编排自动重试的续作 Turn）；历史「succeeded 但绑定写入缺失」的数据其自动重试会显式失败（不写错话题、不重复执行）。
- **审批**：v2 store 生命周期结果仍走原 `store.finish` 路径（本轮未改）；旧审批 approved 但工具未执行完时历史停在 approved（界面明说等待执行，不伪造成功）；旧 `request()` 内存在进程重启后无法再应答（恢复为 interrupted_legacy 只读），这是既有语义。
- **通知**：跨崩溃仍是 at-least-once（submit 与写 delivered_at 无共享事务，可能在极小窗口重投一次；同进程内 request_id 查重已压掉，正常失败重试不重复）；延迟重试为单次 10s，无指数退避/最大次数；未构造完整 AppContext 的端到端 pump 重试验收（F 按代码核对 + 单测覆盖，列 LIMITED）。
- **真实桌面 / 安装形态 / 真实模型 / POSIX**：本轮未新增证据。真机桌面与安装 e2e 沿用 P90 证据（见 docs/post-merge-integration-cleanup-20261025.md 与 `_fixpm/evidence/D/f07`），本轮代码未改壳侧；真实模型链路全程 fake/mock；POSIX 仅由 CI 覆盖。
- **UI**：本轮前端改动只做审批结果/重连/预算/确认失败原因，未做全量视觉走查；F 的 480 窄窗与断线重连真实界面证据见 §4.5。

## 7. 合并准备

- **分支状态**：修复分支推送 origin/fix/post-merge-followups-20261026（独立分支，未合入 main、未打包、未发布、未改版本号/锁文件）。
- **本轮唯一一次完整 CI**：由修复分支 push 触发 ci.yml 完整矩阵（含 install e2e、冻结 worker、三平台后端、前端类型/测试/构建、rust 两平台、docs）。若失败：修复提交带 `[skip ci]` 推送（不产生第二次完整矩阵），再用 targeted-verify.yml 指定 ref + 完整 SHA + 平台 + 筛选定向复验；最终 HEAD 若含仅文档回填提交同样 `[skip ci]`，并记录「受测 SHA 与最终 HEAD 的差异仅为 docs」。
- **CI 触发防重复（沿用 P90 规则）**：push 排除 fix/pm-* 与 verify/pm-*；本轮子分支 fix/fu2-*、docs/fu2-*、verify/fu2-* 不 push（只在本地集成），集成分支 push 就是唯一完整矩阵；concurrency 按实际受测提交（PR head 回退 github.sha）；main push 与 PR 门禁未改动。
- **未来合入 main**：PR 运行按 head SHA、main push 按 merge SHA，是两个不同提交、各自一次完整矩阵（concurrency 不会把它们当重复）；若仓库保护要求两者都跑，按「不同 SHA 两次完整运行」如实记录，不冒充去重。减负方案：fast-forward/rebase 合并让 main SHA 等于已验证 head，或 PR 只跑 targeted、main push 作为唯一完整门禁。本轮未创建 PR。
- **数据库兼容**：本轮只追加迁移 71/72（SCHEMA_VERSION=COMPENSATION_VERSION=72），空库、版本 70 旧库升级、对象缺失补偿均有测试；迁移只进不退，代码回退不会自动降库，恢复旧备份前须确认不会覆盖升级后新增数据。未改写任何历史迁移。
- **合并材料**：本文档（五项逐项、反例/修复/验证、保留机制、未解决边界）+ 各工作流证据（`_fixpm2/evidence/{A..F}`）+ 分支提交历史（merge --no-ff 保留工作流边界）。
