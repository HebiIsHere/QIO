# Phase 4-B 后端确认续作 / 重试链路验收

> 任务：task-21（P4-B 后端确认续作/重试链路验收）
> 工作区：`D:\qio-dev\qio-i2-p4-b`；分支 `qio-i2/p4-b`；基线 `e0af7d16`
> 范围：只读审查 + 定向测试；本文件是唯一交付物，**未修改任何生产代码**。
> 日期：2026-10-24

## 0. 结论摘要

- 后端确认 / 批量确认 / 恢复后的**真实互动 Turn**、单意图有效执行、授权与材料版本、重试系列（首次失败不计数、3 次自动、手动不重置、跨连接持久化）、固定提示词键、意图显示位置写入，均有实现与定向测试覆盖；定向 9 个文件 105 条用例 **104 通过 / 1 失败**。
- 唯一失败是**冻结面测试** `test_p3_d_frozen_surface::test_interactive_policy_exists_and_contains_hard_rules`：`INTERACTIVE_POLICY` 的冻结措辞已被 Phase 3-D 改写，测试仍断言旧句子（§3 详述）。工作区未改动任何代码，该失败**在基线即可复现**。
- 另发现 3 个高优先级风险（不影响本次测试通过，但目前**无测试覆盖**）：① 确认 Turn 的「认领窗口」不是原子保护，并发确认可在 claiming 期间重复提交（已用最小探针演示）；② 进程重启后没有对 `running/claiming` 的重试系列与意图 Turn 做对账/重排，自动重试可能停在半途；③ 自动重试由「任意互动 Turn 收尾」顺带调度，聊天型互动 Turn 也可能触发无关意图的重试。
- 建议 Lead 决策：先处理冻结面测试与 `claiming` 并发保护（P1），再补重启对账与重试系列释放在所有结局上的闭合（P2）。

## 1. 审查与运行方式

- 代码审查：`backend/src/agent/interactive/{intent_service,submission,intents,models,board_tools}.py`、`backend/src/agent/api/interactive_intents.py`、`backend/src/agent/services/turn_orchestrator.py`、`backend/src/agent/prompts.py`、`backend/src/agent/storage/migration_p3_d.py`。
- 测试环境：复用 `D:\qio-dev\qio-cv-main\backend\.venv`；`PYTHONPATH=D:\qio-dev\qio-i2-p4-b\backend\src`；工作目录 `backend`。
- 无联网、无真实模型：测试用 `FakeTurns` / `ExplodingTurns` 与 `TestClient`；没有 `api.sendTurn`、没有真实凭据。
- 命令：

```powershell
$env:PYTHONPATH='D:\qio-dev\qio-i2-p4-b\backend\src'
& 'D:\qio-dev\qio-cv-main\backend\.venv\Scripts\python.exe' -m pytest `
  tests/test_p3_d_intent_resume_retry.py tests/test_p3_d_authorization_scope.py `
  tests/test_p3_c_intents_api.py tests/test_p3_d_submission_resume.py `
  tests/test_p3_d_finish_ledger.py tests/test_p3_d_batch_api_content.py `
  tests/test_p3_d_migrations.py tests/test_p3_d_static_boundaries.py `
  tests/test_p3_d_frozen_surface.py -q --no-header -p no:cacheprovider
