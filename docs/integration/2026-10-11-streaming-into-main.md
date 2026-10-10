# 流式回复修复线并入 main —— 集成记录与合并后收尾（2026-10-11）

## 一、先用非技术的话说结论

这次要做的不是"把两个分支合在一起"，而是**让两条各自开发了很久的线在同一个版本里都能正常跑**。

- 一条是**主线**：可靠性、安全、恢复入口、实例归属、用量记账（已经在 `origin/main` 上）。
- 一条是**流式回复线**：真实边生成边显示、统一过程区、附件从上传到删除的完整生命周期、
  取消/断流/恢复、以及这条线自己 8 轮审计留下的全部修复。

两条线都改了同一批文件（后端运行管理、数据库、前端状态机），所以合并时在 13 个文件上撞车。
处理原则只有一条：**两边已经验证过的行为都保留，不为了省事丢掉任何一边**。

数据库是这次最需要小心的地方：两条线各自给数据库加了列和表，而且**编号撞号**。
处理办法不是二选一，而是"给流式线的三条迁移换一个更大的编号，再额外加一条把所有对象
都补齐的补偿迁移"——这样无论用户手里的库是哪一类（全新的、旧 main 的、最新 main 的、
流式线的），升级后结构都完整，而且**不删改任何用户数据**。

另外在集成过程中发现一个**真实的功能空洞**：流式调用不记用量、也不受预算上限约束
（界面上数字照常显示，所以光看界面发现不了）。已经补上并加了测试。

**PR #2 已把这条集成分支合并进 main**（合并提交 `7ff5e2e`）。本文分两部分：

- §一–§七 是**合并前的原始集成报告**（写于评审阶段，原文保留以便对照）；
- §八 起是**合并后的核对与收尾**：原合并 CI 的真实结果、两处缺口的复现与修复、
  独立分支的补充验证，以及修正后的回退说明。

## 二、实际分支与基线（全部为真实 SHA）

| 项 | 值 |
| --- | --- |
| 合并前 main（集成基线） | `84eb4b4`（含可靠性跟进线与安全/响应性线两次合并） |
| 来源（流式回复修复线线头） | `cc19b68`（`fix/process-attachment-audit-round2`） |
| 共同祖先 | `6e073e9` |
| 集成分支 | `integrate/streaming-main-20261011`（线头 `6356c0e`） |
| 集成提交 | `0e00dbe`（合并提交，含冲突解决与集成补丁；其后 `f516589` / `c270605` / `6356c0e` 为集成期修复与文档） |
| 相对 main | `git rev-list --count 7ff5e2e^2 ^7ff5e2e^1` = 328 个来源提交；main 侧 61 个提交全部保留 |
| **合并（PR #2）** | **`7ff5e2e`**（`merge: 流式回复修复线集成到 main`；父提交 `84eb4b4` + `6356c0e`） |
| 合并 CI | run `38075732622`（push，`7ff5e2e`）—— 逐任务结果见 §八 |

来源线的祖先关系已核对：`feat/unified-process-attachments-streaming` →
`fix/unified-process-audit` → … → `fix/process-attachment-audit-consolidation` →
`fix/process-attachment-audit-final-boundaries` → `fix/process-attachment-audit-round2`
**全部是 round2 的祖先**，因此只合并 round2 即可覆盖全部历史成果，无需重复引入早期分支。

## 三、集成范围

**完整引入（流式回复线）**

- 统一过程区：阶段由服务端判定、阶段切换自动收起前一阶段、同阶段说明更新当前展示；
- 真实流式：回答正文边生成边显示（不是前端模拟），TURN_START / 增量 / TURN_END 事件身份与顺序；
- 附件生命周期：上传/绑定/独立副本、>100MB 走路径引用、历史附件打开、删除与重发、话题切换、
  异步复制与取消、关闭时的资源清理、路径失效提示；
- 结束事实：`reason_code / stopped_by / actions` 落库，随历史/分页/RESYNC 下发，
  终态动作表只有一份读时投影（`project_terminal_actions`）；
- 取消/断流/恢复/重发：预留→准备→放行（准备期间不入队、模型零调用），
  准备期中止以后端确认为准；
- 附件准备态与发送闸门、结构化拒绝（逐条原因 + 恢复出口）。

**完整保留（main 侧）**

- 持久接受（契约 C2）：台账写不进去 → 503 `accepted=false`，绝不返回假 200；
  本次集成**扩大到附件预留路径**（`reserve()` 也走同一个持久化入口）；
