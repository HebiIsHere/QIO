# 互动页 v2 · Phase 4-C 旧数据兼容与功能覆盖审计

> 工作区：`D:\qio-dev\qio-i2-p4-c`；分支 `qio-i2/p4-c`；基线 `e0af7d16`
> 审计日期：2026-10-24。性质：**只读审计**；本文件之外未改生产代码、迁移或测试。
> 事实优先级：代码 > 迁移 > 冻结契约 > docs/status / delivery；引用 `路径:行` 相对仓库根。

## 0. 结论摘要

**旧板面可加载；participation 一次性迁移、checked/hidden 兼容、meta.display/版本、group collapsed、group:<id> 端点、意图 display/retry 持久化均已落地；旧入口大多保留。仍有 2 个 P1 兼容缺口与若干 P2 一致性风险。**

1. **P1 自动重试“数据持久化但启动不继续调度”**：retry 系列和 intent Turn 认领都在 SQLite；`pending_auto_retries` 只在互动 Turn 收尾调用（`backend/src/agent/services/turn_orchestrator.py:1151`、`:1216`），启动无扫描。硬崩溃遗留的 `board_intent_turns.active` 无实例归属/回收，可能永久挡住新 Turn。delivery 也承认（`docs/interactive-v2/delivery.md:49-50`）。
2. **P1 重试状态前后端词表不一致**：后端 `pending/running/exhausted/stopped/completed`（`backend/src/agent/interactive/intent_service.py:86`）；前端 `retryStatusLabel` 只认 `scheduled/auto_retry_scheduled/retrying/running/exhausted/failed/cancelled/manual_pending/manual`（`frontend/src/components/interactive/v2/retryApi.ts:269-288`）。`pending/stopped/completed` 显示“未识别状态”；`retryInFlight` 不含 `pending`（`:291-294`），自动重试等待中手动按钮可能仍可点。
3. **旧 DemoIntentEntry 未挂载**：组件在，但 `InteractiveView.vue` 挂载列表没有它，全仓库无其它非测试引用；演示意图 UI 入口丢失，后端 demo 路由仍在。
4. **组端点前端归一化只看“存活成员”**：`frontend/src/interactive/board.ts:800-834` 的 `normalizeLink` 不校验 participation；全部 excluded 的组仍可能在画布保留组端点线，后端提交投影/本地范围预览会过滤。不是交给 QIO 的越权，但画布显示与本次可见范围不一致。
5. **display 解析细节漂移**：前端 `applyDisplayRatio`（`board.ts:573-638`）接受数字字符串、任意 file 卡；后端 `models.clean_card_display`（`models.py:551-581`）要求数值类型且图片名/附件卡。正常路径一致，旧/坏数据可能“前端保留、后端清掉”并改写 w/h。

## 1. 方法与实测

读：`interactive/{models,board,board_store,submission,intent_service,board_tools,materials}.py`、`services/turn_orchestrator.py`、`api/interactive_intents.py`、迁移 56/补偿 57、前端 `interactive/board.ts`/`types.ts`/`stores/interactive.ts`/`v2/*`/旧入口组件、交付说明与契约 §5/§6。

后端定向（真实 venv，PYTHONPATH 指向本 worktree）：

- `pytest tests/test_p2_d_participation_endpoints_display_version.py tests/test_p2_d_migration_append_only.py tests/test_p3_d_intent_resume_retry.py tests/test_p3_d_migrations.py tests/test_text_license.py tests/test_text_migration.py tests/test_interactive_submission.py tests/test_p2_d_frozen_models_prompts.py tests/test_p3_d_static_boundaries.py tests/test_p4_c_legacy_cleanup.py` → **200 passed**。
- `pytest tests/test_interactive_migration_integration.py tests/test_p3_d_submission_resume.py tests/test_p3_d_batch_api_content.py tests/test_p3_d_authorization_scope.py tests/test_frozen_materials_packaging.py` → **39 passed**。

前端定向（本 worktree 无 node_modules；临时把 `frontend/node_modules` Junction 到 `D:\qio-dev\qio-interactive-v2\frontend\node_modules`，跑完删除、未提交）：

