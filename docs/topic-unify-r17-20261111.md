# R17 · 话题系统复核补修交付记录（2026-11-11 · 第六轮）

> 本文件记录本轮**实际做了什么、证据在哪、边界是什么**，数据均来自本轮实际命令，不是推断。
> 唯一共享契约是 `docs/topic-unify-r17-contract-20261111.md`（在 R16 契约 `docs/topic-unify-20261111-contract.md`
> 之上只做补修）；结构语义补充见 `docs/architecture.md` §22.9。本轮不合并 main、不打标签、不发布。

## 1. 基线、分支与提交

- 仓库 `HebiIsHere/QIO`；受检分支 `fix/topic-review-20261110`。
- 开工核实（fetch 后）：`origin/fix/topic-review-20261110` = 本地工作树 =
  `929e8d7c12cf5073b32bf832b391df1ea22c2e1a`（ahead/behind 0/0，工作树干净，无后续远端修复）。
- 开发隔离：Lead 在 `D:\qio-dev\qio-r14` 集成；六个角色各有独立 worktree（`r17-a`…`r17-f`），
  均从脚手架提交拉出，共享 `backend/.venv` 与 `frontend/node_modules` junction；原始证据在
  `D:\qio-dev\r17-evidence\<role>\`，不写进工作树。
- 提交序列（相对 929e8d7c，共 15 个实现/测试/脚手架提交 + 本 docs 提交）：

| 提交 | 内容 |
| --- | --- |
| `9728ce34` | 承接 r16 全量失败项的**已有后续修复**（工具导航恢复 related 边 + 快照 nav 版本断言）；复现确认后保留 |
| `cdc075b0` | 冻结 R17 共享契约与 `save_normal_answer` 脚手架接口 |
| `5c430c83` / `73aa5169` | E：双连接并发测试等待顺序修正（合并） |
| `8554b56f` / `860625a6` | D：普通正式回答接入统一答案身份、唯一性与共享暂存收口（合并） |
| `0e1a19fb` / `fbfaf847` | B：取消接续目标身份 + 原子导航真实 HTTP 验收（合并） |
| `a52921a2` / `2c536248` | C：取消响应整份接受 + 一次点击一次导航（合并） |
| `262e4837` / `fb013094` | A：轮次受理依据链 CAS，旧排队轮次不覆盖用户新导航（合并） |
| `d31cd27b` | Lead：集成验收用例（真实 HTTP + fake provider） |
| `4be2028e` / `6fac3197` | B：重发受理也冻结位置依据（F 独立评审发现的 P1-1 同路径） |

- **集成代码受测 SHA：`6fac319707fdd64fc7a4a0471d4b17c135a96645`**（A/B/C/D/E 全部合并、Lead 集成验收测试与 F 发现的 resend 修复合并后）。
  本文件所在 docs 提交之后只改文档，不再改生产代码；最终推送 SHA 以交付回复为准。
- 全量与 CI：**沿用 r16 已启动的台账**；本轮按任务书只做定向验证，**不启动全量、不触发 CI**（见 §6）。

## 2. 四项复核问题的复现与修复

### 2.1 P1-1 旧排队轮次覆盖用户的新导航

- 复现（基线 `929e8d7c`，证据 `D:\qio-dev\r17-evidence\a\baseline_p1_1_repro.log`）：
  ① 轮次 A 排队期间用户手动进入话题 B；A 开始时轮首补锚用临时读取的 current position_version 作
  `expected_position_version`，CAS 通过，把 Anchor 从 B 改回 A 并广播 A 的位置事件；
  ② 离开再返回：旧轮轮末 `advance_anchor` 同样临时读 current 版本，把用户返回的历史位置推进到旧轮片段。
- 修复：`TurnManager.submit/reserve` 新增冻结 `basis`，`TurnContext` 记录受理依据（轮次归属/接续选择/两类
  版本）与 CAS 链 `position_basis_version`；轮首补锚、内部片段落位、轮末推进、边界/容量分段全部以依据链作
  `expected_position_version`，**不再临时读取最新位置版本**；被拒只 trace + 标记 superseded，位置零副作用
  零广播；本回合自己的工具导航/已获批准操作按本回合 applied operation 命令结果因果更新链；无导航干扰时
  既有补锚/自动分段/轮末推进行为不变。
- 补充入口：F 独立评审发现 `POST /api/turns/{turn_id}/resend`（「未执行消息重发」）是同一缺陷路径的
  生产入口——重发轮 reserve 未传 basis，排队/准备期间用户导航后 begin 补锚仍会以实时 position_version 做
  CAS 把位置改回原轮话题。B 已修复：resend 的 reserve 传 `basis=_submission_basis(ctx)`，intent_id 取自
  同一份受理快照（与 /api/turns 同构、同一时刻一次读取）；新增真实 HTTP 回归 `test_r17_b_resend_basis.py`
  （修复前红：最终位置被改回 A；修复后绿：回答归属 A、位置保持 B、导航后无 A 的 ANCHOR）。
- 验证：A 新用例 `test_r17_a_turn_basis.py` 8 passed（A1—A5）；Lead 集成验收
  `test_r17_lead_integration.py` 2 passed（真实 HTTP：A 提交排队 → 手动切 B → A 执行完成；轮次结束前新导航），
  断言原问题/回答归属 A、权威位置仍为 B、切换后不再出现把 A 广播为新位置的 ANCHOR、position_version 不再推进。

### 2.2 P1-2 迟到的取消响应清空较新的接续选择

- 复现（基线）：旧实现前端在检查响应版本之前先 `pendingContinuation = null`，随后才 `acceptPositionState`；
  较新状态（接续 B）已接受后旧取消 A 响应到达，位置仍在 B 而接续提示被清空（F 基线独立用例：迟到响应清 B、
  同版本冲突清 pending、取消请求不带目标身份）。后端旧取消命令不携带目标身份，实测「取消 A 实际取消了 B」
  （B 基线红证据 `00-red-excerpt.txt`）。
- 修复：后端取消命令必填 `pending_intent_id`（+可选版本），统一入口在事务内、任何位置写入之前校验目标，
  不存在/版本不一致 409 `continuation_cancel_target_mismatch`、已失效 409 `continuation_request_inactive`，
  零位置写入零广播；`param_digest` 仅当版本非 None 才计入（旧库摘要不漂移）。前端 `cancelPendingContinuation`
  返回 `{ok, commandSucceeded, stateAdopted, code, error}`，所有带 position 的取消响应先经
  `acceptPositionState(position, "command")`，只有整份接受才更新位置/历史/pending；拒绝时零部分修改并触发
  既有权威恢复；正常取消仍正确清理提示。
- 验证：B `test_r17_b_cancel_atomic.py` 12 passed（含目标不匹配/失效/缺字段/正常取消+重放零副作用）；
  C `r17-c-cancel-position.test.ts` 9 passed（含「取消 A 在飞 → 接受 B → 旧响应到达」断言 B 的位置/接续/草稿/
  版本全部保持、零部分修改、权威恢复恰好一次）；F 独立用例集成绿（见 §5）。

### 2.3 P2-3 「返回当前开放片段 / 进入话题最新位置」需要一次原子导航

- 复现：星球页先发独立取消请求再发导航；第二步失败时用户没有进入目标位置，旧接续已经被清除。
- 修复：一次点击只发一个 `/api/anchor`（前端删除 `ManualNavigationInput.cancelPending` 与
  `_runManualNavigation` 独立取消步骤；用户单独点击取消接续仍走既有取消入口）。后端同一 apply 事务内完成
  命令身份/参数校验、依据 CAS、落位与不再适用 registered 接续清理；失败整份回滚（目标不可用/版本冲突/
  事务中途异常时原位置与原接续一起保留）；成功只一次状态变化一次广播；同 request_id 重放零落位零广播。
- 验证：C `r17-c-planet-nav.test.ts` 6 passed（一次点击只发一个导航命令、cancelContinuation 0 次、失败保留、
  重试复用 request_id）；B 真实 HTTP 五场景（目标不可用/版本冲突/事务中途失败/成功/响应丢失后重试）12 passed，
  断言位置、接续来源、两类版本与广播次数。

### 2.4 P2-4 普通正式回答接入统一答案身份

- 复现：普通轮次成功保存后助手消息 `answer_key` 为空；恢复入口只能按记录身份找答案，普通答案不参与
  统一唯一性；生成成功保存失败时正文没有进入共享暂存。
- 修复：`topic_recovery.save_normal_answer` 与恢复回答共用 `answer_key_for(turn_id, user_message_id)`；
  正式保存事务内先复用既有唯一答案（返回原 answer_id、不新增第二条），否则按普通回答原有语义写一条 assistant
  消息（片段由轮次绑定解析、content_type/model/raw/原轮次保留、带 answer_key），并同事务收口同 answer_key 的
  pending 恢复记录与执行行；保存失败先把正文写入共享暂存（run 行 staged_*）再抛原异常，任意同 key 恢复记录
  之后只补存不调模型；旧库 answer_key 为空的历史答案只在可靠归属（同轮/同原用户消息/同片段同话题/该轮恰好
  一条正式候选/无其它带 key 答案）时关联并同事务补写，歧义不猜；**未新增迁移**（既有迁移 79/83 列已足够）。
- 验证：D `test_r17_d_answer_identity.py` 13 passed（D1—D4 + I11/I12 边界），基线红 10 failed/1 passed；
  Lead 集成验收在真实 HTTP 轮次上断言正式消息 answer_key = `answer_key_for(turn, user_msg)`；F 独立用例覆盖
  重复保存/既有答案恢复/保存失败补存/旧库可靠与歧义归属。

## 3. 双连接测试等待顺序修正

- 旧测试先按 A→B 顺序各等 20s；B 先获胜时主线程空等 20s，随后与假模型 20s 释放窗口竞争（组合运行偶发失败）。
- 修正：共享「任一执行者进入」事件（任一 adapter 首次进入即 set），主线程只等它；执行者阻塞释放放入
  try/finally（断言失败/异常也释放）；分别确定性控制 A 获胜与 B 获胜两种顺序；保留原验收标准（非执行者 202
  `recovery_in_progress`、只生成一次、一份正式答案、两条记录同一 answer_id）。
- 证据：修正前红用例 `20.28s call` 空等 FAILED；修正后单文件 4 passed（两种顺序 call 0.18s/0.14s），
  与 test_r16_e_http/test_r16_e_integration 组合 5 轮均 21 passed、exit 0、无 20s 空等。

## 4. 承接的 r16 全量失败项已有后续修复（复现确认后保留）

- r16 唯一的实际全量批次（attempt2，受测 `929e8d7c`，2026-11-11T10:52:17 启动、11:15:54 结束）结果：exit 1；
  `backend-full`（6707 tests / 4 failed）与 `frontend-tests`（router-aliases 2 failed）失败，其余阶段（前端类型检查/
  构建、文档一致性、第三方声明、发布闸门自检、workflow 契约、frozen worker）exit 0。该结果**只归属 `929e8d7c`**。
- 工作树中已有的后续修复（本轮开工时存在、本次核实后保留为提交 `9728ce34`）：
  ① `backend/src/agent/tools/topic_tools.py`：工具真实 switch/create 成功后恢复既有 related 边（修复
  `test_p7_topic_and_knowledge_flow::test_topic_graph_navigation_flow` 与
  `test_turn_short_term::test_run_turn_injects_short_term_and_switches_topic` 的失败）；
  ② `backend/tests/test_r15_a_r1_snapshot.py`：按 R16 冻结语义（机械落实接续不推进 nav_version）更新断言。
- 本轮复现确认：`test_r15_a_r1_snapshot.py` 7 passed、`test_turn_short_term.py` 4 passed（Lead 后端定向批 1）、
  `test_p7_topic_and_knowledge_flow.py` 3 passed（单独定向），均 exit 0。
- 前端 2 个 router-aliases 失败经单独复跑通过（4537ms，5000ms 上限内；与全量并发负载相关），定性为原批次的
  负载敏感 flake，未在补修中改产品代码，也不把该失败归因于补修提交。

## 5. 本轮定向验证（命令、次数、数字、受测 SHA）

全部命令在集成工作树 `D:\qio-dev\qio-r14`，使用共享 `.venv` / `node_modules`，临时 SQLite / fake provider，
受测代码 SHA = `d31cd27b5846bce2280f336e9fd564fbfe309123`（Lead 集成验收测试提交）。

| # | 命令/范围 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | Lead 集成验收 `tests/test_r17_lead_integration.py`（真实 HTTP + fake provider） | 2 passed，exit 0 | 定向批 1 |
| 2 | Lead 后端定向批 1（12 文件：集成验收/A/B/D/E 新用例 + 位置/回答/快照/话题应用/计时） | 132 passed，exit 0 | `r17-evidence\lead\final2-batch1-core.log` |
| 3 | Lead 后端定向批 2（11 文件：anchor 路由/取消/导航命令/真实 HTTP/参考模型等） | 117 passed，exit 0 | `r17-evidence\lead\final2-batch2-nav.log` |
| 4 | Lead 后端定向批 3（15 文件：恢复/绑定/边界/工具导航/取消语义等相邻回归） | 115 passed，exit 0 | `r17-evidence\lead\final2-batch3-recovery.log` |
| 5 | Lead 前端定向批（16 文件：C 新/适配 spec + 事件/历史/自动恢复/审批/草稿/互动接线） | 171 passed，exit 0 | `r17-evidence\lead\final-frontend-batch.log` |
| 5b | Lead resend 修复定向（5 文件：新增 resend 回归 + 重发/导航相邻回归） | 31 passed，exit 0 | `r17-evidence\lead\final-resend-fix.log` |
| 6 | `npx vue-tsc --noEmit`（全仓） | exit 0，0 错误 | `r17-evidence\lead\final-vue-tsc.log` |
| 7 | `python scripts/licenses/check_third_party.py` | 通过（916 组件 / 920 声明 / 42 许可证） | `r17-evidence\lead\final-third-party.log` |
| 8 | `tests/test_p7_topic_and_knowledge_flow.py`（保留修复复验） | 3 passed，exit 0 | `r17-evidence\lead\final-p7-preserved.log` |
| 9 | F 独立后端用例（PYTHONPATH=集成 src；SHA d31cd27b 时 13 项，SHA 6fac3197 时含 resend 回归共 14 项） | 14 passed（基线红 8 failed/5 passed；新增 resend 回归基线红 1 failed） | `r17-evidence\f\integration-green\backend-f-integration-6fac3197.txt`、`r17-evidence\f\baseline-red\backend-f-baseline-summary.txt`、`r17-evidence\f\baseline-red\resend-baseline-red.txt` |
| 10 | F 独立前端 spec（经批准临时复制到集成工作树运行后清理；SHA 6fac3197） | 7 passed（基线红 5 failed/2 passed） | `r17-evidence\f\integration-green\frontend-f-integration-6fac3197.txt` |
| 11 | F 对 E 双连接用例的独立复跑（集成 SHA d31cd27b；该文件此后未再改动） | 2 轮均 4 passed、exit 0 | `r17-evidence\f\integration-green\e-concurrency-f-verify.txt` |

- 本轮**Lead 最终定向汇总：后端 364 passed / 38 文件（三段）+ resend 修复定向 31 passed / 5 文件（含与前述批次重叠的文件）+ 前端 171 passed / 16 文件，全部 exit 0**；
  F 独立验证在同一受测 SHA 上：后端 14 passed（含 F 自己新增的 resend 独立回归；基线红 8 failed / 5 passed，
  resend 场景基线红 1 failed）、前端 7 passed（基线红 5 failed / 2 passed）、E 双连接用例独立复跑 2 轮均 4 passed（见 §5 表）。
  F 四项边界评审结论：边界 1（旧轮次写入，含 resend 入口）在 6fac3197 上**封闭**；边界 2/3/4 封闭；
  允许路径 `interactive_store.submit` / `app.run_turn` / 启动恢复 / notify 轮仍 basis=None（契约 §2.1 只强制
  `/api/turns` 族），作为已知边界记录。
- 各角色分支内的定向数字（其分支 SHA，非最终集成 SHA）：A 新用例 8 passed + 相邻回归 114+ 项；B 新用例 12 passed、
  适配后导航/取消 114 passed、最终 18 文件 210 passed；C 新用例 15 passed、最终 7 spec 86 passed、外部相关 8 文件 81 passed、
  vue-tsc 0 错误；D 新用例 13 passed、D5 37 passed、额外恢复/内存回归 171 passed；E 单文件 4 passed、组合 5×21 passed。
- **本轮未运行**：全量 pytest、全量 vitest/npm test、前端生产构建、CI workflow。原 r16 全量/CI 结果不记到补修提交上。

## 6. 全量与 CI 台账沿用（不归属补修提交）

- 原全量批次 attempt2 受测 `929e8d7c`（见 §4），结果已定稿；本轮不重跑、不等待、不把结果记到补修提交。
- 原 CI run `42948420148`（`ci.yml` push 触发，headSha `929e8d7c`，createdAt 2026-11-11T02:47:38Z，启动时状态
  in_progress）属于 `929e8d7c`；本轮不等待其完成、不核验终态、不把任何结果记到补修提交。
- 推送前核对触发规则：`ci.yml` 的 push 触发器覆盖全部分支（仅排除 `fix/pm-*`、`verify/pm-*`），本分支在触发范围内；
  按仓库既有 `[skip ci]` 策略，最终推送的 head 提交信息包含 `[skip ci]`，不产生第二次完整矩阵。

## 7. 已知限制与剩余验证范围

- 真实模型中文语义判断/取消目标选择未验证：fake/scripted provider 只证明接线、事件顺序、事务与执行权收口。
- 补修提交**未跑全量批次与 CI**（按任务书）；原 r16 全量/CI 结果只归属 `929e8d7c`；两者不能互相冒充。
- 轮次依据链边界：边界分段被拒时「片段封存（记忆业务）发生在 CAS 之前」沿用 R16 结构，位置写入本身零副作用；
  多进程共享同一数据库时的 auto_segment CAS 窗口沿用 R16 已知限制。生产受理入口 `/api/turns` 与
  `/api/turns/{turn_id}/resend` 均冻结并传递 basis（F 独立复验，resend 缺口已修复）；
  `interactive_store.submit` / `app.run_turn` / 启动恢复 / notify 轮仍 basis=None —— 契约 §2.1 只强制 `/api/turns`
  族的旧/internal 路径，作为已知边界保留（若后续认定 interactive 提交也是强制入口，可另开小任务）。
- 普通回答：外部模型调用不保证严格 exactly-once（崩溃在生成与暂存之间仍可能再次调用）；另一活跃执行者持有
  写入权时失败暂存会跳过（不覆盖他人暂存/租约，由该执行者自行完成）；普通保存创建执行行后若正式消息被外部删除，
  恢复会按执行行返回历史 answer_id（产品无删除消息路径）。
- 前端：旧后端无 position 的取消响应兼容分支单独标注；权威恢复自身失败会覆盖 lastError（既有 R16 行为）。
- 双连接并发证明同一逻辑答案唯一持久化与执行权收口，不等于外部模型 exactly-once；Windows runner、安装包 E2E、
  真实厂商链路、浏览器端到端不在本轮范围。

## 8. 明确声明

**未合并 main、未打标签、未发布、未改变生产更新源、未操作真实应用数据库、未调用真实或付费模型。**
补修提交供复核；合并与发布留待后续决定。