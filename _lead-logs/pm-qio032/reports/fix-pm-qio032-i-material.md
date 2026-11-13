# fix/pm-qio032-i-material · 交付报告

- 分支：`fix/pm-qio032-i-material`
- worktree：`D:\qio-dev\pm-qio032-i-material`
- 开工 SHA：`7445170d0f51756b2740eb7209d798540d8042a2`
- 本分支提交：
  - `81e04c9d` fix(interactive-material): 提交轮次只读 A 版快照，材料/读卡/附件同一授权
  - `c33392d6` fix(interactive-material): 三态参与入口与提交版本关系的统一口径
  - `63499e73` docs(i-material): 交付报告（材料/三态/提交版本读取）
  - `73c6d9bf` fix(interactive-material): 附件服务不可用时不做假变化判定
  - `9f80b0cf`（合并父分支 fix/pm-qio032-interactive，fast-forward，含 i-submit 的 #01 轮次接线）
  - `facb8a84` feat(interactive-material): 材料内容版本冻结接口，识别同大小内容替换
- 环境：backend/.venv、frontend/node_modules 为 Junction；未改 docs/、未改 schema 迁移、未新增依赖、未 push。

## 1. 改动文件

- `backend/src/agent/interactive/materials.py`：提交快照材料读取（A 版）。
- `backend/src/agent/interactive/board_tools.py`：板面查看 / 读卡只读提交快照；事件旁路；file_ref 事实。
- `backend/src/agent/tools/attachment_tools.py`：提交轮次附件读取的范围授权（增量，不影响普通对话）。
- `backend/src/agent/interactive/board.py`：三态参与纯函数（card_access_state / set_card_access_state）。
- `backend/tests/pm_qio032_material_scope_test.py`（新增，21 条）。

未改：submission.py / api/interactive.py / records.py / runtime.py / models.py / app.py / turn_orchestrator.py。

## 2. 逐项结果（后端部分）

| 条目 | 结果 | 证据 |
| --- | --- | --- |
| #01 TXT/PNG 走同一正式提交入口交付 | **部分（数据源已完成，轮次入口接线转 i-submit）** | `materials.submission_materials` 从提交冻结投影解析：TXT→`attachment_text/inline`+正文，PNG→`image_delivered`+`images[dataUrl]`；排除/隐藏/提交后新增不进；用例 `test_submission_materials_deliver_txt_and_png_from_submission_snapshot`。首轮把材料塞进 Turn 的接线（turn_orchestrator/app.py）按 Lead 决定转 i-submit，我已把 A/B 规格与调用形状发出。 |
| #02 普通聊天收紧 / 追问用原提交范围 | **基线已满足 + 本分支加固** | 无绑定时板面工具拒绝（`test_board_tools_reject_unbound_normal_mode`）；绑定提交后 board_inspect/read_cards/read_attachment 只认 A 版，故普通聊天与 B 版改动读不到（见 #28 用例）；追问轮沿 runtime 绑定走同一份快照。前端收紧不属本分支。 |
| #08/#54 三态授权统一 | **已完成（后端）** | `board.card_access_state` / `set_card_access_state` 给出唯一三态映射；恢复隐藏只回「显示但不参与」（`test_card_access_state_maps_three_states_and_restore_defaults_to_displayed`）；迁移冲突永远不扩大范围（`test_legacy_conflicts_never_expand_reading_scope`）；提交投影 / 关系范围 / 工具读卡 / 材料解析四处集合一致（`test_three_state_consistent_across_submission_links_tools_and_materials`）；选择/框选/提交投影不改三态（`test_selection_and_submission_projection_do_not_change_participation`）。 |
| #28 提交的一轮只用提交当时的版本 | **已修复（核心三路）** | board_inspect / board_read_cards 在绑定提交时读 `board_submissions.visible`（A 版），返回 `basis={source,submission_id,fallback}`；读文件（read_attachment）按同一快照授权；B 版正文/新增卡/关系改义均不出现（`test_board_inspect_and_read_cards_only_see_submission_version`）；源文件变化（附件缺失/非 ready/真实大小≠快照 meta.file.size）→ 标 stale、read=false、提示「请重新提交」，不交付新内容（`test_submission_materials_mark_changed_source_as_needs_resubmit`）；提交后新增附件读取返回 `not_in_submission_scope`（`test_read_attachment_scope_matches_submission_included_materials`）。 |
| #27/#81 支撑事实（真实变更/版本/可读性） | **部分（事实接口已备）** | 材料条目含 version/source/read/reason/file_ref；stale 列表含原因；附件 scope 含 allowed/stale/changed。接口已发 i-submit/lead（`load_submission_snapshot` / `submission_materials` / `submission_attachment_scope`）。 |
| #26 后端事件（i-submit 请求的 2 处） | **已完成（我这边）** | `board_propose_intents` / `board_update_intent` 调 getattr 包装的 `_publish_intents_changed`；helper 缺失/抛错不影响工具结果（3 条用例）。等 i-submit 的 `intent_service.publish_intents_changed` 合入即生效。 |