- 12 个兼容 spec（p2-int-store、p2-int-store-board、linkEndpoints、image-card-simplification、image-cover-card、image-detail-card、imageDisplay、p2b-group-frame、p3a-intent-layer、intentWorkbench、IntentWorkbench、IntentBatchTraySwitchBar）→ **12 files / 185 tests passed**。
- `npx vue-tsc --noEmit` → 退出码 0（通过）。
- 额外一行验证：`board.normalize_state` 保留 `group.collapsed=True`，group version 仍为 1。

**未跑**：完整后端/完整前端/完整 CI/真实模型端到端。

## 2. 逐项

### 2.1 旧板面加载

结论：可加载；旧 code/url 逐卡迁移、组/链接/选择保留；旧 image 卡是有意损失。

- `backend/src/agent/interactive/board_store.py:85-133`：`load_board` 读 `board_states`，`text_migration.migrate_state` 有变化则保存一次（reason=text_card_migration），第二次不再写。
- `text_migration.py`：旧 code/url → 带 `meta.doc` 的文字卡，id 不变；旧卡 checked=True（随后 participation 迁移会按旧 code/url 规则标 excluded）。
- `models.loads`（`models.py:140-153`）：板面状态在读取层 `sanitize_board_state`，补 participation/marker、清洗 display；所以旧板面首次 GET 就带参与状态。
- 保存：`board_store.save_board`（`:223-273`）先 text_migration、再 normalize、最后以数据库上一版 state 为版本基线。
- 前端：`stores/interactive.ts:1600-1612` `adoptServerFacts` 用 `applyParticipation` 对齐服务器事实；`board.ts:202-252` 补 `participationVersion=1`。
- **旧 kind="image"/meta.image**：前端 `board.ts:648-652` 明确不兼容，未知 kind 落 text；后端 `board._normalize_card` 只认 `CARD_KINDS`。旧图片卡附件关联不显示，是契约 §16.3 的有意不迁移，不是本轮回归。
- 观察：未触发 text migration 时 `load_board` 返回清洗后的 raw state，旧卡可能暂时无 `version`，直到下一次保存/工具读取；前端 version 可选，不阻塞。低风险。

### 2.2 participation 一次性迁移

结论：已实现且幂等，规则与契约一致。

- `models.py:259-287` `_legacy_participation`：hidden=true→excluded；legacy code/url→excluded（迁移成 text 后保留 `meta.language/meta.url` 也 excluded）；file→included；text/reply 保留旧 checked；其余 excluded。
- `card_participation`（`:290-304`）优先级 hidden > legacy kind > 显式 participation > 旧规则；显式值优先。
- `migrate_participation_state`（`:626-654`）写 cards participation + `participationVersion=1`；`sanitize_board_state`（`:657-672`）同时清 display。
- `models.loads` 对板面状态自动调用 sanitize（`:140-153`），GET 即迁移，无用户可见历史页。
- 前端 `board.ts:120-200` 同口径；`stores/interactive.ts:254` 用 `participationVisibleRange` 做本地范围预览。
- 实测：`test_p2_d_participation_endpoints_display_version.py` 覆盖各 kind、hidden、显式值优先、幂等、marker、迁移后旧 code；前端 p2-int-store 通过。

### 2.3 checked / hidden 兼容

结论：保留为历史字段；迁移后不再决定可见性，旧写入口会同步 participation。

- 后端 `card_participation` hidden 恒定优先（`models.py:295-297`）；`board.set_checked`（`board.py:958`）/`set_hidden`（`:982`）/`update_card` 会镜像 participation。
- 前端 `syncLegacyParticipation`（`board.ts:175-200`）、`normalizeCard`（`:669-702`）只在没有显式 participation 的旧数据上推导 checked/hidden 镜像；显式 participation 后不反写旧字段，避免旧界面“未勾选”加载后变已勾选。
- 差异：前端 `setHidden(false)` 会重新 included（`board.ts:1449-1464`）；后端 `board.set_hidden(false)` 只清 hidden、不改 participation（`board.py:982`）。正常 UI 走前端 board.ts，不会触发后端函数；脚本/工具直接调后端时语义不同。低风险。
- G6 互斥仍保留（`board._normalize_card`）。

### 2.4 meta.display / 版本

结论：display 数据与比例约束双端实现；display 不参与材料语义版本；旧/坏 display 不猜。存在解析细节漂移。

