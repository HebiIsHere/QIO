# 话题系统结构整理 R16 · 共享契约（2026-11-11 · 第五轮）

> 本文件由 Lead 冻结，是本轮**唯一共享契约**。A—F 全部角色先读完再动手；不得单方面修改。
> 需要变更接口 → 发消息给 Lead，由 Lead 裁决并更新本文件（修订记录见 §13）。
> 本轮只整理三个核心，不重做已完成的 F01—F14 / P1-01…05 / R1—R4；不扩展记忆算法、模型选择、
> 页面重设计或测试框架重构。

## 0. 基线与隔离

- 仓库 `HebiIsHere/QIO`；受检分支 `fix/topic-review-20261110`。
- 开工核实：`origin/fix/topic-review-20261110` = 本地工作树 = `390fca1f6991f7cb68d127685e71072040ee89a7`
  （ahead/behind 0/0），工作树干净，无后续修复。上一轮受测代码 `1830395` 是 390fca1f 的父提交。
- 集成工作树：`D:\qio-dev\qio-r14`（Lead 独占：合并、最终定向验证、git push、全量、CI）。
- 六条实现分支（各自 worktree，均由 390fca1f 拉出，`backend/.venv` 与 `frontend/node_modules`
  为共享 junction，不要删除/重装）：

| 角色 | worktree | 分支 |
| --- | --- | --- |
| A | `D:\qio-dev\r16-a` | `r16/a-position-state` |
| B | `D:\qio-dev\r16-b` | `r16/b-nav-commands` |
| C | `D:\qio-dev\r16-c` | `r16/c-frontend-position` |
| D | `D:\qio-dev\r16-d` | `r16/d-answer-execution` |
| E | `D:\qio-dev\r16-e` | `r16/e-verification` |
| F | `D:\qio-dev\r16-f` | `r16/f-independent` |

- 原始证据（日志/JUnit/截图/命令）写到 `D:\qio-dev\r16-evidence\<role>\`，**不要写进工作树**。
  工作树 `git status` 必须只包含你的实现/测试改动。
- 基线只读副本：`D:\qio-dev\r15-baseline`（detached 于 44d92fb3，旧代码）。只用于「基线红」证据，
  **不得**在基线副本改实现；F/E 的临时测试文件可放基线副本，不提交。

### 0.1 Git 与并行纪律

- 子智能体：只允许在自己的 worktree / 自己分支上 `git add/commit`；**禁止** push、切换分支、
  merge、rebase、建 worktree、触发 CI、改别人的 worktree。
- Lead：合并六条分支到 `qio-r14`、最终受测 SHA、唯一一次全量本地测试、唯一一次 CI。
- 同一时刻同一文件只有一个编辑者（§9）。需要改别人文件 → 发消息给 Lead，由 Lead 转交/裁决。
- 运行预算：同一时刻最多 3 个 pytest 进程；前端只跑相关 spec；禁止全量 pytest / `npm test` /
  全量 vitest / 生产构建 / 离线评测 / CI。

### 0.2 禁止事项（所有人）

- 不调用真实或付费模型、不联网、不写真实应用数据库；模型一律 fake/scripted provider。
- 不改历史迁移，只能追加新迁移；不新增 Python/前端依赖。
- 新日志/事件/错误文本必须过 `agent/trace/redact.py`（后端）或沿用脱敏字段（前端不显示密钥）。
- 不新增关键词 / 正则 / 包含匹配 / 相似度阈值式的自然语言意图识别；不新增第二模型授权层。
- 不用 `sleep` 构造并发/时序：用事件、屏障、受控时钟、测试缝。
- 不放宽/删除必须断言换通过；不清空草稿规避问题；不扩大 skip。

## 1. 不可变产品决定（逐条保留）

1. 所有用户消息完整进入主循环，由主模型判断「主动要求」或「QIO 建议」。
2. 不增加关键词、正则或第二个模型进行授权识别。
3. 切换与新建继续复用现有工具及导航服务（无第二套前端直建/直切执行器）。
4. QIO 建议在正式回答前确认；拒绝申请后继续回答原问题；停止整轮是另一动作。
5. 关闭弹窗仅收起，保留申请及名称。
6. 等待期间可写草稿，确认后跟随真实目标，不自动发送。
7. 原问题和正式答案只有一份真实归属。
8. 不合并 main、不打标签、不发布、不改变生产更新源。

## 2. 必须成立的规则 I01—I14（测试预期只能从这里推导）

规则由 Lead、E、F 共同确认；E 的参考模型与 F 的独立用例都必须能逐条判定，不能照当前实现写预期。

- **I01 旧请求不得覆盖用户后来的选择。** 任何导航命令/事件/恢复写入，只要其依据（授权快照的
  nav 决定版本、命令身份对应的 params、或事件所属位置状态版本）早于用户更新的导航决定，
  都不得修改当前位置/待接续，也不得把旧结果重新广播成成功。
- **I02 当前位置与待接续提示必须同源。** 前端一次可见的位置 + pending 只能来自同一份被接受的
  完整位置状态；后端事件、runtime 快照、HTTP 命令结果使用同源完整状态与版本，不得分别读取后拼接。
- **I03 正常推进必须正确。** 自动容量分段、片段补全（下一段接续）、已登记接续的落实，必须
  按转换表推进完整位置状态版本并产生可被前端接受的位置事件；不得把这些内部推进伪装成用户
  新导航决定，也不得无故让旧申请/恢复记录失效。
- **I04 重复请求不重复执行。** 同一命令身份 + 相同实际参数的重放：返回既有结果，零位置写入、
  零版本推进、零重复事件/成功广播。
- **I05 同身份不同参数冲突。** 同一命令身份被用于不同实际参数：明确冲突，零业务副作用
  （位置、待接续、版本均不变），不得返回旧结果冒充成功。
- **I06 旧状态不能部分覆盖。** 前端收到较旧完整位置状态：整份拒绝；相同版本相同内容：幂等、
  无副作用；相同版本内容冲突：不猜测、不部分合并，记录可观察事实并按既有恢复机制取权威状态；
  **禁止**「保留较新位置、又把较旧 pending 写进来」。
- **I07 重启不复活旧控制权。** 版本持久化、命令结果持久化；进程重启/实例变化后，旧请求 ID
  重放要么返回既有结果、要么按既有前提明确拒绝，绝不能重新执行或重新导航；旧实例事件不得
  仅因带有 nav 版本而绕过身份检查。
- **I08 依据不得事后重新包装。** 本轮判断依据在异步上下文装配前冻结；装配后只读复核，
  不得用「当前最新位置」回填依据；superseded 只让旧依据失效，不等于取消原问题。
- **I09 同一逻辑答案只有一个有效生成执行者。** answer_key 是唯一逻辑答案身份；恢复记录只是
  同一答案的入口，不得各自生成。
- **I10 已有生成结果时重试只补存。** 记录 A 生成成功但保存失败后，记录 B（同 answer_key，
  哪怕是另一条恢复记录）必须取得**共享暂存结果**并补存，不再调用模型；暂存正文只有一个权威来源。
- **I11 失去执行权的迟到写入全部拒绝。** 旧执行者的暂存、保存、成功收口、失败更新、释放
  均被写入权校验拒绝，零行改动；返回结构化 superseded。
- **I12 同一逻辑答案只有一份正式持久化结果。** answer_key 唯一；保存与成功状态原子提交；
  同 answer_key 的恢复记录一致收口；成功后返回相同 answer_id。
- **I13 恢复不改变导航、不抢占新轮次、不覆盖草稿。** 恢复只重做未完成步骤；不推进导航决定
  版本；不移动当前位置；不覆盖用户等待期间的新编辑。
- **I14 正式回答必须满足归属与上下文前提。** 原轮次/原用户消息/固定片段/本轮绑定一致，
  且与正式保存的 answer_key 归属一致；否则不得作为正式回答落库。

## 3. 完整位置状态（唯一权威）与两类版本

### 3.1 完整位置状态定义

一份完整位置状态 = 以下事实的**同源读取**（后端唯一权威；不新建第二份 Anchor/pending 权威）：

- `topic_id` / `fragment_id`：`cursor` 表 active 行（AnchorService，唯一写者仍是统一入口）；
- `historic`：现有 `AnchorService.is_historic_position` 语义（历史位置 = focus 片段非开放）；
- `pending_intent_id / pending_intent_version / pending_source_fragment_id`：`continuation_intents`
  中唯一 `registered` 选择（沿用现有存储，不复制）；
- `nav_version`：`navigation_version` 单行表（导航**决定**版本）；
- `position_version`：本轮新增单行表 `position_state`（完整位置状态版本）；
- `position_identity`：数据库身份（复用 `storage/db_identity.py::ensure_instance_id`，即
  `settings['db.instance_id']`）；
- 因果信息：`position_state.last_source / last_turn_id / last_operation_id / last_nav_version / updated_at`。
- 派生展示字段：`topic_name` / `fragment_title`（同一读取内补全，只用于展示）。

**wire 形状（冻结）**：`position` 对象（事件/快照/HTTP 结果通用，snake_case）：

```json
{"topic_id": "...", "topic_name": "...", "fragment_id": null, "fragment_title": null,
 "historic": false,
 "pending_intent_id": null, "pending_intent_version": null,
 "pending_source_fragment_id": null, "pending_source_title": null,
 "nav_version": 12, "position_version": 7, "position_identity": "db-..."}
