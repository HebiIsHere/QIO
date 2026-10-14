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
| 文字卡 `text`（统一卡：其中的普通文字 / 标题 / 列表 / 引用 / **链接** / **代码**） | **否**，需整卡许可 | 未许可 = QIO 完全看不到这张卡的任何内容，包括链接地址、代码正文与派生文本，以及提交的前后状态（2026-10-12 起，链接与代码不再有默认可见通道） |
| 材料 `file` / `image` | 是 | 添加材料本身不等于要求总结、比较、修改或执行（那是意图问题，不是可见性问题） |
| QIO 结果 `reply` | — | 是 QIO 自己的产出，不是用户表达，不进提交载荷 |
| 任意卡片 `hidden=true` | 否 | 明确隐藏 = 退出讨论范围 |
| `deleted=true` | 否 | 已删除的卡片不进范围 |

- **许可持续有效**：`checked` 打开后一直有效，提交成功 / 失败 / 重复回执 / 超时 / 受理结果未知 /
  恢复 / 刷新 / 重启 / 模式切换都**不会**清除它；只有用户手动取消（或把卡片隐藏）才失效。
  旧回执不得把用户更晚的取消恢复成开启（2026-10-12 起，取代此前「提交成功后自动取消勾选」的规则）。
- 许可按**整张文字卡**计：卡里的文字、链接、代码共用这一个许可；勾选框只出现在文字卡上。
- 「许可」与「可见范围」分别判断：许可决定这张卡能不能被 QIO 看到，隐藏 / 删除决定它是否退出范围。
  隐藏不是「提交后偷偷清许可」的替代手段 —— 隐藏与取消许可都是用户可见、可撤销的动作。
- 提交时两份状态都**限于本次允许查看的范围**；范围外的一切都不进提交载荷。
- 组名是独立关系依据：组内至少有一名可见成员时，提交载荷包含该组名，但**只列出可见成员**；
  一名可见成员都没有的组整体不出现（否则组名会间接暴露被隐藏注释的存在）。
- 被隐藏注释不得通过成员描述、链接端点、引用、搜索结果或任何间接内容泄露。
  **链接只有两端都可见时才进入提交载荷。**
- 板内局部搜索可以查到用户自己的未勾选注释，这不等于把它交给 QIO。
- 服务端从**已保存的板面状态**推导可见范围，不接受客户端传入未勾选卡片 id。

#### 1.4.1 统一文字卡与历史迁移（2026-10-12）

- 用户可见的字符内容只有一种卡片：**文字卡**。它的内容是一个块序列
  （`paragraph` / `heading` / `list` / `quote` / `code` / `link`），存在 `card.meta.doc`
  （`{"schema": "qio.textcard/1", "blocks": [...]}`）；`card.meta.docText` 是编辑区文本，
  `card.content` 是块序列的纯文本派生（摘要 / 搜索用）。
- 旧的 `kind="code"` / `kind="url"` 卡片在**板面状态加载时逐张迁移**成 `kind="text"`：
  一张旧卡对应一张新卡（**绝不合并**），id / 位置 / 分组 / 关系 / 书签 / 折叠 / 草稿键 / 任务引用全部保留；
  代码保真（缩进 / 换行 / 行尾空白 / 语言），链接保留地址与标题。迁移幂等，第二次加载不再写。
- 迁移保持**原可见范围**：旧注释保留原许可（未许可的迁移后仍然不可见）；旧代码 / 网址卡
  过去默认在材料范围内，迁移后 `checked=true`。迁移**不得**让任何原本不可见的内容变成可见。
- 添加菜单里新建字符内容只有「文字卡」一项。代码**文件**仍属于文件材料（`kind="file"`，只读预览），
  与卡内代码块是两件事。
- 未迁移的遗留 `code` / `url` 卡片在迁移完成前**一律不可见**（不回退成默认可见），界面上标注「旧卡片，尚未迁移」。

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
2. 提交前可查看本次有效改动与本次允许查看的范围；不要求逐条勾选普通表达；提交不会取消许可（许可只有用户手动取消才失效）。
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
  **落点规则（2026-10-14 修）**：不许压住卡片 —— 竖向先上方、后下方，且整个工具栏都要在可视区内；
  竖向两边都放不下（卡片比可视区还高）时挪到卡片**横向之外**（右侧优先），而不是整块压在卡片上。
  实现是纯函数 `frontend/src/interactive/overlayPlacement.ts`（单测逐条断言落点），
  组件只负责把结果换算到 offsetParent 坐标。

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
`tasks-entry`、`tasks-popover`、`impact-dialog`、`impact-continue`、`impact-cancel`、
`card-draft-hint`、`card-draft-retry`、`chat-draft-status`、`chat-draft-retry`（§9.4 / §9.5 的草稿状态与重试入口）。

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

### 9.7 第二轮前端所有权（一个文件同一时间只有一个写者；第三轮见 §10.9）

| 文件 | 写者 |
| --- | --- |
| `views/InteractiveView.vue`、`interactive/types.ts`、`services/interactive.ts`、`scripts/interactive-verify/fe-scenarios.mjs`、`docs/**` | 主智能体 |
| `components/interactive/{BoardToolbar,AddMenu,BoardSearchPanel,BoardCard,BoardGroupFrame,SelectionMenu}.vue` | A |
| `interactive/board.ts`、`interactive/approval.ts`、`backend/src/agent/interactive/{models,board}.py` 及对应用例 | B |
| `stores/session.ts`、`stores/interactive.ts`、`interactive/drafts.ts`、`components/interactive/{ChatDock,CardDraftHint}.vue` 及对应用例 | C |
| `interactive/overlayLayout.ts`、`components/interactive/{IntentBatchTray,IntentStatusPopover,ImpactConfirmDialog}.vue`、`styles/interactive-shell.css`、`docs/interactive-ui-verify.md` | D |

冻结签名见 `docs/superpowers/plans/2026-10-07-interactive-ui-refinement.md` §4。

---

### 11.10 拖动中的实时显示（2026-10-08 补充，见交付报告 §3.8）

## 13. 收尾后续轮约定（2026-10-10 第七轮，**更新此前被本轮六条反例推翻的条文**）

本轮针对六条新反例（R1–R6）统一修共同根因，不为单条反例加特例；完整矩阵、分工与证据见
`docs/interactive-closure-followup-contract.md`。

### 13.1 板面候选与已保存版本分开接收（覆盖第七轮 M1「只在 dirty=true 时保护」的写法）

- 服务端 `seq` 是「已保存事实」，用户正在编辑的是候选，两者必须**分开接收**；
- 任何 GET/PUT 回执落地前，都要与**请求发出时刻**的板面版本基线（候选版本 / 已保存版本 / 读取代次 / 板面身份）比较：
  期间出现过更新的候选、任意一次保存被确认、或已有更新的读取落地 → 这次回执不整块覆盖板面，只合并草稿；
  **已保存成功的新版本与未保存的新输入受同样保护**（保护条件不是 `dirty`，而是版本基线）；
- 一次 PUT 成功但回执期间用户又改了：吸收**版本事实**（新 `seq` / `updatedAt`）而**不替换候选正文**，
  候选保持未保存并安排下一轮保存 —— 否则候选仍带旧 `seq`，下一轮保存会被服务端预判拒绝、
  一般回读又被候选保护挡住，用户永久卡在「第二版存不下去」；