```

## 2. 测试证据

### 2.1 定向 9 文件（本次验收主命令）

总计 **105 条 / 104 通过 / 1 失败**（失败为冻结面措辞，见 §3）：

| 文件 | 用例 | 结果 | 覆盖点 |
| --- | --- | --- | --- |
| `test_p3_d_intent_resume_retry.py` | 15 | 15 通过 | 迁移 56、display 读写、确认/恢复启动真实 Turn、重复确认不并行、失败如实返回、自动重试 3 次与耗尽、不可恢复不重试、跨连接持久化、orchestrator 调度、批量只认领未开始意图、固定提示词 |
| `test_p3_d_authorization_scope.py` | 10 | 10 通过 | 未确认不可写、模型自报批准无效、范围越界拒绝、范围扩大使旧授权失效、operation_key 幂等/冲突、跨恢复配额累计、依赖环/未知依赖拒绝、冲突对称归一 |
| `test_p3_c_intents_api.py` | 16 | 16 通过 | 意图视图 / 单条确认 / 批量确认阈值与路径 / 恢复预览与恢复 / 等待 / 账本 / 结果 的 API 形状（服务用替身）；**display 路由未覆盖（R7）** |
| `test_p3_d_submission_resume.py` | 9 | 9 通过 | 提交等待 / 继续 / 续作轮绑定 |
| `test_p3_d_finish_ledger.py` | 9 | 9 通过 | completed / partially_completed / failed 的账本核对、结果保留、阻塞项 |
| `test_p3_d_batch_api_content.py` | 8 | 8 通过 | 批量阈值≥2、单条拒绝、列表真实状态、长 Markdown 完整保存/读取、过期版本不覆盖 |
| `test_p3_d_migrations.py` | 10 | 10 通过 | 迁移只追加、幂等、补偿重放、表/列/索引存在 |
| `test_p3_d_static_boundaries.py` | 12 | 12 通过 | 固定文案单点、工具注册/参数模型边界、无第二套协议 |
| `test_p3_d_frozen_surface.py` | 16 | **15 通过 / 1 失败** | 冻结提示词硬规则、12 工具说明、参数文案、工具参数模型、可信上下文 |

### 2.2 全 Phase 3 后端回归（补充证据）

`tests/test_p3_*.py` 共 17 个文件 **178 条 / 177 通过 / 1 失败**（仍是同一冻结面用例）。
其余 16 个文件全绿，含 `test_p3_a_submission_flow`（6）、`test_p3_a_turn_binding`（6）、`test_p3_a_waiting`（13）、`test_p3_b_service`（12）、`test_p3_b_tools`（14）、`test_p3_d_tools_registry`（5）、`test_p3_memory_topic_experiments`（11）等。

### 2.3 需求覆盖矩阵

| 要求 | 结论 | 证据 |
| --- | --- | --- |
| confirm 后真实互动 Turn | **通过** | `intent_service.confirm_intents`（1286）→ API `_start_intent_turn`（460）→ `submission.start_interactive_turn`（1714）；`test_confirm_api_returns_real_turn_id_and_binds_facts`、`test_confirm_api_reports_turn_failure_when_main_loop_rejects`（主循环失败 → turnId=None、confirmationSaved=True） |
| batch-confirm 后真实 Turn | **通过** | API（724）同批启动；`test_batch_start_only_claims_not_yet_running_intents`（已在跑的意图不重复启动，只对剩余意图建 Turn）；批阈值测试在 `test_p3_d_batch_api_content` |
| recover 后真实续作 | **通过** | `recover_intent`（1883）→ API（927）→ `_start_intent_turn(source="recover", manual_retry=True)`；`test_recover_api_starts_turn_and_counts_one_manual_attempt`（turnId 真实、manual_attempts=1、attempts 不重置） |
| 单意图有效执行（不并行） | **部分通过（顺序场景）** | `active_intent_turn`（1530）、`claim_intent_turns`（1551）、`start_interactive_turn`（1829）保证串行重复确认复用同一 turnId；但 claiming 窗口存在并发缺口（R2） |
| 授权 / 材料版本 | **通过（服务/工具层）** | 授权快照（`confirm_intents` 1342-1350）、`_authorization_valid`（236）、`authorize_operation`（1439）、`finish_intent` 的 expected_versions/ledger 核对（1650+）；`test_p3_d_authorization_scope` 10 条、`test_p3_b_tools` 14 条、`test_p3_d_tools_registry` 5 条 |
| 重试系列：首次失败不计数 | **通过** | `record_retry_failure` 新建 attempts=0/pending（446-474）；`test_finish_failed_records_retry_and_orchestrator_exhausts_three_attempts` |
| 最多 3 次自动重试 + 耗尽 | **通过** | `RETRY_LIMIT=3`、`claim_auto_retry` 耗尽转 exhausted（598-606）；orchestrator 每轮最多排一条（1216-1238）；同一测试 |
| 手动重试一次点击一次、不重置 | **通过** | `claim_manual_retry`（626）只加 manual_attempts；`test_recover_api...` 断言 attempts=3 不变 |
| 重启 / 重连不重置 | **通过（持久化）/ 未覆盖（自动恢复）** | `test_retry_persists_across_new_connection` 证明跨连接状态保留；但重启后的调度/对账未覆盖（R3） |
| 固定提示词键 | **部分通过** | `PROMPTS.INTERACTIVE_POLICY`（966 字符）、`INTERACTIVE_RETRY_POLICY`（473 字符）、12 个板面工具说明、31 个互动参数文案齐全（`test_fixed_prompts_are_present...` 通过）；冻结面措辞测试失败（§3） |
| intent display 写入口 | **部分通过** | 迁移 56 `board_intents.display`（migration_p3_d 23-28）、`set_intent_display`（524）、视图 `display`（269-275）、API `POST /intents/{id}/display`（970）都有；**但 API 路由没有测试**（仅服务层测试），见 R7 |
| 错误与不可恢复原因 | **部分通过** | `NON_RETRYABLE_MARKERS`（82-115）、`retry_reason_is_recoverable`（341）、`preview_recovery` 的 reasons/can_resume（1928-1975）、`recover_intent` 的 authorization_stale（1904-1909）；覆盖见 `test_non_retryable_failure_never_auto_retries`、`test_p3_a_waiting`；最终说明只呈现一次等细节见未覆盖清单 |

## 3. 唯一失败项：冻结面测试与提示词措辞漂移（P1）

失败用例：`backend/tests/test_p3_d_frozen_surface.py::test_interactive_policy_exists_and_contains_hard_rules`（第 48 行断言，第 59 行报错）。

- 测试期望（契约 §13.2 冻结句子）：
  > 板面身份、提交身份、查看权限、意图状态及授权范围以程序提供的数据为准。
- 当前 `prompts.PROMPTS["INTERACTIVE_POLICY"]` 实际句子：
  > 板面身份、提交身份、意图身份、对象版本、查看权限和授权范围，以程序提供的可信数据为准。
- 其余 9 条硬规则短语均通过。
- 历史证据：
  - 测试文件最后修改：`7b8a7c09 test(p3-d): 适配 A/B/C 已提交实现的前提 + 补 DEFECT-P3-2 结构动作回归`；
  - `prompts.py` 最后修改：`edeb56da feat(interactive): Phase 3-D 意图确认续作 / 持久化重试 / 固定提示词`；
  - `git show f80a5fa8:backend/src/agent/prompts.py` 中该句仍是旧措辞（测试的期望），说明 Phase 3-D 改写提示词时未同步更新冻结面测试。
- 风险：两种可能都要 Lead 明确选择，不能两边都算“通过”：
  1. 若新措辞是**有意的**（补充对象版本、身份、可信数据来源），应更新冻结面测试与契约文档的引用文案；
  2. 若旧措辞是**契约冻结原文**，则提示词被改写属于冻结面回退，应恢复原文。
- 本任务只读，不改生产代码与测试；建议 Lead 在集成分支处理并补一条“文案变更必须同步冻结面测试”的守卫。

## 4. 未覆盖、缺口与风险

### R1（P1）提示词冻结面漂移 —— 已由失败测试证明

见 §3。影响：`test_p3_d_frozen_surface` 失去“冻结面被改写”的守护意义；后续整合无法靠该测试判断提示词是否被改动。

### R2（P1）确认 Turn 的「认领窗口」不是原子保护，并发确认可重复提交

- 代码：`claim_intent_turns`（submission.py 1551-1603）只把 `status='active'` 当作已在执行；写入时用 `ON CONFLICT(intent_id) DO UPDATE ... status='claiming'` 无条件覆盖；`start_interactive_turn`（1829-1855）在提交主循环 Turn **之前**先查 active，claim 成功后到 `bind_claimed_turn`（1606-1623）写入真实 turn_id 之间，行状态是 `claiming`，`active_intent_turn`（1530-1548）查不到它。
- 最小探针（临时脚本，未入库）输出：
  ```
  first_claim_ok = True
  stored_status = claiming turn_id = ''
  active_intent_turn = None
  second_claim_ok = True code = None
  stored_status_after_second = claiming
  ```
  即第一次认领后、绑定 turn_id 前，第二次认领同样成功（覆盖同一行）——两个并发确认请求都可能各自提交一次主循环 Turn，违反“同一意图同一时刻只有一个有效执行”。
- 测试现状：只覆盖顺序场景（`test_start_turn_claims_intent_and_duplicate_confirm_does_not_parallel` 在 Turn 已 active 后才重复确认），没有并发/认领窗口用例。
- 建议：把认领改成条件更新（如 `WHERE status NOT IN ('claiming','active')` 并检查 rowcount / 或在同一事务内用唯一 token 认领），补并发测试；或确认 API 层是否串行化了同一意图请求。

### R3（P1）进程重启后没有重试系列 / 意图 Turn 的对账与重排

- `pending_auto_retries` 只被 `turn_orchestrator._finish_interactive_turn`（1216）调用；全仓没有启动时扫描/恢复 `board_intent_retries` 或 `board_intent_turns` 的入口（`recover_running_intents` 只处理旧链运行态，不处理这两张表）。
- 后果：
  - 自动重试已认领（status=running）后进程崩溃 → 重启后没有任何代码把它释放或重排，系列停住；
  - `board_intent_turns` 若停在 `claiming`（claim 成功、Turn 未绑定）或 `active`（Turn 结束后没走到 release），重启后仍挡住后续执行。
- 契约“重启/重连不重置”已由持久化满足，但“重启后仍能继续自动重试”没有实现/测试。
- 建议：启动时按 `updated_at` 对 `running` 重试与 `claiming/active` 意图 Turn 做一次保守对账（能确认未在跑才重排；不能确认则显式转 manual 并记录原因），并补重启测试。

### R4（P2）Turn 成功但意图未 completed 时，重试系列停在 running

- `_finish_interactive_turn` 成功分支只对 `status='completed'` 的意图调用 `complete_retries_for_intents`（1205-1214）；paused / awaiting_confirmation / partially_completed 等不会被收口。
- 若这轮是自动重试（claim 已把 series 置 `running`），而 Turn 正常结束、意图仍不是 completed，系列保持 `running`；`pending_auto_retries` 只挑 `pending`，后续不会再自动排，用户也看不到“已停止/耗尽”的最终说明。
- 建议：Turn 结束（无论成败）都对范围内重试系列做明确结算（completed / manual / stopped 并记录原因），不要留在 running。

### R5（P2）自动重试调度范围过宽：任意互动 Turn 收尾都会尝试调度

- `_finish_interactive_turn` 在 `intent_ids` 为空时也会继续执行，`pending_auto_retries(intent_ids=intent_ids or None)`（1216-1220）变成“整块板面的任意 pending 系列”。
- 触发点只是“一个互动 Turn 收尾”，与失败意图是否属于本次 Turn 无关；聊天型互动 Turn 结束也可能启动一个不相关意图的自动重试（服务端主动执行，用户没有本次点击）。
- 语义上自动重试本来就由服务端调度，未必是缺陷；但当前没有任何“调度边界”的说明/测试。建议 Lead 明确：是否允许无关 Turn 触发；若不允许，传入明确的 intent_ids 或建立独立调度器。

### R6（P2）自动重试启动失败被当成“永久停止”

- orchestrator 在 `start_interactive_turn` 失败时把系列置 `stopped`（1243-1249），原因可能是主循环暂时不可提交（turn_unavailable）等临时情况。
- 契约要求“主循环不可用时不得假装已开始”，这点实现正确；但把**临时不可用**永久停止会消耗掉用户本可得到的剩余自动尝试。
- 建议区分 `turn_unavailable`（保留 pending，等待下次调度）与不可恢复错误（stopped），并补测试。

### R7（P3）display 写入口的 API 路由未覆盖；错误状态码不精确

- API `POST /api/interactive/intents/{intent_id}/display`（interactive_intents.py 970-995）没有后端测试；现有测试只覆盖迁移、视图和服务函数（`test_p3_d_intent_resume_retry` 72-112）。
- 非法有限数：服务返回 `invalid_display`（intent_service 536），API 统一抛 404（994），对“输入非法”不够准确（400 更合适）；建议补路由测试与状态码区分。
- 另：`_intent_view` 直接 `float(raw_display['x'])`（269-275），若数据库里被写成非数字（绕过服务层）会抛异常；建议容错为 None 并记录。

### R8（P2）真实主循环 / 模型工具调用链路未测

- 本任务按要求只用 fake turn、无真实模型/网络；`test_p3_b_tools`、`test_p3_d_tools_registry` 覆盖工具参数模型与授权执行，但没有覆盖“模型在真实主循环里调用 board_* 工具 → ToolContext 授权 → 执行 → finish”这条端到端链路。
- 风险：运行时上下文（board/submission/intent/授权版本）与工具层校验之间若出现绑定漂移，现有测试抓不到。属 Phase 4 之外的已知边界（需 fake adapter 的集成测试或真实本地模型验证）。

### R9（P3）互动收尾异常被吞掉

- `_finish_interactive_turn` 整体包在 `try/except` 里，只 `logging.warning`（1250-1256）；若重试记录/调度异常，用户与接口都看不到“没有安排重试”。
- 建议至少在意图视图/重试系列视图里留下 `last_error`/`next_action` 的失败痕迹，或暴露给上层监控。

### R10（P3）部分批确认的“部分失败分别反馈”未穷举

- 批量阈值与单条拒绝已测（`test_batch_api_accepts_two_and_rejects_single`）；确认集合中部分意图状态不可确认/冲突时，API 仍会用 `confirmed` 子集启动 Turn，响应 `ok` 可能为 false 但 `turnId` 非空——目前没有测试钉住这种混合响应的字段语义。
- 建议补一条“两条里一条冲突”的批量 API 用例，断言 `results/confirmed/turnId/ok` 的组合与界面展示口径。

## 5. 固定提示词清单（当前基线实际内容）

位置：`backend/src/agent/prompts.py`（唯一来源，契约 §6.5）。

- 键 `INTERACTIVE_POLICY`（966 字符，全文）：
```text
当前运行于互动模式，使用同一个 QIO 的理解、记忆、对话和工具能力。
板面身份、提交身份、意图身份、对象版本、查看权限和授权范围，以程序提供的可信数据为准。