```

ANCHOR 事件额外保留既有扁平字段（topic_id/topic_name/fragment_id/fragment_title/historic/
pending_*/origin/operation_id/param_version/turn_id/approval_id/nav_version/seq/instance_id），
并新增 `position_version`、`position_identity`、`command_id`、`position`（上表整份）。
`GET /api/runtime/state` 的 `anchor` 字段 = 同一份 `position` 对象（含 position_identity）。
后端任何位置广播/快照必须用同一函数生成这份 wire 对象，不得分别读位置与 pending 拼接。

### 3.2 两类版本（职责严格分开）

**导航决定版本 nav_version（沿用 navigation_version 表，持久化）**
- 只表示「用户/工具的**真实导航决定**」；真实新选择推进一次；重放、渲染、草稿编辑、弹窗收起、
  自动分段、内部片段细化不推进。
- 用于判断旧申请/旧命令是否失效（CAS）。用户后来浏览/改选使旧依据失效，但不取消原问题。

**完整位置状态版本 position_version（新表 position_state，持久化）**
- 用于排序完整位置状态；**任何真实位置状态变化**推进一次：话题变化、片段变化、historic 变化、
  待接续意图出现/替换/取消/落实/消费、开放片段因自动分段/片段补全而变化。
- 无变化、幂等重放、同值响应不推进。
- 进程内 seq 只作辅助（同实例乱序兜底），不承担跨重启排序。
- 数据库身份变化（库被整体替换）时，只能由**权威重建流程**（启动时
  `PositionStateService.ensure_identity()`）把 position_version 归零并采用新 `db_identity`；
  实时事件/命令不得自行切换身份。

### 3.3 状态转换表（冻结；E 参考模型按此建）

| kind | 触发 | Anchor | 待接续 | nav_version | position_version | 命令身份 |
| --- | --- | --- | --- | --- | --- | --- |
| `enter_topic` | 用户进入话题最新位置 / 工具 switch 成功 | 目标话题 + 目标片段（None=最新，**不恢复旧位置**） | 取消不兼容的旧 registered（若有） | 真实变化或有 pending 被取消时 +1；否则 0 | 有任一事实变化 +1 | request:<请求ID> / operation:<opID>:<pv> |
| `select_history` | 用户从封存历史继续（点击）/ 工具 continue 登记 | 目标话题 + 目标历史片段（登记态，不创建片段） | 登记/替换 registered 意图 | 选择真实改变 +1；重放 0 | 有变化 +1 | request / operation |
| `open_fragment` | 用户明确返回当前开放片段 | 目标话题 + 当前开放片段 | 结束不再适用的旧 registered | 有 pending 被取消或位置变化 +1；否则 0 | 有变化 +1 | request |
| `create_topic` | 工具 create 成功 | 新话题 + None | 取消不兼容旧 registered | +1（真实新话题） | +1 | operation |
| `apply_continuation` | 轮首落实已登记接续（机械落实） | 目标话题 + 落实后的真实片段（复用或新建） | registered → resolved | **0**（决定在登记时已计） | +1（片段/状态真实变化） | internal:apply:<turn>:<intent> |
| `internal_refine` | 轮首补锚点、轮末推进到本轮真实片段、边界策略细化 | 目标话题 + 目标片段 | 成功收尾消费 intent 时 consume（+position 变化） | **0** | 有变化 +1 | internal:refine:<turn>:<fragment> |
| `auto_segment` | 容量分段封存开放片段（post_turn） | 不变（该片段转为 historic 即真实变化） | 不变 | **0** | +1（开放片段/历史状态变化，记 causal：sealed_fragment_id） | internal:segment:<turn>:<sealed> |
| `cancel_continuation` | 用户取消未落实接续 | 不变 | registered → cancelled | **0**（取消不是新的导航选择，不使旧申请失效；与既有 r15-b 断言一致） | 有变化 +1 | request:<请求ID> |
| recovery | 恢复入口 | **从不导航**（只读复核） | 不变 | 0 | 0 | 不走位置转换 |

补充冻结：
- `historic` 只在同一状态下按现有语义计算，不允许转换表把历史片段改写成「当前开放」。
- 边界策略（stage_change）的封存+新建子片段按 `auto_segment + internal_refine` 两条转换分别记账；
  不得合并成一次「用户导航」。
- 同轮工具导航：只用本次执行返回的权威 `position` 更新依据（source=`tool.<name>` 的 command
  结果），不得在 await 后重读当前位置冒充执行结果。
- 恢复入口（topic_recovery）对位置只读；不得调用任何位置转换。

## 4. 统一变更入口（后端唯一）

新增 `agent/services/position_state.py`（A 实现，B/C/D 只按接口调用）：

```python
POSITION_STATE_TABLE = "position_state"
NAVIGATION_COMMANDS_TABLE = "navigation_commands"