- 正常的最新读取仍然照常采用：保护是**按次请求的基线**，不是永久拒绝刷新。

### 13.2 取消影响确认必须真正撤回（补强 §1.6 与第七轮 08）

- 取消 = 该次候选不生效、相关任务继续、草稿与恢复来源保留；
- 丢弃候选必须**同时**把版本记账收回（本地候选版本回到与服务器一致的版本），
  否则回读会被「仍有未保存候选」的候选保护挡住，出现「dirty=false / 状态 idle 但板面还是被取消的正文」；
- 回读失败要如实提示原因并把候选恢复为未保存、可重试，不假装撤回成功；
- 等待确认期间出现的更晚输入或独立变化，不得被这次取消的回读清掉。

### 13.3 本机记录的处理目的必须显式保留（覆盖 §12.2 的调用口径）

- 「删除本机冗余副本」与「整份草稿清除」是两个不同决定：
  - `remove-local-copy`：用户选了服务器那份，或服务器已保存成功后的本机清理 —— 只删记录，**绝不写 `cleared` 依据**；
  - `clear-draft`：用户要清掉这份草稿 —— 才允许幂等补写 `cleared` 依据；
- 待处理决定必须同时记住**目的**与版本守卫口径（`version` / `absent` / `object`），重试按目的分派；
- 无法判断目的/来源的旧记录保留两份候选并说明，**不得**一律当成整份清除。

### 13.4 影响确认必须绑定真实范围（补强第七轮 M4）

- 只在用户**看到并确认过**的范围内生效；服务端保存前重新核对受影响任务、对应材料与运行/暂停状态；
- 出现未在说明里列出的**新增运行中**受影响任务 → HTTP 409 `stale_check`（附 `scopeChanged: true` 与当前全量 `affectedTasks`），
  **不落库、不暂停未授权任务、不推进快照**，并给出「为什么要重新说明」的准确原因；
- 前端收到后重新预判，把**完整范围**放回确认框，由用户重新确认后才生效；
- 前端任务清单滞后（前端不知道有新的运行任务）时，服务端以 `impact_confirmation_required` 拦下，
  前端必须补取带 `checkId` 的影响预判后再让用户确认；补取失败保留候选与真实原因，不无限弹窗；
- 不得用自动确认、新增「无条件保存」接口或循环重试绕过服务端校验；
- 已暂停任务、无关任务变化、普通位置变化不得造成无意义重复确认；判定依据是真实影响，不是任务总数或整个列表。

### 13.5 本轮文件所有权（一个文件同一时间只有一个写者）

| 角色 | 主要文件 |
| --- | --- |
| 主智能体 | `frontend/src/stores/interactive.ts`、`frontend/src/services/interactive.ts`、`docs/**` |
| A 草稿与恢复 | `frontend/src/interactive/drafts.ts`、`frontend/src/components/interactive/CardDraftHint.vue` |
| B 后端确认 | `backend/src/agent/interactive/**`、`backend/src/agent/api/interactive*.py` |
| C 界面与审美 | 互动组件与样式（`CardDraftHint.vue` 除外）、`frontend/src/views/InteractiveView.vue` |
| D 独立验收 | 独立命名的验收测试与脚本（不改产品代码） |

---
## 12. 内容保护与界面精修约定（2026-10-09 第五轮）

本节与前面冲突时以本节为准。核心口径：**冲突由用户选择；已清除/早已处理的内容不删新输入；失败原文始终归属正确且不会静默淘汰；界面在支持的尺寸下真实可读可用。**

### 12.1 草稿冲突的选择不许被绕过（覆盖 §11.1 冲突部分）

- 「打开编辑器」**不等于**选择本机版本；「编辑另一张卡片」也**不等于**解决本卡片冲突。
- 选择发生前，两份文字与其来源都必须保留：不自动替换、不自动清理任何一份。
- 冲突入口必须在真实操作路径中可见可点：提示不能在打开编辑器的同一动作里消失。
- 服务端保存是**整份替换**：未决冲突键不允许把本机候选写上去，也不允许从请求里删掉——
  两种方向都会覆盖或删除服务器上的那份。未决时按**服务器事实**回写该键；
  本机候选留在本机记录里等用户选择。
- 用户明确选本机版本后才按该选择保存；明确选服务器版本后编辑框跟随选择；
  清理只作用于**已经确认处理**的对应版本。
- 请求在飞期间的新输入不被旧的清理动作误删；旧格式与当前格式都按此检查
  （只有一个本地正数版本号，不足以证明比服务器新，无法判定时不许直接覆盖）。
- 验收：未选择→编辑 B→保存→刷新，两份 A 内容都可处理；未选择→仅打开 A 编辑器，冲突仍在且服务器未改；
  分别选择两种版本后，保存与恢复符合选择；请求在飞期间新输入不被误删。

### 12.2 清除依据只作用于**当时的版本**（补强 §11.2）

- 旧清除记录（`kind:"cleared"`）不许作用于它之后的新输入。
- 本地记录写失败时，当前内存文字与失败状态都保留，不能因失败让旧持久化记录重新取得决定权。
- 恢复处理顺序：先确认记录**归属**（板面/对象），再看它是否已被**更晚的编辑或清除**取代，最后才落到显示。
- 别的板面的清除依据不能作用于当前板面；在飞的旧保存/清除回执不能清理后来的本地副本
  （同一浏览器多页面时按记录版本校验）。
- 「重新读取服务器状态」不等于「关闭重开」：后者要如实说明恢复能力限制，但重读期间内存里的文字不许被主动删除。

### 12.3 未绑定发送的失败归属迁移后**稳定**（补强 §11.5）

- 每次发送的归属独立保存：绑定真实话题 A 后，本次发送的归属**稳定为 A**；
  用户切到 B、在 B 输入、解除旧草稿保护，都改变不了它。
- 发送归属与输入框草稿保护是**两种生命周期**：不能为了保存新草稿丢掉仍在等待结果的发送归属。
- 不把所有未绑定记录无条件搬到任意新话题；失败入口、原文、原因一起属于原发送对应的话题。

### 12.4 成功只处理**该次发送**的记录（补强 §11.4）

- 成功清理只能匹配**实际被该次发送接受**的记录及版本，**不许按文字相同匹配**。
- 找回失败原文后主动重发，要显式关联被重发的记录；成功后只清这一份。
- 互换过的记录（正文已变）不许凭旧关联被删除；老格式记录缺身份时**保留**，不用文字猜测批量删除。
- 草稿清理与失败记录清理都按身份与版本验证。

### 12.5 失败原文不做静默淘汰（扩展 §11.4）

- 不自动淘汰用户尚未处理的失败原文（原「每话题保留 8 条」的静默裁剪口径废止）。
- 展示数量与保留数量分开：可以默认只显示较少项目并提供「查看其余」，但**隐藏不等于删除**。
- 达到实际容量或写失败时准确告知；长列表与长原文限高滚动，不挤掉输入框与恢复操作。
- 记录只在对应发送成功或用户明确放弃后清理；刷新时不许再裁剪一次丢掉磁盘已有记录。

### 12.6 切换条只在**确有第二个面板**时出现（补强 §11.7）

- 只有确实存在另一个可展示的面板、且需要切换显示时才进入切换布局；
  审批列表不存在/已处理完时不显示虚假切换提示、不预留不存在的切换条空间；
  进出切换状态都以同一个布局状态为准。

