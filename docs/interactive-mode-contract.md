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

**允许查看的范围**由 `models.py:selectable_cards` **唯一实现**（前后端与提交载荷都用它）：

| 卡片 | 默认是否允许查看 | 说明 |
| --- | --- | --- |
| 文字注释 `text`（说明 / 态度 / 优先级 / 任务要求） | **否**，勾选后才允许 | 未勾选 = QIO 完全看不到它的文字与注释链接，包括提交的前后状态 |
| 材料 `file` / `image` / `code` / `url` | 是 | 添加材料本身不等于要求总结、比较、修改或执行（那是意图问题，不是可见性问题） |
| QIO 结果 `reply` | — | 是 QIO 自己的产出，不是用户表达，不进提交载荷 |
| 任意卡片 `hidden=true` | 否 | 明确隐藏 = 退出讨论范围 |
| `deleted=true` | 否 | 已删除的卡片不进范围 |

- 勾选只表示本次允许查看，仍须提交；提交成功后自动取消勾选（不是删除或撤回）。
- 勾选框只出现在文字注释上（材料没有这个选择框）。
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
`intentBearing=false`（不作为意图依据，但仍记录为板面变化）：
`layout_only`、`material_added`、`material_removed`、`note_deleted`、`link_removed`。
其余表达式 `intentBearing=true`；`focus_selection` 表示仍然有效的关注范围。
见 `models.NON_INTENT_EXPRESSIONS`。

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

## 7. 第一阶段不做（**在本轮前端改版之后**仍不做）

高亮、指向箭头、自由手写绘制、旋转、标签、筛选、前后遮挡顺序、直接对象包含、独立重点标记控件；
QIO 的真实模型理解、外部工具执行；多板面协同编辑。
---

## 8. 前端改版约定（2026-10-07 追加，取代 §6 中与布局相关的初始选择）

本轮只改互动板前端；板面数据、保存、提交、审批、影响判断与恢复的契约（§1–§7）保持不变。

### 8.1 页面结构

- **板面为主体**，材料、注释、分组、关系与虚线预览都在板面上；操作界面浮在板面上，**没有常驻右侧栏**。
- **顶部**只保留板面身份、回到对话、保存/提交状态、任务入口与演示入口；不放整套编辑按钮，不长期显示大段说明。
- **底部横向悬浮工具栏**：添加菜单（文字/文件/图片/代码/网址统一入口）、整理（成组/移出/解除/有序/合并）、
  选择、撤销/重做、板内搜索；**右端是提交区**，与编辑操作留出间距并区分样式。
- **右下角独立聊天入口**：点击展开/收起悬浮对话框；收起不清消息、不删草稿、不打断进行中的对话。
  面板背景透明，消息气泡与输入区可用轻量底色；不整块不透明、不重度模糊。
- **右上角批量列表入口**：只在**同一批**等待审批的意图 ≥4 时出现；默认收起，由用户点击展开；
  不因数量变化抢占用户的开合决定。
- **中央影响确认框**：改动执行中任务依赖的材料前，在改动生效前说明影响；确认=改动生效+相关任务暂停并保留进度，
  取消=改动不生效、任务继续。不再用顶部横幅承担这一步。
- **卡片局部工具栏**：选中后才在卡片附近出现，取消选择即隐藏；注释卡片的「本次允许 QIO 查看」勾选框在这里。

### 8.2 板面操作（必须真实可用）

| 操作 | 规则 |
| --- | --- |
| 拖动空白处 | 平移查看位置（不按修饰键） |
| 空格 + 拖动空白处 | 框选卡片（不要改成 Shift；其他加选方式可保留，但不替代这条） |
| 输入框 / 聊天 / 菜单 / 确认框内 | 空格正常输入，不触发板面操作 |
| 滚轮 | 在板面上以指针附近为缩放中心；聊天、列表、代码区优先滚动自身，不穿透成板面缩放 |
| 连接点拖线 | 选中卡片显示连接点，拖到另一张卡片建链；拖到无效位置或取消时不建链、不保存半条链接 |
| 卡片重叠 | 拖到另一张未分组卡片上时提示「松开后合并成组」，松手才成组；靠近不成组、边框相碰不擅自合并 |
| 组名 | 成组后初始名「默认组名」；输入即用，留空/取消保留默认名，组仍成立并可提交 |
| 坐标 | 平移缩放后，卡片拖动、框选、连线、预览与局部工具栏必须仍然准确 |