- 单事务重发（契约 C3）：`claim_for_resend` 一个事务完成"老记录 recovered_at + 后继行 + recovered_by"；
- 幂等受理（契约 5）：`client_request_id` 复用、`deduplicated` 回执、by-request 查证；
- 发送回执台账：回执只单向推进、绝不复活已结束的轮次；
- 实例归属与运行恢复（instances / record_owners / owner_guard / 接管）；
- 日志与事件打码总闸、工具路由移出响应线程、命令真实退出码、进程树清理；
- 记忆/知识/实体人工纠正保护、用量记账。

**没有做的事**：没有并入互动模式线；没有做与本次集成无关的重构；没有删除任何来源分支。

## 四、冲突与解决（13 个文件，逐条）

| 文件 | 冲突内容 | 处理 |
| --- | --- | --- |
| `backend/src/agent/storage/schema.py` | main 的 29/30 与来源线的 26/27/28 号段 | 来源线迁移**改号 31/32/33**（SQL 逐字保留），并新增**补偿迁移 34**（见第五节） |
| `backend/src/agent/storage/turn_journal.py` | 导入行；归属守卫/记账 vs 结束事实 helper | 并集：`transaction + RECORD_TURN + redact_text`；`_owner_guard`/`JournalWriteError` 与 `_redacted_or_none`/`project_terminal_actions` 全保留 |
| `backend/src/agent/storage/turn_journal.py`（补丁） | `claim_for_resend` 与 `reserve()` 的关系 | 新行插入改**幂等 upsert**（只覆盖仍为 queued 的行）：重发路径上这一行可能已由 `reserve()` 落过 |
| `backend/src/agent/core/turn.py` | `__init__` 字段；提交参数 | 并集：`_request_index`（幂等）与 `_reserved/_prepares/...`（预留）全保留；`submit` 带 `turn_id + request_id` |
| `backend/src/agent/core/turn.py`（补丁） | `reserve()` 未遵守 C2 | 抽出唯一持久化入口 `_persist_accept()`，`submit()` 与 `reserve()` 同故障同结果（都抛 `TurnAcceptError` → 503） |
| `backend/src/agent/core/loop.py` | 记账 helper vs 流式实现；`_plan`；错误脱敏 | 并集：来源线流式方法 + main 的 `_usage_accounting`；`_plan` 取来源线（已含 tool_routing 分区），调用点补 `await`；错误走 `sanitize_error_text` |
| `backend/src/agent/api/server.py` | `/api/turns` 与 resend 的受理顺序 | 取来源线"预留→准备→放行"，并在预留处保留 main 的 C2（503）与 C3（`claim_for_resend` 单事务消费 claim）+ F03 派发失败标记 |
| `backend/src/agent/adapters/{anthropic,native,text}.py` | 类属性与 prompt | 并集：`accounts_requests` 与 `supports_stream` 并存；text 档 `observation_note` 与 `CONTENT_ROLE_PROTOCOL` 并存 |
| `frontend/src/stores/session.ts` | 7 块：import / state / getters / 重发后处理 / 发送路径 / 失败分流 | 并集：回执台账与过程区状态并存；发送用 `startIdentity.topicId` + 显式附件数组；失败统一走 `_onSendFailure`（4xx=明确拒绝，5xx/无 status=结果未知） |
| `frontend/src/components/Composer.vue` | submit 的失败恢复 vs 附件准备态；模板 | 并集：准备态包裹发送、失败恢复补上"正在确认时不恢复草稿"的例外；确认条与浏览器回退上传入口都保留 |
| `frontend/src/services/api.ts` | `sendTurn` 签名与请求体 | 6 参签名（附件/重试/中止/准备标识）+ `client_request_id` + `retry_of_turn_id` + 显式 `attachment_ids`（空数组也成字段） |
| `frontend/src/stores/events.ts` | 导入行 | 并集：`api` 与 `TurnFactsRow` |
| `docs/status.md` | 两轮各自的里程碑章节 | 两段都保留（main 的 F01–F10 与来源线的附件边界收口） |

**没有使用 `ours`/`theirs` 整体取舍，没有删除测试、跳过校验、禁用功能或吞异常。**

## 五、数据库兼容

**号段与处理**

- main：迁移 1–25、**29、30**（26–28 被同基线的其它分支占用，因此 29 起步）；
- 来源线：迁移 1–25、**26（attachments 表 + 3 个索引）、27（source_attachment_id）、28（turn_journal 结束事实三列）**；
- 合并后：1–25、29、30、**31、32、33**（来源线三条改号）+ **34**（补偿迁移），`SCHEMA_VERSION = COMPENSATION_VERSION = 34`。