## 3. 定向测试（本 worktree）

命令（CONTRACT §4 口径）：

```powershell
cd D:\qio-dev\pm-qio032-i-material\backend
$env:QIO_DATA_DIR='D:\qio-dev\pm-qio032-i-material\.tmp-test-data'
$env:QIO_DISABLE_DB_CHECK='1'
& .\.venv\Scripts\python.exe -m pytest <files> -q
```

| 范围 | 结果 |
| --- | --- |
| `tests/pm_qio032_material_scope_test.py` | **15 passed** |
| 既有回归 A：`test_p3_b_tools / test_p3_b_service / test_p3_d_tools_registry / test_p3_d_static_boundaries / test_p3_d_frozen_surface / test_p3_d_authorization_scope / test_interactive_materials / test_attachment_tools` | **111 passed** |
| 既有回归 B：`test_interactive_board / test_p2_d_participation_endpoints_display_version / test_p3_b_tools / test_p3_b_service / test_p3_d_tools_registry / test_p3_d_static_boundaries / test_interactive_materials` | **162 passed** |
| 既有回归 C：`test_p3_a_submission_flow / test_p3_a_turn_binding / test_p3_d_submission_resume / test_interactive_submission` | **66 passed** |

未跑全量 pytest（按 CONTRACT 由 Lead 统一跑）；未跑前端（无前端改动）。

## 4. 新增 / 变更的对外接口

### materials.py（新）

- `load_submission_snapshot(conn, *, board_id, submission_id) -> {available, reason, snapshot, submissionId, boardId, createdAt}`
  —— 提交当时的可见范围冻结投影（A 版）；缺快照的历史记录如实 `available=False`。
- `submission_materials(conn, *, board_id, submission_id, attachments=None, max_chars=DEFAULT_MAX_CHARS, vision=False) -> {available, reason, boardId, submissionId, createdAt, state, cards, materials, images, attachmentIds, stale}`
  —— 提交轮材料：正文 entries + `images[dataUrl]`，排除/隐藏/提交后新增不进；变化源标 stale。
- `submission_attachment_scope(conn, *, board_id, submission_id, attachments=None) -> {available, allowed, stale, changed, ...}`
  —— 附件读取的同一授权判断。
- `material_change_reason(service, att, card) -> str` —— 只认可核对事实的变化判定。

### board_tools.py（字段只增）

- `board_inspect` / `board_read_cards` 响应新增 `basis: {source: "submission"|"current", submission_id, fallback?, created_at?}`；
- 卡片项新增 `file_ref`（filename/availability/size/missing_reason 等源引用事实，不是正文）；
- `board_read_cards` 在绑定提交的轮次里，默认 `source="latest"` 也读 A 版；无快照时退回当前板面并在 `basis.fallback` 说明（existing tests 兼容）。
- `_publish_intents_changed`（内部旁路，供 #26），依赖 i-submit 的 `intent_service.publish_intents_changed`。

### board.py（新，三态纯函数）