class PositionStateService:
    def __init__(self, conn: sqlite3.Connection) -> None: ...
    def read(self) -> dict: ...                    # 完整位置状态 wire 对象（含派生标题/identity）
    def ensure_identity(self) -> bool: ...         # 仅启动期权威重建；返回是否重建
    def apply(self, transition: "PositionTransition") -> "CommandResult": ...   # 统一变更入口
    def replay(self, identity_key: str, param_digest: str) -> "CommandResult | None": ...  # 只读
```

**所有生产位置写入必须且只能经 `PositionStateService.apply()`。** 以下底层/业务入口全部改为
其薄包装（保留函数名与既有返回形状以兼容调用方/测试，但内部不再各自写 Anchor/intent/版本）：
`TopicNavigationService.enter_topic/register_continuation/register_and_position_continuation/
apply_continuation/create_topic/cancel_continuation`、`turn_orchestrator.begin/advance_anchor/
_apply_boundary_policy/post_turn`、`memory_lifecycle.seal_fragment` 的位置副作用、
`/api/anchor`、`/api/anchor/continue/cancel`、工具 `switch_topic/create_topic/continue_from_fragment`。
`AnchorService` / `TurnBindingService` 的底层写方法保留，但业务层不得绕过统一入口直接调用；
本轮删除已无生产入口的旁路（如 `request_switch/confirm_switch/reject_switch` 若确认无生产调用，
连同 `/api/topic-switch/*` 的 410 占位保留策略由 B 盘点后报告，不新增第二套入口）。

`PositionTransition` 字段（冻结）：
`kind`（上表取值）、`source`（稳定来源字符串，如 `manual.enter_topic`/`tool.switch_topic`/
`turn.advance_anchor`/`capacity.segment`）、`target_topic_id`、`target_fragment_id`、
`base_nav_version: int | None`（CAS；None=无依据的用户新选择）、
`expected_position_version: int | None`（可选 CAS，用于旧调度细化）、
`pending`（keep|cancel|register|resolve|consume）、`pending_topic_id/pending_fragment_id/
pending_request_id`、`nav_decision: bool`（必须与 kind 的冻结值一致，服务层校验）、
`initiator`（user|tool|internal|recovery）、`identity: NavCommandIdentity`、
`turn_id/approval_id/operation_id/param_version`、`causal: dict`（如 sealed_fragment_id）、
`result_extra: dict`（命令结果附加事实，如 changed 的语义化说明）。

`apply()` 在**同一 SQLite 事务**内完成：校验命令身份/参数摘要 → 校验 CAS/前提 → 写 Anchor →
写/清 pending intent → 推进 nav_version（如适用）→ 推进 position_version（如适用）→ 写命令结果行。
任何一步失败整个事务回滚（拒绝类命令的「零业务副作用 + 命令结果留痕」允许在回滚后以独立事务只写
命令拒绝行）。**事务提交后才发布事件**；调用方用 `CommandResult.position` 直接发布，不再二次读取。

## 5. 导航命令身份与幂等（覆盖所有导航动作）

新增持久表 `navigation_commands`（迁移 83，A 建）：

```sql
CREATE TABLE IF NOT EXISTS navigation_commands (
  command_id     TEXT PRIMARY KEY,
  identity_key   TEXT NOT NULL,      -- request:<id> | operation:<opID>:<pv> | internal:<kind>:<scope>
  identity_kind  TEXT NOT NULL,      -- request | operation | internal
  command_kind   TEXT NOT NULL,      -- enter_topic | create_topic | select_history | open_fragment | apply_continuation | internal_refine | auto_segment | cancel_continuation
  param_digest   TEXT NOT NULL,      -- 实际执行参数规范摘要（精确：topic/fragment/name/restore 等）
  request_id     TEXT, operation_id TEXT, param_version INTEGER,
  status         TEXT NOT NULL,      -- applied | rejected | superseded | conflict
  reason         TEXT NOT NULL DEFAULT '',
  result_json    TEXT NOT NULL DEFAULT '{}',
  position_json  TEXT NOT NULL DEFAULT '{}',   -- 命令结果时的 position wire 快照
  nav_version    INTEGER, position_version INTEGER,
  initiator      TEXT NOT NULL,
  turn_id        TEXT, approval_id TEXT,
  created_at     TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_navigation_commands_identity ON navigation_commands(identity_key);
CREATE INDEX IF NOT EXISTS idx_navigation_commands_created ON navigation_commands(created_at);
```

身份规则（冻结）：
- `request:<client_request_id>`：手动导航（进入话题/返回开放片段/选历史/取消接续）。
  客户端每次**真实用户动作**生成新的 UUID request_id；同一次动作的所有网络重试复用同一 ID。
- `operation:<operation_id>:<param_version>`：工具 switch/create/continue 的真实执行身份。
  复用既有 `services/topic_operation.py` 的 operation_id / param_version；参数实质变化推进
  param_version（旧身份不再可用）。同一 operation + param_version 的不同 param_digest =
  数据损坏/越权，按 I05 冲突拒绝。
- `internal:<command_kind>:<scope>`：自动分段/内部细化/落实接续。scope 确定性（如
  `<turn_id>:<fragment_id>`），不得每调用一次换新 ID；内部动作有明确前提（expected_position_version
  或事实校验），不得借「不是用户操作」绕过一致性校验。
- 已失效/被替换的请求：重放其身份 → 返回既有 rejected/superseded 结果或明确拒绝，
  **绝不**重新获得位置控制权。
- 保留策略：request/operation 身份行**永久保留**（低频、保证旧 ID 不被当成新请求重新执行）；
  internal 行可按有界策略清理（保留最近 2000 条，且只删 30 天前的 internal 行），清理前写审计日志。
- 参数摘要 `param_digest` 必须是**精确执行语义**（名称只外层 strip；topic/fragment id 原样），
  不得用宽松归一化/相似度参与身份。

命令结果与 HTTP/工具返回（冻结字段，均可在既有字段上追加）：
`changed: bool`、`replay: bool`、`inactive: bool`、`conflict: bool`、`code: str | null`、
`command_id: str`、`nav_version: int`、`position_version: int`、`position: {...}`。
- 命中身份且参数相同：`replay=true, changed=false`，返回既有 result + **当前** `position`（只读），
  零写入、零版本推进、零 ANCHOR 广播。
- 命中身份但参数不同：409 `navigation_command_conflict`（或既有 `continuation_request_conflict`），
  零业务副作用。
- 命中 rejected/superseded：返回既有拒绝事实（如 409 `continuation_request_inactive` /
  `topic_application_stale`），零写入。
- 真实新命令成功：按 §3.3 推进，广播一次 ANCHOR（携带同源 complete position），返回 changed=true。

开放片段选择同样必须有命令身份记录（不能因为「不创建接续意图」就没有幂等依据）：
`open_fragment` / 进入当前话题最新位置均登记 `request:<id>` 命令行。

## 6. 逻辑答案执行权、共享暂存与最终保存

沿用 `topic_answer_runs`（迁移 81）为执行权权威；本轮**只追加**迁移 83 列：

```sql
ALTER TABLE topic_answer_runs ADD COLUMN phase TEXT NOT NULL DEFAULT 'waiting';
ALTER TABLE topic_answer_runs ADD COLUMN staged_answer TEXT NOT NULL DEFAULT '';
ALTER TABLE topic_answer_runs ADD COLUMN staged_at TEXT;
ALTER TABLE topic_answer_runs ADD COLUMN staged_model TEXT;
```

- `phase` 冻结取值：`waiting`（等待处理）→ `preparing`（归属/上下文装配）→ `generating`（模型生成中）
  → `staged`（已生成、保存失败待补存）→ `answered`（正式保存）；`failed`（可恢复失败，仍可重试）。
  phase 与 status 同步写入；status 继续承担既有 pending/running/answered 语义。
- **共享暂存唯一权威 = run 行的 staged_answer/staged_at/staged_model**（按 answer_key）。
  记录行的 `answer_text` 不再作为新暂存的权威：新代码只写 run 行；旧记录残留 answer_text 仅作
  迁移期只读兜底（读到后转写 run 行并在记录上清空）。不得给每条恢复记录复制正文。
- 重试入口（任意 record，同 answer_key）：先查 messages 唯一答案 → 再查 run.staged_answer →
  再查旧 record.answer_text（兜底）→ 都没有才允许生成。补存成功后清空 staged_answer 并置 phase=answered。
- 保存事务：校验执行权 → 唯一答案写入/复用 → 所有同 answer_key pending 记录 answered → run
  phase=answered、staged 清空。旧执行者迟到写全部 superseded（I11）。
- 恢复入口不推进 nav_version、不移动 Anchor、不改 pending continuation（I13）；只读复核恢复记录
  的 base/recovery nav 版本。
- 不声称外部模型调用严格 exactly-once；崩溃在模型返回与结果持久化之间仍可能再次调用，文档与
  结果文案如实写明。

## 7. 前端统一接受完整状态（C 实现）

### 7.1 唯一入口

前端 session 新增：
- 状态：`_positionFacts: {topicId, fragmentId, historic, navVersion, positionVersion, positionIdentity,
  pendingIntentId, pendingIntentVersion, pendingSourceFragmentId, pendingSourceTitle,
  instanceId, seq, source} | null`；`_positionStateConflict: {...} | null`（可观察）；
  `_draftFollowConsumed: string[]`（有界 200）。
- 方法 `acceptPositionState(input, source: "anchor" | "snapshot" | "command"): {
  accepted, reason, idempotent, conflicted, adopted }`。
- 事件层 ANCHOR、`/api/runtime/state` 快照、以及**所有导航 HTTP 响应**的位置事实都必须走它；
  业务代码（PlanetView 等）成功后不得直接 `setAnchor`/`setAnchorAndSync`，而是把响应里的
  `position` 交给接受入口。`setAnchor` 保留为底层视图赋值，只允许被接受入口调用（测试/兼容）。
- 兼容：旧后端无 position_version 时，允许 ANCHOR 走旧 nav_version 规则（仅兼容路径，文档标注
  为「旧后端兼容」，新测试只测新入口）。

### 7.2 整份接受规则（I06）

输入按 position identity/version 排序：
1. 无本地 facts → 采用（baseline）。
2. `positionIdentity` 与本地不同：
   - 来自权威快照（runtime state / 重连恢复）→ 以新身份重建基线并采用整份状态；
   - 来自实时事件/旧命令响应 → 拒绝 `stale_position_identity`，触发一次权威恢复（restoreRuntimeState），
     不得让旧身份事件改状态。
3. `positionVersion < 本地` → 整份拒绝 `stale_position_state`（不写 pending、不迁移草稿）。
4. `==`：比较规范内容（topic/fragment/historic/pending*/nav_version）：
   - 完全相同 → 幂等接受、零副作用；
   - 不同 → 拒绝并记录 `_positionStateConflict`（含两版内容与来源），触发一次权威恢复；
     **不猜测、不部分合并**。
