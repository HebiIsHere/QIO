# 话题系统结构整理 R16 · 交付记录（2026-11-11 · 第五轮）

> 本文件记录本轮**实际做了什么、证据在哪、边界是什么**。唯一共享契约是
> `docs/topic-unify-20261111-contract.md`；结构语义见 `docs/architecture.md` §22。
> 本轮不合并 main、不打标签、不发布。所有数据均来自本轮实际命令，不是推断。

## 1. 基线、分支与最终受测 SHA

- 仓库：`HebiIsHere/QIO`。受检分支：`fix/topic-review-20261110`。
- 开工核实（fetch 后）：`origin/fix/topic-review-20261110` 与本地工作树同为
  `390fca1f6991f7cb68d127685e71072040ee89a7`（ahead/behind 0/0），工作树干净，无后续远端修复。
  上一轮受测代码 `1830395` 是该提交的父提交。
- 开发隔离：Lead 在 `D:\qio-dev\qio-r14` 集成；六条角色分支各自独立 worktree（`r16-a`…`r16-f`），
  均由 390fca1f 拉出，共享 backend/.venv 与 frontend/node_modules junction；原始证据在
  `D:\qio-dev\r16-evidence\<role>\`，不写进工作树。
- **最终受测 SHA：本文件所在提交**（实现、测试、迁移、文档全部纳入；冻结时工作树干净）。
  该 SHA 同时是本轮唯一一次全量本地批次与唯一一次完整 CI 的受测对象；完整指纹、全量/CI 身份、
  开始时间与当前状态记录在本地台账 `D:\qio-dev\r16-evidence\lead\` 与最终交付回复中。

## 2. 三个核心分别如何整理、删除了哪些旁路

### 2.1 后端完整位置状态及统一变更入口

- 新增 `backend/src/agent/services/position_state.py`：完整位置状态的唯一读取（单读事务）与唯一写入口
  `PositionStateService.apply(transition)`。完整状态由既有权威存储同源读取（cursor active 行、
  continuation_intents 唯一 registered、navigation_version、单行表 position_state 的 position_version/
  db_identity/因果字段、当前话题 open_fragment_id），**没有新建第二份 Anchor/pending 权威**。
- 新增迁移 `backend/src/agent/storage/migration_r16_position_state.py`（83）与补偿 84：position_state、
  navigation_commands、topic_answer_runs 追加 phase/staged_answer/staged_at/staged_model + phase 回填；
  `SCHEMA_VERSION = COMPENSATION_VERSION = 84`，`REQUIRED_OBJECTS` 同步；历史迁移零改动。
- 两类版本职责分开：nav_version 只由真实导航决定推进；position_version 在任何真实位置状态变化时推进
  （含自动容量分段、内部落位、接续状态）。apply() 在同一 SQLite 事务内完成命令身份/参数摘要校验、
  CAS/前提、Anchor、pending、两类版本、命令结果行；事务提交后由调用方用 `CommandResult.position` 发布。
- `TopicNavigationService` 全部写入口（进入/登记接续/落位/落实/新建/取消）改为薄包装；新增
  `apply_auto_segment` / `apply_internal_refine` 内部转换；`turn_orchestrator` 的轮首接续落实/补锚点、
  轮末位置推进、边界分段、容量分段全部调用统一入口；`services/app.py` 的 `_publish_anchor_event`
  使用同源整份 position，`AppContext.position_state` + 启动期 `ensure_identity()` 接入。
- **删除的旁路**：旧「待确认切换」业务入口 `request_switch / pending_switch / confirm_switch / reject_switch`
  已无生产调用，本轮整体删除（`/api/topic-switch/*` 保留 410 占位给旧客户端）；
  Anchor 唯一业务写入点从 Navigator 收口到 `services/position_state.py`（架构守卫测试同步、语义未放宽）；
  低层 `AnchorService` / `TurnBindingService` / `fragments` 写方法保留，但业务使用只经统一入口。
  生产写入口清单见 `D:\qio-dev\r16-evidence\lead\entry-point-inventory.md` 与 A/B 的最终清单。

### 2.2 所有导航请求的身份、幂等与失效

- 持久表 `navigation_commands`：`command_id == identity_key`，三类身份：
  `request:<client_request_id>`（手动导航进入/返回/选历史/取消）、
  `operation:<operation_id>:<param_version>`（工具 switch/create/continue 真实执行身份）、
  `internal:<command_kind>:<scope>`（自动分段/内部细化/接续落实，确定性 scope + expected_position_version 前提）。
- `/api/anchor` 两分支与 `/api/anchor/continue/cancel` 统一走 `PositionStateService.apply()`：
  手动导航必填 `request_id` + `base_nav_version`（缺 400 `navigation_request_id_required` /
  `navigation_basis_required`）；工具复用 operation_id+param_version；审批结果（APPROVAL_RESULT）
  携带命令的 position/command_id/changed。
- 同身份同参数 → replay 返回既有结果 + 当前只读 position（零写入/零版本推进/零 ANCHOR 广播）；
  同身份不同参数 → 409 `navigation_command_conflict`（零写入、不新增行）；依据过期 → 409
  `navigation_command_stale`；已失效旧选择 → 409 `continuation_request_inactive`；显式封存片段
  非 continue → 400 `historic_fragment_requires_continuation`。request/operation 身份行本轮不清理，
  旧请求 ID 不会在重启后被当成新请求重新执行。
- 旧依据保护保留并被统一入口强化：判断依据在异步上下文装配前冻结，装配后只读复核，不回填新版本；
  用户浏览/改选只让基于旧依据的新导航失效，不取消原问题。

### 2.3 同一逻辑答案的执行权、生成结果暂存与最终保存

- `topic_answer_runs`（迁移 81）仍是 answer_key 唯一执行权行；迁移 83 追加 phase 与共享暂存列。
  phase：waiting → preparing → generating → staged（生成成功待补存）→ answered；可恢复失败走 failed；
  phase 与既有 status 同步写入。执行权 =（answer_key, generation, owner），心跳续租不持写事务、不全局锁。
- 暂存正文唯一权威是 run 行 `staged_answer/staged_at/staged_model`（按 answer_key 共享）：记录 A 生成
  成功但保存失败后，记录 B（即使另一条恢复记录）先查唯一答案 → run.staged_answer → 旧
  `record.answer_text` 只读兜底（同事务转写并清旧列），命中即只补存、不再调用模型。
- 保存事务内校验执行权 → 唯一 answer_key 写入/复用 → 所有同 answer_key pending 记录 answered（相同
  answer_id）→ run phase=answered 且清空暂存；旧执行者迟到的暂存/保存/成功/失败/释放全部被
  写入权 CAS 拒绝（`recovery_execution_superseded`，零行改动）。恢复入口只读复核导航版本，不导航、
  不改 pending、不抢新轮次。I14 冻结码 `answer_attribution_mismatch` / `answer_context_missing`
  （recoverable、503、零正式答案行）。

### 2.4 前端统一接受完整状态（本轮必要消费）

- `session.acceptPositionState(input, source)` 成为 ANCHOR 事件、`/api/runtime/state` 快照、导航 HTTP
  响应（含 APPROVAL_RESULT position）唯一接受入口；业务代码不再直接 `setAnchor/setAnchorAndSync` 做导航
  （PlanetView 改为经 `session.manualNavigation` 使用响应 position；`setAnchor` 仅接受入口内部视图赋值）。
- 整份接受：较旧整份拒绝；相同版本相同内容幂等零副作用；相同版本内容冲突不合并、记录可观察事实并
  single-flight 触发权威恢复；较新整份采用；实例/数据库身份变化按冻结规则处理；同版本内容比较排除
  展示标题与 instance/seq。
- 草稿跟随独立幂等消费身份：仅本次操作权威成功结果迁移一次；无关手动导航不消费；旧/被更新选择替代
  的结果不搬并清除；重复结果零副作用；重连快照先到不清等待草稿、不覆盖等待期间新编辑；目标已有草稿
  保护；始终不自动发送。手动导航单飞串行（后点排队、每次真实动作新身份、重试复用）。

## 3. 原剩余问题逐项如何闭环

| 上轮/长期残留问题 | 本轮处理 | 证据 |
| --- | --- | --- |
| 位置权威分散（Anchor + pending + nav 版本分别读写，事件/快照可能拼不同时刻） | 完整位置状态同源读取 + 统一入口；事件/快照/HTTP 只用同一 position wire | A 用例 `test_r16_a_position_state.py`；E 同源断言 |
| 导航决定版本承担了位置排序职责，自动分段/内部落位无法表达 | 分离 nav_version 与 position_version；position_version 覆盖真实状态变化 | A/E 用例；前端整份接受用例 |
| 生产写入口存在旁路（业务直接改 Anchor/pending） | 全部导航入口收口 apply()；删除旧待确认切换业务入口；架构守卫更新 | A 清单/静态宰卫、F 旁路检查 |
| 历史接续请求重放会二次落位；只有 continue 分支有请求身份 | 所有导航动作统一命令身份；A→B→重放 A 保持 B；开放片段选择也有 request 身份 | B `test_r16_b_nav_commands.py`、E HTTP/并发 |
| 旧请求无法区分「重放」「失效」与「新选择」 | replay / conflict / inactive / stale 四类冻结语义；同身份异参零副作用 | B/E 用例；F 评审 |
| 前端 ANCHOR 无条件 setAnchor、旧事件可跳回；业务代码直接改位置 | 唯一 `acceptPositionState` 整份接受；旧状态整份拒绝/同版本冲突不合并；业务导航走 manualNavigation | C `r16-c-position-state.test.ts`；F 前端独立用例 |
| 草稿跟随依赖一次性对象，快照先到/重复结果/被替代结果会丢草稿或重复迁移 | 独立幂等消费身份；快照先到抑制普通 bind、不丢等待正文；结果到达迁移一次 | C 用例；E 前端接线 |
| 逻辑答案暂存写在单条恢复记录上，同 answer_key 另一记录无法补存 | 暂存权威迁移到 run 行并按 answer_key 共享；B 记录只补存不生成 | D `test_r16_d_answer_execution.py`；F I10；E 集成 |
| phase 缺失，恢复/收件箱无法区分生成中与已生成待补存 | phase 状态机与 status 同步；保存/暂存/失败/释放原子写入 | D 用例；F I10/I12 |
| 恢复入口可能导航/改 pending/覆盖草稿 | 恢复只读复核 nav 版本、不导航、不改 pending、不抢新轮次（前端草稿同样保护） | D/E/F I13 用例 |
| 迁移与旧数据兼容未覆盖完整位置知识与暂存新列 | 追加迁移 83+补偿 84；phase 回填；旧 record.answer_text 只读兜底 | A 迁移 smoke；D 迁移用例；仓库迁移测试 |
| 文档仍按旧机制描述 | 契约 v2 + 架构 §22 + 状态 P105 + 本交付记录 | `docs/` |

## 4. 多智能体实际分工与独立评审

- **Lead**：基线与写入口清点、规则/转换表冻结与 v2 delta、文件所有权与迁移编号协调、集成合并、
  跨角色接缝裁决、文档、唯一全量/CI、最终验证与报告。
- **A**：完整位置状态与统一入口、迁移 83/84、navigation/orchestrator/app 接线、退役旧旁路；
  提交 `4a1266b1` / `5870dc0f` / `b8896665`。
- **B**：导航命令身份/幂等/失效、HTTP 路由与工具路径、runtime 同源快照、审批结果 position 透传；
  提交 `218e7ccb`。
- **C**：前端整份位置状态接受入口、ANCHOR/快照/响应统一消费、草稿跟随独立消费身份与单飞手动导航；
  提交 `a41d23dc`。
- **D**：逻辑答案执行权阶段、共享暂存、正式保存原子收口、I14 冻结码；提交 `843ebabf`
  （Lead 代提交其已完成的工作树交付）。
- **E**：独立参考模型 + 固定种子组合序列、真实 HTTP、双独立连接竞争、有界真实进程退出/接管、
  fake 模型真实事件流、真实前端 store、实际界面核对；提交 `c374dd4a`/`a7bef429`/`1634bba1`。
- **F**：独立复现（基线红→集成绿）、实现与规则覆盖评审、旧测试最小适配；提交 `35bd6137`/`d42dd94a`。
- **独立评审发现并已修复的真实问题**：
  1) `select_history` 点击路径在工具已登记同一历史时移动 Anchor 却不推进 nav_version（I01 漏洞）；
  2) `apply()` 未复查 `NavCommandIdentity.kind` 与 identity_key 前缀（可被 `object.__new__` 绕过）；
  3) `navigation.py` 仍保留 `request_switch` 等无生产入口旁路直写 pending。
- 评审同时确认：生产写入口无第二个写者、两类版本职责分离、命令重放/冲突/失效零副作用、
  前端整份接受与草稿跟随、共享暂存与迟到写拒绝、恢复只读边界均符合契约；未处理风险见 §7。

## 5. 定向、模型化、并发、集成与界面核对证据

（以下全部为本轮实际命令结果，不是推断；未运行的部分如实标注。）

| 范围 | 结果 | 证据 |
| --- | --- | --- |
| A 新用例（两类版本/转换表/原子/幂等/CAS/旁路） | 30 项通过，exit 0 | `r16-evidence\a\` |
| A 退役+导航核心定向批 | 175 项通过，exit 0 | `r16-evidence\a\retire-final.txt` |
| A 迁移 smoke | version=84、missing_objects 全空 | `r16-evidence\a\` |
| B 主定向（真实 HTTP 命令身份） | 61 项通过，exit 0 | `r16-evidence\b\integration-green-r16b.txt` |
| B 扩展受影响定向 | 186 项通过，exit 0 | `r16-evidence\b\integration-regression-r16b.txt` |
| C 新用例（整份接受/草稿跟随） | 27 项通过，exit 0 | `r16-evidence\c\` |
| C 集成定向批（23 文件） | 298 项通过，exit 0 | `r16-evidence\c\integration-green-broad.txt` |
| D 新用例（phase/共享暂存/I10—I14） | 10 项通过；集成指定文件 47/47，扩展 84/84，exit 0 | `r16-evidence\d\` |
| E 参考模型（种子 20261111/20261112/20261113，每步判规则） | 5 项通过，exit 0 | `r16-evidence\e\integration-green-refmodel.txt` |
| E 真实 HTTP / 双连接竞争 / 有界进程退出接管 / fake 模型事件流 | 12 / 3 / 5 项通过，exit 0 | `r16-evidence\e\integration-green-*.txt` |
| E 前端 store 与事件接线 | 20 项通过，exit 0 | `r16-evidence\e\integration-green-frontend.txt` |
| E 实际界面核对（headless Edge + 真实前后端 + 本地 fake provider） | PASS 11/11（对话页渲染、真实发送、消息与回答出现、runtime anchor 同源被采用、无 console error/失败请求） | `r16-evidence\e\ui-check.md` + 截图 |
| F 独立复现（基线红→集成绿） | 基线后端 29/29 红、前端 16/16 红；集成后端 60 项通过、前端 16 项通过，exit 0 | `r16-evidence\f\summary.md` |
| Lead 最终后端定向批（31 文件） | exit 0 | `r16-evidence\lead\final-backend-targeted2.log` |
| Lead 最终前端定向批（18 文件） | 250 项通过，exit 0 | `r16-evidence\lead\final-frontend-targeted.log` |
| Lead 全仓类型检查 | `npx vue-tsc --noEmit` exit 0 | `r16-evidence\lead\final-vue-tsc.log` |
| 仓库检查 | 文档一致性、第三方声明、发布闸门自检、workflow 契约检查 | `r16-evidence\lead\` |

**模型化**：E 的参考模型是纯状态机，不 import 生产转换、不复制生产分支；固定种子生成选择历史/返回开放/
切换新建/自动分段/片段补全/确认拒绝失效/请求重放与参数冲突/快照事件乱序/断连重连实例变化/生成失败
保存失败等组合序列，每步检查规则而非只查最终页面；失败自动缩减为最短前缀，并有稳定短序列回归。
**并发**：真实同一数据库文件的**两个独立 SQLite 连接**竞争同一命令身份与同一 answer_key，用
Barrier/Event 控制交错，断言只执行一次、一份写入、一次广播。**真实进程**：有界子进程退出后由
InstanceRegistry 既有机制判死并交还归属，不无限等待。
**能力边界**：模拟事件不是浏览器端到端；fake 模型不证明中文理解准确率；界面核对是 headless Edge
单视口浅色、开发豁免后端、一次性数据目录，≠ 桌面壳/Tauri WebView、≠ 真实厂商。

## 6. 唯一一次全量本地测试与唯一一次完整 CI

- 按本轮硬约束：**只允许一次**完整本地测试批次、**一个**预先确定的完整 CI workflow run；
  实现、定向检查、接口集成与独立评审全部完成后才冻结最终受测 SHA。
- 本地批次：冻结 SHA 的**独立工作目录** `D:\qio-dev\r16-full`（git worktree，detached 于受测 SHA），
  单个后台任务顺序执行：后端全量（CI 同一入口 `scripts/run_backend_suite.py`，并行组+串行组分阶段 JUnit）、
  前端 `npx vue-tsc --noEmit`、`npm test`（vitest 全量）、`npm run build`、仓库文档一致性、
  第三方声明、发布闸门自检、workflow 契约检查、frozen worker smoke。
  命令、受测 SHA、北京时间、任务身份（PID）、日志/JUnit/最终退出码位置记录在
  `D:\qio-dev\r16-evidence\lead\full\meta.json` / `status.json` / `full-batch.log` 与 `launcher.*.log`；
  启动后只核验任务真实存在、受测 SHA 正确、日志开始产生，**不等待完成、不持续轮询**。
- CI：本分支 push 触发 `CI`（`ci.yml`，一次 push 只对 head SHA 生成一条工作流运行；平台矩阵同属一次 CI）。
  开发期间不 push；冻结后只 push 一次（不带 `[skip ci]`）。run ID/链接/事件/受测 SHA/当前状态
  在启动后立即记录到本地台账，并写入最终交付回复；若报告前已经失败则如实写失败，不把排队/运行中
  混写成完成。
- 后续若发现失败：保留本次完整证据，只做失败项及必要受影响的本地定向修复验证；不自动启动第二轮
  全量、不创建第二个 CI run，不隐瞒最终代码与受测 SHA 之间的差异。

## 7. 已知限制与尚未完成的验证

- **真实模型能力未验证**：fake/scripted 模型只证明 QIO 自身接线、事件顺序、事务与执行权收口，
  不证明中文语义理解或真实厂商行为。
- **外部模型调用不保证严格 exactly-once**：崩溃发生在模型返回与结果持久化之间仍可能再次调用；
  本轮保证的是同一逻辑答案唯一持久化、共享暂存只补存与执行权/世代收口。
- **浏览器端到端有限**：界面核对为 headless Edge + 单视口浅色 + 开发豁免后端 + 一次性数据目录；
  未覆盖桌面壳/Tauri WebView、窄窗口/深色、浏览器内审批/草稿跟随/断线重连（这些由 store/HTTP 用例覆盖）。
- **跨进程 auto_segment CAS 窗口**（评审指出，无同进程 await 窗口）：若多进程共享同一数据库且在
  「封存片段」与「apply 推进 position_version」之间发生竞争，可能片段已封存而本位置版本/事件未推进；
  本轮按产品单进程假设（桌面单实例）实现，未做多进程写协调。
- **前端同版本冲突恢复**：single-flight 权威恢复避免风暴，但没有硬环保护；冲突事件持续到达时依赖
  既有 resync 缓冲与恢复机制。
- **I14 边界**：正式答案写入前的归属复核覆盖固定片段/话题/原用户消息轮次一致性，未额外比对
  `turn_bindings` 行本身。
- **旧兼容入口留存**：`acceptTopicPositionEvent` / `applyAuthoritativePositionSnapshot` 作为旧后端/
  旧测试兼容入口保留，生产新代码只走 `acceptPositionState`；后续可 deprecated/移除。
- **外部环境限制**：Windows runner、安装包 E2E、真实厂商链路、Rust/Tauri 构建不在本轮；
  本地全量使用本机 Node/Python 版本，与 CI runner 版本可能不同（CI 矩阵覆盖其自身环境）。
- 「本轮的定向与全量没有发现更多问题」**不等于**「以后不会再出问题」；以上边界是真实存在的。

## 8. 明确声明

**未合并 main、未打标签、未发布、未改变生产更新源、未操作真实应用数据库、未调用真实或付费模型。**
所有模型调用均使用本地 fake/scripted provider；隔离验收使用一次性数据目录。