平移与缩放只改变查看状态：不形成表达、不调用 QIO、不触发保存。

### 8.3 聊天与提交互相独立

- 文字发送**只发文字**，沿用当前对话上下文；不得附带板面、未提交改动、注释或选择范围，
  **不得调用板面提交接口**。
- 板面提交仍走 `POST /api/interactive/boards/{id}/submissions`（既有前后状态与可见范围规则不变）。
- 收起/展开聊天不丢消息、不丢回复状态、不丢草稿；同一会话不重复建立事件订阅、不重复写消息、不重复建轮次。
- Enter 发送、Shift+Enter 换行、中文选字中不发送、空白不发送；失败保留输入并给真实原因。

### 8.4 前端接口与所有权（冻结）

| 组件 / 模块 | 所有权 | 约定 |
| --- | --- | --- |
| `views/InteractiveView.vue`、`stores/interactive.ts` | 主智能体 | 组合全部浮层；新增 `chatOpen` / `batchOpen` / `tasksOpen` / `batches` / `listBatches` / `nonPendingIntents` |
| `components/interactive/BoardToolbar.vue`、`AddMenu.vue`、`BoardSearchPanel.vue`、`styles/interactive-shell.css` | A | 底部工具栏与添加菜单；工具栏右端放 `<SubmitCluster />` |
| `components/interactive/BoardCanvas.vue`、`BoardCard.vue`、`BoardGroupFrame.vue`、`BoardLinkLayer.vue`、`interactive/viewport.ts`、`interactive/board.ts` | B | 平移/缩放/空格框选/连接点/重叠成组；坐标换算统一走 `viewport.ts` |
| `components/interactive/ChatDock.vue`、`SubmitCluster.vue`、`BoardChangeList.vue`、`interactive/chat.ts` | C | 聊天与提交状态；直接读 `stores/session.ts` 与互动 store |
| `components/interactive/IntentBatchTray.vue`、`IntentStatusPopover.vue`、`ImpactConfirmDialog.vue`、`IntentPreviewCard.vue`、`interactive/approval.ts` | D | 批量列表、任务状态浮层、中央影响确认、单项审批 |

**冻结函数签名**

```ts
// interactive/viewport.ts（B 实现）
export interface Viewport { scale: number; x: number; y: number }
export const IDENTITY_VIEWPORT: Viewport;
export function toBoardPoint(viewport, client: {x,y}, rect): {x,y};
export function toScreenPoint(viewport, point: {x,y}, rect): {x,y};
export function zoomAt(viewport, factor: number, client: {x,y}, rect): Viewport;
export function rectFromDrag(viewport, start: {x,y}, end: {x,y}, rect): {x,y,w,h};
export function rectsIntersect(a, b): boolean;

// interactive/approval.ts（D 追加，既有函数不动）
export interface IntentBatch { key: string; intentIds: string[]; pendingIds: string[] }
export function batchKeyOf(intent: Intent): string;
export function groupIntentsByBatch(intents: Intent[]): IntentBatch[];
export function batchesWithList(intents: Intent[]): IntentBatch[];   // 同一批等待审批 ≥4

// interactive/chat.ts（C 实现）
export function canSend(text: string): { ok: boolean; reason?: string };
export function sendFailureText(message: string | null): string;
```

### 8.4.1 跨组件通道（2026-10-07 裁定）

工具栏与画布现在是**兄弟节点**（页面壳同时渲染），因此约定：

| 交互 | 走哪条通道 | 理由 |
| --- | --- | --- |
| 会改板面状态的操作（添加卡片、撤销/重做、成组/移出/解除/有序/合并、删除所选） | 触发方**自己**调 `interactive/board.ts` 的纯函数算出 next，再 `store.commit(next, 中文说明)` | 契约 §4.3 早就规定「组件不直接改 store.board，算好新状态后调 commit」；同一批纯函数谁调都一样，不存在逻辑复制 |
| 板面指针模式（单选多选 / 区域选择 / 关系模式） | `store.boardMode` + `store.setBoardMode()` | 它是**持久的共享界面状态**，工具栏写、画布读，用 Pinia 而不是事件更不容易失步 |
| 一次性动作（定位到某张卡片） | window 事件 `qio:interactive:locate-card`（detail `{ cardId }`） | 与既有 `qio:interactive:locate-preview` 同一约定：一次性命令用事件，不用持久状态 |