**为什么改号而不是保留 26–28**：`apply_migrations` 只按版本号前进（`target <= 已记录版本` 就整段跳过）。
任何已经记到 30 的存量库（就是最新 main 用户手里的库）都会把 26–28 跳过，
`attachments` 表永远不会被建出来。改号到 30 以上是唯一保证它**必然被应用一次**的做法。

**补偿迁移 34**：把 main 的实例归属对象（instances / record_owners / owner_instance_id /
knowledge 链列 / entity_cards 修订列）与本次集成新引入的 attachments / 结束事实对象合并成
一份**幂等**语句（CREATE TABLE IF NOT EXISTS + 列已存在就跳过）。`verify_required_objects()`
在迁移跑完后按**对象**复核，缺哪个补哪个，仍缺就抛 `SchemaIncompleteError`（不静默放过）。

**四种库状态都验证过（见第六节测试）**

| 起点 | 结果 |
| --- | --- |
| 全新库 | 1–25、29–34 全跑，对象齐全 |
| 旧 main 库（≤25） | 补齐 main 与流式线的全部对象 |
| 最新 main 库（30，无 attachments） | 应用 31–33 → 建表建列；数据不动 |
| 流式线库（28，有 attachments） | 应用 29–33；31 的 `IF NOT EXISTS` 与 32/33 的 `ADD COLUMN` 幂等跳过 |

**数据保护**：34 与 31/32/33 里没有 DROP / DELETE / UPDATE，只建对象；
没有修改任何历史迁移（迁移只追加的纪律用例仍绿）；没有靠改版本号掩盖结构差异。

## 六、测试证据（原始集成轮，写于合并前评审）

> 本节是**分支 `integrate/streaming-main-20261011` 上的原始记录**，不是合并后的复核。
> 合并后的核对、两处缺口的修复与再验证见 §八–§十一；未通过 / 未执行的项目在第七节逐条列出。

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `uv run --frozen pytest` | **3298 passed / 11 skipped / 0 failed**（exit 0，12 分 21 秒） |
| 前端类型检查 | `npx vue-tsc --noEmit` | **exit 0**（205 个测试文件的类型检查全过） |
| 前端全量 | `npm test` | **205 文件 / 1592 用例全部通过** |
| 文档一致性 | `python scripts/check_docs.py` | **exit 0** |
| 评测基线 | `uv run --frozen python -m agent.eval.run` | 与合并前基线 `da0436b` 的 **29 项指标逐项一致**（工具策略与流式改造无回归） |
| 流式记账（集成补丁新增） | `pytest tests/test_streaming_usage_accounting.py` | 3 passed |
| 定向回归（发送/附件契约） | `vitest run` 6 个相关文件 | 38 passed |

**定向验证覆盖**：发送回执顺序与幂等（`sr-e-sendReceiptOrdering`、`sr-e-sendConfirm`、
`sr-f-turn-lifecycle`）、附件准备与中止（`PreparingAttachments`）、附件显式空数组
（`attachmentExplicitEmpty`）、重发与结构化拒绝（`attachmentRetrySend`、`sendTurnAttachments`）、
起点身份（`rm-e-continuation`）、迁移纪律与补偿（`test_rm_lead_migration_discipline`、
`test_fu_w3_b01_schema`、`test_fu_verify_b01_migration`、`test_migrations_append_only_verify`）。

## 七、遗留问题与未验证项（不粉饰）

1. **未在真实 Windows 桌面上做端到端走查**（起应用、附件全流程、关闭清理、窄窗口视觉检查）。
   本轮以组件级 + store 级 + 后端全量测试替代，**不等于**桌面端已验证。
2. **Rust 侧未在本地跑**：`cargo check` / `cargo test` 与安装包 E2E 由 CI 覆盖。合并 CI
   run `38075732622` 里 rust（两个平台）与 install e2e（windows-latest）**都是成功的**。