### 12.7 透明区域的可读性（补强 §11.8）

- 外层继续透明；聊天**标题、必要说明与状态**要有稳定的局部底色/内容区，不让板面长文字透出来叠在头上。
- 保留输入区与恢复块的既有底色成果；当前输入为主，失败与待恢复为辅；
  所有新底色/边框/阴影走令牌；键盘焦点、可访问名称、中文输入法、Escape 与 reduced motion 保留。

### 12.8 本轮文件所有权

| 文件 | 写者 |
| --- | --- |
| `views/InteractiveView.vue`、`interactive/types.ts`、`services/interactive.ts`、`scripts/interactive-verify/fe-scenarios.mjs`、`docs/**` | 主智能体 |
| `stores/interactive.ts`、`interactive/drafts.ts`、`BoardCard.vue`、`CardDraftHint.vue` | A |
| `stores/session.ts`、`components/Composer.vue`、`components/interactive/FailedSendNotice.vue` | B |
| `components/interactive/IntentBatchTray.vue`、`interactive/overlayLayout.ts`、`styles/interactive-shell.css` | C |
| **`components/interactive/ChatDock.vue`** | **C（结构与样式）；B 要动数据面先提变更请求，由主智能体裁定** |

独立验收（D/D2）只写自己的用例、探针、报告与截图。

## 11. 草稿恢复补齐与失败处理约定（2026-10-08 第四轮）

本节与前面冲突时以本节为准。核心口径：**尚未完成的输入能恢复，已经清除的草稿不复活，失败原因默认可见，用户能执行恢复操作。**

### 11.1 卡片草稿的记录身份必须统一（覆盖 §10.4 / §10.5）

- 服务器同步的草稿键是 `card:<id>`；本机恢复副本的键是 `card-local:<id>`；两者都要能被识别与枚举。
- 恢复**必须能发现「只存在于本机」的记录**，不能以「服务器已经有这份草稿」为前提；
  判断存在性一律按**记录是否存在**，不用字符串是否非空。
- 恢复前要确认板面与卡片仍然存在：不给别的板面恢复内容，也不恢复已删除对象的草稿。
- 恢复结果只能回到**待编辑内容**（编辑器的草稿），不能自动变成正式卡片内容。
- 服务器返回较慢时，**不许覆盖用户在恢复期间新输入的文字**。
- 正常刷新、新建 store、正常关闭重开是**三种不同场景**，分别验证；只在同一内存对象里重读不算重新启动恢复。

### 11.2 清除草稿是一项必须同步的真实变化（覆盖 §10.4）

- 删除草稿在服务器确认之前**不能标记为已同步**；清除最后一份草稿也必须发出同步请求
  （用现有接口提交剩余草稿集合即可，不改数据库）。
- 本地要保留**尚未完成的删除依据**（待确认删除），使请求失败或用户正常刷新后仍知道这份旧草稿应被清除。
- 先前在飞的保存请求晚到时**不能重新建立已经清除的草稿**；旧版本的清除**不能删除后来新建的版本**。
- 清除同步失败要给出准确状态与重试入口，不能静默丢弃待同步的变化。
- 撤销/重做只作用于正式内容，不因为撤销而恢复一份早已清除的编辑草稿。

### 11.3 本地保护失败要可见，时间判断要按记录（覆盖 §10.5）

- 区分「服务器草稿保存结果」与「本机恢复记录保存结果」：服务器已成功时不许显示成服务器保存失败；
  本机保护失败且服务器尚未保存时，要如实说明当前恢复能力，保留内存内容并提供重试。
- 提示放在编辑处或现有保存状态附近，不重复通知；**不许把「内容仍在当前页面」写成「关闭后一定能恢复」**。
- 判断某条本地草稿是否过期，只能依据**该记录自己的归属、版本与已确认状态**；
  不许用「服务器整个草稿集合的更新时间」或别张卡片的保存时间，也不许用不同客户端的时钟直接判定。
- 出现无法确定的新旧冲突时，保留内容并给用户明确选择，不静默丢弃。

### 11.4 失败原文在对话页与互动聊天都能找回（扩展 §10.1）

- 两个入口都要有：清晰的失败原因、找回原文、后续发送操作；共用会话层状态与同一个恢复组件，不写两套冲突逻辑。
- 失败原文**始终属于原话题**：切到别的话题不插入，回到原话题能找到。
- 输入框已有新文字时不覆盖，采用既有的恢复/互换方式，保留两份并说明当前在编辑哪一份。
- 失败原文与后续新草稿都要**经得起正常刷新与关闭重开**（要持久化，不能只存内存）。
- 清理只依据明确的成功或用户操作；晚到的旧回执不许清掉新版本；不许用单个内存槽静默覆盖另一份待恢复原文。
- **恢复不是发送**：找回原文后仍由用户主动发送，不自动重试。
- 两个入口共用会话时，不重复记录、不重复订阅、不重复发送。

### 11.5 未绑定话题的归属迁移必须完整（扩展 §10.1 / §10.2）

- 发送归属要带**能随迁移追踪的稳定身份**；成功时清理的是被发送的那一个版本，
  不依赖已经失效的旧存储位置，也不仅比较文字是否相同。
- 覆盖：未绑定→绑定→发送成功；迁移后用户已输入新文字；迁移时发送失败；多个回执先后到达。
- 成功发送过的旧文字不应重新出现；后续新文字不应被误删；任何场景都不自动再次发送。

### 11.6 提交失败原因默认可见（扩展 §10.8）

- 默认失败区域直接显示**本次请求**的简短真实原因与可用重试；原因取当前事实，不误读上一次提交结果。
- 内容保留情况单独准确说明；板面保存也失败时，不许声称全部修改已经保存。
- 详情区保留较长诊断，但**不是了解基本失败原因的必经入口**。
- 文案用用户能懂的话，不出现「板面提交接口」这类实现说明；失败区可换行，不撑坏提交按钮、不遮挡板面。

### 11.7 窄窗口切换条是完成的界面元素（覆盖 §10.6）

- 需要切换显示的模式下，始终用**同一个明确布局状态**计算可用空间；关闭一个面板后仍为切换条预留真实高度。
- 切换条要有真实定位、间距、按钮状态、键盘焦点与主题样式，不能只给无效的 left/bottom。
- 面板、切换条与工具栏都保持在视口内，输入区与主要操作可见可点。
- 窗口缩放、工具栏高度变化、列表内容变化时重新计算，不引入观察器循环或持续抖动。
- 保留上次打开的面板、批量选择、滚动位置与聊天草稿；回到宽窗口不自动打开用户已收起的另一个面板。
- 桌面最小窗口仍是 800×600；480px 只是浏览器边缘检查，不改变桌面最小尺寸规则。

### 11.8 审美与结构（补充 §10.8）

- 聊天外层保持透明；输入框、用户消息与必要提示可用更稳定的局部底色；不许给整个面板加厚重不透明背景或强模糊。
- 失败恢复入口分层：当前输入为主，失败原因与待恢复原文为辅；原文较长时限高滚动，不挤掉输入框与发送按钮；
  按钮名称直接说明结果，不用只有图标无含义的按钮。