- 后端 `models.clean_card_display`（`:551-581`）：只接受有限正数、上限 100000、比例 1:1000；只有 aspectRatio 可保留；非法/未知/非图片文件卡丢弃 display、不改 w/h。`apply_card_display`（`:583`）宽度为主算 h。
- 语义隔离：`board._semantic_meta` 排除 display（`board.py:410-414`）；`_card_signature` 不算 display（`:417-425`）；`materials._content_version`、`submission._meta_key/content_fingerprint` 均排除 display。纯 display 变化不推进 version、不改变提交内容。
- 权威版本：`board_store.save_board` 以数据库上一份 state（text_migration + normalize）为基线，调用 `board.apply_versions_from_previous`（`board.py:493`）按语义指纹推进；客户端旧/无 version、重启都不退回 1；工具 `write_board_cas` 同基线。
- 前端 `applyDisplayRatio`（`board.ts:573-638`）与后端大体一致；`normalizeCard` 保留 `version`（`:719-721`），display 变化不碰 version/updatedAt。
- 漂移：前端 `positive` 用 `Number(value)` 接受字符串数字，后端只接受 int/float；前端任意 file 卡接受 display，后端要求图片名/附件卡。旧/坏数据可能出现两端不同 w/h。
- 实测：后端 P2-D 用例覆盖合法/ratio-only/NaN/0/超大/非图片文件/display 不改 version；前端 imageDisplay 等通过。

### 2.5 group collapsed

结论：前端持久化、纯显示；后端通过 `dict(raw)` 保留未知字段，版本指纹不含 collapsed；后端没有专门类型校验。

- 前端 `types.ts:54` `BoardGroup.collapsed?: boolean`；`board.ts:785-787` 只保留 true；`setGroupCollapsed`（`:1211-1225`）只改 collapsed + updatedAt，不碰 version。
- 后端 `board._normalize_group`（`board.py:234-273`）用 `group = dict(raw)` 保留 collapsed；`_group_signature`（`:428-435`）只含 name/ordered/members/deleted，收起不推进版本；保存 round-trip 保留。
- 一行验证：`board.normalize_state` 输入 collapsed=True → 输出 True、version=1。
- 风险：若旧/坏数据写字符串 `"false"`，前端 Boolean 当 true，后端原样返回字符串。低风险；建议后端归一化 bool。

### 2.6 group:<id> 端点

结论：新链端到端接通；旧裸 card id 不变；前端画布与后端可见范围有一处显示不一致。

- 后端 `models` 的 `parse_link_endpoint/normalize_link_endpoint/link_endpoint_visible/link_visible/visible_links`（`models.py:440-...`）；`board._normalize_link`（`board.py:276-315`）校验存在/未删/组有 included 成员；`add_link/update_link` 同步；`dissolve_group` 删组端点线、保留成员卡片线；`submission.project_snapshot/project_baseline` 用 `visible_links`；board_tools 同一可见/授权规则，组端点需 scope.group_ids。
- 前端 `interactive/linkEndpoints.ts` + `board.ts:800-834` 归一化；裸 id 不变；group 端点需组存在且至少一名存活成员；version 原样保留。
- 差异：前端只看“存活成员”，不看 participation；所有成员 excluded 的组，组端点链接在画布数据里仍存在（后端提交/本地范围预览会过滤）。用户看到线但提交不带，不是数据泄露，但显示与本次范围不一致；建议前端归一化/渲染加 participation 过滤。
- 实测：后端 P2-D 组端点用例、前端 linkEndpoints/p2b-group-frame 通过。

### 2.7 意图 display / retry 重启恢复

结论：display 与 retry 数据持久化；display 读写完整；retry 数据跨重启、但自动调度/陈旧认领不跨重启；前端状态词表缺 3 个值。

