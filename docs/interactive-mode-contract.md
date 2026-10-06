# 互动模式第一阶段：公共契约（唯一约定来源）

本文件是「互动模式」第一阶段的**接口与数据含义契约**，由主智能体（Lead）维护。
子智能体实现前必须先读本文件：名字、字段、接口路径、状态取值、可见性边界都以这里为准。
实现细节可以改，**契约签名不能各自改**；确有必要时先找 Lead。

- 实施方案：`docs/superpowers/plans/2026-10-06-interactive-foundation.md`
- 起点提交：`ee6bbff`（`origin/main`），开发分支 `feat/interactive-foundation`

---

## 0. 术语与阶段边界

**互动模式**与现有对话模式并列：用于和 QIO 共同整理材料、关系和工作。
铁律：**QIO 在互动模式中能做的，是用户在互动模式中能做的子集**。不允许给 QIO 增加用户没有的板面操作。

第一阶段只做「能独立验收的基础能力 + 完整流程」，QIO 的真实理解 / 模型调用 / 外部执行
**没有接入**，只留接入位置；演示意图在明确标注的演示入口触发，不得描述成真实 QIO 判断。

## 1. 数据含义

### 1.1 板面状态（BoardState）

板面状态是一个 JSON 文档，前后端同构（`frontend/src/interactive/types.ts` ↔
`backend/src/agent/interactive/models.py`）：

    BoardState = {
      boardId, seq, updatedAt,
      cards: Card[], groups: Group[], links: Link[],
      selection: string[]          // 提交时仍然有效的多选 / 区域选择
    }

`Card`（材料与注释都用它）：

| 字段 | 含义 |
| --- | --- |
| `kind` | `text`（注释 / 说明文字）、`file`、`image`、`code`、`url`、`reply`（QIO 结果卡片） |
| `content` | 文字正文 / 文件说明 / 代码 / 网址文本 |
| `meta` | 材料元信息：`file:{name}`、`image:{name}`、`code:{language}`、`url:{href,title}`、`reply:{intentId}` |
| `x,y,w,h` | 位置与大小。**属于板面状态，但不作为意图依据**（普通移动 / 缩放只影响显示） |
| `checked` | 注释勾选：本次是否允许 QIO 查看。默认 false |
| `hidden` | 明确隐藏 = 退出讨论范围。与 `checked` 互斥 |
| `folded` | 折叠：只改变显示，不影响任何含义 |
| `bookmarked` | 书签：提高查找优先级，不产生意图 |
| `deleted` | 删除 = 撤回当前材料或关系，不表示否定内容、不自动取消任务 |
| `createdAt`/`updatedAt` | ISO 时间 |

`Group`：`{id, name, defaultName, ordered, x, y, w, h, members: string[], deleted, createdAt, updatedAt}`
`Link`：`{id, src, dst, direction: boolean, meaning: string, deleted, createdAt, updatedAt}`

### 1.2 状态不变式（`board.normalize_state`）

- G1 一张卡最多属于一个组；组成员不重复。
- G2 `ordered=true` 的组，`members` 顺序即序号（展示为 1..n）；序号永远连续。
- G3 成员全部被移除或删除的组自动消失（不存在空组）。
- G4 链接端点必须是存在且未删除的卡片；同一对 (src,dst) 只保留一条。
- G5 被删除的卡片不进组、不进链接、不参与选择。
- G6 `hidden=true` 时 `checked` 强制为 false。
- G7 `kind='reply'` 的卡片 `checked` 恒为 false（QIO 结果不参与注释勾选语义）。
- G8 `selection` 只包含存在且未删除的卡片。

### 1.3 分组与顺序（业务规则，A 负责实现）

- 两张**未分组**卡片重叠 → 自动成组，默认组名（`组 N`），可改名；保留默认名也能提交。
- 未分组卡片拖入已有组 → 直接加入并**保留原组名**。
- 两个已有组重叠 → 合并：原组不再独立保留，新组用默认名。
- 普通组的自由摆放不表示先后；有序组显示明确序号，顺序调整可作为依据。
- 新卡片按拖入位置插入有序组，后续序号更新。
- 两个有序组合并：保留各自内部顺序，被拖入组的成员**连续插入**目标位置，统一编号。
- 有序组与普通组合并 → 普通组（取消序号），用户可重新设为有序。
- 关系链接可表达关联、方向和用户写明的含义；**不得自行把方向解释成因果 / 支持 / 执行顺序**。