3. **合并 CI 没有全绿**：run `38075732622` 的 `backend (windows-latest)` 失败，唯一失败用例是
   `backend/tests/test_r8_cancel_target_verify.py::test_repeated_cancel_and_unknown_id_are_idempotent`
   （固定等 1.0 秒后取消「准备中」的轮次；慢 runner 上准备登记还没落下 → 回执 `unknown`，
   断言 `first["cancelled"] is True` 失败）。本机 Windows 复跑该文件 4 个用例全过，
   与 §九 的两项修复没有交集，按**既有问题 / 时序敏感**记录，不在本次收尾里改动它。
   同一 run 的两个 Linux backend 任务（py3.11 / py3.12）最终也失败了，但原因**不是断言**：
   GitHub 注解为「The hosted runner lost communication with the server」（runner 本身失联，
   两个任务分别跑到 30–70 分钟才收场）。同一份代码在本机跑完整后端全量是 3334 passed / 0 failed。
4. **两处产品口径待确认**：
   - 排队期间切话题时，附件 id 属于提交时的话题、`topic_id` 也是提交时的快照，后端按旧话题
     校验附件是否仍能命中（需要后端所有者确认）；
   - "已受理但用户点过中止"时保留附件 chip 清空的行为（来源线原语义）是否符合预期。
5. **`turn.py` 里 `_journal_accept_failed()` 在统一持久化入口后已无调用点**（死代码，行为无影响），
   建议后续单独清理，不混进本次集成。
6. **来源线遗留**：`PREPARE_HISTORY` 注释在来源线里重复一次（同值，无害）。

## 八、合并与 CI（合并后复核）

**合并事实**

- PR #2 已合并：合并提交 `7ff5e2e`，父提交 `84eb4b4`（合并前的 main）+ `6356c0e`
  （集成分支线头）。当前 `origin/main` = `7ff5e2e`（此处数字与 SHA 都在 2026-10-11 实查，
  `git fetch` 之后 `git ls-remote origin main` 与之逐字一致）。
- 集成分支 `integrate/streaming-main-20261011` 保留为历史，未删除。

**合并 CI run `38075732622`（push，`7ff5e2e`）逐任务结果**

| 任务 | 结果 |
| --- | --- |
| docs consistency | ✅ 成功 |
| frontend | ✅ 成功 |
| rust (ubuntu-24.04) | ✅ 成功 |
| rust (windows-latest) | ✅ 成功 |
| frozen worker (windows) | ✅ 成功 |
| install e2e (windows-latest) | ✅ 成功 |
| backend (windows-latest) | ❌ 失败（1 条用例） |
| backend (py3.11) | ❌ 失败（**基础设施**：runner 失联） |
| backend (py3.12) | ❌ 失败（**基础设施**：runner 失联） |

**两个 Linux backend 任务的失败原因**：GitHub 注解是
「The hosted runner lost communication with the server. Anything in your workflow that
terminates the runner process, starves it for CPU/Memory, or blocks its network access can
cause this error.」——两个任务各自跑了很久（远超历史 ~12 分钟）之后 runner 失联，
**没有任何用例断言失败**。这是 CI 基础设施问题，不是产物回归。

**`backend (windows-latest)` 失败定位（既有问题，不是本次两项修复的回归）**

- 唯一失败用例：`backend/tests/test_r8_cancel_target_verify.py::test_repeated_cancel_and_unknown_id_are_idempotent`；
  断言 `first["cancelled"] is True`，实际回执 `{'ok': True, 'unknown': True}`。
- 原因：该用例登记「准备中」的轮次后**固定等 1.0 秒**就发第一次取消；负载高的 Windows runner 上
  准备登记尚未落下，服务如实回 `unknown`（这就是「未知标识」语义），于是断言失败。
- 与本轮两处修复的关系：无交集（一处是迁移清单，一处是流式记账）。本机 Windows 复跑
  `backend/tests/test_r8_cancel_target_verify.py` 4 个用例全过（见 §十）。
- 处理：按**时序敏感的既有问题**记录；本次收尾不扩大范围去改这条独立验证用例。

## 九、合并后核对出的两处缺口（本轮修复）

分支 `fix/streaming-main-closeout-20261011`（从 `7ff5e2e` 起，未并入互动模式线、无无关重构）。

**缺口一：结构完整性检查漏掉了本线新增的半个 schema**

- 反例（修复前）：库已升到 34 → `DROP TABLE attachments` → 再跑迁移：
  `missing_objects()` 报空、`apply_migrations()` 报「结构完整」、表也不补建。
- 根因：`backend/src/agent/storage/migrate.py` 的 `REQUIRED_OBJECTS` 只覆盖实例归属那批对象，
  补偿迁移 34 里已有的 attachments / 结束事实对象没进清单。
