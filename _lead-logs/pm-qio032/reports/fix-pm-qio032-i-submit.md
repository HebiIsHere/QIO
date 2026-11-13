# fix/pm-qio032-i-submit 报告（后端：提交入口 / 轮次关联 / 待确认事件 / 去重）

## 1. 分支与环境

- 分支：`fix/pm-qio032-i-submit`；worktree：`D:\qio-dev\pm-qio032-i-submit`
- 开工 SHA：`7445170d0f51756b2740eb7209d798540d8042a2`
- 提交（全部在本分支）：
  - `f8547e8b` fix(interactive-submit): 空白卡本身不计有效提交，关系/删除按实际语义计入（#13）
  - `5787ae60` fix(interactive-submit): 授权失效事实字段 + 待确认意图变化事件（#27 #26）
  - `4174d480` Merge branch 'fix/pm-qio032-i-material' into fix/pm-qio032-i-submit（按 Lead 授权合入 i-material 依赖：#01 接线所需 materials 函数）
  - `<本轮最终提交>` fix(interactive-submit): 首轮材料交付接线（#01）+ 定向用例 + 报告更新（以 `git log --oneline -1` 为准）
- 说明：开工时 `git worktree add` 被工具 120s 超时打断（index 半截、working tree 未登记），已用 `git worktree unlock` + `git reset --hard HEAD` 修复为干净 7445170d；此后每次提交前都核对无大规模 D 条目。

## 2. 改动文件

源码：
- `backend/src/agent/interactive/submission.py`（#13）
- `backend/src/agent/interactive/intent_service.py`（#27 授权失效事实 + #26 事件发布 helper；另修 `_record_version` 同版本确认时授权快照被 INSERT OR IGNORE 丢掉的问题——这是 #27 能拿到「原授权」证据的前提）
- `backend/src/agent/api/interactive_intents.py`（#27 legacy 视图补字段 + #26 确认路由发布事件 + 事件发布包装）
- `backend/src/agent/api/events.py`（只增 EventType.INTERACTIVE_INTENTS_CHANGED）
- `backend/src/agent/api/bus.py`（只增：新事件加入 CRITICAL_EVENTS）
- `backend/src/agent/services/turn_orchestrator.py`（#01 两处接线，见下表精确位置；普通模式行为不变）

新增测试（`backend/tests/pm_qio032_submit_*.py`）：
- `pm_qio032_submit_authorization_facts.py`（#27，7 项）
- `pm_qio032_submit_intents_event.py`（#26，8 项）
- `pm_qio032_submit_empty_semantics.py`（#13，5 项）
- `pm_qio032_submit_first_round_materials.py`（#01，3 项，真实编排链路 + fake adapter 捕获模型请求）

## 3. 逐项结果