### 1.4 可见范围（权限边界，B 负责实现）

**允许查看的注释** = `checked && !hidden && !deleted && kind !== 'reply'`（`models.py:selectable_cards`）。

- 未勾选 = QIO 完全看不到它的文字与注释链接，包括提交的前后状态。
- 勾选只表示本次允许查看，仍须提交；提交成功后自动取消勾选（不是删除或撤回）。
- 提交时两份状态都**限于本次允许查看的范围**；范围外的一切都不进提交载荷。
- 组名是独立关系依据：组内至少有一名可见成员时，提交载荷包含该组名，但**只列出可见成员**；
  一名可见成员都没有的组整体不出现（否则组名会间接暴露被隐藏注释的存在）。
- 被隐藏注释不得通过成员描述、链接端点、引用、搜索结果或任何间接内容泄露。
  **链接只有两端都可见时才进入提交载荷。**
- 板内局部搜索可以查到用户自己的未勾选注释，这不等于把它交给 QIO。
- 服务端从**已保存的板面状态**推导可见范围，不接受客户端传入未勾选卡片 id。

### 1.5 保存与提交（B 负责实现）

- 用户每完成一次操作就自动保存；**保存不调用 QIO**，`PUT /state` 只落库。
- 点击提交按钮时，QIO 才获取尚未提交的有效表达。
- 每次提交提供「上次成功提交时」与「本次提交时」的板面状态，两份都限于本次允许查看的范围。
- 提交成功后才更新「上次成功提交」基准；提交失败保留改动与本次注释选择，允许再次提交。
- 有效改动由**前后两份状态求差**得出（不是操作流水）：提交前已撤销的中间操作不形成表达；
  普通移动 / 取消掉的选择不产生额外表达。
- 空提交 / 重复点击给出一致且不重复调用的处理：`empty`（无可提交内容）、
  `duplicate`（与上次成功提交内容一致）两种状态都**不更新基准、不调用 QIO**。

**表达式（expression）**：`{id, kind, intentBearing, summary, cardIds, groupId, linkId}`。
`kind` 取值：`note_added | note_edited | note_deleted | material_added | material_removed |
link_added | link_removed | link_meaning_changed | group_formed | group_merged | group_renamed |
group_membership_changed | order_changed | ordered_changed | focus_selection | layout_only`。
`layout_only` 与 `focus_selection` 的 `intentBearing` 为 `false`（位置 / 大小不构成意图依据）。

### 1.6 意图与审批（C 负责实现）

状态机（`INTENT_STATUSES`）：

    pending ──approve──▶ waiting_dependency ──前项 done + 用户再次确认──▶ running
      │                        │                                          │
      │                        └──（前项未 done：一直等，不自动开始）        ├─▶ done
      ├──reject──▶ rejected（预览消失，原内容保留）                        ├─▶ failed ──▶ 撤回
      └──材料变化──▶ needs_update（禁止批准，下次提交后由 QIO 更新）        └─▶ paused（关闭/重启后保持）

- 每项意图有批准 / 拒绝入口；同一批达到 4 项时提供简洁列表，支持选择部分或全部、批量批准或拒绝、点击定位。
- 互不相容的结果不能同时批准（`conflictsWith` 交集或同一非空 `conflictKey`）。
- 互不相容判定与依赖等待都在**服务端**判定，前端只显示结果。
- QIO 对正式板面的任何改动（含新增回复文字卡片）必须经审批；批准后才成为任务；
  拒绝后预览消失、原内容保留；成功完成后结果成为实线正式内容。
- 预览可以被用户移动：只改位置且工作内容 / 材料范围 / 结果关系不变 → 可直接批准；
  改变工作要求或关系含义 → `needs_update`，须提交并由 QIO 更新预览后再批准。
- 相关材料改变 → 待审批预览标记 `needs_update`，禁止批准。
- 执行中用户要改相关材料：先说明受影响任务并要求确认；确认后改动生效、相关任务暂停并保留进度；
  取消则不改动，任务继续。
- 失败 / 取消撤回该任务造成的改动，**保留用户后续修改**；说明失败原因与未撤回部分；
  撤回会影响其他工作时，先撤回不受影响部分，再列出其余部分及具体影响，等待用户决定。
- 不自动重试。正常关闭时任务暂停；重新打开或意外退出后恢复已保存内容与进度，
  任务保持暂停（`running` 在恢复时降级为 `paused`），由用户决定是否继续。

