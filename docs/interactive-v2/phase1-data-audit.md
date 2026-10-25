# 互动页 v2 · Phase 1-C：旧能力与数据/接口审计

> 审计对象：互动页（/interactive）现有卡片、文件图卡、组、连线、参与讨论、意图、审批、草稿、
> 恢复、账本、提交、重试能力，以及旧入口（IntentWorkbench / BatchTray / StatusPopover /
> SubmitCluster 等）的可迁移能力与数据/接口来源。
>
> 工作区：D:/qio-dev/qio-i2-p1-c；分支 qio-i2/p1-c；基线 13ec0935。
> 性质：**只读审计**。本文件之外没有改任何生产代码、测试或迁移。
> 事实优先级：**代码 > 迁移 > 契约/架构 > docs/status**。发现不一致时以代码为准，并在 §6.2 单列。
> 引用格式 路径:行 均相对仓库根；行号对应基线提交。

---

## 0. 给 Lead 的结论（先读这 10 条）

1. **基线里存在两代意图/执行链并存**：旧链 `intents.py + understanding.py + execution.py`
   （表 `board_understanding` / `board_executions`，状态 `pending/needs_update/...`），
   新链 `intent_service.py + board_tools.py`
   （表 `board_operations` / `board_intent_versions`，状态 `proposed/awaiting_confirmation/...`）。
   前端主 store 仍绑旧链，只有 `IntentWorkbench.vue` 读新链；**两套状态词表、两套审批动作、两套执行记录**
   （`frontend/src/stores/interactive.ts:3711` 起的理解/执行段 vs
   `frontend/src/components/interactive/IntentWorkbench.vue:136`）。v2 不能同时消费两套。
2. **提交已经改为主循环 Turn 受理，但前端仍按旧「理解请求」模型工作**：
   `submission.submit_board` 只注入 `turn_acceptor`，返回 `delivery.understandingId = null`、
   `status = accepted`（`backend/src/agent/interactive/submission.py:2154-2173`、`:2285-2298`）；
   而 store 提交成功后仍调用 `startUnderstandingPolling()` 并轮询
   `GET /understanding`（`frontend/src/stores/interactive.ts:3005-3021`、`:4048`）。
   基线里新提交**不再产生** `board_understanding` 行（`understanding.py` 文件头已写明退役，
   `backend/src/agent/interactive/understanding.py:1-12`）。结果是「已受理」可能永远不结束，且旧记录可能冒充本次。
3. **新链「确认」只写授权快照与 `approved`，不会创建续作 Turn，也不会启动任何执行**：
   `confirm_intents` 只做状态/授权/冲突校验并 `status=approved`
   （`backend/src/agent/interactive/intent_service.py:850-943`）；
   全仓库 `turns.submit` 只出现在提交受理、恢复流程与通知
   （`backend/src/agent/api/interactive_store.py:373`、
   `backend/src/agent/api/recovery_routes.py:109`、
   `backend/src/agent/services/app.py:1912/2233`），
   **确认路径没有任何创建 Turn 的代码**。契约 `docs/qio-refactor-contract.md:696-698` 与
   `docs/architecture.md:1453-1455` 所说的「确认后自动续作执行」在代码里没有实现入口，
   目前只能等用户下一条互动聊天触发 Turn（`submission.resolve_chat_binding` 会带上
   `approved/running/paused/partially_completed` 的意图 id，
   `backend/src/agent/interactive/submission.py:1558-1581`）。
4. **新链没有暂停/取消 API**：`intent_service.pause_intent / cancel_intent` 存在
   （`backend/src/agent/interactive/intent_service.py:1369-1385`）但没有任何路由调用它；
   现有 `/api/interactive/intents/{id}/pause|resume|cancel` 走的是旧链
   （`backend/src/agent/api/interactive_intents.py:274-292` → `intents.py` → `execution.py`）。
   新链状态机里有 `paused/cancelled`，却没有可用的程序入口。
5. **对象 version 在真实往返中会退化成 1，CAS/材料版本保护事实上失效**：
   后端在 `board._normalize_card` 写入 `card["version"]`
   （`backend/src/agent/interactive/board.py:186-209`），
   但前端 `normalizeCard` 显式重建对象、**不带 version**
   （`frontend/src/interactive/board.ts:309-356`；group/link 同理 `:374-435`），
   而 `save_board` 直接以客户端 state 为准
   （`backend/src/agent/interactive/board_store.py:223-260`）。
   因此任何一次前端编辑再保存，卡片/组/链接 version 都回到 1；
   `_validate_basis`（`intent_service.py:346-356`）、
   `check_expected_versions`（`board_store.py:606-636`）与工具写前 CAS
   （`board_tools.py:331-337`）实际比较的是同一个 1。
   这与冻结契约「原样使用服务返回的 version，写前 CAS」直接冲突（`docs/qio-refactor-contract.md:718-719`）。
6. **新链没有任何「审批预览」数据**：`IntentSpec` 不含 preview
   （`backend/src/agent/interactive/tool_models.py:246-289`），
   `_insert_intent` 把 `preview` 与 `impact` 都写成 `'{}'`、
   `operations` 写成 `'[]'`（`backend/src/agent/interactive/intent_service.py:489-497`）。
   旧链的虚线卡片/组/关系预览与影响说明（`IntentPreviewCard.vue`、`ImpactConfirmDialog.vue`）
   无法直接用于新意图；迁移时这是**功能删减风险**，不能直接删。
7. **参与/许可判定在前后端有多份实现**：后端唯一权威是
   `models.visible_except_deleted / selectable_cards`
   （`backend/src/agent/interactive/models.py:215-258`）；
   前端至少有 `board.CHECKABLE_KINDS`（`frontend/src/interactive/board.ts:100`）、
   `submission.isVisibleCard`（`frontend/src/interactive/submission.ts:75-81`）、
   `store.cardSubmittedFor`（`frontend/src/stores/interactive.ts:3360-3364`）三份同义规则，
   另有 `board.normalizeState` 的 G6/G7 与 `BoardCard.vue` 的 UI 标签。
   `checkedCleared` 后端恒为 `[]`（`submission.py:2259-2261`），但
   `/visible-range` 仍在返回「提交成功后这些勾选会自动取消」
   （`backend/src/agent/api/interactive_store.py:310-320`），文案与事实相反。
8. **位置、版本、重试、续作四类数据都是碎片**：板面 viewport 只在内存
   （`frontend/src/interactive/viewport.ts:277-287` 只发布可用区，不落盘）；
   阅读位置单独存在 `board_preview_state`（`backend/src/agent/api/preview_routes.py:246-345`）；
   对话窗口位置在本机 localStorage（`frontend/src/interactive/chatFrame.ts`）；
   等待/续作有持久化（`board_waiting`、`board_submissions.turn_id/resume_state`、
   `board_submission_turns`），但 store 的 `load()` **完全不读**等待/绑定
   （`frontend/src/stores/interactive.ts:1788-1812`）。没有「一处能恢复续作」的聚合接口。
9. **前端 MaterialSource 与后端已漂移**：后端新增
   `image_delivered / audio_delivered / audio_transcribed / audio_unsupported / video_delivered /
   video_sampled / video_unsupported`（`backend/src/agent/interactive/models.py:309-325`），
   前端类型与标签仍是旧集合（`frontend/src/interactive/types.ts:394-401`、
   `frontend/src/interactive/materials.ts:17-25`），遇到新来源会显示「未知来源（英文原值）」。
   同类漂移还包括 `docs/status.md`「尚未完成」仍说提交会创建理解请求
   （`docs/status.md:1751-1756`），与 P71 后的代码事实相反。
10. **旧入口不能直接删**：`SubmitCluster.vue` 承担提交的全部诚实状态
    （empty/duplicate/unknown/refresh-failed/acceptance 查证/草稿提升/影响阻断），
    `IntentBatchTray.vue` 承担批次分组与跨浮层几何，
    `IntentStatusPopover.vue` 承担执行控制（pause/resume/cancel）与定位，
    `IntentWorkbench.vue` 承担新链确认/等待/账本/结果/恢复。它们的能力必须逐项迁走（见 §3），
    否则 v2 会丢掉「失败可解释、结果不丢、任务可恢复」这些已经验收过的行为。

---

## 1. 口径与范围

### 1.1 已读事实源（本审计的直接证据）

- 全局/页面：`AGENTS.md`、`docs/status.md`、`docs/architecture.md`（§18 互动主循环、§20 对话页 v2）、
  `docs/conversation-page-contract-20261022.md`、`docs/interactive-mode-contract.md`（§1、§2、§4、§14–§17）、
  `docs/qio-refactor-contract.md`（§13）。
- 前端：`frontend/src/interactive/{types,board,submission,intentWorkbench,approval,drafts,materials,
  attachmentPreview,geometry,cardLayout,serverFacts,viewport,chatFrame}.ts`、
  `frontend/src/stores/interactive.ts`、`frontend/src/services/interactive.ts`、
  `frontend/src/views/InteractiveView.vue`、
  `frontend/src/components/interactive/{IntentWorkbench,IntentBatchTray,IntentStatusPopover,
  SubmitCluster,IntentPreviewCard,ImpactConfirmDialog,DemoIntentEntry,ChatDock,BoardToolbar,BoardCard}.vue`。
- 后端：`backend/src/agent/interactive/{models,board,board_store,submission,materials,understanding,
  intents,execution,intent_service,board_tools,tool_models,runtime,placement}.py`、
  `backend/src/agent/api/{interactive,interactive_store,interactive_intents,preview_routes,server}.py`、
  `backend/src/agent/storage/{schema,migration_p3_a,migration_p3_b}.py`。
- 迁移号：当前 `SCHEMA_VERSION = COMPENSATION_VERSION = 55`
  （`backend/src/agent/storage/schema.py:1596-1597`）。

### 1.2 结论标记

| 标记 | 含义 |
| --- | --- |
| **已实现** | 后端数据/接口、前端入口与持久化都存在，且行为与当前有效契约一致 |
| **部分** | 数据或接口存在，但入口缺失、前端绑错链路、只读兼容、或只有一端可用 |
| **缺口** | 没有可信的数据表示 / 接口 / 入口，或实现与冻结契约冲突且没有可用路径 |

### 1.3 理解本审计的前提：两代链