- display：迁移 56 `board_intents.display TEXT DEFAULT '{}'`（`storage/migration_p3_d.py:26`）；`intent_service._intent_view` 输出 display（`intent_service.py:255-305`）；`set_intent_display`（`:524-548`）+ API `POST /intents/{id}/display`（`api/interactive_intents.py:970-995`）只改 display，不推 version/授权；前端 `saveIntentDisplay` + IntentLayer 本地 override。本地 localStorage 优先于服务端 display（`v2/intentDisplay.ts:82-95`），跨设备服务端改位时本机继续显示旧位置（低风险）。
- retry：迁移 56 `board_intent_retries`（`migration_p3_d.py:50-75`）；字段 series_id/intent_id/authorization_version/attempts/manual_attempts/retry_limit=3/status/last_error/next_action/retryable/turn_id/final_notified_at/exhausted_at。服务 `record_retry_failure`（`intent_service.py:406`）、`claim_auto_retry/claim_manual_retry/release_retry/list_retry_series`；API `GET /boards/{id}/retry-series`、`GET /intents/{id}/retry-series`、`POST /intents/{id}/retry`（`api/interactive_intents.py:1001-1045`）；调度在 `turn_orchestrator._finish_interactive_turn`（`:1151`）调 `pending_auto_retries`（`:1216`）。
- 缺口 A（P1）：启动不扫描 pending；`pending_auto_retries` 只在 Turn 收尾调用，重启后 pending 要等下一次互动 Turn。
- 缺口 B（P1）：`board_intent_turns` 无 instance_id/启动回收；硬崩溃留下的 active 让新 `claim_intent_turns` 一直返回“已有活跃 Turn”，`start_interactive_turn` 返回旧 turnId（`submission.py:1551-1625`、`:1714-...`）。delivery 也列为已知限制。
- 缺口 C（P1）：状态词表不一致（见 §0）；`pending` 不在 in-flight，可能让用户在自动等待时点手动重试。
- 缺口 D（P3）：manual retry 路由传 source=manual_retry，但 `TURN_SOURCES` 只有 confirm/batch_confirm/recover/retry（`submission.py:1527`），认领记为 confirm；只影响审计标签。
- 实测：`test_p3_d_intent_resume_retry.py` 覆盖首次失败不计数、3 次自动、耗尽、不可恢复、跨新连接持久化、orchestrator 排队；通过。未测真实进程重启。

### 2.8 旧入口能力是否丢失

| 旧入口 | 当前挂载/引用 | 能力现状 | 结论 |
| --- | --- | --- | --- |
| IntentWorkbench.vue | `InteractiveView.vue:18,271-286`（HUD“意图与结果”） | 新链确认/等待/账本/结果/恢复；旧链 legacy 只读 | 保留；与新 IntentLayer 确认入口重叠 |
| IntentBatchTray.vue | `InteractiveView.vue:15,284` | 旧链批次审批/几何控制保留；新链批量在 IntentLayer | 保留；存在两套批量语义 |
| IntentStatusPopover.vue | `InteractiveView.vue:16,283` | 旧链任务/暂停/继续/取消/定位；新链暂停取消未全迁（delivery 已列） | 保留；只对旧链有效 |
| IntentPreviewCard.vue | 只在 `IntentStatusPopover.vue:28,659` | 虚线预览/批准/拒绝/执行控制 | 保留 |
| ImpactConfirmDialog.vue | `InteractiveView.vue:17,285` | 影响确认/撤回 | 保留 |
| SubmitCluster.vue | `BoardToolbar.vue:26,241` | 提交状态/失败/未知/重试/范围/草稿提升/旧理解区块 | 保留 |
| DemoIntentEntry.vue | 无挂载；无非测试引用 | 演示意图 UI 丢失；后端 demo 路由仍在 | **能力丢失（演示）**，需 Lead 决定 |
| ChatDock.vue | 无挂载；`InteractiveView.vue:308` 用 InteractiveConversation（MessageStream/Composer/ComposerEntries） | 会话/发送/停止/排队/草稿/附件/失败恢复走会话 store；ChatDock 旧专用呈现（过程/轮次错误/执行记录）未逐项对照验收 | 组件退役；核心能力应复用，旧专用覆盖**未完全验证** |
| 旧“理解”入口 | SubmitCluster 内保留 | 新提交已改主循环 Turn；旧理解只对历史有意义，可能显示过期理解 | 兼容展示，需确认不冒充新事实 |

补充：旧批处理/工作台仍在，与新 IntentLayer 同页存在同类动作入口；不是回退，但有认知风险。

## 3. 风险清单