- 切换条用现有令牌统一边框、底色、圆角、字号、选中态、hover 与 focus；尺寸紧凑但可点击。
- 提交区默认信息完整且简洁：原因、保留状态、重试顺序稳定；长文换行；详细诊断可收起。
- 令牌化：复用既有颜色/阴影/间距/字体令牌，新增令牌要说明用途；明暗两主题都要成立。
- 修掉 `ChatDock.vue` 里 `<p>` 包含 `<details>` 的无效结构；保持可访问名称、键盘操作、Escape 与 reduced motion。
- 不扩大界面范围：不加常驻面板、标签、装饰、操作模式或开发信息。

### 11.10 拖动中的实时显示（2026-10-08 补充）

- 拖动卡片时，**关系线（连线、方向箭头、关系文字）与组框必须实时跟随**被拖动卡片的位置；
  不许出现「拖动中线不动、松手才跳过去」。
- 拖动过程只改变**显示**：板面状态在松手前不变（只预演落点），松手才提交并保存。
- 这条同时适用于桌面最小窗口 800×600 与窄窗口；缩放/平移后仍然准确（几何都在同一坐标系里算）。
- 回归检查：`scripts/interactive-verify/fe-scenarios.mjs` 场景 24（真实浏览器，指针仍按下时量线的端点与卡片中心）。

### 11.9 本轮文件所有权

| 文件 | 写者 |
| --- | --- |
| `views/InteractiveView.vue`、`interactive/types.ts`、`services/interactive.ts`、`scripts/interactive-verify/fe-scenarios.mjs`、`docs/**` | 主智能体 |
| `stores/interactive.ts`、`interactive/drafts.ts`、`components/interactive/BoardCard.vue`、`CardDraftHint.vue` | A |
| `stores/session.ts`、`components/Composer.vue`、`components/interactive/ChatDock.vue`、`components/interactive/FailedSendNotice.vue` | B |
| `components/interactive/IntentBatchTray.vue`、`interactive/overlayLayout.ts`、`SubmitCluster.vue`、`interactive/submission.ts`、`styles/interactive-shell.css` | C |
| 独立用例、独立脚本、独立报告与证据目录 | D |

冻结签名见 `docs/superpowers/plans/2026-10-08-interactive-draft-recovery-completion.md` §3。

## 10. 草稿竞态与界面收敛约定（2026-10-08 第三轮）

用户已确认的规则优先于本契约此前与测试的写法；本节与前面冲突时以本节为准。

### 10.1 发送的归属必须在点击那一刻确定（覆盖 §6.4 / §9.4 的恢复写法）

- 点击发送时**立即**记录：原话题、原始文字、当时的草稿版本；请求本身也必须用这个归属，不许取稍后切换到的话题。
- 失败原文**只能恢复到原话题**。当前在别的话题时：不修改它的输入、草稿或发送状态，也不把原话题的错误显示成它的发送失败。
- 回到原话题后可以继续编辑或重试失败文字，**不自动重新发送**。
- 原话题已有更新文字时**不许用旧失败原文覆盖**：两份文字都保留，用一个简洁明确的恢复入口让用户取回失败的那一份，不自动拼接、改写或发出。
- 恢复与错误事实由**会话层统一管理**，普通对话页与悬浮聊天不各写一套判断。
- 发送前文字还没到自动保存时间也要受保护：不能先清空输入，再因为离开、切话题或失败丢掉原稿。

### 10.2 成功的回执只清理那一次发送对应的旧版本（覆盖 §9.4 的删除写法）

- 清理依据必须包含**原话题 + 发送时的草稿版本**，不能只凭话题键删除。
- 受理成功只清理那一次发送对应的旧版本；同话题后续输入、已经持久保存的新草稿一律保留。
- 用户当前在哪个话题不影响这条规则；必须覆盖 A→B→A→B 的切换。
- 输入为空**不能**独自证明「没有新草稿」：别的话题输入框为空、用户主动清空后的新版本，都要正确区分。
- 成功回执、保存防抖、恢复操作、话题切换迟到时，都不许让旧草稿复活或让新草稿被删。
- 重复回执不重复清理；失败请求不得被当成已受理。不新增第二套轮次或事件订阅。

### 10.3 卡片草稿的串行保存不许把新版本遗忘（覆盖 §9.5 的失败处理）

- 保留串行保存（避免旧请求后落库覆盖新内容），但**等旧请求的后续保存不能直接返回后被遗忘**。
- 无论旧请求成功还是失败，请求结束时都要**重新检查未保存的新版本**：继续尝试就真的发出请求；停止尝试就明确显示未保存原因与可用重试。
- 不许出现「没有请求、没有计时、没有后续工作，但一直显示保存中」。
- 不把旧失败假装成新版本已保存，也不因为版本较新就静默忽略整次失败。
- 保留所有较新输入；多卡片、删除或清理草稿、离开编辑器与手动重试同时发生时，不串内容、不遗漏后续版本。
- **不做无界自动重试**：重试次数与失败后的状态明确。保存与重试都不创建正式卡片、不提交板面、不调用 QIO。

### 10.4 空草稿是有效编辑状态（覆盖 §9.5 的草稿读取）

- 必须区分三种情况：**没有草稿** / **存在且正文为空的草稿** / **确认编辑后已清除草稿**；不能用同一个空字符串同时表达三种。
- 存在空草稿时，重开编辑器、刷新或恢复后输入框保持为空；原正式内容仍要用户明确确认后才改变。
- 确认或清理草稿不留下会遮盖新正式内容的空记录；删除卡片不留下会误恢复到其他对象的草稿。
- 保持历史草稿格式兼容，不改数据库结构。

### 10.5 正常刷新/关闭的保存边界（覆盖 §9.5 的「离页落盘」）

- 现有 `pagehide` 里发普通异步请求**不能**当作关闭恢复的证据（页面销毁后内存无法用于下一次恢复）。
- 为未完成输入提供本地恢复保护，并与服务端最后成功保存的版本正确协调；本地副本**只用于编辑恢复**，不提交、不发送、不扩大 QIO 可见范围。
- 成功保存 / 确认 / 删除后按**对象与版本**清理本地副本；恢复不许让旧副本覆盖更新的版本。
- 存储失败要明确提示并保留内容与重试入口。
- 正常离开、强制结束、崩溃要分开说明验证范围，**不承诺所有退出方式都已验证**。

### 10.6 几何模式改变必须落实面板切换（覆盖 §9.6 的浮层共存）

- 进入空间不足状态时，按**用户最近打开的面板**显示一个，并明确告知可切换；消息、草稿、审批选择与阅读状态保持。
- 首次挂载、恢复开合状态、窗口缩放、工具栏高度变化，都按**最新几何**执行，不能用旧的 `cramped` 值决定。
- 从小窗口恢复大窗口时保留合理开合状态，不未经用户决定自动弹出面板；取消与切换不丢内容。
- 不引入尺寸观察循环、开合状态来回反弹或持续重排。
- 800×600 仍是桌面最小窗口；480px 属于浏览器窄窗口边界，报告里必须区分。

### 10.7 手势固定，不再有常驻模式（覆盖 §9.6 的手势描述）

- 不按空格拖动空白处 = 移动查看位置；按空格拖动空白处 = 框选；从连接点拖到另一张卡片 = 建立关系。
- **移除会改变这些规则的常驻模式入口**，不新增隐藏的持续模式；保留单选、加选、关系建立与编辑能力，必要提示进帮助。
- 空格在输入框、菜单与聊天内正常使用；中文输入法不误发送；Escape 取消；滚轮与浮层内部滚动不穿透。
- 纯单击重叠卡片不成组；真实拖动后松手才成组；默认名称仍是「默认组名」。