| | 旧链（第八/九轮，已退役新流程） | 新链（第三阶段，2026-10-21 起） |
| --- | --- | --- |
| 意图服务 | `interactive/intents.py` | `interactive/intent_service.py` |
| 理解 | `interactive/understanding.py`（主循环改造后不再产生新记录） | 无独立理解记录；提交即主循环 Turn |
| 执行 | `interactive/execution.py`（`board_executions`、`attempt`、checkpoint、pause/resume/cancel） | 12 个板面工具 + `board_operations` 账本；无独立执行记录 |
| 意图状态 | `pending / needs_update / rejected / waiting_dependency / waiting_confirm / running / paused / done / failed / cancelled` | `proposed / awaiting_confirmation / approved / running / paused / completed / partially_completed / failed / cancelled / rejected` |
| 接口 | `/intents`、`/approve`、`/reject`、`/preview`、`/demo/advance`、`/batch`、`/executions`、`/pause`、`/resume`、`/cancel`、`/understanding*` | `/intent-views`、`/confirm`、`/batch-confirm`、`/waiting`、`/ledger`、`/results`、`/recovery-preview`、`/recover` |
| 前端消费者 | store 的 `intents/approve/reject/decideBatch/executions/understanding`；BatchTray、StatusPopover、SubmitCluster、IntentPreviewCard | `IntentWorkbench.vue` + `interactive/intentWorkbench.ts` |
| 当前定位 | **历史只读兼容 + 旧 UI 入口**；不应再写新数据 | **唯一新流程**；但前端只有工作台一个入口，且缺执行入口 |

结论：**v2 必须明确只消费一条链；另一条只读兼容，并在 UI 上标注**。见 §6.3 冻结项 F9。

---

## 2. 现有能力映射（对应任务第 1 项）

### 2.1 卡片 / 文件图卡

| 能力 | 结论 | 证据与现状 |
| --- | --- | --- |
| 文字卡（块序列 paragraph/heading/list/quote/code/link） | **已实现** | 后端 `text_card.py` + `text_migration.py`；前端 `textCard.ts` / `TextCardEditor.vue`；契约 §1.4.1。`card.meta.doc` 是块模型，`card.content` 是纯文本派生。 |
| 遗留 `code/url` 卡 | **已实现（只读迁移）** | 加载时逐张迁成 `text`，迁移完成前整体不可见：`models.LEGACY_TEXT_CARD_KINDS`（`models.py:27-29`）、`board_store.load_board` / `text_migration`；前端 `submission.isVisibleCard` 排除（`submission.ts:67-81`）。 |
| 文件卡（材料） | **已实现** | `kind="file"`，附件只认 `meta.file {name,attachmentId,size}`：`materials.cardAttachmentBlock`（`frontend/src/interactive/materials.ts:93-111`）；添加/重关联/打开/预览见 `BoardCard.vue`、`CardAttachmentPreview.vue`、`AddMenu.vue`；后端 `materials.py` 解析。 |
| 图片（作为文件卡附件） | **部分** | 展示：`object-fit: contain` 等比完整显示，不裁切不拉伸（契约 §16.3，`CardAttachmentPreview.vue:447/501`；`CardAttachmentPreview.test.ts:307-310`）。**比例数据没有落点**：后端预览能算出 `width/height`（`backend/src/agent/services/preview.py:325-348`），但卡片 `meta.file` 只存 name/attachmentId/size；前端完全不读尺寸。卡片尺寸仍是用户设定的固定值。 |
| 结果卡 `reply` | **已实现** | Markdown 渲染（`BoardCard.vue` 的 `kind="reply"` 分支用 `MarkdownContent.vue`，契约 §17.6）；默认未许可、用户勾选后可再次参与（`models.CHECKABLE_KINDS` 加入 reply，`models.py:209-212`；`submission.diff_states` 有 `result_added/result_updated`）；来源 `meta.reply`。 |
| 卡片位置/尺寸 `x/y/w/h` | **已实现（但两端口径不同）** | 前端权威 `board.ts`：`clampCardRect` 允许 ±1,000,000、画布随内容增长、落下后 `rebaseToFit` 把整板移回 ≥0（`board.ts:159-198`、`:1209-1240`）；后端 `board._normalize_card` 只保底 `w/h ≥ 1`，**不夹画布、不重定位**（`board.py:186-209`）。同一份状态在两端可能得到不同坐标语义。 |
| 卡片内预览/阅读位置 | **已实现** | `board_preview_state`（`schema.py:1404-1414`），按 `(board_id, card_id)` 存 `attachment_id + version + position`；只允许 `mode/page/slide/sheet/row/line/zoom/scrollTop`（`preview_routes.py:42-49`），`attachmentId` 与卡不一致 409、旧 version 拒绝写入（`:295-309`）。图片不进阅读态（store `toggleCardPreview` 入口拒绝，`stores/interactive.ts:411-440`）。 |

**卡片侧结论**：卡片/文件图卡主体能力已具备，v2 的主要缺口不是渲染，而是
①图片比例没有可持久化/可读字段；②位置语义两端不一致；③参与/许可判定多份实现（见 2.4）。

### 2.2 组

| 能力 | 结论 | 证据与现状 |
| --- | --- | --- |
| 建组/加入/移出/解散/改名/有序/合并/组内排序 | **已实现** | 后端 `board.py:447-621`；前端 `board.ts:678-846`；组件 `SelectionMenu.vue`（`group-form/group-join/group-leave/group-dissolve/group-ordered/group-unordered/group-merge`）、拖动重叠成组 `dropCard`。 |
| 组框位置/大小 | **已实现（派生）** | `group.x/y/w/h` 由成员外框 + padding 推导：后端 `_reflow_group`（`board.py:212-224`）、前端 `reflowGroup`（`board.ts:358-372`）。组本身不能拖动，没有独立位置来源。 |
| 组名/默认名/有序语义 | **已实现** | 默认名固定 `默认组名`（`models.DEFAULT_GROUP_NAME`，`models.py:86-108`）；组身份按 id，不依赖名称唯一；有序组 `members` 顺序即序号。 |
| 组版本 `version` | **部分** | 后端 normalizes 并 `_bump_version`（`board.py:103-114`、`:264`）；前端 group 归一化丢弃 version（`board.ts:400-413`），同 §0.5。 |
| **组端点连线**（link 连到组） | **缺口** | 当前所有入口只接受卡片端点：`board._normalize_link` 用 live 卡片集合校验（`board.py:269-287`）、`add_link` 同理（`:627-651`）；前端 `normalizeLink / addLink` 用卡片 live 集合（`board.ts:418-435`、`:848-880`）；`BoardLinkLayer.vue` 只按卡片中心画线（`:46-64`）；契约 G4 明确「链接端点必须是存在且未删除的卡片」（契约 §1.2）。任务模型也没有 link/group endpoint 概念（`tool_models.py:475-488`）。 |

### 2.3 连线

| 能力 | 结论 | 证据与现状 |
| --- | --- | --- |
| 建链/改含义/方向/删除 | **已实现** | 前端 `addLink/updateLink/removeLink`（`board.ts:848-917`）；连线层 `BoardLinkLayer.vue`（方向箭头、含义标签、拖线预览）；后端 `add_link/update_link/remove_link`（`board.py:627-683`）；同一对端点只保留一条（G4）。 |
| 端点可见性 | **已实现（卡片）** | 提交投影要求两端都可见：`submission.project_snapshot`（`submission.py:196-201`）；前端 `localVisibleRange` 用 `isVisibleCard` 两端过滤（`submission.ts:118-124`）。 |
| 组端点 | **缺口** | 同 §2.2。若 v2 要「组—组 / 组—卡」连线，需要同时改：`Link.src/dst` 语义、G4、可见范围、diff 表达式、渲染层、工具参数与 `board_add_link` 校验、旧客户端兼容。 |
| 连线版本 | **部分** | 后端有 `link.version`（`board.py:286`）、前端丢弃（`board.ts:426-435`）。 |

### 2.4 参与讨论（“参与状态”）

当前没有名为「参与状态」的数据；它由一组正交字段表达：

| 维度 | 后端权威 | 前端副本 | 语义 |
| --- | --- | --- | --- |
| 文字卡许可 | `CHECKABLE_KINDS=(text, reply)` + `checked`（`models.py:212`、`:232-234`） | `board.CHECKABLE_KINDS`（`board.ts:100`）、`submission.isVisibleCard`（`submission.ts:75-81`）、`store.cardSubmittedFor`（`stores/interactive.ts:3360-3364`） | 未勾选 → QIO 完全看不到（含卡内链接/代码）；许可持续有效，只有手动取消/隐藏才失效（契约 §1.4、§17.2） |
| 明确隐藏 | `hidden`，G6 强制 `checked=false`（`models.py:224-227`；`board.py:205-208`） | `board.setHidden`（`board.ts:962-972`） | 退出讨论范围 |
| 删除 | `deleted`，G5 不进组/链接/选择（`models.py:205-206`、`board.py:306-337`） | `board.removeCard` | 撤回当前材料/关系 |
| 材料默认在范围 | `visible_except_deleted` 对 `file` 返回 true（`models.py:225-234`） | `isVisibleCard` 对 file 返回 true | 添加材料 ≠ 要求总结/比较/执行 |
| 遗留 code/url | 恒不可见（`models.py:228-231`） | `submission.ts:67-81` | 迁移前不交付 |
| 组/链接的参与 | 组至少 1 名可见成员才进载荷且只列可见成员；链接两端可见才进（`submission.py:183-209`；契约 §1.4） | `localVisibleRange` 同规则（`submission.ts:114-134`） | 组名/关系不泄露未许可内容 |

**结论：部分**。规则本身完整且经过验收，但：
1. **没有统一的 `participation` 状态字段**，UI 只能读 `checked/hidden/deleted` 三个布尔自行拼文案；
2. 前端三份同义实现 + 后端一份，任何一处漂移都会让「显示可见」与「实际交给 QIO」不一致；
3. 组/链接的参与状态没有在对象上暴露，只能通过 `visibleRange` 间接推断；
4. `visible-range` 接口仍返回「提交成功后勾选会自动取消」的错误提示（§0.7）。

### 2.5 意图