| # | 结果 | 证据 |
| --- | --- | --- |
| #01 首轮材料交付（互动提交轮） | **已修复（第二轮，合入 i-material 依赖后接线）** | 接线位置：① `turn_orchestrator.py` 的 `_plan` 中 read_grants 计算之后（原 1132 行块后）：互动模式且 `ctx.board_submission_id` 非空时调用 `materials.submission_attachment_scope`，对 `scope['allowed']` 每个 attachment 走既有 `attachment_access.authorize_read` 网关（同一 purpose/tool 口径，degraded 沿用 READ 放行），verdict=allow 才并入 allowed 并 `set_read_grants`；stale 不并入。② 同函数 interactive 上下文块组装处（原 1196-1231 行块）：调用 `materials.submission_materials(conn, board_id, submission_id, attachments, vision=adapter.supports_images)`，`materials/available/reason/materials_stale` 进 `interactive` 结构化块；`images` 经 `understanding.image_parts_from_context({"imageParts": ...})` 追加 `plan.media_parts`（**仅当 vision 能力为真才交付字节**；无能力不交付、材料条目已如实标 image_unsupported）。同一轮只调用一次。注：没有改 `app.py`——两处都需要 `ctx.board_id/board_submission_id`，而该上下文只在 `turn_orchestrator._plan` 内可用，语义与 Lead/i-material 规格一致。测试：`pm_qio032_submit_first_round_materials.py` 3 项全绿——真实 AppContext/TurnOrchestrator/AgentLoop + fake adapter 捕获请求：TXT 正文真的进首轮提示词（interactive.materials）、PNG 以 `data:image/png` 进首轮 user 消息、`supports_images=False` 时不交付字节且标 image_unsupported、提交快照附件并入本轮 read grants（monkeypatch 捕获 set_read_grants）。 |
| #13 空白卡不计有效提交 | **已修复（后端）** | `submission.py`：`blank_card_added` 归入非内容表达（`DISPLAY_ONLY_EXPRESSIONS`），新增 `_is_blank_card`（文本卡空 doc 壳也算空白）；空白卡保留在板面/after 快照但只加空白卡时 status=empty、不更新基准、不受理 Turn，理由里给下一步；关系/分组/删除仍按实际语义计入。测试：`pm_qio032_submit_empty_semantics.py` 5 项全绿。 |
| #27 授权失效事实 | **已修复（后端）** | IntentView 只增 `authorization_invalidated: bool` 与 `authorization_changes: list[str]`（list/detail/legacy 三处形状一致）；仅当「当前授权无效 + 历史确有非空授权快照」才 true，变更用历史快照与当前行逐字段比较（目标/预期/读取写入分组范围/允许动作/新建上限/依据/依赖/冲突/授权版本），无事实返回 []，不用时间/版本冒充。同时修 `_record_version`：同版本确认时补写授权快照（原来被 INSERT OR IGNORE 静默丢弃，导致「原授权」证据缺失）。测试：`pm_qio032_submit_authorization_facts.py` 7 项全绿。 |
| #26 待确认任务事件 / 去重 | **已修复（后端；工具路径依赖 i-material 合入，已合）** | 新事件 `INTERACTIVE_INTENTS_CHANGED`（`api/events.py`）+ 进 `bus.CRITICAL_EVENTS` + `intent_service.publish_intents_changed`（payload `{board_id, submission_id, intent_ids, statuses, reason}`，按 intent_id 幂等；无总线/总线失败静默降级不影响主流程）；HTTP 单条/批量确认成功后已发布；工具路径 `board_propose_intents`/`board_update_intent` 已随 i-material 合入用 getattr 包装调同一 helper。测试：`pm_qio032_submit_intents_event.py` 8 项全绿。**集成风险**：`tests/test_event_protocol.py` 两条既有守卫要求前端 `services/events.ts` EVENT_TYPES 集合相等 + `stores/events.ts` 有消费分支；Lead 已授权 i-state 接线，合并前需确认这两个前端文件随 i-state 分支进入父分支。 |
| #80 重复提交幂等 / 查原进度 | **基线已满足**（未造轮子） | 同身份重试返回原记录事实：`tests/test_irm_b_acceptance_api.py::test_submit_replay_returns_original_facts`；同身份不同内容 409：`::test_submit_same_identity_different_content_is_409`；DB 级唯一约束 `storage/schema.py:1353-1355`（`idx_board_submissions_client_request` UNIQUE partial，防并发重复落行）；原进度可查：`::test_acceptance_lookup_*` + `test_audit_f04_acceptance_receipt.py`。「显示已提交过」文案属前端（i-state 已就绪）。 |
| #81 回执待确认按原请求查证 | **基线已满足** | `submission.acceptance_lookup`（`submission.py:942`）+ `GET .../submissions/acceptance`（`interactive_store.py:475`）；查不到 404 + acceptance=null 不编造：`test_irm_b_acceptance_api.py::test_acceptance_lookup_404_returns_null_not_guessed_success`；只认精确身份不用时间接近：`::test_acceptance_lookup_is_exact_not_time_based`；重启后按身份：`::test_acceptance_lookup_after_restart_is_by_identity`。 |
| #03 轮次状态 / 迟到事件不倒退 | **部分（后端事实面基线已满足，未新增字段）** | 提交与真实轮次绑定：`submission.bind_submission_turn`（`submission.py:1440`，迁移 49）；受理响应/重放带 `turnId`、`waiting`；状态事实取真实记录（acceptance.status/delivery/understandingStatus）；Turn 生命周期事件 TURN_START/TURN_END 基线已有。刷新/迟到事件的「不倒退」状态机主要由 i-state 前端按这些事实实现；本次未新增「按 turnId 查当前终态」后端接口（见 §6）。 |

## 4. 定向测试（命令与结果）