不做「命令总线」：画布不再要求工具栏把事件发给自己转发，工具栏也不把板面状态传输给画布。
两者都只依赖 store 与 `board.ts` 纯函数。板内搜索面板由工具栏渲染成浮层（不再常驻右列）。

**data-im 钩子（实机验收依赖）**：`board-toolbar`、`add-menu`、`add-text|add-file|add-image|add-code|add-url`、`undo`、`redo`、
`search`、`submit`、`save-status`、`submit-status`、`change-list`、`visible-range`、`chat-toggle`、`chat-panel`、`chat-input`、`chat-send`、
`card-toolbar`、`check`、`connect-point`、`group-merge-hint`、`batch-entry`、`batch-list`、`batch-item`、`batch-approve`、`batch-reject`、
`tasks-entry`、`tasks-popover`、`impact-dialog`、`impact-continue`、`impact-cancel`。

---

## 9. 修复与审美优化约定（2026-10-07 第二轮，**覆盖此前写偏的条文**）

用户已确认的规则优先于本契约此前与测试的写法。以下条文与前面冲突时，以本节为准。

### 9.1 默认组名是「默认组名」（覆盖 §1.3 / §6.1 的「组 N」）

- 需要系统默认名的所有路径（两张卡片重叠成组、手动新建组、两个已有组合并等）统一使用 **`默认组名`**；
  `defaultName: true` 只表示「这个名字是系统给的」，**不替用户表达任何关系判断**。
- 成组后可以改名；留空或取消改名时**分组仍然成立**并保留默认名；卡片加入已有组时**保留已有组名**。
- 组身份按 **id** 区分，**不依赖名称唯一性**：默认名可以重复。
- 历史数据里的「组 N」**不被批量覆盖**（`defaultName` 按存储值保留）；识别默认名时要同时认这两种形态。
- 前端预览、后端保存与重新读取必须一致（不只是改显示文字），成员、链接、顺序与撤销/重做不受影响。

### 9.2 批次只能靠可证明的来源（覆盖 §8.5 的③与「绝不会混批」的表述）

- **删除**「创建时间相近 / 同一秒就是同批」的推断。
- 只用能证明同一次产生过程的来源：① 前端按次记录的创建批次（有界、可清理）；② 真实的 `submissionId`
  （仅在实际调用链保证同一次提交产生多项时使用，不因为字段存在就扩大含义）。
- 两个来源都不可靠时：该意图**各自成批**，或如实说明「批次不可恢复」；**绝不误合并**。
  本地记录被清除、写入失败、被禁用、达到容量上限或损坏时，一律走这条规则。
- 本轮不改数据库结构；前端记录的范围与清理策略必须有界。

### 9.3 同批达到四项后，列表保留到处理完（覆盖 §8.1 的「同一批等待审批 ≥4」）

- 判定资格用**该批产生的意图总量 ≥ 4**，不是「每次剩余的待审批数 ≥ 4」。
- 用户处理掉其中若干项后，只要还有未处理项，该批入口与列表**继续保留**，并显示剩余待处理数量；
  已处理项不能再次提交审批。
- 全部处理完后关闭/移除该批入口。初始只有三项的批次**不会**因为别处另有一项而变成四项列表。
- 刷新恢复后继续保持批次资格与处理状态（不能只靠组件内临时标记）；开合由用户决定，
  不因数量更新自动展开、不因部分处理自动收起；内容更新时保留合理的阅读位置。
- 批量操作继续遵守依赖、冲突、过期预览与服务器审批结果。

### 9.4 聊天草稿要持久保存并可恢复（覆盖 §6.4 的「不承诺恢复」）

- 草稿按**实际会话/话题**区分并持久保存；与卡片草稿互相独立，切换话题不会错用。
- 输入过程中及时保存（防抖，不阻塞输入）；正常离开或关闭页面时处理待保存内容。
- 刷新/关闭后重开可继续编辑，**不自动发送**、不提交板面、不附带板面。
- 发送受理成功后才删除对应草稿；失败恢复原稿且**不覆盖用户后来输入的新文字**；
  旧会话写入与迟到的保存回执不得覆盖新草稿。
- 保存失败要明确提示、保留内存文字并可重试，**不得显示「已保存」**。
- 悬浮聊天与对话页共用同一份草稿与同一套保存/恢复逻辑，**只有一个写者**，不新建第二套事件订阅或轮次管理。