| 能力 | 结论 | 证据与现状 |
| --- | --- | --- |
| 旧链意图列表/预览/依赖/冲突/进度/应用与撤回 | **部分（只读兼容）** | `GET /boards/{id}/intents` 返回 `board_intents` 全量（`intents.py:741-757`），状态仍是旧词表；前端 `store.intents` / `approval.ts` / `IntentPreviewCard` / `IntentBatchTray` 消费。新流程不再创建旧理解记录（`understanding.py:1-12`），但旧行仍可读出。 |
| 新链意图视图（目标/预期/依据/scope/依赖/冲突/版本/授权/真实状态） | **已实现（后端+工作台）** | `GET /boards/{id}/intent-views`（`api/interactive_intents.py:551-585`）；服务 `intent_service.list_intents_view/_intent_view`（`intent_service.py:217-259`、`:949-972`）；前端 `IntentWorkbench.vue:136` + `intentWorkbench.ts`。 |
| 新链意图的「预览」（虚线卡/组/关系） | **缺口** | `IntentSpec` 无 preview；`_insert_intent` 写 `preview='{}'`、`impact='{}'`、`operations='[]'`（`intent_service.py:489-497`）；`_intent_view` 不返回 preview（`:217-259`）。旧预览/影响确认能力无法直接复用。 |
| 新链确认（单条 / 同批≥2） | **部分** | 单条 `/confirm`、批量 `/batch-confirm`（`api/interactive_intents.py:598-657`）；`confirm_intents` 写授权快照与 `approved`（`intent_service.py:850-943`）。**没有创建续作 Turn/执行**（§0.3）。 |
| 新链执行状态 | **部分** | 意图状态可由工具推进：`begin_operation` 置 `running`、`finish_intent` 置终态（`intent_service.py:1081-1128`、`:1215-1368`）；操作账本 `board_operations` 记录。但执行只发生在「后续主循环 Turn 调用 12 个 board_* 工具」时，没有独立执行器。 |
| 新链暂停/取消 | **缺口（有函数无 API）** | `intent_service.pause_intent/cancel_intent`（`:1369-1385`）无路由；旧 `/pause|/resume|/cancel` 操作旧 `board_executions`（`api/interactive_intents.py:274-292`）。 |
| 意图版本历史 | **部分** | `board_intent_versions` 每次实质更新写入（`intent_service.py:265-277`，迁移 50 建表），但**没有任何读取接口**（全仓库仅此一处引用）。 |
| 旧链与新链混读 | **部分（有错配）** | `/intent-views` 在服务不可用时把旧状态映射成新状态（`_LEGACY_STATUS_MAP`，`api/interactive_intents.py:370-381`、`:481-533`）；映射出的 `proposed` 会被前端当作「可确认」，但 `/confirm` 调新服务会拒绝 `pending` 状态（`intent_service.py:877-886`）→ 按钮可点但必然失败。 |

### 2.6 审批 / 确认

| 能力 | 结论 | 证据与现状 |
| --- | --- | --- |
| 旧链单项批准/拒绝 | **部分（只读兼容）** | `POST /intents/{id}/approve|reject`（`api/interactive_intents.py:240-260`）→ `intents.approve_intent/reject_intent`；旧 UI `IntentPreviewCard` / `SubmitCluster`。**对新链 `proposed` 意图调用会走旧执行链**：状态不在旧分支里，最终置 `running` 并调用 `ensure_execution`，新意图 `operations=[]` → `no_operations`，留下没有授权/没有执行记录的 `running`（`intents.py:1027-1240`、`:155-204`）。**必须禁止旧 UI/API 碰新链意图**。 |
| 旧链批量审批 | **部分（只读兼容）** | `POST /intents/batch`（`:325-343`）→ `batch_decide`，只对旧 `DECIDABLE_STATUSES` 有意义（`intents.py:34-46`、`:1384-1458`）；前端 `IntentBatchTray`。旧契约 §9.3 要求「同批总量 ≥4」；当前 `BATCH_LIST_MIN = BATCH_MIN = 2`（`approval.ts:38`、`:1144`），旧入口行为与旧契约不一致。 |
| 新链单条/同批确认 | **部分** | 见 §2.5；授权快照 `authorization {version, scope, depends_on, conflicts_with, confirmed_at, confirmed_by, source}`（`intent_service.py:906-923`）；`authorization_valid` 只比较意图自身 version（`:198-214`），**不比较材料版本是否变过**。 |
| 影响确认（改材料前暂停正在执行的任务） | **部分（仅旧链）** | 旧链：`/material-impact`、`/impact-check`、`PUT /state` 的 confirm 校验、`ImpactConfirmDialog.vue`、store `pendingImpact/confirmImpact/cancelImpact`。新链：无 impact-check；`board_tools` 写前只做授权范围与 expected_versions 校验（group_cards 要求全部受影响卡；create/link 无对象版本校验），而这些版本值在往返后退化为 1（§0.5）。 |
| 工具/权限/凭据审批 | **已实现（独立域）** | `POST /api/approvals/{id}/respond`（`backend/src/agent/api/server.py:3409-3422`）；前端 `stores/unifiedApprovals.ts` / `components/approval/*`。与互动意图确认**不是同一套存储**。 |
| 知识保存审批 / 记忆问题 | **已实现（独立域）** | `POST /api/knowledge/{id}/approval`（`api/knowledge_approval_routes.py:82`）、`/api/memory/pending*`（`api/memory_pending_routes.py`）。不是互动意图审批。 |

### 2.7 草稿

| 能力 | 结论 | 证据与现状 |
| --- | --- | --- |
| 服务端草稿（逐键三态、rev/keyVersions、冲突/blockedRemovals） | **已实现** | `GET/PUT /api/interactive/drafts/{board_id}`（`api/interactive_store.py:466-472`）；`board_store.get_draft/save_draft`（`board_store.py:371-586`），键上限 200 / 单值 20000 字（`:28-30`）；前端 `services/interactive.ts:200-259`、store `flushDrafts/retryDraftSave`。 |
| 本机恢复副本（version / fingerprint / clear record） | **已实现** | `frontend/src/interactive/drafts.ts`：`writeDraft`、`writeCardLocalDraft`、`writeCardLocalClear`、`removeCardLocalDraftIfUnchanged`；store 的 `draftLocalStates/draftRemovalStates/draftConflictFor`（`stores/interactive.ts:151-187`）。 |
| 卡片草稿输入（正文 + meta 字段） | **已实现** | `drafts.ts:36-85`、`:460-530`；`TextCardEditor.vue`；`CardDraftHint.vue`（data-im `card-draft-hint/card-draft-retry`）。 |
| 聊天草稿 | **已实现（会话域）** | `session.draft` + `stores/session.ts`；`ChatDock.vue:134-141`、`:516-531`；与互动 store 的卡片草稿分开，只有一个写者。 |
| 提交前草稿提升 | **已实现** | `store.promoteDraftsBeforeSubmit`（`stores/interactive.ts:3379-3435`）；未决冲突阻断提交（`:2841-2850`）；提交回执核对后才记为「随提交生效」（`:2991-2997`）。 |
| 统一草稿入口/数量 | **缺口** | 草稿状态分散在卡片提示、ChatDock、SubmitCluster 详情；没有一处汇总「有几条草稿未保存 / 有冲突 / 可恢复」。互动页没有对话页 v2 的「发送异常与草稿恢复」入口（契约 `docs/conversation-page-contract-20261022.md` §6）。 |

### 2.8 恢复

| 能力 | 结论 | 证据与现状 |
| --- | --- | --- |
| 未决提交身份恢复 + 精确受理查证 | **已实现** | 身份在发请求前生成并写 localStorage（`submission.ts:445-597`、store `writePendingIdentity/restorePendingSubmission`，`stores/interactive.ts:3040-3077`）；`GET /submissions/acceptance?clientRequestId=`（`api/interactive_store.py:424-463`）。 |
| 提交后读取失败的重试（只读） | **已实现** | `retrySubmitRefresh`（`stores/interactive.ts:3107` 起）；`submitRefreshState/Error`；`SubmitCluster` 的 `submit-refresh-failed/retry`。 |
| 理解失败重试 | **部分（旧链）** | `/understanding/{id}/retry`（`api/interactive_store.py:729`）；store `retryUnderstanding`（`:4090-4115`）；但新提交不再产生 understanding 行（§0.2），此路径只对旧记录有效。 |
| 旧意图暂停恢复 | **部分（旧链）** | `recover_running_intents` 只把**别的实例**遗留的 running 降级为 paused（`intents.py:2317` 起；路由 `api/interactive_intents.py:111-115`）；旧执行 `pause/resume/cancel`（`execution.py:1814-1980`）。 |
| 新意图恢复预览/恢复 | **部分（后端+工作台）** | `/intents/{id}/recovery-preview`、`/recover`（`api/interactive_intents.py:807-844`）；`preview_recovery / recover_intent`（`intent_service.py:1435-1527`）；前端 `IntentWorkbench.vue:216-258`。只把意图放回 `approved`，仍不会创建 Turn（§0.3）。 |
| 等待/续作持久化 | **已实现（后端）** | `board_waiting`（迁移 49）；`record_waiting/resolve_waiting/get_waiting/waiting_state`（`submission.py:1227-1394`）；`resolve_chat_binding` 支持「没有 submissionId 但有等待 → 自动续作」（`:1486-1582`）。 |
| 页面启动时恢复等待/绑定 | **缺口** | store `load()` 不读 `/waiting`、不读 `submission_binding`（`stores/interactive.ts:1788-1812`）；`lastSubmission` 只在内存（`:195-208`），刷新后 `chatBinding.submissionId` 为 undefined，下一条互动聊天会被服务端**自动绑定到某个等待中的提交**（`submission.py:1539-1543`），用户没有确认步骤。 |
| 统一恢复入口/计数 | **缺口** | 旧恢复提示 `recoverNotice` 只来自旧 `/intents` 的 `recovery.paused`（`stores/interactive.ts:1759`）；新链恢复只在工作台；草稿/提交/等待/失败各自的恢复分散。 |

### 2.9 账本

| 能力 | 结论 | 证据与现状 |
| --- | --- | --- |
| 权威操作账本（operation_key 幂等、request_hash、before/after/result） | **已实现（DB）** | `board_operations`（迁移 50，`migration_p3_b.py:52-73`）；`begin_operation/complete_operation/fail_operation`（`intent_service.py:1081-1180`）；工具写事务 `_run_write`（`board_tools.py:291-384`）。 |
| 账本读取 | **部分** | `GET /boards/{id}/ledger`（`api/interactive_intents.py:742-788`）只返回 `status/result` 等摘要字段，不返回 `before/after`；最多 100 条、无分页；`IntentWorkbench.vue` 只显示前 8 条。 |
| 单意图账本摘要 | **部分** | `IntentView.operations_summary`（计数：applied/running/pending/failed/conflict，`intent_service.py:189-196`）；详情视图有 `operations_summary`，但没有按意图返回完整操作列表的接口。 |
| 意图版本历史 | **部分（只存不读）** | `board_intent_versions` 只写不读（`intent_service.py:265-277`）。 |
| 旧链进度/应用/撤回 | **部分（只读兼容）** | `board_intents.progress/applied/revert`（`schema.py:1230-1232`），旧 UI 显示；新链不使用。 |
| 结果卡派生 | **已实现（新链）** | `list_results` 只从 `board_operations` 中 `applied` 的 `create_card` 派生（`intent_service.py:1570-1598`）；`GET /results`（`api/interactive_intents.py:791-804`）。结果正文不截断（`MAX_CONTENT_CHARS=200_000`，`tool_models.py:42`）。 |

### 2.10 提交