## 2. HTTP 接口（冻结）

全部挂在 `/api/interactive` 下，由 `agent/api/interactive.py` 聚合注册。
每个子模块自带 `APIRouter`（写全路径），聚合器只做 `include_router`。

**B 负责**：`agent/api/interactive_store.py`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/interactive/boards` | 板面列表（无板面时自动建默认板面） |
| POST | `/api/interactive/boards` | 新建板面 `{title}` |
| GET | `/api/interactive/boards/{board_id}/state` | 读取板面状态 + 保存序号 + 上次成功提交信息 |
| PUT | `/api/interactive/boards/{board_id}/state` | **保存**（自动保存入口，绝不调用 QIO） |
| GET | `/api/interactive/boards/{board_id}/history` | 最近状态快照（撤销 / 重做依据） |
| GET | `/api/interactive/boards/{board_id}/submissions` | 提交历史 |
| POST | `/api/interactive/boards/{board_id}/submissions` | **提交**（QIO 获取表达的唯一入口） |
| GET | `/api/interactive/boards/{board_id}/visible-range` | 本次允许查看的范围（提交前预览用） |
| GET/PUT | `/api/interactive/drafts/{board_id}` | 文字草稿保存 / 恢复（草稿不是提交内容） |

提交返回：`{status, submission, before, after, expressions, baseline, delivery, visibleRange, checkedCleared}`。
`status ∈ {succeeded, failed, empty, duplicate}`。`before`/`after` 都是**投影到本次可见范围**的快照：
`{cards, groups, links, selection, empty}`。

**C 负责**：`agent/api/interactive_intents.py`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/interactive/boards/{board_id}/intents` | 意图列表 + 冲突分组 + 批量可用性 + 恢复信息 |
| POST | `/api/interactive/boards/{board_id}/intents` | 创建意图（演示入口与集成接入都走它） |
| POST | `/api/interactive/intents/{intent_id}/approve` | `{confirmDependency}` |
| POST | `/api/interactive/intents/{intent_id}/reject` | 拒绝 |
| POST | `/api/interactive/intents/{intent_id}/preview` | `{preview}` 调整预览（判定可否直接批准） |
| POST | `/api/interactive/intents/{intent_id}/demo/advance` | 演示执行推进 `{outcome}`（**明确标注为演示**） |
| POST | `/api/interactive/intents/batch` | `{approve: [], reject: []}` 批量审批 |

## 3. 跨模块函数（冻结签名）

模块文件由**归属者**实现；骨架提交里已给出同名占位函数，保证任何分支都能 import。

`agent/interactive/board.py`（A）：

    def normalize_state(state: dict) -> dict: ...      # 落实 §1.2 不变式，返回新状态
    def preview_drop(state: dict, card_id: str, x: float, y: float) -> dict:
        """拖动期间显示将要加入的组或插入位置。

        -> {"groupId": str | None, "index": int | None, "mergesWith": str | None}
        """
    def drop_card(state: dict, card_id: str, x: float, y: float) -> dict:
        """放下后才完成操作：重叠成组 / 加入已有组 / 合并两组 / 有序组按落点插入。

        -> {"state": dict, "groupId": str | None, "merged": bool, "index": int | None}
        """

`agent/interactive/board_store.py`（B）：

    def ensure_board(conn, *, board_id: str | None = None, title: str | None = None) -> dict
    def load_board(conn, board_id: str) -> dict
    def save_board(conn, board_id: str, state: dict, *, reason: str = "op") -> dict
        # 返回 {"seq": int, "savedAt": str}; 内部必须过 board.normalize_state
    def list_board_states(conn, board_id: str, *, limit: int = 50) -> list[dict]
    def get_draft(conn, board_id: str) -> dict
    def save_draft(conn, board_id: str, drafts: dict) -> dict

`agent/interactive/submission.py`（B）：

    def visible_range(state: dict) -> dict           # 见 §1.4，{"cards","groups","links","selection","empty"}
    def project_snapshot(state: dict) -> dict        # 前后状态投影，仅含可见范围
    def diff_states(before: dict, after: dict) -> list[dict]   # 有效改动表达式，见 §1.5
    async def submit_board(conn, board_id: str, *, requested_visible=None, note="") -> dict
    def last_success_baseline(conn, board_id: str) -> dict | None