### 10.8 界面收敛（补充 §9.6，不靠裁切长说明）

- 聊天常驻一句「文字发送不会提交板面」；会话复用、允许范围、未接入情况放入**可打开的详情**；真实错误仍及时显示；
  删除界面上的开发用语（如「提交接口」）；**不许用 `line-clamp` 裁掉完整说明代替重新组织文字**。
- 聊天外层保持透明；消息、提示、输入区可用适量轻底色；后方板面有图片、代码与高亮文字时，聊天文字不得与后方文字混在一起
  （尤其检查用户消息与过程说明）；样式限定在互动聊天，普通对话页不被意外改变。
- 提交区常态只有：简短范围「本次可见：材料 X · 注释 Y」、简短提交状态、提交按钮；完整排除清单/改动详情/保存与提交说明按需打开；
  重写 `SubmitCluster.vue` 内部结构，减少靠父组件 `:deep()` 隐藏或压缩文字维持高度。
- 工具栏常态 ≤2 行、800×600 ≤约 96px；正文与主要操作不得缩成元信息字号；选中对象操作继续在对象附近出现，连接点不被遮挡。
- 强调色有主次：提交与发送位置清楚；中性入口、选中状态与说明不要都用同等强度的粉色。
- 令牌、字体与焦点统一：标题衬线、正文与操作无衬线、数据等宽；颜色/阴影/备用值走令牌
  （含 `IntentPreviewCard.vue` 备用阴影里的硬编码 `rgba(...)`）；双主题检查阅读对比；焦点可见；减少动态效果有效。

### 10.9 本轮文件所有权

| 文件 | 写者 |
| --- | --- |
| `views/InteractiveView.vue`、`interactive/types.ts`、`services/interactive.ts`、`scripts/interactive-verify/fe-scenarios.mjs`、`docs/**` | 主智能体 |
| `stores/session.ts`、`components/interactive/ChatDock.vue`、`components/Composer.vue`、`interactive/drafts.ts`、`interactive/chat.ts` | A |
| `stores/interactive.ts`、`components/interactive/BoardCard.vue`、`components/interactive/CardDraftHint.vue` | B |
| `components/interactive/{IntentBatchTray,BoardToolbar,BoardCanvas,SubmitCluster,IntentPreviewCard}.vue`、`interactive/overlayLayout.ts`、`styles/interactive-shell.css` | C |
| 独立验收脚本、独立用例、`docs/interactive-draft-races-verify.md`、证据目录 | D |

冻结签名见 `docs/superpowers/plans/2026-10-08-interactive-draft-races-ui-polish.md` §4。

### 8.5 批次判定（前端，不改数据库）—— **本条已被 §9.2 取代，下面保留的是写错的历史版本**

> **已作废**：这一版把「`createdAt` 截断到秒相同」当作第三级归批依据，并声称「宁可不出批量列表，
> 也不把不同批次相加」。**该声称是错的**：独立复现证明，两个不同批次各两项、在本地记录丢失后
> 会因创建时间落在同一秒而被误并成一个四项批次。正确规则见 §9.2（只用可证明的来源，禁止时间推断）。
> 保留这段文字只为说明「为什么改」，实现与测试不得再按它写。

~~优先级：① 本次会话里由同一次创建动作产生的意图记在 `localStorage["qio.interactive.intentBatches"]`；
② 服务端 `submissionId` 相同；③ `createdAt` 截断到秒相同。三条都拿不到时，该意图自成一批。~~


---

## 14. 真实模型理解与真实执行（2026-10-11 第九轮，**本轮冻结接口**）

本轮把互动模式从「可控演示」接到**真实模型理解 + 真实执行**。此前 §1.6 的状态机、可见范围、
审批边界与撤回保护**全部继续有效**；本轮只补上「理解从哪来」与「批准后真的做什么」。

### 14.0 本轮边界

- 聊天发送与板面提交仍然分离：聊天只发用户输入，不自动读取板面、未提交改动、
  未获准注释或关注范围（§8.3 不变）。
- 保存只落库、不调用模型（§1.5 不变）。**只有提交**才建立理解请求。
- 添加材料不等于要求总结 / 比较 / 修改 / 执行；空提交、重复提交、纯显示变化
  **不产生理解请求、不调用模型**（沿用 §1.5 的 empty / duplicate 判定）。
- 模型不能直接写数据库、不能绕过审批。QIO 在互动模式中能执行的板面操作是
  **用户在互动模式中能做的子集**（见 14.3 的允许操作集合）。
- 演示入口（`demo=true` 与 `/demo/advance`）保留，但**演示推进不得驱动真实任务**，
  正式路径不显示演示内容。

### 14.1 状态与判定

**受理与理解分开**（取代「`succeeded` 同时代表本地记录成功与模型完成」）：

    accepted ──▶ understanding ──▶ understood              （产出意图，等待审批）
                              ├─▶ needs_clarification     （需要用户补充说明）
                              ├─▶ no_action               （当前无需开展任务）
                              └─▶ failed                  （可重试，从保存的快照恢复）

- `accepted`：提交已可靠受理，理解请求已落库（模型还没开始）。受理成功后更新提交基准、
  清除本次勾选；受理前失败保留改动与勾选。
- `understanding`：正在读取材料 / 调用模型。
- 三种理解结果**必须区分**：`intent`（提出可执行意图）、`clarification`（需要用户补充）、
  `no_action`（当前无需开展任务）。**不强制每次提交都生成任务**。
- `failed` 重试只从该请求保存的 `snapshot` 恢复：不重读当前板面、不重建同一批任务。

**材料来源（诚实口径，`MATERIAL_SOURCES`）**：`inline | attachment_text | url_fetched |
url_address_only | image_unsupported | unreadable | unsupported`。
拿到文件名 / 地址**不等于**读到正文；未读取、截断与不支持都要如实记录，禁止声称已完整阅读。

**执行状态（一次执行尝试）**：`queued | running | paused | done | failed | cancelled`。
意图行的 `status` 仍是任务状态（§1.6）；排队、等待依赖、实际执行、暂停与结束分别显示真实状态，
**不允许「已批准就一直运行中」**。

### 14.2 数据库（迁移 37 / 补偿 38，只追加）

- `board_understanding`：`id, board_id, submission_id, request_id, status, stage, progress,
  snapshot, context, materials, output, validation, memory, error, attempt, adapter,
  created_at, updated_at, started_at, finished_at`。
  `request_id` 唯一 —— 同一请求身份重复受理不会新建第二条。
- `board_executions`：`id, intent_id, board_id, attempt, status, plan, steps, checkpoints,
  result, error, owner_instance_id, created_at, updated_at, started_at, finished_at`。
- `board_intents` 追加列：`goal, expected, operations, basis, intent_key, understanding_id`。
- `COMPENSATION_VERSION` 由 36 抬到 **38**（= 迁移 36 的全部语句 + 本轮对象）；
  `REQUIRED_OBJECTS` 同步补进新表 / 新列 / 新索引。历史迁移一行不动。

### 14.3 跨模块函数（冻结签名）