| 能力 | 结论 | 证据与现状 |
| --- | --- | --- |
| 保存/提交分离 | **已实现** | `PUT /state` 只落库（`api/interactive_store.py:144`；`board_store.save_board` 不调用 QIO）；提交只在 `POST /submissions`。 |
| 前后状态投影 + 求差 + 空/重复判定 | **已实现** | `project_snapshot/project_baseline/diff_states/_decide_status`（`submission.py:164-211`、`:222-360`、`:697-727`）；`DISPLAY_ONLY_EXPRESSIONS=("layout_only",)`（`:65-69`）；`empty`/`duplicate` 不更新基准、不调用 QIO（契约 §1.5、§14.9）。 |
| 稳定请求身份 + 幂等重放/冲突 | **已实现** | `clientRequestId` 生成/校验（`submission.ts:445-597`）；后端 `(board_id, client_request_id)` 部分唯一索引（`schema.py:1352-1356`），同身份同内容重放原记录、不同内容 409（`submission.py:2019-2031`、`:1057-1086`）。 |
| 冻结视图 | **已实现** | 前端 `frozenViewPayload`（`submission.ts:103-111`）；后端 `normalize_view`（`submission.py:748-772`）；落进 delivery。 |
| 影响确认前置 | **部分（旧链）** | 旧链 `checkMaterialImpact/confirmImpact`；新链无对应。 |
| 主循环 Turn 受理 | **已实现（后端）** | `turn_acceptor` 注入（`api/interactive_store.py:357-390`）；成功后写 `turn_id/resume_state` 与 `board_submission_turns`（`submission.py:2263-2289`）；返回 `turnId/waiting`。 |
| 受理后的理解状态 | **缺口（前后端错配）** | 后端不再创建 understanding；前端仍轮询（§0.2）。`delivery.understandingId`、`delivery.status=accepted`、`understanding*` 字段是**兼容占位**，不能作为新事实。 |
| 勾选清理 | **已实现（行为已变更）** | 契约 §2.2/§17.2：许可持续有效，不自动取消；后端 `checkedCleared` 恒 `[]`（`submission.py:2259-2261`）；前端仍调用 `applyCheckedCleared`（无害但语义空）。`/visible-range` 的提示文案仍说会自动取消（`api/interactive_store.py:319`），需删。 |
| 提交历史/受理查证 | **已实现** | `GET /submissions`（limit 20，`api/interactive_store.py:283`）、`GET /submissions/acceptance`。 |

### 2.11 重试

| 重试域 | 现状 | 入口/限制 | 结论 |
| --- | --- | --- | --- |
| 提交请求（同身份重试） | `clientRequestId` + localStorage 台账 + acceptance 查证 | 前端自动沿用身份；不自动重发；成功/明确失败后收掉身份（`stores/interactive.ts:2882-2990`） | **已实现** |
| 提交后读取 | `retrySubmitRefresh` | 只读刷新，绝不重复提交（`:3107` 起） | **已实现** |
| 理解失败 | `/understanding/{id}/retry` + `attempt` | 旧链；从 snapshot 恢复；无次数上限（`schema.py:1279`） | **部分（旧链）** |
| 旧执行 | `resume`（`execution.py:1926-1980`，attempt+1，丢弃迟到结果）、`pause/cancel` | 旧 `/resume` 路由；需 `confirmDependency` 或 paused 状态；无统一上限 | **部分（旧链）** |
| 新链操作 | `operation_key` 幂等 + `request_hash` 冲突（`intent_service.py:1044-1069`） | 同一 key 同请求重放原结果；不同请求拒绝；工具调用方决定重试 | **已实现（操作层）** |
| 新链意图恢复 | `/recover`（expectedVersion CAS） | 只回 `approved`；不建 Turn（§0.3） | **部分** |
| 草稿保存 | `retryDraftSave(key?)`（`stores/interactive.ts:2627`） | 逐键重试；失败原因可见；不清新稿 | **已实现** |
| 本机草稿删除/清除 | `retryLocalRemoval / retryLocalRemovalByPurpose`（`drafts.ts:996`） | version/fingerprint 守卫 | **已实现** |
| 影响确认撤回 | `retryCancelRecovery`（`stores/interactive.ts:3679`） | 仅旧链 impact 场景 | **部分（旧链）** |
| 聊天发送失败 | 会话域 `session.retryFailedSend` | 失败重发**不携带互动绑定**（架构 §18.3 已知限制，`docs/architecture.md:1486-1487`） | **部分（越权安全但续作断开）** |
| 重试次数/系列统一 | 无 | 各域自己的 attempt/version；没有「第 N 次 / 上限 / 系列 id」 | **缺口** |

---

## 3. 旧入口的有效能力与迁移去处（对应任务第 2 项）

> 这里的「旧入口」指基线 `InteractiveView.vue` 组合出来的入口，不代表它们都已废弃。
> 迁移原则：**先迁能力，再删入口；能力没有新家之前不得删除**（任务书明确要求）。

### 3.1 页面级入口（`frontend/src/views/InteractiveView.vue`）

| 入口 | 有效能力 | 数据来源 | 建议去处（v2） | 不能丢的行为 |
| --- | --- | --- | --- | --- |
| `data-im="save-status"` 顶部保存状态 | 唯一的「已保存/保存中/失败/有未保存改动」主状态；失败短句 + title 详情 | `store.saveStatus/saveError/dirty/lastSavedAt` | P1-A 共享栏附加区（`#qio-interactive-chrome-extra`）或底部工具栏左端；只保留一处主状态 | 保存≠提交；失败原因默认可见 |
| `not-connected / understanding-status / understanding-retry / understanding-refresh` | 「未提交给 QIO」与旧理解状态 + 重试/刷新 | 旧链 `store.understanding*`、`retryUnderstanding` | **替换**为提交/续作事实：`delivery.turnId/waiting` + 新链等待/意图状态；旧理解状态只在「历史记录」只读展示（若保留） | 不得把「已受理」说成「已完成」；失败可重试；不得显示过期理解状态 |
| `collapse-previews` | 收起全部卡片内预览（图片不算） | `store.expandedCardIds/previewExpandedCount` | 板面浮层/工具栏；保留 | 图片不计入 |
| `tasks-entry` | 打开旧任务浮层 | `store.tasksOpen` + `IntentStatusPopover` | 新「意图/任务」面板入口（建议与工作台合并）；保留数量/状态 | 任务真实状态、暂停/继续/取消入口 |
| `workbench-entry` | 打开意图与结果工作台 | `IntentWorkbench.vue` | **保留并升级为 v2 的唯一意图入口**（右侧书签栏/向上展开区） | 确认、等待、账本、结果、恢复全部保留 |
| `demo-entry / demo-popover` | 演示意图生成与推进 | `DemoIntentEntry.vue` + 旧链 `/demo/advance` | 设置/调试入口或隐藏的演示面板；**不得混入正式路径** | 演示标识、不得驱动真实任务 |
| `回到对话` router-link | 双向模式切换的旧入口 | router | 由 P1-A `ConversationModeSwitch` 接管；旧链接可留兜底 | 保留状态、双向可达 |
| `load-error` | 板面读取失败 + 重试 | store.loadError | 底部/浮层错误条；保留重试 | 失败 ≠ 空板面 |
| `recover-notice` | 「上次没有结束的任务已暂停」 | 旧链 `recovery.paused` | 新链恢复聚合入口（§5.8）；旧提示只用于旧记录 | 不自动继续，需用户确认 |

### 3.2 组件级入口

| 组件 | 有效能力（含 data-im） | 数据来源 | 迁移去处 | 迁移前置条件 |
| --- | --- | --- | --- | --- |
| `IntentWorkbench.vue` | 意图视图（`iw-intent/iw-goal/iw-basis/iw-dependencies/iw-operations/iw-auth`）、单条确认（`iw-confirm`）、同批确认（`iw-batch-confirm`）、恢复预览/恢复（`iw-recovery-preview/iw-recover-*`）、等待（`iw-waiting*`）、账本（`iw-ledger*`）、结果卡（`IntentResultCards`） | **新链** `/intent-views`、`/confirm`、`/batch-confirm`、`/waiting`、`/ledger`、`/results`、`/recovery-preview`、`/recover` | v2「意图与结果」主面板（建议右侧书签栏 + 向上展开详情）；保留全部区块与 `source=legacy` 提示 | 新链状态词表进入主 UI；legacy 映射的可确认项必须禁用（§2.5 末） |
| `IntentBatchTray.vue` | 批次分组（`batch-entry/batch-item/batch-remaining`）、批量选择/批准/拒绝（`batch-approve/batch-reject/batch-all/batch-clear`）、定位预览（`batch-locate`）、执行摘要、**跨浮层几何控制**（`overlay-switch*`，写 `--im-geo-*` 变量） | 旧链 `store.intents` + `approval.ts`；batch 本地台账 localStorage | ① 批量确认能力 → 工作台的「同批确认」区块（新链 `/batch-confirm`）；② 旧链批量审批在兼容窗口保留为「旧记录」只读入口；③ 几何控制器 → P1-B 布局外壳的浮层避让 | 旧链 pending 记录清零/迁移前不能删；新链批量确认必须先有预览/依据展示 |
| `IntentStatusPopover.vue` | 任务列表（`task-item/task-execution/task-locate/task-detail`）、执行控制（`task-pause/task-resume/task-resume-execution/task-cancel`）、失败重试读取（`tasks-refresh-retry`）、单项审批浮条（dock `IntentPreviewCard`）、按实际可用区避让 | 旧链 `store.intents/intentsLoad*` + `execution` | ① 真实状态/进度/定位 → 工作台意图详情；② 暂停/继续/取消 → **新链需要新增 API**（§5.7/§6.3）；③ 浮条/dock → v2 预览层（如果新链补预览） | 新链 pause/cancel API + 意图详情不丢执行记录；旧执行控制只对旧记录显示 |
| `SubmitCluster.vue` | 保存/提交状态分离（`submit-status`）、提交（`submit`）、失败原因/保留（`submit-failure-*`）、未知结果查证（`submit-unknown*`）、提交后刷新失败重试（`submit-refresh-*`）、本次范围（`visible-range`）与改动（`submit-pending`）、草稿提升结果（`submit-draft-promoted`）、影响阻断（`submit-blocked`）、冲突处理（`board-removal-conflict*`）、旧理解/旧审批区块（`understanding*`、`submit-approve/reject/locate/open-batch`） | 旧链 store + `approval.ts/submission.ts` + `/submissions`、`/submissions/acceptance`、`/impact-check` | v2 底部提交区（保留全部提交诚实状态 + 范围/改动/失败/重试）；理解区块改为「本次受理的 Turn/等待」；旧审批区块保留为 legacy 只读 | 提交后 UI 改读 `turnId/waiting`；`checkedCleared`/旧理解字段标记兼容只读 |
| `IntentPreviewCard.vue` | 虚线预览（卡/组/关系/符号引用）、预览调整入口、批准/拒绝、执行暂停/继续/取消、执行进度与结果引用、定位 | 旧链 `intent.preview/operations/basis/execution` | 工作台意图详情（若新链补充 preview）；否则降级为「目标/范围/依据」静态卡，**预览能力需要新数据**（§5 风险） | 新链需补 preview/impact 数据，或 Lead 明确接受能力降级 |
| `ImpactConfirmDialog.vue` | 保存前影响说明（对象/任务/后果）、确认=保存并暂停、取消=不保存；撤回残留决定；键盘焦点约束 | 旧链 `pendingImpact` + `/impact-check` + `/material-impact` | 保留为 v2 中央影响确认框；**仅对旧链任务有效**；新链需要等价机制（§5） | 新链 impact 方案冻结前不得删 |
| `DemoIntentEntry.vue` | 演示意图创建/推进 | 旧链 `/intents {demo}`、`/demo/advance` | 设置/调试入口；保留 | 演示不得驱动真实任务 |
| `ChatDock.vue` | 聊天、发送/停止、排队、草稿、失败恢复、附件、回到最新、过程区、执行记录穿插、互动绑定（`store.chatBinding`） | `stores/session.ts` + 互动 store + `/chat` 主循环 | v2 向上展开对话区（P1-B）；保留全部会话能力；绑定改为新链 `waiting/submission` 事实 | 失败重试/中断重发不带互动绑定是已知限制（§2.11），v2 若要「恢复续作」必须补 |
| `BoardToolbar.vue` | 添加（文字/文件，`add-menu/add-text/add-file`）、搜索、撤销/重做、选择菜单、提交区容器 | store + `board.ts` 纯函数 | v2 底部工具栏；保留 | 760/800 窄窗规则 |
| `BoardCard.vue / SelectionMenu.vue` | 卡片编辑/复制/折叠/隐藏/书签/删除/勾选；组操作；尺寸把手；连接点；卡片内预览 | `board.ts` 纯函数 + `store.commit` | 保留（v2 板面主体）；勾选/隐藏文案与参与状态统一（§5.1） | 参与状态统一前不改权限语义 |