`agent/interactive/intents.py`（C）：

    def on_new_submission(conn, *, board_id, submission_id, expressions) -> list[str]
        """把受本次提交影响、且仍在等待审批的意图标记为 needs_update，返回被标记的 id。"""
    def create_demo_intents(conn, *, board_id: str) -> list[dict]
    def list_intents(conn, board_id: str) -> dict
    def approve_intent(conn, intent_id: str, *, confirm_dependency: bool = False) -> dict
    def reject_intent(conn, intent_id: str) -> dict
    def update_preview(conn, intent_id: str, preview: dict) -> dict
    def advance_intent(conn, intent_id: str, *, outcome: str) -> dict
    def batch_decide(conn, *, approve: list[str], reject: list[str]) -> dict
    def recover_running_intents(conn, board_id: str) -> dict   # 重启后 running -> paused

**B → C 的调用**：`submit_board` 成功后调用 `intents.on_new_submission(...)`（延迟 import，
失败不得让提交失败，只能记进返回值 `delivery.marking`）。
**C → B 的调用**：意图完成后由 C 调 `board_store.load_board` / `save_board` 落板面结果。

## 4. 前端接口（冻结）

### 4.1 归属

| 文件 | 归属 |
| --- | --- |
| `frontend/src/interactive/types.ts`、`frontend/src/services/interactive.ts`、`frontend/src/stores/interactive.ts`、`frontend/src/views/InteractiveView.vue`、`frontend/src/router.ts` | Lead |
| `frontend/src/interactive/board.ts`（纯函数）、`frontend/src/components/interactive/BoardCanvas.vue`、`BoardCard.vue`、`BoardGroupFrame.vue`、`BoardLinkLayer.vue`、`BoardToolbar.vue`、`BoardSearchPanel.vue` | A |
| `frontend/src/interactive/submission.ts`（纯函数）、`frontend/src/components/interactive/SubmitPanel.vue`、`BoardChangeList.vue` | B |
| `frontend/src/interactive/approval.ts`（纯函数）、`frontend/src/components/interactive/ReplyPanel.vue`、`IntentPreviewCard.vue`、`IntentBatchList.vue`、`IntentImpactNotice.vue`、`DemoIntentEntry.vue` | C |

### 4.2 板面纯函数（A 实现，签名冻结）

    export function emptyState(boardId: string): BoardState
    export function addCard(state, card: Partial<Card> & { kind: CardKind }): BoardState
    export function updateCard(state, cardId: string, patch: Partial<Card>): BoardState
    export function removeCard(state, cardId: string): BoardState
    export function duplicateCard(state, cardId: string): BoardState
    export function previewDrop(state, cardId, x, y): { groupId: string | null; index: number | null; mergesWith: string | null }
    export function dropCard(state, cardId, x, y): { state: BoardState; groupId: string | null; merged: boolean; index: number | null }
    export function joinGroup(state, cardId, groupId, index?): BoardState
    export function removeFromGroup(state, cardId): BoardState
    export function dissolveGroup(state, groupId): BoardState
    export function renameGroup(state, groupId, name): BoardState
    export function setGroupOrdered(state, groupId, ordered): BoardState
    export function moveWithinGroup(state, groupId, cardId, index): BoardState
    export function mergeGroups(state, sourceGroupId, targetGroupId, insertIndex?): BoardState
    export function addLink(state, src, dst, direction, meaning): BoardState
    export function updateLink(state, linkId, patch): BoardState
    export function removeLink(state, linkId): BoardState
    export function setSelection(state, cardIds): BoardState
    export function selectInRect(state, rect, additive): BoardState
    export function setChecked(state, cardId, checked): BoardState
    export function setHidden(state, cardId, hidden): BoardState
    export function setFolded(state, cardId, folded): BoardState
    export function setBookmark(state, cardId, bookmarked): BoardState
    export function searchCards(state, query): string[]
    export function normalizeState(state): BoardState

### 4.3 Pinia store（Lead 实现，A/B/C 只消费）

`useInteractiveStore()`（`frontend/src/stores/interactive.ts`）：

- 状态：`board`、`loading`、`saveStatus`（`idle|saving|saved|error`）、`dirty`、`lastSavedAt`、
  `submissions`、`lastSubmission`、`submitStatus`（`idle|submitting|succeeded|failed`）、
  `submitError`、`intents`、`intentConflicts`、`batchAvailable`、`visibleRange`、`auxOpen`、
  `demoMode`、`recoverNotice`、`canUndo`、`canRedo`