`agent/interactive/materials.py`（A，新）：

    def resolve_materials(conn, *, state: dict, cards: Iterable[dict], attachments=None,
                          *, max_chars: int = 40000) -> list[dict]
        """把本次可见范围里的材料解析成可交给模型的条目（见 14.1 的 MATERIAL_SOURCES）。
        只读已保存的附件与已保存的板面状态，不抓取未获准内容。"""

`agent/interactive/understanding.py`（B，新）：

    def build_context(conn, *, board_id: str, submission_id: str | None = None,
                      understanding_id: str | None = None) -> dict
        """从**冻结的提交快照**构建互动上下文；不读当前板面。"""
    async def run_understanding(conn, understanding_id: str, *, adapter_provider) -> dict
        """执行一次真实理解：材料 → 模型 → 结构化校验 → 落库。
        adapter_provider: async () -> BaseAdapter | None（复用 QIO 既有凭据 / 用量 / 预算链路）"""
    def request_understanding(conn, *, board_id: str, submission_id: str,
                              snapshot: dict | None = None) -> dict
        """受理：建立一条 accepted 的理解请求（幂等，按 request_id）。"""

`agent/interactive/execution.py`（C，新）：

    async def run_execution(conn, execution_id: str, *, adapter_provider=None,
                            instance_id: str | None = None) -> dict
        """真实执行一条已批准的任务；逐步落 checkpoint，支持暂停 / 取消 / 恢复。"""

`agent/interactive/intents.py`（C，扩展）：

    def create_intents_from_understanding(conn, *, board_id: str, understanding_id: str,
                                          payloads: list[dict]) -> list[dict]
        """把已校验的模型输出落成意图：沿用 intent_key 身份，不复制新任务。"""

**B → C**：理解校验通过后由 B 调用 `intents.create_intents_from_understanding`
（延迟 import；失败不得让理解结果丢失，只记进理解记录）。
**C → B**：批准后的真实执行不重新调用模型理解；执行需要的文本来自已审批的操作与
结果生成（同一条模型链路，但输出受 `ALLOWED_INTENT_OPS` 约束）。

### 14.4 HTTP 接口（冻结）

A（`agent/api/interactive_store.py`）新增：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/interactive/boards/{board_id}/understanding` | 本板面理解过程列表；`?submissionId=` 过滤 |
| POST | `/api/interactive/understanding/{understanding_id}/retry` | 从保存的快照重试失败的理解 |
| POST | `/api/interactive/boards/{board_id}/understanding/{understanding_id}/cancel` | 取消尚未落地的理解 |

提交返回新增：`delivery.delivered`（受理成功为 true）、`delivery.understandingId`、
`delivery.status`（= `accepted`）。

C（`agent/api/interactive_intents.py`）新增 / 变更：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/interactive/boards/{board_id}/intents` | **真实入口**：`{submissionId}` → 取该次理解产出的意图；`{demo:true}` 仍走演示 |
| GET | `/api/interactive/boards/{board_id}/executions` | 本板面执行记录 |
| POST | `/api/interactive/intents/{intent_id}/pause` | 暂停真实执行 |
| POST | `/api/interactive/intents/{intent_id}/resume` | 继续（校验材料与已完成步骤，不重复已完成操作） |
| POST | `/api/interactive/intents/{intent_id}/cancel` | 取消并撤回该任务仍可安全撤回的改动 |
| POST | `/api/interactive/intents/{intent_id}/approve` | **批准后必须实际启动真实执行**，返回 `execution` |

### 14.5 允许的板面操作集合（冻结）

    add_reply_card   {content, title?, x?, y?, w?, h?}
    add_text_card    {content, x?, y?, w?, h?}
    move_card        {cardId, x, y}
    group_cards      {cardIds[], ordered?, name?}
    order_group      {groupId, memberOrder[]}
    add_link         {src, dst, direction?, meaning?}

不允许：删除、隐藏、勾选用户内容、改用户已有卡片的正文、任意改数据库。
程序校验模型输出：结构、对象引用、材料范围、操作类型、依赖、冲突与修改范围；
**无效输出不得落成可执行任务**，给出可理解原因，自动修复至多一次。

### 14.6 前端接口（冻结）

`frontend/src/interactive/types.ts` 新增：`UnderstandingStatus / UnderstandingKind /
MaterialSource / UnderstandingMaterial / UnderstandingStep / UnderstandingClarification /
Understanding / UnderstandingResponse / IntentOperation / IntentBasis / ExecutionStatus /
ExecutionStep / Execution / ExecutionControlResult`。
`Intent` 追加 `goal / expected / operations / basis / intentKey / understandingId / execution`。

`frontend/src/services/interactive.ts` 新增：`createIntents(boardId, submissionId?)`、
`fetchUnderstanding(boardId, submissionId?)`、`retryUnderstanding(id)`、
`fetchExecutions(boardId)`、`pauseIntent(id)`、`resumeIntent(id)`、`cancelIntent(id)`。

前端复用统一过程规则：无说明时立即显示统一状态、说明后补；阶段切换收起上一阶段、
同阶段更新当前说明、历史可展开；工具记录默认一行、审批入口自动展开；正式文字边生成边显示；
成功 / 失败 / 取消都结束读条。

### 14.7 本轮文件所有权（一个文件同一时间只有一个写者）

| 角色 | 写作用域 |
| --- | --- |
| Lead | `docs/interactive-mode-contract.md`、`docs/status.md`、`docs/interactive-real-model-report.md`、`backend/src/agent/interactive/models.py`、`backend/src/agent/storage/schema.py`、`backend/src/agent/storage/migrate.py`、`backend/src/agent/api/interactive.py`、`frontend/src/interactive/types.ts`、`frontend/src/services/interactive.ts`、`backend/tests/test_interactive_real_model.py`、`backend/tests/test_interactive_migration_integration.py` |
| A | `backend/src/agent/interactive/submission.py`、`backend/src/agent/interactive/materials.py`（新）、`backend/src/agent/api/interactive_store.py`、`backend/tests/test_interactive_submission.py`、`backend/tests/test_interactive_materials.py`（新）、`backend/tests/test_interactive_store_understanding.py`（新）、`frontend/src/interactive/materials.ts`（新）、`frontend/src/components/interactive/AddMenu.vue`、`frontend/src/components/interactive/BoardCard.vue` |
| B | `backend/src/agent/interactive/understanding.py`（新）、`backend/src/agent/interactive/prompts.py`（新）、`backend/tests/test_interactive_understanding.py`（新） |
| C | `backend/src/agent/interactive/execution.py`（新）、`backend/src/agent/interactive/intents.py`、`backend/src/agent/api/interactive_intents.py`、`backend/tests/test_interactive_intents.py`、`backend/tests/test_interactive_execution.py`（新） |
| D | `frontend/src/stores/interactive.ts`、`frontend/src/interactive/approval.ts`、`frontend/src/components/interactive/{ChatDock,IntentPreviewCard,IntentBatchTray,IntentStatusPopover,SubmitCluster}.vue`、`frontend/src/views/InteractiveView.vue`、`frontend/src/interactive/__tests__/*` |
| E | `docs/interactive-real-model-review.md`、`scripts/interactive-real-model-verify/**`（只写自己的复核产物） |

### 14.8 本轮不做

通用聊天循环的全部工具对互动规划开放；模型直接写库；绕过审批；多板面协同编辑；
图片视觉理解（当前模型链路没有视觉能力，如实显示不支持）；真实模型自动化测试
（自动化测试一律用 fake/mock provider）。