用户点击“提交给 QIO”后，结合实际要求和获准材料判断下一步：需要落实的工作通过板面工具提出待确认意图；信息不足时在对话中自然询问；当前无需操作时不强行创建意图。是否需要额外聊天回复，由实际交流需要决定。

意图是待确认的工作目标，尚未确认时不执行真实板面改动，不把意图预览说成已经形成的成果。使用 board_propose_intents 或 board_update_intent 提出和更新意图。

信息不足时使用 board_wait_for_input 记录必要问题，不创建板面问题卡或待补充草案卡。系统已将问题作为正常对话展示时，不再重复发送同一问题。用户回答后继续原提交；信息已补齐时，通过 resolved_question_ids 解除对应等待。

根据需要读取最新已保存且获准查看的卡片，必要时对照提交快照。没有取得的正文、图片或文件内容不能声称已经读取。普通选中、书签、阅读位置、卡片缩放和平移不自动构成新的工作请求；保存和编辑本身也不等于再次提交。

执行以用户确认的目标、材料依据和授权范围为准。确认范围内可以调整实施步骤；实质扩大目标、对象、数量、成本或影响时，更新意图并重新确认。参与讨论只表示允许读取，不等于允许修改正文或写回文件。

修改前读取当前版本，保护用户后来的编辑。发生版本冲突时暂停受影响操作，其他独立工作可在授权内继续。组及其成员、连接关系和图片尺寸，以工具返回的真实结构和版本为准，不根据画面位置猜测关系含义，不改变图片原有比例。