5. `>` → 整份采用：先写位置 facts，再整份写 Anchor 视图与 pending，最后按草稿跟随规则处理；
   位置变化且非运行中时触发历史重载。
- 旧实例（instance_id 不同）事件不得绕过上述检查；seq 只在同实例、同 identity、缺版本时兜底。
- 禁止「保留较新位置 + 写入较旧快照 pending」的拼接。

### 7.3 草稿跟随（独立幂等消费身份）

保留既有中央弹窗、收起重开、拒绝后回答、发送限制；不新增第二套页面/话题操作面板。
草稿跟随由一个稳定消费身份控制：`follow:<operation_id>:<param_version|approval_id>`（有界记录）：
- 仅**本次操作**的权威成功结果（工具导航命令结果 / APPROVAL_RESULT executed 携带的 position）
  可迁移草稿一次；重复结果命中已消费集合 → 零副作用。
- 无关手动导航不消费跟随；旧操作结果（身份不匹配）不搬草稿；被用户更新选择替代的结果
  （其 position_version 旧于当前已接受状态）不搬草稿并清除跟随。
- 重连快照先到、操作结果后到：快照采用位置时**不得**按普通导航规则清掉已武装的跟随/等待草稿；
  等权威操作结果到达后按上述身份规则迁移一次；位置恢复本身不许覆盖等待期间的新编辑。