### 14.9 落地说明与对 §14 的补充（2026-10-11，按实际实现）

本节只记录**实现后的真实口径**，不改变 §14 的冻结签名。

1. **`request_understanding` 的 `snapshot` 形状**：只传该次提交结果里的 `after` 投影
   （`submission.project_snapshot` 的输出）。这份投影是「这次提交究竟交出了什么」的权威事实，
   后续板面继续变化（用户改字、取消许可、移动卡片）都不应该改写它，所以必须传内存里那一份
   —— 与许可是否持续有效**无关**（许可自 2026-10-12 起持续有效，见 §2.2）。
   理解侧需要「有效变化 / 用户备注」时按 `submission_id` 从 `board_submissions` 补读，
   **仍然不读当前板面**，并在上下文里如实标注来源（`requirement.expressionSource`、`honesty.expressionsSource`）。
2. **`empty` / `duplicate` 判定顺序**（补强 §1.5）：先判「与上次成功提交内容一致」→ `duplicate`，
   再判「去掉显示类表达后没有内容」→ `empty`。纯位置变化后内容与上次成功提交一致，必须判 `duplicate`。
   两种状态都不更新基准、都不调用模型。
3. **模型适配器注入点**：`app.state.interactive_adapter_provider` 优先（`async () -> BaseAdapter | None`），
   回落 `app.state.ctx.build_adapter`。提交路径的理解、重试、批准后的执行共用同一个注入点；
   拿不到适配器时给出可读失败原因，不抛异常、不假装成功。
4. **意图 → 同一件工作的更新**：`create_intents_from_understanding` 按 `intent_key` 沿用身份；
   同一理解请求 + 同一 `intent_key` 重复调用幂等（不新建、不刷新依据）；
   旧意图仍在等待审批时原地更新（保留 id），已执行中 / 已结束时用 `key#2` 建新身份，不覆盖在跑的任务。
5. **结果卡正文来源**：正文是模型在**理解阶段**产出、并在预览中被用户批准的那段内容；
   执行阶段只在方案里没有正文时才调用模型生成。执行阶段绝不把模型输出解析成新的操作。
6. **取消尚未落地的理解**：`board_understanding.status` 的冻结取值里没有 `cancelled`，
   用户取消映射为 `failed` + 明确原因（可从保存的快照重试），并返回 `cancelled: true`；**不假装成功**。
7. **长期记忆**：复用既有的知识 / 长期记忆机制，落实为「待审批」，只记录适合后续合作的目标、结论、
   依据与成果，并区分用户表达、模型推测、待审批建议与已完成事实；
   不把整块板面或未获准内容写入长期记忆，也不把互动提交写进用户会话 transcript。
8. **演示路径**：`demo=true` 与 `/demo/advance` 保留；存在活跃真实执行时演示推进被拒绝
   （`reason=real_execution`），演示意图批准不建立真实执行。


---

## 15. 互动空间结构与自由尺寸调整（2026-10-13 第十一轮，**本轮边界**）

### 15.0 本轮边界

- 一句话：**卡片与互动对话窗口都能自由调整尺寸**；尺寸只改变布局与查看，不产生表达 / 意图 / 审批 /
  模型调用，不暂停任务，不改变许可（`checked` / `hidden` / `deleted`），不清草稿，不触发重叠成组。
- 本轮**不做**：打包桌面窗口的真机复验、跨设备尺寸偏好同步、把尺寸偏好写进服务端用户数据。

### 15.1 三层彼此独立的状态（冻结）

| 状态 | 载体 | 语义 |
| --- | --- | --- |
| 卡片 / 窗口**容器尺寸** | 卡片：板面状态 `card.w` / `card.h`；对话窗口：本机偏好 | 只改布局与查看 |
| **阅读展开**（临时让位） | `planExpandedLayout` 的显示槽位，**只用于渲染** | 临时显示布局，绝不写回板面 |
| **画布缩放 / 平移** | `viewport.ts` 的 `Viewport{scale,x,y}` | 查看适配 |

- 调整一张卡片的尺寸**不得**改变其它卡片的文字大小，也不得改变聊天文字大小；
  不得用 `transform: scale()` 冒充容器尺寸调整。

### 15.2 尺寸的持久化与提交口径（冻结）

- **卡片常态尺寸 = 板面状态的 `card.w/h`**，走既有保存路径
  （`store.commit(updateCard(board, cardId, {...}), "调整卡片尺寸")`），一次提交、一条撤销记录。
- 提交字段：**常态卡片提交 `{x, y, w, h}`**；
  **展开中、或被让位（显示槽位 ≠ 正式位置）时只提交 `{w, h}`** —— 临时槽位坐标绝不写回板面。
- 后端 `submission.py` 的 `_card_meaning` 不含位置 / 大小 / 折叠 / 书签，
  纯几何变化归入 `DISPLAY_ONLY_EXPRESSIONS`（`layout_only`），**不产生意图、不申请审批**。
- **对话窗口**的位置与尺寸是本机查看偏好（`interactive/chatFrame.ts`），
  按**板面身份**绑定 localStorage 键（`qio-chat-frame:<boardId>`），
  恢复时按当前可用空间收敛，不把窗口恢复到屏幕外；存储不可用时静默降级为「仅本次会话生效」。
- 拖动过程中**不得**提交（只回调 `onPreview`）；松手才提交一次。
- 尺寸未变（`boxEquals`，容差 `0.01`）不提交。

### 15.3 尺寸原语（冻结签名）

`frontend/src/interactive/resize.ts`（纯函数，无 DOM）：

```ts
export type ResizeHandle = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";
export interface ResizeBox { x: number; y: number; w: number; h: number }
export interface SizeLimits { minW: number; minH: number; maxW: number; maxH: number }
export const RESIZE_HANDLES: readonly ResizeHandle[];
export const RESIZE_STEP: number;
export function usableArea(area): boolean;
export function sizeLimits(minW, minH, maxW, maxH): SizeLimits;
export function limitsForArea(area, min, pad?): SizeLimits;   // 面积未知 → 无上界，绝不塌成 1×1
export function clampBox(box, limits): ResizeBox;
export function resizeBox(start, handle, dx, dy, limits): ResizeBox;
export function stepBox(start, axis, direction, limits, step?): ResizeBox;
export function convergeBox(box, area, origin?): ResizeBox;
export function boxEquals(a, b, epsilon?): boolean;
export function boxesOverlap(a, b): boolean;
export function findFreeSlot(input): ResizeBox;
```

`frontend/src/composables/useResizeGesture.ts`：`box` / `limits` / `scale` / `onPreview` / `onCommit` /
`onCancel`；阈值按**未换算的屏幕位移**判定；松手按当下的限制复算一次并先推给显示层再提交；
指针取消 / Esc / 失焦 / 组件卸载 = 恢复拖动前状态并清理监听。

### 15.4 跨组件通道（冻结）

- `interactive/chatFrameState.ts`：`liveChatFrame` / `liveChatCollapsed` /
  `publishChatFrame(frame, collapsed?)` / `readChatFrame()` / `chatObstacleFrame()`（返回**副本**；收起时为 null）。
  ChatDock 发布，审批浮层与展开布局消费。