- `ACCESS_INCLUDED="included"` / `ACCESS_DISPLAYED="displayed_excluded"` / `ACCESS_HIDDEN="hidden_excluded"`；
- `card_access_state(card) -> str`、`set_card_access_state(state, card_id, access_state) -> dict`。
- i-canvas/i-state 对齐标签：显示并参与 / 显示但不参与 / 隐藏且不参与；恢复隐藏只回第二态。

### attachment_tools.py（行为增量）

- 互动提交轮次的 `read_attachment` 新增拒绝码：`not_in_submission_scope`（提交后新增/未参与/隐藏）、`material_source_changed`（源文件变化，提示重新提交）、`material_scope_unavailable`（快照不可核对，失败关闭）。
- 本轮消息自带附件（`turn_id` 绑定）不受提交快照限制；普通对话轮完全不受影响。

## 5. 需要集成方接线（已发 Lead / i-submit，原样规格）

1. **turn_orchestrator.py**：互动提交轮把 `materials.submission_materials(...)` 的 `materials` 放进 interactive 上下文块、`stale` 作为 read_denied 事实、`images` 经 `understanding.image_parts_from_context({"imageParts": ...})` 并入 `plan.media_parts`。
2. **app.py / turn_orchestrator.py**：`set_read_grants` 的 allowed 并入 `submission_attachment_scope(...)["allowed"]`（每个 id 走既有 `attachment_access.authorize_read` 网关），否则 A 版板面材料的 `read_attachment` 仍会被前置 `read_allowed` 拦掉。
3. **i-submit**：补 `intent_service.publish_intents_changed` 后 #26 事件即生效（我这边调用已就绪）。

## 6. 未做 / 已知风险

- **#01 轮次入口接线未落**（按 Lead 决定转 i-submit）；在接线完成前，「首轮立即拿到材料」只在材料数据源/工具读取层成立，PNG 还不会被作为多模态内容发给模型。
- **#28 变化检测边界**：只按「附件记录缺失 / 非 ready / 真实文件大小 ≠ 快照 meta.file.size」判定；同大小内容被替换检测不到（不在快照里的哈希无法凭空得到），报告为已知限制；若要更强，需要 i-submit 在提交受理时把附件内容版本（sha256）附进 delivery/snapshot（接口可另发）。
- **快照缺失的历史提交**：board 工具退回当前板面并标 `basis.source="current"`+`fallback`，不假装 A 版；这是兼容取舍，验收时按真实提交（有 visible 快照）验证。
- 用户侧「查看板面」HTTP `GET /state` 仍返回用户当前保存的 B 版（这是用户自己的板面，不是模型视角）；模型三路（查看/读卡/读文件）返回 A 版，符合 #28 的模型侧语义。
- 未改 schema；未新增依赖；未跑全量 pytest / 前端类型检查（按分工）。

## 7. 第三轮（合并父分支后 · 2026-11-13）

### 7.1 合并

- `git merge fix/pm-qio032-interactive` → fast-forward 到 `9f80b0cf`（父分支已含本分支前 4 个提交）。
- 合并后父分支的 i-submit 接线已存在：`turn_orchestrator.py` 调 `materials.submission_materials`（首轮交付）
  与 `submission_attachment_scope`（read grants）；`submission.py`/`intent_service.py` 改动不在本分支名下。

### 7.2 任务 1：#01 材料数据源（TXT 文件卡正文）

- `materials.submission_materials` 对参与项 TXT 文件卡返回 `source="attachment_text"`、`read=true`、
  `text` 为真实正文；PNG 仍走同一入口（`image_delivered` + `images[dataUrl]`）。
- 附件未就绪（`prepared` 等非 ready）→ 该卡进 `stale`、材料条目 `read=false`、原因含「重新提交」，
  不交付任何正文；附件服务不可用时不做假变化判定（保持既有 unreadable 口径）。
- 证据：`test_txt_file_card_body_is_delivered_as_attachment_text`、
  `test_hidden_and_post_submission_files_are_not_delivered`、
  `test_not_ready_attachment_is_stale_never_delivered`。
- 集成证据：`tests/pm_qio032_submit_first_round_materials.py` 3/3 通过（真实 AppContext/TurnOrchestrator，
  fake adapter 捕获首轮请求：TXT 正文进入 interactive 上下文块、PNG 作为图片块、无视觉时 image_unsupported）。