### 3.3 迁移顺序建议（能力不丢失）

1. **先补新链的只读事实入口**：`resume-state`（等待/绑定/可恢复）+ 意图详情（含预览/账本/执行）。此时旧入口仍可用。
2. **把 `IntentWorkbench` 升级为唯一意图入口**：接入新链状态词表、禁用 legacy 可确认项、加入暂停/取消（需 API）、加入等待/续作确认。
3. **迁移 `SubmitCluster`**：提交状态只读 `turnId/waiting`；旧理解/旧审批区块加「历史只读」标记；保留失败/未知/重试/范围/改动全部行为。
4. **迁移 `IntentStatusPopover` 的执行控制**：新链 pause/cancel API 落地后切换；旧记录继续走旧链。
5. **最后删 `IntentBatchTray` 的旧批量审批交互**（保留 batch 分组台账或一次性迁移），并移除旧理解轮询（store）。
6. **DemoIntentEntry** 移入调试入口；正式 UI 不显示。
7. **禁止**在同一屏同时出现「旧批准」与「新确认」两个提交动作（会给出不同结果）。

---

## 4. 新旧接口来源表（对应任务第 3 项）

### 4.1 前端服务 → 后端路由/服务 → 表

| 前端调用（文件:函数） | 方法/路径 | 后端路由 → 服务 | 表/事实 | 字段状态 |
| --- | --- | --- | --- | --- |
| `services/interactive.ts:listBoards` | GET `/api/interactive/boards` | `interactive_store.list_boards` → `board_store.ensure_board` | `boards` | 唯一事实 |
| `:createBoard` | POST `/api/interactive/boards` | 同上 | `boards` | 唯一事实 |
| `:fetchBoardState` | GET `/boards/{id}/state` | `interactive_store.get_board_state` → `load_board` | `board_states`、`board_drafts`、`board_pending`、最近提交 | 唯一事实；`state.cards/group/link.version` 后端有、前端类型不含（§0.5） |
| `:saveBoardState` | PUT `/boards/{id}/state` | `put_board_state` → `save_board`（可带 confirm checkId） | `board_states`、`board_state_snapshots`、`board_pending` | 唯一事实；保存不调用 QIO |
| `:fetchHistory` | GET `/boards/{id}/history` | `list_board_states` | `board_state_snapshots`（最多 200，`SNAPSHOT_KEEP`） | 唯一事实（撤销/重做依据） |
| `:fetchSubmissions` | GET `/boards/{id}/submissions` | `list_submissions` | `board_submissions` | 唯一事实 |
| `:fetchVisibleRange` | GET `/boards/{id}/visible-range` | `visible_range` | 由 `board_states` 推导 | 唯一事实；但 `checkedClearedHint` 文案过期（§0.7） |
| `:submitBoard` | POST `/boards/{id}/submissions` | `submit` → `submission.submit_board`（注入 `turn_acceptor`） | `board_submissions`、`board_pending`、`board_states`、`board_waiting` | 唯一事实；`delivery.understandingId/status` 为兼容占位 |
| `:fetchAcceptance` | GET `/submissions/acceptance?clientRequestId=` | `acceptance_lookup` | `board_submissions` | 唯一事实（404 = 查不到，不是失败） |
| `:fetchDrafts / :saveDrafts` | GET/PUT `/api/interactive/drafts/{board_id}` | `get_draft / save_draft` | `board_drafts`（JSON v2：rev/keys） | 唯一事实；`legacyReplace` 仅旧客户端 |
| `:fetchIntents / :listIntentBoard` | GET `/boards/{id}/intents` | `intents.list_intents` | `board_intents`、`board_executions` | **旧链只读兼容**（但会混读新链行，状态词表不同，见 §2.5） |
| `:previewMaterialImpact` | POST `/boards/{id}/material-impact` | `intents.preview_material_impact` | 由 state 推导 | **旧链专用** |
| `:checkMaterialImpact` | POST `/boards/{id}/impact-check` | `intents.impact_check` | 进程内 `_CONFIRM_CHECK` + `board_states` | **旧链专用**；重启后确认失效（有意） |
| `:createDemoIntents` | POST `/boards/{id}/intents {demo:true}` | `intents.create_demo_intents` | `board_intents` | 演示；不得驱动真实任务 |
| `:approveIntent / :rejectIntent / :updateIntentPreview / :advanceIntent / :batchDecide` | POST `/intents/{id}/approve|reject|preview|demo/advance`、`/intents/batch` | `intents.*` | `board_intents`、`board_executions` | **旧链写路径**；对新链意图危险（§2.6） |
| `:fetchUnderstanding / :retryUnderstanding` | GET `/boards/{id}/understanding`、POST `/understanding/{id}/retry` | `understanding.*` | `board_understanding` | **仅旧记录只读兼容**；新提交流程不再产生 |
| `:fetchExecutions` | GET `/boards/{id}/executions` | `intents.list_executions` | `board_executions` | **仅旧链**；新链执行写 `board_operations`，此接口看不到 |
| `:pauseIntent / :resumeIntent / :cancelIntent` | POST `/intents/{id}/pause|resume|cancel` | `intents.pause_intent/...` → `execution.*` | `board_executions` | **旧链专用**；新链没有对应 API（§0.4） |
| `:fetchIntentViews` | GET `/boards/{id}/intent-views` | `intent_service.list_intents_view`；失败/未接入回退 `_legacy_views` | `board_intents`、`board_operations` | 新链事实；`source=legacy` 时是只读兼容映射 |
| `:fetchIntentView` | GET `/boards/{id}/intent-views/{id}` | `intent_service.get_intent` | 同上 | 新链事实 |
| `:confirmSingleIntent` | POST `/intents/{id}/confirm` | `interactive_intents.confirm_single_intent` → `intent_service.confirm_intents` | `board_intents`、`board_intent_versions` | 新链事实；不建 Turn（§0.3）；对 legacy pending 必然失败（§2.5） |
| `:batchConfirmIntents` | POST `/intents/batch-confirm` | `confirm_intents_batch`（门槛 ≥2） | 同上 | 新链事实；门槛前后端同值 `BATCH_CONFIRM_MIN=2`（`api/interactive_intents.py:361-363`、`intentWorkbench.ts:13`） |
| `:fetchWaiting` | GET `/boards/{id}/waiting` | `_waiting_for` → `submission.get_waiting` / provider | `board_waiting` | 新链事实；`available=false` 时如实不可用 |
| `:fetchLedger` | GET `/boards/{id}/ledger` | `intent_service.ledger_summary`；无服务时读 `board_operations` | `board_operations` | 新链事实；只给摘要 |
| `:fetchIntentResults` | GET `/boards/{id}/results` | `intent_service.list_results` | `board_operations`（applied create_card） | 新链事实；不读模型文本 |
| `:fetchRecoveryPreview / :recoverIntent` | GET `/intents/{id}/recovery-preview`、POST `/intents/{id}/recover` | `intent_service.preview_recovery/recover_intent` | `board_intents`、`board_operations`、`board_waiting` | 新链事实；恢复后仍需 Turn |
| `services/attachments.ts:putBoardPreviewPosition` 等 | GET/PUT/DELETE `/boards/{id}/preview[/{card_id}]` | `preview_routes.*` | `board_preview_state` | 唯一事实；只影响阅读位置，不写板面 |
| `services/attachments.ts:fetchAttachmentPreview` | GET `/api/attachments/{id}/preview` | `preview.render_for_async` | `attachment_previews` 缓存 + 附件表 | 唯一事实；图片返回 `data.width/height`（未进卡片 meta） |
| `services/api.ts:respondApproval` | POST `/api/approvals/{id}/respond` | `server.approvals.respond` | `pending_approvals` | 独立审批域（工具/权限/凭据） |
| `services/api.ts` 知识/记忆 | `/api/knowledge/{id}/approval`、`/api/memory/pending*` | 知识/维护服务 | `knowledge`、`maintenance_*` | 独立审批域 |

### 4.2 只能读 / 只兼容的字段（v2 不得再作为新事实写回）

| 字段/接口 | 现状 | v2 处理 |
| --- | --- | --- |
| `delivery.understandingId` | 新提交恒 `null`（`submission.py:2154-2173`） | 只读兼容；不要据此轮询/显示理解状态 |
| `delivery.status = "accepted"` | 只表示主循环 Turn 已受理 | 可显示「已受理」；不得推导「理解完成」 |
| `GET /understanding`、`POST /understanding/{id}/retry|cancel` | 仅旧 `board_understanding` 行 | 仅「历史记录」只读；新提交不产生 |
| `understanding*` store 状态 | 旧链 | v2 新数据源为 `turnId + waiting + intent-views`；旧字段只在兼容视图 |
| `checkedCleared` | 恒 `[]`（`submission.py:2259-2261`） | 保留字段兼容；UI 不显示「已取消勾选」；`/visible-range` 错误提示删除 |
| `GET /intents`、旧状态词表、`progress/applied/revert` | 旧链 + 新链行混读 | 只用于旧记录；新链使用 `/intent-views` |
| `GET /executions`、`board_executions` | 旧链 | 只用于旧记录；新链用 `/ledger` + `operations_summary` |
| `POST /approve|/reject|/preview|/batch|/demo/advance` | 旧链写路径 | 冻结为 legacy；**禁止**对新链意图调用；UI 加状态门槛 |
| `source: "legacy"` / `raw_legacy_status` | `intent-views` 回退映射（`api/interactive_intents.py:481-533`） | 必须显示「旧数据只读兼容」；`proposed` 的 legacy 项禁止点确认（否则必失败） |
| `board_intent_versions` | 只写不读 | 只读兼容/后续补详情接口 |
| `board_understanding` / `board_executions` 历史行 | 旧链 | 只读；不删（审计/回溯） |
| `meta.image`、`kind=code/url` | 遗留数据，不迁移/不可见（`models.py:26-29`、`board.ts:312-322`） | 保持只读兜底，不恢复兼容 |