生成完整成果后使用板面工具保存，不让保存工具再次生成正文。使用稳定 operation_key 防止重复写入。已经完成的步骤和有用成果应保留，恢复时继续未完成部分，不重复创建卡片、分组或连线。

用户指定的成果位置优先作为新成果落点，实际几何和必要避让由程序校验。移动意图预览不代表已经移动真实材料。

通过 board_finish_intent 声明真实结果。只有工具核验支持时才能说明已经完成；部分完成、失败、等待回答和等待授权应分别说明。自然语言中的“完成”不能替代程序状态。
```
- 键 `INTERACTIVE_RETRY_POLICY`（473 字符，全文）：
```text
当前正在处理一项已有互动工作的失败恢复。
原意图、确认范围、授权版本、已完成操作、未完成事项、实际失败原因、重试方式和剩余次数，以程序提供的结构化事实为准。

先核对已经完成的内容，再决定如何继续未完成部分。保留仍然有效的成果，不重复创建或重复应用已经完成的操作，不自行扩大目标或授权范围。

首次执行失败后，程序最多安排三次自动重试。是否还能自动重试由程序决定，不能自行增加次数、重置次数或连续调用恢复入口绕过限制。等待用户回答、等待授权、用户拒绝、材料撤回、授权失效、需要用户决定的冲突和真实预算限制，不应伪装成可通过反复执行解决的临时故障。