- 目标已有草稿继续受既有保护（让位/恢复语义不变），始终不自动发送。

## 8. 迁移、存储与兼容

- 只追加迁移 **83**（新表 `position_state`、`navigation_commands` + `topic_answer_runs` 追加列）
  与补偿 **84**（重放 82 全部语句 + 83 建对象语句）；按仓库既有口径 `SCHEMA_VERSION = COMPENSATION_VERSION = 84`（补偿版本 = max(MIGRATIONS 版本)，与既有 76/78/80/82 一致）；
  `migrate.REQUIRED_OBJECTS` 追加新表/列/索引。历史迁移一条不改。
- `position_state` 单行种子 `(id=1, position_version=0, db_identity='', last_source='')`；
  启动期 `ensure_identity()` 用 `db_identity.ensure_instance_id` 对齐/重建（权威流程）。
- 旧库升级：migrate 自动补对象；旧恢复记录无 run 行时按既有「无执行/可接管」处理；旧
  record.answer_text 只作只读兜底。不复制、不清空真实用户数据。
- 新旧切换后不长期双写：新代码不得再以 record.answer_text 为主写暂存；`_publish_anchor_event`
  不得再分别读位置与 pending 拼接。

## 9. 文件所有权与共享文件

| 角色 | 独占实现文件（相对仓库根） |
| --- | --- |
| A | `backend/src/agent/services/position_state.py`（新）、`services/navigation.py`、`services/navigation_version.py`、`services/memory_lifecycle.py`、`core/turn_orchestrator.py`、`services/app.py`、`storage/migration_r16_position_state.py`（新）、`storage/schema.py`、`storage/migrate.py` |
| B | `backend/src/agent/api/server.py`、`services/binding.py`、`services/topic_operation.py`、`agent/tools/topic_tools.py`、`agent/tools/continue_tool.py`、`services/topic_application.py`（仅导航命令接缝） |
| C | `frontend/src/stores/session.ts`、`stores/events.ts`、`stores/restore.ts`、`services/api.ts`、`views/PlanetView.vue` |
| D | `backend/src/agent/services/topic_recovery.py`、`api/recovery_routes.py` |
| E | 新建 `backend/tests/test_r16_e_*.py`、`frontend/src/__tests__/r16-e-*.test.ts`（+ 参考模型模块放 backend/tests 下，不碰生产文件） |
| F | 新建 `backend/tests/test_r16_f_*.py`、`frontend/src/__tests__/r16-f-*.test.ts`（不碰生产文件） |
| Lead | `docs/*`、集成、最终验证；不代写实现 |

**共享文件唯一编辑者**：`storage/schema.py`、`storage/migrate.py`、迁移文件 = A 唯一编辑；
D 把所需 DDL 列要求发给 A（A 已按 §6 冻结），D 不改 schema；B 需要 A 的接口 → 按 §4/§5 调用，
不改 A 的文件；C 不得改后端；A/B 不得改前端。共享文件冲突 → 消息 Lead 裁决。