```powershell
cd D:\qio-dev\pm-qio032-i-submit\backend
$env:QIO_DATA_DIR='<wt>\.tmp-test-data'; $env:QIO_DISABLE_DB_CHECK='1'
& .\.venv\Scripts\python.exe -m pytest \
  tests/pm_qio032_submit_first_round_materials.py tests/pm_qio032_material_scope_test.py \
  tests/test_interactive_submission.py tests/test_interactive_intents.py \
  tests/pm_qio032_submit_authorization_facts.py tests/pm_qio032_submit_intents_event.py \
  tests/pm_qio032_submit_empty_semantics.py -q
```

结果：**全绿**（7 个文件，119 项；其中本分支新增 4 个文件 23 项）。
早前另跑：`test_irm_b_acceptance_api.py / test_audit_f04_acceptance_receipt.py / test_p3_b_service.py / test_p3_c_intents_api.py` 全绿（基线 15 skipped 未变）。
已知：`tests/test_event_protocol.py` 在第二轮本分支曾红 2 条（前端 EVENT_TYPES / stores 消费分支在 i-state 分支）；第三轮父分支已合并 i-state 改动，该守卫**已转绿**（见 §7）。

## 5. 新增 / 变更的对外接口（只增不改）

- 后端 EventType：`INTERACTIVE_INTENTS_CHANGED`（SSE，CRITICAL）；payload `{board_id, submission_id, intent_ids, statuses, reason}`，reason ∈ proposed/updated/confirmed/changed；客户端按 `intent_id` upsert（重复投递/重连不重复建卡）。发布时机：propose 工具成功、update 工具成功、HTTP confirm/batch-confirm 成功。
- IntentView 新字段（`GET /api/interactive/boards/{id}/intent-views`、`.../intent-views/{intent_id}`、legacy 回退视图同形状）：`authorization_invalidated: bool`、`authorization_changes: list[str]`；旧字段语义不变。
- `intent_service.publish_intents_changed(conn, *, board_id, submission_id='', intent_ids=(), reason='changed') -> bool`（异步；board_tools 与 API 路由消费）。
- 互动提交轮的模型输入（内部契约，不改 API 响应）：`interactive` 上下文块新增 `materials / materials_available / materials_reason / materials_stale`；图片字节进媒体内容块。`plan.media_parts` 的既有消费方式不变。
- 互动提交轮的附件读取授权：提交快照 allowed 附件并入本轮 read grants（经既有 `authorize_read`），不新增接口。
- 提交/查证接口本身无字段变更（#80/#81 基线已是只增形状）。

## 6. 未做 / 风险 / 集成注意

1. **#03 未新增**「按 turnId 查轮次终态」接口：若 i-state 刷新后需要后端直接给轮次终态（而非从 events/acceptance 推导），需在 acceptance 或新只读接口补 `turn` 事实；本轮未做，建议与 i-state 对齐是否需要。
2. **#26 事件协议守卫**：合并前必须确认 i-state 分支已把 `"INTERACTIVE_INTENTS_CHANGED"` 加进 `frontend/src/services/events.ts` 的 EVENT_TYPES，并在 `frontend/src/stores/events.ts` 加消费分支；否则 `tests/test_event_protocol.py` 红。
3. `_record_version` 改为 UPSERT 只补非空 authorization（payload/created_at 不变）：旧库历史行不会补写，旧意图的 `authorization_invalidated` 可能为 false（宁可不显示，不误报）；新数据完整。
4. #01 图片交付按 `adapter.supports_images` 能力位门控；音频/视频仍走既有媒体链路（不在本条目范围）。`materials.submission_materials` 的 stale 事实保留在 `materials_stale` 与材料条目 reason 里（提示重新提交）。
5. 未跑全量 pytest / 前端类型检查（本分支只改后端；按 CONTRACT 由 Lead 统一跑）。合入 i-material 后其报告与用例一并进入本分支。

## 7. 第三轮（父分支合并后复核 · E2E ①-6b / #80 / #81）

### 7.1 合并

- git merge fix/pm-qio032-interactive → **fast-forward 到 9f80b0cf**（父分支此前已合并本分支第二轮提交 603581b3，无冲突；工作树干净）。合并后包含 i-state 前端事件接线，tests/test_event_protocol.py 已转绿。

### 7.2 ①-6b「参与项 TXT 文件正文未进入首轮请求」结论：**不是产品缺陷，是 E2E 场景自相矛盾**