- 修法：清单补上 `attachments` 表、`attachments.source_attachment_id`、三个附件索引、
  `turn_journal` 的 `reason_code / stopped_by / actions` —— 每一项都与迁移 34 的补偿语句一一对应
  （用例逐项核对，不接受「只能发现、补不了」的项）。缺了就重放 34；仍缺抛
  `SchemaIncompleteError`，绝不返回「完整」。只追加清单，不动任何历史迁移，不 DROP/DELETE/UPDATE 用户数据。
- 反例（修复后）：同上步骤 → 表、索引、列都被补建；补偿补不齐时（表在但形状不对）
  抛出可读错误并如实列出仍缺的对象。

**缺口二：流式降级时漏记已经真实发出的请求**

- 反例（修复前）：假客户端记录一次 `create()`、返回空异步流 → 真实 `NativeAdapter` 抛
  `UnsupportedCapability`，却**没有调用任何记账入口**（`requests` 停在 0）。
- 根因：`stream()` 的失败记账按**异常类别**判断 —— `UnsupportedCapability` / `NotImplementedError`
  一律当成「没有请求发出」。但 `_stream_once()` 可能在已经调用客户端、读到响应**之后**才抛它
  （客户端不返回异步流 / 零增量且看不到 Content-Type）。
- 修法（`backend/src/agent/adapters/native.py`）：

  1. 引入按请求事实记账的 `_StreamAttempt`（`requested` = 真的把请求交给了客户端；
     `settled` = 这次尝试已登记），记账只看事实，不看异常类别；
  2. 「还没发请求就发现能力不支持」→ `requested` 为假 → 零请求、零记账
     （上层整段降级的那次请求自己记）；
  3. 「请求已发出、响应后才发现不能流式」→ 记一次**不完整用量**（`incomplete`，不估算 token）；
  4. 整段降级是**另一实际请求**，由 `complete()` 自己登记 —— 请求数 = 登记数；
  5. 正文已透出后不得重新生成的规则保持不变（`yielded` 守卫未动）；
  6. `stream_options` 被拒后的重试是第二次实际请求：**发出之前**重新核对预算
     （原来只在第一次之前核对，存在绕过）；
  7. 记账只有一个负责入口：Native / Anthropic 声明 `accounts_requests`，上层的
     `credential_usage_sink` 据此 `covers_requests=True` 而跳过补记（流式路径也有用例钉住不双记）。

- 反例（修复后）：同一装置下这次请求登记为 `requests=1 / incomplete=1`，且不写任何 token 数。

## 十、本轮验证结果

> 定向与全量都在本机（Windows）跑；命令与 AGENTS.md 一致。数值见下表。

**定向（本轮缺陷 + 受影响面）**

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 缺口一反例（本轮新增） | `pytest tests/test_main_closeout_migration_objects.py` | 13 passed |
| 缺口二反例（本轮新增） | `pytest tests/test_main_closeout_streaming_accounting.py` | 12 passed |
| 既有 B01 清单 / 独立验证 | `pytest tests/test_fu_w3_b01_schema.py tests/test_fu_verify_b01_migration.py` | 8 + 7 passed |
| 流式契约 / 记账 / 预算 / 迁移纪律 | `pytest tests/test_streaming_deltas.py tests/test_streaming_usage_accounting.py tests/test_migrations_append_only_verify.py tests/test_rm_d_m10_usage_ledger.py tests/test_rm_d_m09_budget.py tests/test_rm_verify_m07_m09_m10_c01.py` | 96 / 3 / 2 / 12 / 8 / 5 passed |
| R8 取消用例（合并 CI 的红点）本机复跑 | `pytest tests/test_r8_cancel_target_verify.py` | 4 passed |

**全量（交付前集中一次）**

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `uv run --frozen pytest`（本机用仓库自带 venv 的同一套 pytest） | 3334 passed / 11 skipped / 0 failed（746 秒） |
| 前端类型检查 | `npx vue-tsc --noEmit` | exit 0 |
| 前端全量 | `npm test` | 205 files / 1592 tests passed |
| 文档一致性 | `python scripts/check_docs.py`（含 `--selftest`） | exit 0（36 个里程碑条目；自检 6 个场景全绿） |
| 发布闸门自检 | `python scripts/release_gate.py --selftest` | PASS |
| 评测基线 | `uv run --frozen python -m agent.eval.run` | 与基线 `7ff5e2e` **逐项相等**：在 `7ff5e2e` 的独立 worktree 里跑同一条命令并深度比较，**0 差异** |