**提交信息**：中文、范围前缀（如 `feat(r16-a): 统一完整位置状态…`），只提交自己文件。
每个角色开始前先在自己的 worktree 读本契约与相关实现，先写最小失败用例再实现（TDD）。

## 10. 验证义务（分角色）

### 10.1 通用

- 定向命令（主工作树换成自己的 worktree）：
  后端 `cd <worktree>\backend; $env:PYTHONPATH='<worktree>\backend\src'; .\.venv\Scripts\python.exe -m pytest tests/<file> -q -p no:cacheprovider`；
  前端 `cd <worktree>\frontend; npx vitest run src/<spec>`；类型检查 `npx vue-tsc --noEmit`（交付前一次）。
- 先建立能红的最小场景（记录基线红证据），再实现到绿；并发用事件/屏障/受控时钟/测试缝。
- 不跑全量、不跑构建、不触发 CI、不 push。

### 10.2 必须覆盖的具体回归（A—H，来自用户任务书）

A. 正常自动分段后，真实后端事件能推进前端片段（后端 auto_segment/internal_refine + 前端接受）。
B. 较新位置和 pending 已接受后，旧快照三者（位置/历史标记/pending）都不能覆盖。
C. 导航版本相同时，旧实例和旧序号不能修改状态（identity/position_version 排序）。
D. 开放片段 A → 历史 B → 重放原请求 A：B 保持不变（命令重放 + 服务层零写入）。
E. 真正返回开放片段时，不残留指向旧历史的接续选择（open_fragment 取消 pending）。
F. 装配期间改选，旧依据不覆盖新选择（I08；既有 R1 回归保持）。
G. 记录 A 暂存成功但保存失败，记录 B 只补存、不再生成（共享暂存）。
H. 多记录并发、租约续期、失效接管及旧执行者迟到写入（执行权 + staged 共享 + superseded）。
I. 旧问题恢复与新轮次、当前草稿互不干扰（恢复只读导航 + 前端草稿保护）。

### 10.3 E（模型化 + 真实接线）

- 独立参考模型（纯状态机，不调用生产转换函数、不复制生产分支）：按 §2 规则与 §3.3 转换表
  判定期望；生成自动组合操作序列（选择历史/返回开放/切换新建/自动分段/片段补全/确认拒绝失效/
  重放与参数冲突/快照事件乱序/断连重连实例变化/生成失败保存失败/恢复续租接管），**固定并保存
  随机种子**；每步断言规则，不只断言最终页面/计数；失败序列缩减为最短反例并转稳定回归。
- 真实 HTTP（`TestClient` + 临时 SQLite）：所有导航命令身份/重放/冲突/失效；**至少两个独立
  数据库连接**的竞争场景（同一命令并发，只有一个执行/一份写入）。
- 有界真实进程退出/接管验证（复用既有 InstanceRegistry 机制，不无限等）。
- 真实前端 store + 事件消费；至少一条本地 fake/scripted 模型驱动的真实事件流集成路径。
- 必要的实际界面核对（能用现有脚本/受控服务完成的最小事实核对；做不了就如实写 NOT VERIFIED）。
- 记录能力边界：模拟事件不是浏览器端到端；fake 模型不是中文理解准确率验证。

### 10.4 F（独立复现 + 评审）

- 从规则出发写独立用例（不参考 A—D 的实现细节），先在基线只读副本得到「红」，再在集成
  工作树获得「绿」；逐条覆盖 I01—I14 与 A—I 回归。
- 评审三个核心：生产写入口是否全部归入统一规则（列出清单并核对无旁路）、两类版本职责是否
  分开、命令重放/失效/参数冲突、前端整份接受、共享暂存、恢复边界；发现问题发回 Lead，
  不直接改别人文件。

## 11. 汇报格式（子智能体 → Lead，send_message）

1. 角色/状态（完成/受阻/部分）；2. 分支 + 本地 commit SHA 列表；3. 改动文件精确清单；
4. 逐条行为变化 + 最小复现（测试名/命令）；5. 定向命令、通过/失败数、exit code、证据路径；
6. 与契约的偏差（必须显式）；7. 未完成/已知限制/集成接缝。

## 12. 本轮硬约束（全角色）

- 实现、定向检查、集成、独立评审全部完成后，Lead 才冻结最终受测 SHA。
- 只允许**一次**全量本地测试、**一次**完整 CI workflow run；由 Lead 在固定 SHA 的独立工作目录
  启动，启动后只核验「任务真实存在、受测 SHA 正确、日志开始产生」，不等待完成、不反复轮询。
- 子智能体不触发 CI、不跑全量；响应后不要求长验证结束。
- 不合并 main、不打标签、不发布、不改变生产更新源。

## 13. 修订记录

- v1（Lead 初稿）：§0—§12 冻结基线、规则、转换表、接口、迁移 83/84、所有权、验证义务。
  待 E/F 审阅后出 v2（仅允许澄清/收窄，不改变用户任务书要求）。

## 14. v2 冻结 delta（2026-11-11，F/E 预审后；与上文冲突处**以本节为准**）

### 14.1 CommandResult 与 Python 侧接口（B1/B2/B5）
````python
@dataclass(frozen=True)
class CommandResult:
    command_id: str          # 冻结：command_id == identity_key（确定性、跨重启可复现）
    identity_key: str
    status: str              # applied | rejected | superseded | conflict
    changed: bool
    replay: bool
    inactive: bool
    conflict: bool
    code: str | None         # 结构化拒绝码；成功为 None
    reason: str              # 人读原因（已脱敏）
    nav_version: int
    position_version: int
    position: dict           # §3.1 wire 完整状态（命令结果时同源快照）
    result: dict             # 命令结果的附加事实（result_extra 合并）