### 4.3 新链接口与状态词表（v2 应作为唯一新事实）

| 事实 | 接口/字段 | 定义处 |
| --- | --- | --- |
| 意图视图 | `GET /intent-views`、`GET /intent-views/{id}` | `api/interactive_intents.py:551-595` |
| 确认 | `POST /intents/{id}/confirm`、`POST /intents/batch-confirm` | `:598-657` |
| 等待 | `GET /boards/{id}/waiting` | `:736-739` |
| 账本 | `GET /boards/{id}/ledger` | `:742-788` |
| 结果 | `GET /boards/{id}/results` | `:791-804` |
| 恢复 | `GET /intents/{id}/recovery-preview`、`POST /intents/{id}/recover` | `:807-844` |
| 状态词表 | `INTENT_STATUSES`（10 值） | `backend/src/agent/interactive/tool_models.py:60-71`；前端 `interactive/intentWorkbench.ts:16-26` |
| 授权快照 | `authorization {version,scope,depends_on,conflicts_with,confirmed_at,confirmed_by,source}` | `intent_service.py:906-923` |
| 操作账本 | `board_operations {operation_key,request_hash,action,status,before,after,result}` | 迁移 50；`intent_service.py:1081-1180` |
| 提交—Turn 绑定 | `board_submissions.turn_id/resume_state`、`board_submission_turns` | 迁移 49；`submission.py:1397-1483` |
| 等待事项 | `board_waiting {question,missing_fields,status,resolved_question_ids}` | 迁移 49；`submission.py:1227-1360` |

---

## 5. 用户要求的新字段/接口建议（对应任务第 4 项）

> 原则：**只追加、不破坏**；新字段先由服务端计算/返回，前端只读；旧字段保留兼容；
> 一次性迁移集中在 v2 切换点，不做长期双写。以下每条都标出「需 Lead 冻结」的部分。

### 5.1 参与状态统一

**现状问题**：无统一状态（§2.4）；前端三份实现；组/链接的参与只能间接推断；文案与数据可能不一致。

**建议形状**（服务端只读投影，仅出现在响应里，**不写回**板面保存）：

```ts
// frontend/src/interactive/types.ts（建议新增）
export type ParticipationReason =
  | "material"      // file 默认在讨论范围
  | "checked"       // text/reply 用户勾选
  | "unchecked"     // text/reply 未勾选
  | "hidden"        // 用户明确隐藏
  | "deleted"       // 已删除（撤回）
  | "legacy";       // 遗留 code/url，未迁移完成

export interface CardParticipation {
  included: boolean;              // 是否需要交给 QIO（本次提交/上下文）
  state: "included" | "excluded" | "withdrawn"; // withdrawn = deleted
  reason: ParticipationReason;
  /** 仅 text/reply 有意义；file 为 null；reply 未勾选时为 false */
  license: boolean | null;
}
```

- 响应位置：`GET /boards/{id}/state` 的 `state.cards[].participation`（只读）与
  `GET /visible-range` 的 `visibleRange.cards[].participation`；服务端函数唯一实现为
  `models.visible_except_deleted` + `selectable_cards`（保持现有语义，不合并 `hidden` 与 `license`）。
- 前端改为只读 `card.participation`；`submission.isVisibleCard` / `store.cardSubmittedFor` /
  `board.CHECKABLE_KINDS` 降级为「旧数据兜底」，或统一委托给同一个纯函数。
- **不要**把许可与可见性合并成一个布尔：`hidden` 与 `unchecked` 的后果不同
  （一个退出范围，一个只是未许可），合并会让用户无法表达「保留在板面但不给 QIO 看」。

**兼容/迁移**：纯服务端计算，无 schema 迁移；前端分两步（先加只读字段与测试，再替换旧判定）。
**需 Lead 冻结**：`CardParticipation` 字段名/枚举、`license` 是否保留、组/链接是否也暴露 `participation`。

### 5.2 图片比例

**现状问题**：预览服务能算 `width/height`（`preview.py:325-348`），但卡片 `meta.file` 不存；前端不读；
卡片尺寸是固定值，无法按图片比例呈现（当前只用 `object-fit: contain` 在固定框内留白）。
契约 §16.3 明确：卡片尺寸/位置由用户设置、不因图片加载重排；§17.1 禁止按视野/内容自动改几何。

**建议形状**：
- 方案 A（推荐，最小）：只读展示字段，不参与语义：
  ```ts
  interface FileDisplayMeta { width?: number; height?: number; aspectRatio?: number }
  // 放在 card.meta.display 或 card.meta.file.display，**不**放在 meta.file 的语义键里
  ```
  服务端在 `/api/attachments/{id}/preview` 已返回图片 `width/height`（`preview.py:337-348`），
  前端在**首次打开/创建**时可按需读取；若要落盘，只写 `meta.display`（必须从 `_card_meaning` 排除，
  否则会被当作材料语义变化，见 §5.4）。
- 方案 B（如产品要求「按比例显示」）：只提供**用户显式动作**「按图片比例调整卡片大小」，
  一次性写入 `card.w/h`（位置/尺寸属于板面状态、本来就 display-only），**不在图片加载时自动改**。
- 禁止：图片加载/解码完成后自动改卡片 `w/h` 或重排（违反 §16.3/§17.1，也会与用户尺寸偏好冲突）。

**兼容/迁移**：`meta.display` 是新增可选块；旧数据没有就不显示比例。无 DB 迁移。
**需 Lead 冻结**：字段位置（`meta.display` vs `meta.file`）、是否允许写盘、是否只做方案 A。

### 5.3 组端点

**现状问题**：`Link.src/dst` 只接受卡片 id；G4、normalize、可见范围、渲染、工具参数全部按卡片实现（§2.2/§2.3）。
**建议形状**（字符串端点，向后兼容）：

```ts
type LinkEndpoint = string;
// 编码： "card:<id>" | "group:<id>" | "<cardId>"（裸 = 卡片，兼容旧数据）
interface BoardLink { src: LinkEndpoint; dst: LinkEndpoint; ... } // 字段名不变
```

- 归一化（`board._normalize_link` / 前端 `normalizeLink`）：端点解析为 `{kind,id}`；
  `card` 必须在 live cards；`group` 必须是未删除且有成员的组。
- 唯一性：同一对**规范化端点**只保留一条（`frozenset((kind,id), ...)`），避免 card/group 同 id 碰撞。
- 可见范围（`submission.project_snapshot`）：两端都可见才进载荷；
  group 端点可见 = 组至少有 1 名可见成员（沿用现有组规则，组名仍不泄露未许可内容）。
- 渲染：`BoardGroupFrame` 的中心/边框作为端点锚点；`BoardLinkLayer` 支持 `kind=group`；
  连接点拖线允许目标组（若产品要求）。
- 工具：`board_add_link` 的 `src/dst` 接受同一编码；`_validate_operation` / 可见范围校验同步；
  `IntentScope.group_ids` 已有，可用于授权「改这个组的关系」。
- 语义：方向/含义仍由用户写明，不得自行解释成因果/支持/顺序（契约 §1.3）。

**兼容/迁移**：新增端点编码不改变旧数据；**旧客户端**（不认识 `group:`）加载时会把组端点当卡片 id 丢掉
（`normalizeLink` 的 live 检查）→ 必须锁最低客户端版本或在旧端显示「有组端点，请升级」；
一次性切换，不做长期双格式写。
**需 Lead 冻结**：编码格式（前缀 vs 对象）、是否允许组—组、组端点可见性规则、工具参数 schema 变更。

### 5.4 语义材料版本

**现状问题**：至少 5 种「版本」：板面 `seq`、对象 `version`、附件 `version`、内容指纹、
意图 `version` / 执行 `attempt` / 理解 `attempt`；且对象 version 会在前端往返后退化为 1（§0.5）。
新链 `_validate_basis` 只在提案时比较版本（`intent_service.py:346-356`），确认/执行时不复核材料；
旧链的 `__materialWatch`/`_stale_basis_materials` 保护在新链没有等价物。

**建议定义**：

```ts
// 读模型：每张卡/组/链接（以及材料条目）都带一个稳定语义版本
interface MaterialVersion {
  kind: "content" | "attachment" | "composite";
  value: string;          // 如 "sha256:..." / "c:<sha256>"
  // 供 UI 显示与对账；不参与 x/y/w/h/折叠/书签
}
// IntentBasisRef 追加（兼容旧 version）
interface IntentBasisRef { card_id; version: number; material_version?: string; role?: "source"|"target"|"context" }
```

- 计算（服务端唯一实现）：复用 `materials._content_version` 的规范化 JSON（kind/content/meta，
  排除显示字段），附件用 `materials._attachment_version`；文件卡用 composite（content + attachment version）。
- **服务端权威 version**：`save_board` 在事务内对比上一份已保存 state 与本次 state，
  对语义变化的 card/group/link `version+1`，忽略客户端传入的 version（或仅在缺失时接受 1）。
  这样 `check_expected_versions` / `_validate_basis` / 工具 CAS 才真正生效。
- 复核点：提案（`propose_intents`）、确认（`confirm_intents` 新增：basis.material_version 与当前不符 →
  `basis_stale`，回到 `awaiting_confirmation`/`needs_update`）、执行前（`begin_operation`/工具参数）、
  完成核对（`finish_intent`）。
- 兼容：旧 `basis[].version`（int）继续返回；旧意图没有 material_version 时不比较，
  只按旧 version（并如实标 legacy）。
- 一次性迁移：**惰性**——下一次保存/读取时计算；intent basis 新字段只对新意图写入；
  旧意图不改写（无法伪造历史版本）。

**需 Lead 冻结**：`material_version` 字段名/算法/参与字段、version 自增规则（移动/缩放不算）、
是否允许客户端传 version、确认时材料变化的产品行为（拒绝确认 vs 标记需更新）。

### 5.5 位置

**现状**：位置有 5 套（§0.8）：卡片 `x/y/w/h`（板面状态，权威）、组框（派生）、
viewport（内存）、对话窗口（本机）、阅读位置（`board_preview_state`）、意图摆放（服务端 `Position`）。