证据链：
1. E2E 脚本 scripts/verify-pm-qio032/e2e.mjs：①-4（353-358 行）把 TXT 文件卡设为「显示但不参与」，①-5（359-360 行）把 PNG 卡设为「隐藏且不参与」，之后 **370 行才点提交**；TXT/PNG 在提交时就是排除项，按 #08/#54 它们本就不该进提交快照。
2. 同一个 txtBody 标记同时被 ①-6b（394-395 行，期望**出现**）与 ①-7（400-401 行，期望**不出现**）使用 —— 两条断言互斥，不可能同时通过；exTxtBody（305 行）定义了却从未使用（疑似原本给「排除项」用的标记）。
3. fake provider 台账 _lead-logs/pm-qio032/e2e/provider-log-s1.json 显示该轮提交事实是「本次提交范围：卡片 1 项」，interactive materials 只有文字卡 inline —— 与「TXT 卡被排除」一致；产品行为正确。
4. 新增真实编排链路用例（见 7.3）证明：**参与的 TXT 文件卡正文确实交付**（source=attachment_text，正文逐字出现在首轮请求），只有排除/隐藏卡不交付。

建议（scripts/** 属 Lead 维护，本分支不改）：把 E2E ①-6b 拆成两段——先让 TXT 卡保持「显示并参与」提交并断言正文出现（①-6b/①-9），再用独立排除卡 + exTxtBody 断言排除项不出现（①-7），或提交前把 TXT 卡恢复为参与再提交。

### 7.3 本轮新增/加强的测试（定向全绿）

- tests/pm_qio032_submit_first_round_materials.py 新增 1 项「参与范围」真实编排链用例（现共 4 项）：板面同时有参与文字卡 + 参与 TXT 文件卡（attachmentId 附件）+ 参与 PNG 卡 + 排除文字卡 + 隐藏文字卡 + 排除 TXT 文件卡；fake adapter 捕获首轮模型请求，断言参与文字卡正文、参与 TXT 文件正文（attachment_text）都在；排除/隐藏正文与文件名都不在；PNG 仅在 supports_images=True 时以 data:image/png 字节交付。
- 新增 tests/pm_qio032_submit_replay_lookup.py 2 项（**活动**覆盖，替代被整文件 skip 的 test_irm_b_acceptance_api.py）：
  - #80：同 clientRequestId 重放 → replayed=true、同一 submissionId/status/turnId，board_submissions **不增行**，turns.submit 只被调用一次（同一请求不再执行）；同身份不同内容 409；换身份 + 真实内容变化才新增一行并再受理一次。
  - #81：未知身份查证 404 + acceptance=null + 可读原因且不写任何记录；不带身份同样未知；已有提交后未知身份仍未知（不拿最近一条冒充）。
- 回归：pm_qio032_submit_* 5 个文件 + test_interactive_submission.py / test_audit_f04_acceptance_receipt.py / test_event_protocol.py / test_events_bus.py 共 89 项全绿（含前端事件协议守卫转绿）。

### 7.4 #28 材料内容版本接线（第四轮，已完成）

- 合并：父分支 f2716e57（含 i-material 第三轮 facb8a84）fast-forward 进本分支，无冲突。
- 接线位置：backend/src/agent/interactive/submission.py 的 submit_board 成功路径、_record_submission 之前（约 2858 行前）：status==succeeded 时调用 materials.stamp_snapshot_material_versions 对 visible / after 各打一次顶层 materialVersions（附件服务从绑定的宿主 AppContext 取，拿不到时由材料层按 DB 位置推导）；getattr + try 包裹 —— 函数缺失或打标失败保持旧行为（只按 size 核对），不影响提交。只加一个顶层键，cards/groups/links/selection 不改，diff/content_fingerprint 不受影响。
- 新增测试 tests/pm_qio032_submit_material_versions.py（2 项，先红后绿）：
  1) 正常提交：after_state / visible 顶层都有 materialVersions（cardId→{attachmentId,...,sha256 64 位}），去掉该键后 content_fingerprint 与带键完全一致；同内容再提交仍 duplicate（证明指纹/重复判定不变）。
  2) 同大小内容替换：提交后把已保存内容写回同样大小不同字节 → submission_materials 的 stale 命中并含「重新提交」与内容哈希不一致说明，materials 条目 read=false、正文不含新内容；submission_attachment_scope 同样把该附件标 stale（读授权范围也按内容版本核对）。
- 该行为在接线前用例为红（无 materialVersions → 只按 size 核对，同大小替换识别不到），接线后转绿，证明缺口真实存在。