````
- PositionStateService 额外冻结：
  - `param_digest(transition) -> str`（纯函数，精确执行参数规范摘要；E/F 只读使用）；
  - `replay(identity_key, param_digest) -> CommandResult | None`（只读；不写任何行）；
  - `lookup(identity_key) -> dict | None`（只读命令行事实，供诊断/测试）。
- 同 identity_key + 相同 digest 的重放返回**存储时**的 result 与当前只读 position 组合：
  `replay=True, changed=False`，`position` 为**当前**完整状态（不推进版本、不广播）。

### 14.2 手动导航的请求依据（B4/C1；I01 收窄）
- `/api/anchor`（两分支）与 `/api/anchor/continue/cancel` 冻结字段：`request_id`（必填，UUID），
  `base_nav_version`（int，**用户手动导航必填** = 客户端最近已接受的 nav_version；缺失 → 400
  `navigation_basis_required`）；工具/审批路径用 operation_id/param_version/base_nav_version；
  内部转换用 `expected_position_version`（int，必填）或显式事实前提。
- 前端手动导航必须**单飞串行**：同一时刻只有一个手动作业在飞（后点排队，拿到新 base 再发）；
  每次真实用户动作新 request_id，网络重试复用同一 id。旧请求迟到且 base 已过期 → 409
  `navigation_command_stale`（零写入、零广播）。
- 关闭/收起/渲染/草稿编辑不得发导航命令。

### 14.3 前端 acceptPositionState 冻结输入/返回（B3/C6）
- 输入形状 = §3.1 position wire 对象（snake_case 全套）+ 可选 `instance_id`、`seq`；第二个参数
  `source: "anchor" | "snapshot" | "command"`。
- 返回 `{ accepted: boolean; reason: string; idempotent: boolean; conflicted: boolean; adopted: boolean }`。
- reason 码冻结：接受 `baseline` / `new_position_state` / `new_position_identity` / `idempotent`；
  拒绝 `stale_position_state` / `stale_position_identity` / `same_version_conflict` / `no_input` /
  `no_topic`；旧后端兼容路径 reason=`legacy_nav_order`（只允许 ANCHOR 事件，且文档标注）。
- 同版本内容比较**排除**派生展示字段（topic_name/fragment_title/pending_source_title）与
  instance_id/seq；精确比较：topic_id、fragment_id、historic、pending_intent_id、
  pending_intent_version、pending_source_fragment_id、nav_version。
- 权威恢复 single-flight：冲突/身份不一致触发 `restoreRuntimeState("position-state-conflict")`；
  已在飞时合并丢弃（不并发、不风暴），本轮其余位置状态按现有 resync 缓冲补放。

### 14.4 命令身份/事务语义补充（B6/B7/C5/C7）
- 本轮 **不清理任何** navigation_commands 行（external 与 internal 都保留；低频、体积可控）。
  未来清理必须带 tombstone 或由前提拒绝，不得让旧 ID 重新执行。
- 同身份不同参数（I05）：**零写入**（不写新行），返回 conflict + 既有 command_id + 当前 position。
- 首次出现的 rejected/superseded 身份：允许在业务回滚后以独立小事务**只写命令行拒绝事实**
  （零位置副作用），供重放返回既有拒绝。
- 工具 continue 返回开放片段：身份 = `operation:<opID>:<pv>`，不得复用 manual request。

### 14.5 转换表澄清（C2/C3/C4 + 工具 continue）
- select_history 增加 `move_anchor` 语义：**点击路径** Anchor 落历史片段并广播；**工具登记路径**
  只登记/替换 pending，不动 Anchor、不广播位置事件（既有 test_continue_fragment 断言保持）；
  两者 changed 时 nav_version +1、position_version +1（pending 变化）。
- 完整位置状态内部事实还包括**当前话题的开放片段 id**：open_fragment_id 变化属于真实状态变化。
  因此 auto_segment（封存开放片段）**一律 position_version +1**（causal 记 sealed_fragment_id），
  §3.2 与 §3.3 的表述按此统一；后续 create/refine 再各计真实变化。
- 旧 registered 意图的兼容判定冻结为：**任何真实用户/工具导航决定**（enter_topic/open_fragment/
  create_topic/工具 switch、cancel_continuation）都清除全局唯一 registered 意图（计入 nav_version
  +1 的「pending 被取消」条件）；select_history 以新登记替换旧登记；internal_refine/apply_continuation/
  auto_segment 不改 pending（apply_continuation 只把自己那条 registered→resolved）。
- enter_topic 的 `target_fragment_id=None` 如实表示存储 NULL（未物化话题最新位置），读取端
  **不得**擅自解析成 open_fragment；historic 按现有语义计算。

### 14.6 逻辑答案补充（B8/C8/C9）
- `topic_answer_runs` 以 answer_key 为唯一行（PK），不存在多 run 行歧义；staged_answer/staged_at/
  staged_model 只写这一行。保存成功同事务：清 staged_*、phase=answered、status=answered、
  所有同 answer_key pending 记录 answered（相同 answer_id）。
- 迁移 83 回填 phase：`UPDATE topic_answer_runs SET phase = CASE status WHEN 'answered' THEN
  'answered' ELSE 'waiting' END WHERE phase = 'waiting'`（仅追加迁移内执行，不改历史迁移）。
- 暂存命中顺序冻结：messages 唯一答案 → run.staged_answer → 旧 record.answer_text 只读兜底
  （同事务转写 run 并清 record）。
- I14 结构化拒绝码冻结：`answer_attribution_mismatch`（固定片段/话题/原消息归属不一致）与
  `answer_context_missing`（上下文装配为空），均 `recoverable=true`、HTTP 503、零正式答案行；
  F 用例按此断言。
- 迟到写判定顺序（I11）冻结：run 行不存在 / run.status != running / generation 不匹配 / owner
  不匹配 / 记录 status != pending / 记录执行令牌不匹配 → 任一成立即 `recovery_execution_superseded`
  （`superseded=true`、HTTP 409、零行改动）；租约未到期不得接管，接管需 owner_alive is False。