- 动作：`load()`、`commit(next: BoardState, label: string)`（推撤销栈 + 自动保存，**不调用 QIO**）、
  `undo()`、`redo()`、`saveNow()`、`refreshVisibleRange()`、`submit()`、
  `loadIntents()`、`approve(id, confirmDependency?)`、`reject(id)`、`decideBatch(approve, reject)`、
  `updatePreview(id, preview)`、`advanceDemo(id, outcome)`、`createDemoIntents()`

组件**不直接改 `store.board`**：算好新状态后调 `store.commit(next, label)`，由 store 负责撤销栈与保存。

### 4.4 视觉与交互约定（Lead 定，可调整）

- 板面占主要空间；常用添加 / 整理操作集中显示；提交按钮始终容易找到。
- QIO 回复与任务列表放在**可收起**的辅助区域（右侧），能定位板面预览；局部批准 / 拒绝入口保留在预览附近。
- 虚线预览用 `stroke-dasharray` / `border-style: dashed`，实线表示正式内容。
- 颜色只用 `var(--*)`（`frontend/src/styles/tokens.css`）；状态**同时用文字说明**，不能只靠颜色。
- 拖动期间显示将加入的组 / 插入位置；未完成拖动在中断后回到操作前位置。
- 动画不得妨碍拖动、输入与审批；高频操作即时反馈。

## 5. 分工与写作用域（避免多人改同一文件）

| 角色 | 写作用域 |
| --- | --- |
| Lead | `docs/interactive-mode-contract.md`、`docs/superpowers/plans/2026-10-06-interactive-foundation.md`、`docs/status.md`、`backend/src/agent/storage/schema.py`、`backend/src/agent/interactive/__init__.py`、`backend/src/agent/interactive/models.py`、`backend/src/agent/api/interactive.py`、`backend/src/agent/api/server.py`、`frontend/src/interactive/types.ts`、`frontend/src/services/interactive.ts`、`frontend/src/stores/interactive.ts`、`frontend/src/views/InteractiveView.vue`、`frontend/src/views/ConversationView.vue`、`frontend/src/router.ts`、`backend/tests/test_interactive_integration.py` |
| A | `backend/src/agent/interactive/board.py`、`backend/tests/test_interactive_board.py`、`frontend/src/interactive/board.ts`、`frontend/src/interactive/__tests__/board.test.ts`、`frontend/src/components/interactive/Board*.vue` |
| B | `backend/src/agent/interactive/board_store.py`、`backend/src/agent/interactive/submission.py`、`backend/src/agent/api/interactive_store.py`、`backend/tests/test_interactive_submission.py`、`frontend/src/interactive/submission.ts`、`frontend/src/interactive/__tests__/submission.test.ts`、`frontend/src/components/interactive/SubmitPanel.vue`、`BoardChangeList.vue` |
| C | `backend/src/agent/interactive/intents.py`、`backend/src/agent/api/interactive_intents.py`、`backend/tests/test_interactive_intents.py`、`frontend/src/interactive/approval.ts`、`frontend/src/interactive/__tests__/approval.test.ts`、`frontend/src/components/interactive/ReplyPanel.vue`、`Intent*.vue`、`DemoIntentEntry.vue` |

## 6. 明确标注为「本轮设计选择」的部分（不是用户已确认）

1. 辅助区域放右侧且可收起，局部批准 / 拒绝入口留在预览附近。
2. 提交前可查看本次有效改动与允许查看的注释范围；不要求逐条勾选普通表达；自动取消勾选不等于删除。
3. 拖动期间只显示预演，放下才提交操作；中断后回到操作前位置。
4. 文字输入保存为**草稿**：重新打开保留草稿且仍未提交；输入过程中保存不调用 QIO；
   提交或确认编辑时才确定有效文字状态。意外退出只保证已保存内容，不承诺未保存的输入能恢复。
5. 影响说明列出对象、任务与后果，并提供明确的继续 / 取消；一般布局调整不增加确认。
6. 默认组名使用「组 N」；有序组序号从 1 开始。
7. 首次提交无历史基准时，`before` 为「空快照 + `firstSubmission: true`」。
8. 组名进入提交载荷的条件见 §1.4（至少一名可见成员）。

## 7. 第一阶段不做（不得声称已实现）

高亮、指向箭头、自由手写绘制、旋转、标签、筛选、前后遮挡顺序、直接对象包含、独立重点标记控件；
QIO 的真实模型理解、外部工具执行；多板面协同编辑。