**建议**：
- 明确定义：**「位置」= 板面坐标下的矩形 `{x,y,w,h}`，唯一权威是 `board.cards[]`**；
  组矩形是派生只读；不新增第二套卡片位置。
- 如果要恢复「用户上次看到哪里」：新增**本机查看偏好**（不写板面）：
  ```ts
  interface InteractiveViewState { scale: number; scrollLeft: number; scrollTop: number }
  // localStorage key: qio.interactive.view:<boardId>；恢复时按当前内容夹取
  ```
  只在 v2 布局外壳读写；提交的 `FrozenView` 仍走现有 `submission.frozenViewPayload`，
  不从 view 偏好伪造。
- 如果要跨设备同步：复用 `PUT /api/settings/ui`（像对话页 `conversation_layout`）新增
  `interactive_layout` 或新增 `board_view` 表（需迁移 56）；Phase 1 建议先本机。
- 意图位置：新链 `Position(absolute|relative)` 已冻结（`tool_models.py:185-243`）；v2 预览必须与服务端
  `placement.plan_card_placement` 同口径（`placement.py:206`），不得前端自己猜坐标。

**需 Lead 冻结**：位置是否包含 viewport、是否跨设备、localStorage 键名、服务端是否夹取负坐标（当前两端不一致）。

### 5.6 retry 系列 / 次数

**现状**：各域有 attempt（理解/执行/意图）或 identity（提交）/version（草稿），**没有统一「重试系列」**，
也没有统一上限；前端多个 retry 函数各自为政（§2.11）。

**建议形状**（统一读模型，按域返回）：

```ts
type RetryDomain = "submission" | "resume" | "intent_confirm" | "operation" | "draft" | "preview";
interface RetryState {
  seriesId: string;        // 稳定系列身份（提交用 clientRequestId / 意图用 intentId / 操作 intentId+operation_key）
  attempt: number;         // 第几次（从 1 开始）
  limit: number | null;    // 上限；null = 仅人工/策略约束
  canRetry: boolean;
  retryOf?: string;        // 上一次尝试的身份（如 retry_of_attempt）
  lastReason?: string;     // 上一次失败原因（人话）
  nextAllowedAt?: string | null;
}
```

- 写入/返回：提交响应 `retry`（提交/受理）、`IntentView.retry`（确认/恢复）、
  `operations_summary` 旁给 `retry`（操作层）、草稿响应 `retry`（保存失败）、`recovery-preview.retry`。
- 计数来源：现有 `intent.attempt`、`execution.attempt`、`understanding.attempt`、
  `board_operations.operation_key` 可直接派生；提交重试次数需要新列 `board_submissions.attempt`（迁移 56）
  或本机台账（不建议只本机）。
- 上限：由服务端常量给出（按域），前端只显示；**不得无界自动重试**。
- 兼容：旧字段保留只读；新 UI 显示「第 N 次 / 上限 / 为什么不能重试」。
- 一次性迁移：旧记录 `retry` 由现有 attempt 派生（无历史则 attempt=1、seriesId=对象 id）。

**需 Lead 冻结**：统一形状、各域上限、提交 attempt 是否落库、重试与「恢复续作」的边界。

### 5.7 确认续作

**现状**：等待回答后服务端**自动续作**（`resolve_chat_binding` 在互动聊天没有 submissionId 时
自动挑等待中的提交，`submission.py:1539-1543`）；用户没有确认步骤；确认新链意图（confirm）只到
`approved`，不创建 Turn（§0.3）。这既可能把无关聊天绑定到等待提交，也无法让「确认」真正继续执行。

**建议形状**：

```ts
interface ResumeRequest {
  boardId: string;
  submissionId: string;
  resumeKey: string;             // 幂等身份，由前端在点击前生成
  source: "waiting" | "paused" | "failed" | "partial" | "restart";
  questionIds?: string[];        // 用户本次实际回答的等待项（不自动全解）
  intentIds?: string[];          // 要续作的已确认意图
  expectedVersion?: number;      // 意图版本 CAS（按需）
  confirmedByUser: true;         // 程序门槛：没有它不得续作
}
interface ResumeResult {
  ok: boolean; turnId?: string; attempt: number; status: string;
  waiting?: WaitingState; intentIds?: string[]; retry?: RetryState; reason?: string;
}
```

- 新接口：`POST /api/interactive/boards/{board_id}/submissions/{submission_id}/resume`
  （或 `POST /boards/{id}/resume`，body 带 submissionId）；服务端用
  `turn_acceptor(..., resume=True, resolved_question_ids=..., intent_ids=...)` 真正创建主循环 Turn；
  不新建提交、不重复调模型；`resumeKey` 幂等（同 key 返回原 turn）。
- 确认新链意图也走同一条路：`confirm` 成功后（或显式「确认并继续」）创建续作 Turn；
  如果产品要求「确认只授权、用户再说继续」，则 confirm 不建 Turn，但 UI 必须给出「继续」按钮，
  不能像现在这样无声等待下一条聊天。
- 前端：持久化 `lastSubmission/waiting`（见 5.8），在输入区上方给一条明确的
  「上次提交在等待你的回答（第 N 次）— 继续 / 忽略」；只有用户点继续或发送回答才绑定；
  禁止服务端静默挑选等待提交。
- 兼容：保留服务端自动续作能力作为**后备**，但必须带 `confirmedByUser` 或明确用户动作；
  旧链 `confirmDependency` 语义保留只读。

**需 Lead 冻结**：是否「回答即确认」、confirm 是否建 Turn、resumeKey 生成方、幂等窗口、
自动续作是否彻底关闭。

### 5.8 恢复续作

**现状**：数据齐（`board_waiting`、`board_submissions.turn_id/resume_state`、
`board_submission_turns`、`board_intents`、`board_operations`），但**没有聚合读接口**，
前端启动也不读（§2.8）；页面刷新后不知道「还有什么在等我」。

**建议形状**：

```ts
interface ResumeState {
  boardId: string;
  seq: number;
  instanceId?: string;
  restarted: boolean;                 // 服务重启后的首次读取
  waiting: WaitingState;              // board_waiting（含 submissionId / questionIds）
  submissions: Array<{
    submissionId: string; turnId: string; attempt: number;
    status: string;                   // 主循环 Turn / 提交状态
    resumeState: Record<string, unknown>;
    waiting: WaitingView[];           // 该提交的未解等待
    canResume: boolean;
  }>;
  recoverableIntents: IntentView[];    // paused/failed/partially_completed 且授权有效
  lastTurn: { turnId: string; attempt: number; topicId?: string } | null;
  retry?: RetryState;
}
```

- 新接口：`GET /api/interactive/boards/{board_id}/resume-state`（只读；数据来自 §4.3 的现有表，
  **不需要新迁移**）；`preview_recovery` 继续按意图给细节；`recover` 仍按版本 CAS。
- 前端：`store.load()` 第一步拉 `resume-state`；在 v2 给「恢复」入口（数量 + 明细）；
  用户选择后调用 `resume`（5.7）；恢复后仍必须经过 Turn 才执行。
- 兼容：旧 `recoverNotice`（旧链 paused）保留为 legacy 区块；`/intents` 的 `recovery` 字段不回退。
- 一次性迁移：无（聚合读）；如果 Lead 要求「旧 paused 意图也出现在新恢复面板」，
  需要把旧记录映射到 `IntentView`（明确 `source=legacy`，不得直接 confirm）。

**需 Lead 冻结**：聚合形状、是否包含旧链记录、`restarted` 判定口径（实例 id vs 启动时间）、
恢复入口的唯一位置（工作台/输入区/左栏）。

### 5.9 兼容与一次性迁移总策略（建议）

1. **加法优先**：所有新字段可选、读取方容错；旧字段至少保留一个版本周期。
2. **单一事实源**：新链只消费 `/intent-views + /waiting + /ledger + /results + /resume-state`；
   旧链只用于「历史只读」，UI 明确标注 `source=legacy`。
3. **状态映射表唯一**：旧→新只在后端 `_LEGACY_STATUS_MAP` 一处（`api/interactive_intents.py:370-381`），
   前端不得再写第二份；且 legacy 映射出的可确认项在 UI 禁用。
4. **一次性数据迁移**：对象 version/materialVersion 惰性重算；提交 attempt/retry 用迁移 56（如需）；
   `meta.display`/组端点无需历史数据转换；不批量改写 `board_intents` 的历史状态。
5. **切换顺序**：后端先上只读聚合与版本修复 → 前端工作台升级 → SubmitCluster 收敛 → 旧入口下线；
   每步都保留回滚开关（旧入口可再打开）。
6. **验收**：为每个新接口写定向测试（fake provider，不联网）；至少覆盖「legacy 不可确认」「版本变化拒绝确认」
   「等待不自动绑定」「确认建 Turn/幂等」「恢复跨重启」「组端点不可见不泄露」。

---

## 6. 风险与必须由 Lead 冻结的清单（对应任务第 5 项）

### 6.1 风险（按严重度）