### 14.7 角色边界与测试归属补充
- F 可以调用生产 `PositionStateService.apply()`/HTTP 端点作为**被测系统**断言 I04/I05/§3.3；
  「不调用生产转换函数」只约束 E 的参考模型（模型不得 import 生产转换代码）。
- 既有测试的精确字段断言（如 RUNTIME_ANCHOR_FIELDS、ANCHOR payload 字段集）由该测试所属
  角色最小更新，不改断言语义；跨角色测试文件冲突发 Lead。
- A 冻结接缝：`AppContext.position_state`（PositionStateService 实例）；
  `_publish_anchor_event(position: dict | None = None, command_id: str | None = None, **legacy)`；
  `NavigationResult` 追加 `changed: bool=False`、`replay: bool=False`、`command_id: str|None=None`、
  `position: dict|None=None`（保留既有字段与默认值，旧调用方不破）。
- B 冻结消费：`ctx.position_state.read()` 是 runtime 快照/HTTP 响应唯一来源；事件用 A 传入的
  position 对象，不再第二遍读 Anchor。

### 14.8 v2 追加冻结（F B9/B10、C 语义确认）
- NavCommandIdentity 冻结：
````python
@dataclass(frozen=True)
class NavCommandIdentity:
    kind: str                 # request | operation | internal
    identity_key: str         # request:<id> | operation:<opID>:<pv> | internal:<command_kind>:<scope>
    request_id: str | None
    operation_id: str | None
    param_version: int | None
    internal_scope: str | None
````
  apply() 必须校验 kind 与 identity_key 前缀一致；command_id == identity_key。
- I05/B7 最终口径：同身份不同参数 → 零写入、**不新增行**，返回 conflict + 既有 command_id + 当前 position；
  rejected/superseded 行只在**该身份首次被处理且被拒**时写一次（含 CAS 失败的全新命令）。
- 手动导航 base_nav_version 必填（B10，最终）：缺失 → 400 navigation_basis_required；不匹配 → 409
  navigation_command_stale。客户端总能取得最近接受的 nav_version，缺失只可能是旧客户端/旧页面。
- 前端 acceptPositionState reason 码以 §14.3 为准（不使用 adopted/position_state_conflict 等别名；
  adopted 是布尔字段，不是 reason）。
- 同版本同内容幂等接受时：位置/视图/pending 零变化，但匹配本次操作身份的草稿跟随**仍可消费迁移一次**；
  草稿迁移身份与位置版本新旧是两件事。
- 快照先于操作结果到达：采用位置时不得让等待草稿从输入框消失、不得覆盖等待期新编辑；实现可用
  「跟随武装期间抑制 draft keeper 的普通 bind 切换」等内部机制，结果到达后按操作身份迁移一次；
  若输入框必须临时绑定目标话题草稿，只能「存储不丢、不删除」，并在报告中写明该边界。
- 审批 respond 响应的 position：前端消费点为 APPROVAL_RESULT 事件（B/A 保证事件携带 position）；
  unifiedApprovals.ts 若必须做最小透传，由 C 实施并在报告声明（仅透传，不改审批状态机），冲突时先问 Lead。
### 14.9 v2 追加裁决（A 报的两处硬冲突）
- cancel_continuation：**nav_version 不推进（0）**（取消不是新的导航选择，不使既有申请失效；既有 tests/test_r15_b_r3_anchor_replay.py:412 断言保持），position_version 在 pending 事实变化时 +1。open_fragment 若取消了旧 pending，按「真实导航决定」nav +1。
- 迁移版本按仓库既有口径：追加迁移 83 + 补偿 84，SCHEMA_VERSION = COMPENSATION_VERSION = 84（补偿版本 = max(MIGRATIONS 版本)）；REQUIRED_OBJECTS 同步。

### 14.10 v2 追加（B：既有测试/文件授权）
- 既有 anchor 路由测试的 request_id+base_nav_version 适配归属：B 改 test_r15_b_r3_anchor_replay.py、test_anchor_cancel_api.py、test_anchor_event.py、test_focus.py、test_topic_navigation.py 及其它 anchor 路由/取消相关既有测试；F 改 test_r14_f_navigation.py、test_r15_f_independent.py；E 改 test_r15_e_integration.py。断言语义不放宽，只补请求身份/依据与同 ID 重放。
- 授权 B 最小修改 backend/src/agent/tools/approval_results.py 与 backend/src/agent/tools/approval.py：仅把命令的 command_id/position/position_version/nav_version/changed 透传到 APPROVAL_RESULT 事件（navigation 参数），不改审批状态机。
- NavCommandIdentity 以 §14.8 冻结形状为准（A 已确认按此实现并同步 B）。

### 14.11 v2 追加（D：测试更新与兼容口径）
- 授权 D 最小更新既有测试：test_r14_b_f03.py、test_r14_f_recovery.py、test_r15_d_r4_recovery_lease.py（暂存权威迁移到 run 行后的断言等价改写、迁移版本号 83/84 显式断言），test_r15_f_independent.py 由 F 更新；断言语义不放宽。
- get_record/list_records 不再用读形状回填 answer_text 冒充暂存（避免读侧第二权威）；如需恢复项预览，先与 Lead 确认后只加显式 staged 预览字段。D 的代码在迁移列缺失时不得静默降级：沿用既有结构化 503 recovery_unavailable（与 run 表不可用同口径）；PRAGMA 探测只用于测试 fixture 补列。
- 迁移版本口径（已与 A 确认）：追加迁移 83 + 补偿 84，SCHEMA_VERSION = COMPENSATION_VERSION = 84。A 负责通用迁移/版本断言测试（test_r14_b_migration.py、test_p1r_c_navigation_version.py 等）；D 负责 test_r15_d_r4_recovery_lease.py 的版本断言。
- 迟到写/失败更新/释放的 CAS 顺序与 recovery_execution_superseded 见 §14.6。