**本轮分支 CI**（绑定实际 SHA 与 run ID，**不是全绿**）

run `38078800507`（push，SHA `6078b613b9171cd8e2409ac8b97e1cfa582fc319`）。
被测代码与本分支最终代码**逐字相同**（其后只有本文档的补充提交，不改任何代码；PR 上的
再次运行因此是同代码的另一次 run）。

| 任务 | 结果 |
| --- | --- |
| docs consistency | ✅ 成功 |
| frontend | ✅ 成功 |
| rust (ubuntu-24.04) | ✅ 成功 |
| rust (windows-latest) | ✅ 成功 |
| frozen worker (windows) | ✅ 成功 |
| install e2e (windows-latest) | ✅ 成功 |
| backend (windows-latest) | ❌ 失败（唯一失败：`tests/test_interactive_during_heavy_work.py::test_health_probe_stays_responsive_while_slow_prediction_runs`，阈值 `gap_ms < 100` 实测 **104 ms**） |
| backend (py3.11) / (py3.12) | ⏳ 收尾时仍未结束；同一份代码在原合并 run 上的同类任务最终是「runner 失联」（基础设施，不是断言失败） |

关于这个红点：本机 Windows 复跑 `tests/test_interactive_during_heavy_work.py` 为
**13 passed / 4 skipped**，且同一份代码在本机跑完整后端全量是 **3334 passed / 11 skipped /
0 failed**；该用例量的是「慢嵌入期间事件循环的最大停顿」，阈值 100 ms 在负载高的共享 runner
上会被压线越过。它与本次两项修复没有交集（那条路径用 `AsyncMock` 的 adapter，不经过任何
记账入口）。**不通过反复重跑直到变绿**：按负载敏感的既有问题如实记录。

## 十一、回退说明（代码回退 ≠ 数据库回退）

- **代码回退**：本次成果已经进了 main（`7ff5e2e`）。要撤销集成，只能对合并提交做
  `git revert -m 1 7ff5e2e`（或在合并前准备一个回退分支），**不是**「切回未集成的 main」——
  那个用作基线的 `84eb4b4` 已经不再是 main 的顶端。回退代码**不会**把库降回旧结构。
- **数据库回退**：迁移 31–34 与本次的补偿只建对象，没有 DROP / DELETE / UPDATE 任何用户数据，
  但**版本号与服务端结构不会自动降级**：
  - 回退后的旧代码运行在已升到 34 的库上时，会看到多出来的 `attachments` 表、
    `turn_journal` 的三个事实列与附件索引；旧代码不认识它们，列可空、表无人读，
    **不影响旧代码读写**（这正是「只追加、不回填」的代价与边界）。
  - 若必须回到真正的旧结构，那是一次**人工数据迁移**（先把 34 之后写入的数据导出/备份，
    再重建旧结构库），不是 `git revert` 能做到的事；操作前先复制一份数据目录
    （`QIO_DATA_DIR`）验证。
- **升级建议**：先在数据目录副本上启动一次确认，再在真实数据上启动。

## 十二、未验证项与最终状态

**未验证（不粉饰）**

1. **Windows 桌面实机走查**（起应用、附件发送与中止、重发、历史打开、关闭清理、窄窗口视觉检查）
   本轮**仍未做**：本机可以跑组件级/后端用例，但没有完成桌面端（Tauri 壳）实机走查，
   组件测试**不能**替代实机结论。
2. **本轮分支 CI 未全绿**：run `38078800507` 的 `backend (windows-latest)` 失败在
   `test_health_probe_stays_responsive_while_slow_prediction_runs` 的 `gap_ms < 100` 阈值上
   （实测 104 ms；本机复跑该文件 13 passed / 4 skipped）。与本次两项修复无交集，
   按**负载敏感**记录，不用重跑掩盖。
3. **原合并 CI 的两个 Linux backend 任务**最终失败，但 GitHub 给出的原因是 runner 失联
   （基础设施），不是用例断言；本轮分支的同类任务在收尾时仍未结束。
4. 两处产品口径待确认（排队切话题时附件话题归属；「已受理但用户点过中止」的附件 chip 语义）。

**最终状态**

- 两处缺口已修并各有「修复前红 / 修复后绿」的对照装置；数据库升级路径（全新 / 旧 main 25 /
  main 30 / 来源流式线 28）与补偿幂等、数据保护都有真实业务行的断言。
- 未合并 main、未发布、未并入互动模式线；独立分支与 PR 以评审为准。