| 级别 | 风险 | 证据 | 建议 |
| --- | --- | --- | --- |
| P1 | 崩溃后 stale active 永久挡住新 Turn | submission.py:1551-1625 无实例归属；delivery.md:50 | 迁移追加 owner_instance_id/启动回收，或按 Turn 状态回收 |
| P1 | 重启后 pending 自动重试不继续 | turn_orchestrator.py:1151/1216 | 启动扫描 pending 并由服务端调度 |
| P1 | retry 状态词表不一致 | intent_service.py:86 vs retryApi.ts:269-294 | 统一状态或补映射；pending 加入 in-flight |
| P1 | 演示入口丢失 | 无挂载 DemoIntentEntry | 明确产品决定；补挂或删组件并更新文档 |
| P2 | 组端点画布显示与可见范围不一致 | 前端 normalizeLink 只看存活成员；后端 link_visible 要 participating | 前端归一化/渲染加 participation 过滤 |
| P2 | display 解析漂移 | board.ts:573-638 vs models.py:551-581 | 收敛数值类型/图片判定 |
| P2 | checked/hidden 取消隐藏语义两端不同 | 前端 setHidden 重新 included；后端 set_hidden 只清 hidden | 统一或标注；正常 UI 不触发后端函数 |
| P2 | group.collapsed 后端不校验类型 | board.py dict(raw) | 归一化 bool；不影响版本 |
| P2 | 旧 GET 可能暂时无 version | board_store.load_board 无迁移时返回 raw | 可接受；如需统一在 GET 前 normalize |
| P2 | 本地 intent display 覆盖服务端 | intentDisplay.ts:82-95 | 明确“本机优先”或合并策略 |
| P3 | manual_retry 来源被记成 confirm | submission.py:1527/1854 | 把 manual_retry 加入 TURN_SOURCES |
| P3 | 旧批处理/工作台与新 IntentLayer 入口重叠 | InteractiveView 同时挂载 | 加“旧链只读”标识或后续下线 |

## 4. 未测 / 未验证

- 未跑完整后端 pytest/完整前端 vitest/CI；只跑定向兼容 spec。
- 未做真实模型/真实主循环端到端；重试/Turn 认领测试均用 fake turns。
- 未在真实进程重启下验证 stale active 回收（代码确认无回收）、未测启动调度 pending。
- 未逐项人工验收 ChatDock 旧专用呈现是否在 InteractiveConversation 完全等价；只确认复用 MessageStream/Composer/ComposerEntries。
- 未验证旧 image/meta.image 数据在真实用户库中的比例与损失（契约明确不迁移）。
- 未验证 320px/暗色/减少动画下的 collapsed/组端点/display 视觉；delivery 已列未完成。
- 前端测试使用临时 node_modules Junction（借用集成 worktree），之后已删除；本 worktree 不含 node_modules。

## 5. 建议 Lead 冻结/后续

1. P1 先修 retry 重启：启动扫描 pending + stale active 回收（实例归属或启动清理）。
2. 统一 retry 状态词表（后端 5 值 vs 前端 9 值）；pending 加入 in-flight，手动按钮禁用。
3. 明确 DemoIntentEntry 去留：补挂调试入口或删除死组件并更新文档。
4. 旧入口下线顺序：IntentBatchTray/StatusPopover/Workbench/SubmitCluster 仍挂载，不能直接删；先确认新入口覆盖、加“旧链只读”标识。
5. display/组端点前端一致性：组端点加 participation 过滤；display 解析与后端同一规则。
6. 可选：旧 GET 统一 normalize 补 version，或在文档明确“首次保存才补 version”。

## 6. 关键证据索引

- 旧板面/迁移：board_store.py:85-133、223-273；text_migration.py；models.py:140-153。
- participation：models.py:259-321、626-672；frontend board.ts:120-252。
- display/版本：models.py:508-638；board.py:401-540；board_store.py:223-273；frontend board.ts:566-638。
- collapsed：frontend board.ts:785-787、1211-1225；backend board.py:234-273、428-435。
- 组端点：backend models.py:440-...、board.py:276-315、submission.py；frontend board.ts:800-834、linkEndpoints.ts。
- intent display：migration_p3_d.py:26；intent_service.py:255-305、524-548；api/interactive_intents.py:970-995。
- retry：migration_p3_d.py:50-75；intent_service.py:86、406-700；turn_orchestrator.py:1151-1250；frontend retryApi.ts:269-294。
- 旧入口：InteractiveView.vue:15-18、268-286；BoardToolbar.vue:26、241；InteractiveConversation.vue:15-17。
- 测试：backend test_p2_d_participation_endpoints_display_version.py、test_p3_d_intent_resume_retry.py、test_text_migration.py、test_text_license.py、test_interactive_migration_integration.py；frontend p2-int-store/p2-int-store-board/linkEndpoints/p3a-intent-layer/image*。