- `interactive/overlayAvoid.ts` 的 `widestFreeBand(full, obstacles, gap, vertical?)`：
  在一条水平带里找出避开障碍后**最宽**的一段；返回 `null` 表示真的没位置，
  调用方应进入「同一时间只显示一个面板」的切换布局，而不是硬叠上去。
- `overlayLayout.planOverlayGeometry` 的 `chatFrame?` 为**可选**入参：不传时逐字段保持既有行为。

### 15.5 本轮文件所有权（一个文件同一时间只有一个写者）

| 角色 | 独占文件 |
| --- | --- |
| Lead | `interactive/resize.ts`、`interactive/chatFrame.ts`、`interactive/chatFrameState.ts`、`interactive/overlayAvoid.ts`、`composables/useResizeGesture.ts`、`components/interactive/ResizeHandles.vue`、`docs/*` |
| A | `components/interactive/ChatDock.vue` |
| B | `components/interactive/BoardCard.vue`、`CardAttachmentPreview.vue` |
| C | `interactive/cardLayout.ts`、`overlayLayout.ts`、`viewport.ts`、`components/interactive/BoardCanvas.vue`、`BoardGroupFrame.vue`、`BoardLinkLayer.vue`、`BoardPreviewLayer.vue`、`BoardResizeLayer.vue`、`AddMenu.vue` |
| D | `components/interactive/SubmitCluster.vue`、`BoardToolbar.vue`、`BoardChangeList.vue`、`IntentStatusPopover.vue`、`IntentBatchTray.vue`、`IntentPreviewCard.vue`、`views/InteractiveView.vue`、`stores/interactive.ts`（状态段） |

### 15.6 已知限制（如实记）

- 临时显示布局（展开 / 被让位）里的西 / 北把手按左上角锚定；折叠卡片与部分 `height: auto` 状态不摆把手。
- `findFreeSlot` 在可用面积为 0 时无法给出合理落点，**调用方必须先量到面积**再调用。
- 浮层扣减用「最大剩余矩形」：浮层位于视口正中时会选面积较大的一侧。
- 对话窗口的位置/尺寸不做跨设备同步；打包桌面窗口（自绘标题栏真实高度）未复验。

---

## 16. 统一滚动与对话宽度约定（2026-10-14 第十二轮）

**覆盖范围**：§8.2 的板面手势条文不受影响（板面仍可用滚轮/平移/缩放），
但本节**取代** §8.2、§9.6、§10.8、§12.7 中一切与「滚动条」「限高滚动」「代码块横向滚动」有关的写法。
完整规则见 `docs/superpowers/specs/2026-08-09-qio-frontend-design.md` §15。

### 16.1 板面（保持不变的部分）

- 板面滚动区策略为 `hidden`：**原生滚动条与 QIO 指示条都不出现**，但
  `scrollLeft` / `scrollTop`、平移、滚轮缩放、定位与坐标换算**全部保留**；
  空白拖动查看、空格框选等已确认手势不变。
- 隐藏滚动条不得改变 `canScroll` / `insideScrollable` 的判定（它们读 computed overflow，
  与滚动条外观无关）。

### 16.2 互动对话（收紧的部分）

- 面板内**任何层级**都不得有横向可滚动范围：消息、代码块、表格、输入区、附件区、过程、
  工具输出、错误与恢复详情、对话内预览都不是例外。
- 聊天区是面板内**唯一**的主要纵向滚动视口。通知、过程、附件摘要等属于同一阅读流的区域
  改为自然展开 + 折叠入口，不再各自成为固定高度的小滚动窗口。
- 附件、失败恢复、提示默认「数量 / 简述 + 展开入口」；**不得**因此隐藏审批入口或重要错误。
- 面板可以被单独缩放：表格与内容的适配必须对该窗口宽度作出响应（不是只看整机窗口宽度）。

### 16.3 卡片与预览

- 卡片常态显示适量正文 + 展开入口；展开阅读时正文或预览占主要空间，**只保留一个主要纵向视口**
  （不再有 `card-body` → 预览正文 → 内层预览三层纵向滚动）。
- 编辑态是连续编辑区；不因固定最小高度撑出较小卡片。
- 渲染上下文显式区分：**对话内**的预览执行 16.2 的无横向规则；**独立材料阅读**区
  （图片 / PDF 放大、电子表格、代码）可按材料类型保留必要的横向浏览，并统一使用 QIO 指示条。
- **图片卡以图为主（2026-10-14 追加）**：`kind="image"` 的卡片，以及**附件是可显示图片**的
  `kind="file"` 卡片，在**常态与折叠态**把图片铺满整张卡作为背景（`object-fit: cover`），
  卡片上只保留标题 + 一行补充说明 + 右下角「查看」；附件信息行与缩略图让位给封面
  （阅读态仍完整显示）。**编辑态与阅读态不铺封面**：那两个状态的主体分别是表单与真实预览。
  压在图片上的文字必须有蒙版保证对比度；背景图是装饰（`aria-hidden` + 空 alt），
  卡片可访问名称与阅读态预览的替代文本不变。实现是
  `frontend/src/components/interactive/CardAttachmentPreview.vue` 的 `variant="cover"`
  —— 与默认变体共用同一条鉴权 blob 通道与生命周期，不新增取字节路径。
- 摘要 / 折叠只改变呈现，**不得**改变提交内容、许可与已保存尺寸。
- 合并单元格等结构保留真实跨度与对应关系，不展开成误导记录。

### 16.4 本轮文件所有权（一个文件同一时间只有一个写者）

| 写者 | 文件 |
| --- | --- |
| Lead | `frontend/src/styles/scroll.css`、`frontend/src/styles/tokens.css`、`frontend/src/styles/base.css`、`frontend/src/main.ts`、`frontend/src/utils/scrollIndicator.ts`、`frontend/src/utils/qioTable.ts`、`frontend/src/composables/useQioScrollbars.ts`、`frontend/src/composables/useQioTables.ts`、`frontend/src/components/ui/QioScrollArea.vue`、`frontend/src/components/MarkdownContent.vue`、`docs/*` |
| msg-chat | `MessageStream.vue`、`MessageItem.vue`、`Composer.vue`、`TurnProcess.vue`、`ContinueBar.vue`、`QueueChip.vue`、`AttachmentChip.vue`、`InterruptedTurnEntry.vue`、`views/ConversationView.vue` |
| msg-dock | `ChatDock.vue`、`interactive-shell.css` |
| msg-board | `BoardCanvas.vue`、`BoardCard.vue`、`CardInlinePreview.vue`、`CardAttachmentPreview.vue`、`TextCardEditor.vue`、`BoardGroupFrame.vue`、`BoardChangeList.vue`、`BoardSearchPanel.vue`、`BoardPreviewLayer.vue`、`SelectionMenu.vue`、`ResizeHandles.vue`、`BoardToolbar.vue` |
| msg-pages | 设置 / 星球 / 调试 / 审批 / 凭据 / 引导 / 意图与失败恢复等其余页面与浮层 |

（路径均为 `frontend/src/` 下相对路径；本节只约束本轮，后续轮次另立。）

### 16.5 本轮不做

- 不合并 main、不动另一修复分支、不打包发布（本轮停在「独立分支完成、合并材料已备」）。
- 不把滚动条外观做成用户可选的主题皮肤；只提供「自动显示 / 始终显示」。
- 不为系统文件选择器与外部应用做样式覆盖（不属于 QIO 自有界面）。