### 7.3 任务 2：#28 同大小内容替换（哈希核对）

- 新增 `materials.material_version_facts` 与 `materials.stamp_snapshot_material_versions`：
  提交受理时把每张文件卡附件的当前内容版本（`sha256` / size / state / origin / sourceKind）冻结进
  提交快照的**顶层 `materialVersions`** 键；只加一个键，不改 cards/groups/links/selection，
  `submission.content_fingerprint` 对有无该键结果相同（不影响重复提交判定）。
- `load_submission_snapshot` 读回该事实；`material_change_reason` 优先按哈希核对，
  **同大小内容替换也能识别**：`stale` + 原因含「内容哈希与提交时不一致」+「重新提交」，且不交付新内容；
  有记录哈希但当前算不出（读不到 / 超过 `MATERIAL_HASH_MAX_BYTES`=64MiB）时如实报「无法核对」并拒绝交付。
- 证据：`test_same_size_content_replacement_is_detected_with_stamped_versions`、
  `test_stamp_snapshot_material_versions_only_adds_a_key`。

### 7.4 任务 3：三态最终形态复核

- 旧卡（hidden=true + 冲突的 participation=included）迁移后恒为「隐藏且不参与」；
  用户恢复显示只回到「显示但不参与」，不会借迁移把参与意图扩回。
- 旧文件材料（旧规则默认可见）迁移后仍为参与，不被「旧卡一律排除」误伤。
- 证据：`test_legacy_hidden_card_restore_stays_displayed_not_included`、
  既有 `test_legacy_conflicts_never_expand_reading_scope`、`test_card_access_state_maps_three_states_and_restore_defaults_to_displayed` 保持绿。

### 7.5 给 i-submit 的接口（#28 精确版本核对，需 submission.py 受理处调用）

```python
# submission.py：succeeded 路径、记录 visible/after_state 之前（两处都 stamp 也可只 stamp after，
# 但 visible 是 load_submission_snapshot 的首选来源，建议两处一致）
from agent.interactive import materials as _im
stamper = getattr(_im, "stamp_snapshot_material_versions", None)
if callable(stamper):
    visible = stamper(conn, visible)            # attachments 省略时按 DB 位置推导服务（生产 data_dir 一致）
    after = stamper(conn, after)
```

- 语义：只加顶层 `materialVersions={cardId: {attachmentId,name,size,state,origin,sourceKind,sha256}}`；
  不改变任何既有字段与求差/指纹结果，旧记录无该键时行为与原来完全一致（只按 size 核对）。
- 成本：受理时对参与项文件卡计算一次内容哈希（≤64MiB；超过不硬算、如实降级），可用 `attachments=` 传入
  生产 AttachmentService 避免重复推导。
- 不做也能工作（现状只按 size 核对，同大小替换检测不到）；做了才能补齐 #28「同大小内容替换」。

### 7.6 第三轮定向测试

| 范围 | 结果 |
| --- | --- |
| `tests/pm_qio032_material_scope_test.py` | **21 passed** |
| `pm_qio032_submit_first_round_materials / pm_qio032_submit_authorization_facts / pm_qio032_submit_intents_event` + `test_p3_b_tools` + `test_interactive_materials` | **76 passed** |
| `pm_qio032_submit_empty_semantics` + `test_interactive_board` + `test_p3_b_service` + `test_p3_d_frozen_surface` + `test_attachment_tools` + `test_p2_d_participation_endpoints_display_version` | **148 passed** |
| `test_interactive_submission`（合并父分支后的提交/板面集成回归） | **45 passed** |

### 7.7 未做 / 风险（第三轮）

- submission.py 的 stamp 调用归 i-submit；未接线前 #28 仍只按 size 核对（现状如实提示，不静默换内容）。
- 浏览器级 E2E ①-6b（TXT 首轮交付）仍需 Lead 复跑确认；后端真实编排链路测试已 3/3 通过。
- `MATERIAL_HASH_MAX_BYTES` 之外的超大材料：有记录哈希但无法核对 → 拒绝交付并要求重新提交（保守、如实）。