| 级别 | 风险 | 证据 | 影响 | 建议 |
| --- | --- | --- | --- | --- |
| **P0** | 两代链并存，旧 UI 写新链意图 | §1.3、§2.5、§2.6；旧 `approve_intent` 对 `proposed` 会进入 `running` + `no_operations`（`intents.py:1027-1240`、`:155-204`） | 状态/授权/执行记录互相污染；用户看到假「执行中」 | 旧写接口加「只接受旧状态」硬门槛；UI 按 `raw_legacy_status` 禁用；v2 只读新链 |
| **P0** | 新链「确认」不执行，等待下一条聊天 | §0.3；全仓库无确认建 Turn 代码 | 用户确认后没有任何进展，误以为卡住 | 新增 resume 接口/确认后建 Turn（5.7） |
| **P0** | 对象 version 退化，CAS 失效 | §0.5；`board.ts:309-356`、`board_store.py:223-260` | 材料变化不触发复核；并发/重放保护形同虚设 | 服务端权威 bump（5.4） |
| **P0** | 新链无 pause/cancel API | §0.4；`intent_service.py:1369-1385` 无路由 | v2 无法暂停/取消新任务，旧入口又不能用于新任务 | 新增 `/intents/{id}/pause|cancel`（走新服务，expectedVersion） |
| **P0** | 审批预览/影响说明在新链缺失 | §2.5、§2.6；`IntentSpec` 无 preview、`_insert_intent` 空 preview/impact | 「禁止删功能」直接失守；用户无法在确认前看到具体改动 | 冻结：新链补 preview/impact，或明确产品接受降级并保留旧链只读 |
| **P1** | 前端仍轮询旧 understanding，状态可能永远 accepted | §0.2；`stores/interactive.ts:3005-3021`、`:4048` | 顶部状态错误/过期；刷新后可能被旧记录冒充 | v2 改用 `turnId + waiting + intent-views`；旧轮询只对旧记录 |
| **P1** | legacy 意图在工作台可点确认但必失败 | §2.5；`_LEGACY_STATUS_MAP` + `confirm_intents` 状态拒绝 | 用户点了没反应/只看到错误 | 前端按 `source/raw_legacy_status` 禁用；后端 confirm 返回明确 `legacy_readonly` |
| **P1** | `checkedCleared`/`visible-range` 文案与事实相反 | §0.7；`submission.py:2259-2261`、`api/interactive_store.py:310-320` | 用户以为许可会被清掉，或以为没清 | 删除/改写文案；字段标兼容只读 |
| **P1** | 前端 `MaterialSource`/图片能力漂移 | §0.9；`types.ts:394-401`、`materials.ts:17-25` vs `models.py:309-325` | 新来源显示未知；审计口径不一致 | 同步类型/标签；以服务端为准 |
| **P1** | 文档/契约与代码冲突 | `docs/status.md:1751-1756`、`docs/interactive-mode-contract.md:887-1075`（§14 旧链）vs P71/architecture §18 | 后续 Agent 会按旧契约实现错误链路 | Lead 在 Phase 1 文档里明确「唯一现行契约」与废弃章节 |
| **P1** | v2 布局会丢页面级入口 | §3.1；P1-A 移除互动标题栏，P1-B 只提供外壳 | 保存状态/任务/工作台/演示/回到对话失去落点 | 迁移表作为 P1-A/B 验收项；能力未落位前不删旧入口 |
| **P2** | 组端点可能泄露隐藏组名 | 5.3 的组可见规则若实现错误 | 隐藏成员所在组名被 QIO 看到 | 组端点仅当 ≥1 可见成员才进载荷；测试断言 |
| **P2** | 图片比例自动调整违反 §16.3/17.1 | 5.2 | 卡片跳变、用户尺寸偏好被覆盖 | 只做只读比例/用户显式动作 |
| **P2** | 旧 `BATCH_LIST_MIN=2` 与旧契约 §9.3 的 ≥4 不一致 | `approval.ts:38/1144`、旧契约 §9.3 | 旧批量入口行为与文档不符 | 明确旧入口冻结口径；v2 统一用新链门槛 2 |

### 6.2 文档/契约与代码的冲突（需 Lead 定唯一口径）

| 文档位置 | 说法 | 代码事实 | 建议 |
| --- | --- | --- | --- |
| `docs/status.md:1751-1756` | 提交会建立理解请求并调用模型 | 提交作为主循环 Turn，不再建 understanding（`submission.py:1997-2008`、`:2080-2192`） | 更新 status「尚未完成」与历史说明；标 P71 之后口径 |
| `docs/interactive-mode-contract.md` §1.4「reply 不进提交载荷」 | 旧规则 | §17.2 已改为「用户许可后可进」；代码 `models.CHECKABLE_KINDS` 含 reply | 保留 §17 为现行；§1.4 标注被取代 |
| 同文件 §14/§14.9 | 理解链为主流程 | 已退役（`understanding.py:1-12`、`docs/qio-refactor-contract.md:701-703`） | 标注「历史兼容」；新流程以 architecture §18 / refactor §13 为准 |
| 同文件 §9.3 | 同批 ≥4 才显示列表 | 当前 `BATCH_LIST_MIN=2`（`approval.ts:1144`），新链确认门槛 2 | 明确旧列表已按新门槛；或恢复旧门槛（需产品决定） |
| `docs/conversation-page-contract-20261022.md` §4.3 | 预览标签身份/位置规则 | 互动页有自己的 `board_preview_state`；对话页用 `conversation_layout` | v2 若要复用对话页预览，必须统一身份与位置存储，不能两套 |

### 6.3 必须由 Lead 冻结的字段/接口（Freeze List）

| # | 字段/接口 | 建议值/形状 | 为什么必须冻结 |
| --- | --- | --- | --- |
| F1 | `CardParticipation` / `participation` | 见 5.1；`included/state/reason/license` | 权限语义，前端多处依赖；改一次要全端同步 |
| F2 | `FileDisplayMeta` / `meta.display` | `width/height/aspectRatio`，**不参与 `_card_meaning`** | 决定图片是否会自动改尺寸；与 §16.3/17.1 冲突风险 |
| F3 | `LinkEndpoint` 编码 + G4 + 组可见规则 | 字符串 `card:`/`group:` 前缀，裸=卡片；组端点需 ≥1 可见成员 | 关系语义/权限边界/旧客户端兼容 |
| F4 | `materialVersion` + `IntentBasisRef.material_version` | 内容哈希，服务端权威；确认/执行复核点 | 审批依据与 CAS 的唯一算法；影响所有工具 |
| F5 | 对象 `version` 自增规则 | 服务端保存时按语义 diff +1；移动/缩放/折叠不计 | 决定 CAS 是否真正有效；客户端不得自报 |
| F6 | `RetryState` + 各域上限 | 见 5.6；不无界重试 | 重试体验与成本；跨重启一致性 |
| F7 | `ResumeRequest/ResumeResult` + 路径 | 见 5.7；`resumeKey` 幂等；`confirmedByUser` | 确认续作是否建 Turn、是否自动绑定的产品语义 |
| F8 | `ResumeState` 聚合 + `resume-state` 路径 | 见 5.8；只读、无新迁移 | 恢复入口与 UI 归属；跨重启口径 |
| F9 | 新旧链边界 | 旧接口冻结为 legacy；新 UI 只读新链；legacy 项禁用确认 | 防止两套写路径互相污染 |
| F10 | 旧↔新状态映射表 | 唯一后端映射 + `raw_legacy_status` 原样返回 | 避免前端再写第二份；审计可追溯 |
| F11 | 旧入口下线顺序 | §3.3；能力未迁走不得删 | 任务书要求「禁止直接删功能」 |
| F12 | 位置/视口归属 | 板面矩形唯一；viewport 本机（Phase 1）；阅读位置仍按附件版本 | 避免出现第二套卡片位置 |
| F13 | 图片比例是否允许自动改尺寸 | 建议否；只做只读/用户显式动作 | 与冻结契约 §16.3/17.1 直接冲突，必须产品确认 |
| F14 | 新链 pause/cancel API | 走新服务 + `expectedVersion` CAS；旧接口只对旧链 | 否则 v2 无法安全控制新任务 |
| F15 | 新链 preview/impact 数据 | 二选一：补数据 or 明确降级并保留旧链只读 | 决定 IntentPreviewCard/ImpactConfirmDialog 是否可迁 |

### 6.4 明确不做（避免 v2 又造第三套）

- 不新建第三套意图/执行状态词表；新链状态以 `tool_models.INTENT_STATUSES` 为准。
- 不让前端自己推导授权/冲突/依赖/完成；这些只由服务端判定（现有代码已如此）。
- 不在图片加载/窗口变化时自动改卡片几何（§16.3/§17.1）。
- 不把「许可」与「可见范围」合并成一个布尔；不把「隐藏」当取消许可使用。
- 不删除 `board_understanding` / `board_executions` 历史行；只读保留。
- 不为新字段引入第二套渲染/审批组件；复用现有组件并做数据适配。

---

## 7. 附录

### 7.1 关键证据索引（按主题）

- 两代链入口：`docs/architecture.md:1440-1487`；`docs/qio-refactor-contract.md:648-799`；
  `frontend/src/stores/interactive.ts:3711-4115`（旧理解）、`:4120-4300`（旧执行）；
  `frontend/src/components/interactive/IntentWorkbench.vue:136-300`（新链）。
- 提交/受理：`backend/src/agent/interactive/submission.py:1974-2298`；
  `backend/src/agent/api/interactive_store.py:323-421`；`submission.ts:445-647`。
- 等待/续作：`submission.py:1154-1603`；迁移 49 `backend/src/agent/storage/migration_p3_a.py:30-78`。
- 意图/确认/账本/恢复：`backend/src/agent/interactive/intent_service.py:32-71`、`:476-518`、
  `:551-943`、`:1003-1180`、`:1215-1527`、`:1533-1598`；
  迁移 50 `migration_p3_b.py:26-78`。
- 旧意图/执行：`backend/src/agent/interactive/intents.py:34-50`、`:531-593`、`:741-757`、
  `:1027-1240`、`:1384-1458`；`execution.py:296-334`、`:599-660`、`:1926-1980`。
- 参与/许可：`models.py:202-258`；`board.ts:945-972`；`submission.ts:70-134`；
  `stores/interactive.ts:3349-3364`；`BoardCard.vue:1252-1300`、`:1700-1715`。
- 连线/组：`backend/src/agent/interactive/board.py:212-287`、`:447-683`；
  `frontend/src/interactive/board.ts:678-917`；`BoardLinkLayer.vue:46-64`。
- 草稿：`backend/src/agent/interactive/board_store.py:301-586`；`drafts.ts` 全文；
  `services/interactive.ts:200-259`；`SubmitCluster.vue`/`CardDraftHint.vue`。
- 位置/预览：`preview_routes.py:42-49`、`:246-345`；`preview.py:325-348`；
  `viewport.ts:277-287`；`chatFrame.ts`；`stores/interactive.ts:442-465`。
- 文档冲突：`docs/status.md:1751-1756`；`docs/interactive-mode-contract.md` §1.4/§9.3/§14/§17；
  `docs/conversation-page-contract-20261022.md` §4.2-§4.4。

### 7.2 建议的 Phase 1 定向验收（数据/接口层）

- `GET /intent-views` 与 `/intents` 的混读边界：legacy 项不得返回「可确认成功」（可确认但必然失败 = 缺陷）。
- 对象 version 保存往返：前端编辑一次 → 后端 version 必增/不变规则明确；`expected_versions` 冲突可复现。
- `material_version`：改一张依据卡的正文 → 待确认意图确认被拒并说明；改位置/折叠 → 不拒绝。
- `resume-state`：造等待 + 重启后读取 → 仍能看到等待与可恢复意图；未确认前不自动建 Turn。
- `resume`：同 `resumeKey` 重放 → 原 turn；不同内容 → 409/结构化冲突。
- 组端点：不可见成员组成的组不得出现在链接载荷；旧客户端加载不得静默丢数据（最低版本校验）。
- 图片比例：只读字段不参与提交/求差；加载图片不改变 `card.w/h`。
- `retry`：各域 attempt 从 1 递增、上限生效、不自动无限重试；UI 文案与真实状态一致。
- 参与状态：`participation` 与 `visible-range`/提交实际载荷逐项一致（同一服务端函数）。

### 7.3 本审计未做/未验证

- 未运行测试、未启动应用、未做真实界面检查（只读静态审计）。
- 未验证真实模型/真实凭据链路（仓库既有证据也均为 fake/mock）。
- 未逐行覆盖 `BoardCanvas.vue`、`ChatDock.vue` 等超大组件；能力结论基于其对外事件/数据入口与关键分支。
- 未验证打包后 sidecar 的行为差异；接口表以源码路由为准。