### 9.5 卡片草稿保存失败不能静默

- 建立真实的草稿保存状态与失败原因，失败时保留编辑内容并提供重试。
- 提示靠近当前编辑对象/编辑入口，不长期占据整个板面。
- 处理「防抖还没触发就离开编辑器/页面」的情况；编辑器关闭后仍能恢复最后一次已保存的未完成输入。
- 重试不创建正式卡片、不提交板面、不调用 QIO；刷新恢复后也不自动执行这些操作。
- 多个草稿、连续输入、旧请求返回时，不丢较新的内容、不串用其他卡片的内容。

### 9.6 审美与布局目标（可测量）

- **底部工具栏**：常态只留添加、板内搜索、撤销/重做、必要的视图控制、简短的可见范围与右端提交按钮；
  分组/加入组/移出组/解除组/有序/合并/删除所选移到**选中对象附近**的工具栏或按选择出现的菜单；
  未选中时不铺一排禁用按钮；消除重复分隔线与重复包裹；**800×600 下工具栏总高不超过约 96px、最多两行**。
- **信息层级**：顶部只留板面名、准确保存状态与必要入口；数量退到次级；「已保存」与「已提交」各自只设一处主状态；
  提交区常驻「本次可见：材料 X · 注释 Y」+ 提交操作，完整前后变化进详情；错误与未接入状态必须真实可见。
- **界面不出现开发用语**（adapter、接口、求差、状态机、DOM 等）。
- **浮层共存**：较宽窗口同时展开聊天与批量列表时优先**并排**；800×600 同时展开时消息正文、审批说明、
  输入区与提交入口都不得被另一面板遮住；空间不足时采用明确的面板切换或重排，保留消息/草稿/选择/开合状态；
  按**实际可用区域**计算，不用「窗口高度减固定值」；两面板相交面积必须为 0。
- **透明聊天**：外层透明、气泡/输入区可用轻量底色；不加重度模糊或厚重发光；背景有图片/代码/连线时文字仍可读。
- **卡片与分组**：材料类型→标题→正文→元信息依次分层；组框比卡片更轻；正式内容实线、待审批完成后虚线（配文字）；
  单项审批入口靠近对应预览并跟随平移缩放；选中态与悬停态不同；卡片旁工具栏不遮标题、连接点或输入内容。
- **令牌与字体**：颜色/阴影/边界/状态/焦点一律走令牌（含备用阴影与遮罩）；三声部字体；正文与主要操作不得缩成元信息字号；
  动效走既有令牌并遵守减少动态效果；拖动直接跟随指针。
- **作用域**：样式改动限定在互动版；不得让对话页、设置页或星球导航外观意外变化。

### 9.7 本轮前端所有权（一个文件同一时间只有一个写者）

| 文件 | 写者 |
| --- | --- |
| `views/InteractiveView.vue`、`interactive/types.ts`、`services/interactive.ts`、`scripts/interactive-verify/fe-scenarios.mjs`、`docs/**` | 主智能体 |
| `components/interactive/{BoardToolbar,AddMenu,BoardSearchPanel,BoardCard,BoardGroupFrame,SelectionMenu}.vue` | A |
| `interactive/board.ts`、`interactive/approval.ts`、`backend/src/agent/interactive/{models,board}.py` 及对应用例 | B |
| `stores/session.ts`、`stores/interactive.ts`、`interactive/drafts.ts`、`components/interactive/{ChatDock,CardDraftHint}.vue` 及对应用例 | C |
| `interactive/overlayLayout.ts`、`components/interactive/{IntentBatchTray,IntentStatusPopover,ImpactConfirmDialog}.vue`、`styles/interactive-shell.css`、`docs/interactive-ui-verify.md` | D |

冻结签名见 `docs/superpowers/plans/2026-10-07-interactive-ui-refinement.md` §4。

### 8.5 批次判定（前端，不改数据库）【已被 §9.2 覆盖，仅作历史记录】

优先级：① 本次会话里由同一次创建动作产生的意图（演示入口一次四项、提交后一次生成的多项）记在
`localStorage["qio.interactive.intentBatches"]`；② 服务端 `submissionId` 相同；③ `createdAt` 截断到秒相同。
三条都拿不到时，该意图自成一批（**宁可不出批量列表，也不把不同批次相加**）。