自动重试用尽后，停止自动执行，在原对话中自然说明实际原因、已完成部分、尚未完成部分和需要用户采取的下一步。不把未验证的推测当成确定原因。

后续手动重试必须来自用户明确点击，一次点击对应一次尝试，不重新开启三次自动重试。若继续处理需要实质改变工作范围，先更新意图并重新确认。

使用实际工具结果和账本判断状态，通过 board_finish_intent 记录真实结果，不以文字说明代替程序完成。
```
- `TOOL_DESCRIPTIONS` 12 个板面工具键：`board_inspect` / `board_read_cards` / `board_propose_intents` / `board_update_intent` / `board_wait_for_input` / `board_create_card` / `board_update_card` / `board_move_card` / `board_group_cards` / `board_order_group` / `board_add_link` / `board_finish_intent`（全部存在且非空，见 `test_twelve_board_tool_descriptions_are_frozen`）。
- `ARGUMENT_DESCRIPTIONS` 31 个互动相关键：`submission_id`、`intents`、`intent_id`、`changes`、`card_ids`、`source`、`card_id`、`patch`、`group_id`、`group_ids`、`name`、`member_order`、`arrange`、`src`、`dst`、`direction`、`meaning`、`title`、`width`、`height`、`outcome`、`summary`、`operation_keys`、`missing_fields`、`question`、`resolved_question_ids`、`position`、`scope`、`expected_version`、`expected_versions`、`operation_key`。
- 本次审查**未改动**任何提示词；Phase 4 的其他固定文案（子任务/凭据/自动化/引导）不在本任务范围。

## 6. Lead 决策建议

1. **P1 冻结面**：确认 `INTERACTIVE_POLICY` 当前措辞是否为最终版本；同步更新 `test_p3_d_frozen_surface.py`（或多个冻结短语）与契约文档引用，或在提示词恢复旧句。建议加一条“改 prompts.py 必须同时改冻结面测试”的流程守卫。
2. **P1 并发确认**：把 `claim_intent_turns` 改成原子条件认领（claiming 也算占用），补并发/认领窗口测试；同时确认 API 是否对同一 intent 的确认请求做了串行化。
3. **P2 重试闭合**：补启动对账（running/claiming/active 三类），以及 Turn 结束时对所有相关重试系列做明确结算（completed / manual / stopped）；自动重试临时启动失败不要永久 stopped。
4. **P2 调度边界**：明确“任意互动 Turn 可触发整板 pending 自动重试”是否为预期；若否，收窄 intent_ids 或独立调度器。
5. **P3 测试补齐**：display HTTP 路由 + 非法输入状态码；批量部分失败混合响应；真实 fake adapter 的主循环工具链路。

## 7. 边界声明

- 本任务只读：未改 `backend/src/**`、未改测试、未改契约；未 push、未建 PR、未触发完整 CI。
- 所有测试使用本地临时 SQLite、`TestClient`、`FakeTurns`；没有真实模型调用、没有网络请求、没有真实凭据。
- 机器相关的临时 JUnit XML 写在 `%TEMP%`，不进入仓库；并发探针脚本为一次性文件，运行后已删除。
