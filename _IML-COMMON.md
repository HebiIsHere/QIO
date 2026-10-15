# IML 共同契约（本轮冻结）— fix/interactive-markdown-layout-continuity

> 本文件由 **Lead** 维护。所有子智能体开工前必读；与本文冲突的实现一律以本文为准。
> 基线：`origin/main = 29c68b7a017de4d5897122cf1e9714bf4bbd2cae`（2026-10-15 13:52）。
> 集成分支：`fix/interactive-markdown-layout-continuity`（worktree `D:\qio-dev\qio-iml`）。

## 0. 运行命令（已在本机验证）

后端（`<wt>\backend`）：

```powershell
uv sync --frozen --extra dev          # 首次，约 2s；创建本地 .venv
uv run --frozen --no-sync pytest tests/<file> -q
```

前端（`<wt>\frontend`）：先建 junction（不要 npm install，很慢）：

```powershell
cmd /c mklink /J "<wt>\frontend\node_modules" "D:\qio-dev\qio\frontend\node_modules"
npx vitest run src/<path>
npx vue-tsc --noEmit
```

全量：前端 `npx vitest run`；后端 `uv run --frozen --no-sync pytest -q`。
**不要**为了规避次数删改 CI；不要跑真实模型（一律 fake/mock provider）。

## 1. 冻结语义 F1：正式几何是唯一权威

- `board.cards[].x/y/w/h` 是卡片外框的**唯一**来源。**没有**第二套随视野计算的卡片位置。
- 缩放 / 平移 / 窗口变化 / 对话窗口 / 工具栏 / 审批浮层开合与尺寸变化，**只**影响查看方式与浮层自身布局：
  不得改变卡片坐标、尺寸、组成员、成员顺序。
- 展开阅读（`expandedCardIds`）只切换**卡内内容呈现**；不得改变自己或任何其他卡片的外框。
- 卡片可以大于视野、可以部分/完全在屏外。禁止为了"完整显示"自动移动、缩小或持续追踪。
- 只有用户**明确点击"定位／完整查看"**时，一次性调整视野；不改变卡片本身。
- `planExpandedLayout` 保留为 API，但必须退化为**恒等映射**（slots 逐字段等于正式矩形），`displaced` 恒为 `[]`。
  让位实现（`displaceLegacy` / `displaceEnhanced` / `pushAway` / `candidateMove` / `MAX_DISPLACE_ROUNDS` 的生产用途）与
  `expandedRect` 的视野夹取一并删除。
- `BoardCanvas` 的会话内尺寸标记 `userSizedCards` 及其契约删除（被 F1 取代）。
- `EXPANDED_PREVIEW_SIZE` 不再参与卡片外框；若保留导出，必须标注 deprecated 且无人引用。

## 2. 冻结语义 F2：统一几何约束服务（新文件，owner = A）

`frontend/src/interactive/geometry.ts`：

```ts
export interface GeoRect { x: number; y: number; w: number; h: number }
export type PlacementPolicy = "free" | "bounded";
export interface PlacementInput {
  rect: GeoRect;                // 期望矩形（尺寸已按 clampCardSize 收敛）
  others: readonly GeoRect[];   // 其他存活卡片（不含自己）
  policy: PlacementPolicy;      // 拉伸 -> "bounded"；移动 / 新建 -> "free"
  gap?: number;                 // 默认 16
}
export interface PlacementResult {
  rect: GeoRect;
  adjusted: boolean;
  reason: "ok" | "bounded" | "resolved" | "overlap";
}
export function resolvePlacement(input: PlacementInput): PlacementResult;
export function clampCardSize(size: { w: number; h: number }): { w: number; h: number };
export function placeRelative(
  anchor: GeoRect, side: "left" | "right" | "above" | "below",
  size: { w: number; h: number }, gap: number,
): GeoRect;
```

- `bounded`（拉伸）：只沿**本次被拉伸的那条边**在"第一个碰撞邻卡边界 − gap"处停住；**绝不移动别的卡片**。
  无法满足（起点就已经重叠 / 边被多个邻卡封死）时返回最接近可行的矩形并 `reason:"bounded"`，调用方给出简短原因。
- `free`（拖动落点 / QIO 移动 / 新建）：不重叠就保持；重叠则用 `resize.findFreeSlot` 在**确定性**顺序下找附近空位；
  绝不随机挤动其他卡片。
- 尺寸口径：`CARD_MIN_SIZE = {w:180,h:96}`、`CARD_MAX_SIZE = {w:1600,h:1200}`。
  **视野大小永远不是卡片上限**；`resizeAreaFor` 之类按视野算上限的路径必须删掉。
- 所有入口共用这一套：用户拖动、用户拉伸、QIO 新建 / 移动、正式执行与审批预览。

## 3. 冻结语义 F3：结果卡 Markdown（owner = B）

- `kind === "reply"` 的正文本体用 `MarkdownContent.vue` 渲染（`source = card.content`，**不**传 `reveal`）。
  不再走普通文字分支。用户不需要点"解析"，不需要复制到别的卡。
- 支持：标题、加粗、斜体、删除线、普通/有序列表、**任务列表**、引用、链接、行内代码、代码块、表格。
- `card.content` 始终保存 Markdown 原文；"复制原文"给出完整原文；代码复制保留代码原文。
- 历史普通文字结果照旧显示，不要求迁移。
- 表格不得超出卡片正文两侧；代码、长链接、长单词不得撑宽卡片；**不引入横向滚动**。
- 复用既有安全处理（`markdownPipeline` 的转义 + `isSafeExternalUrl` 白名单），**不得**把模型原文当可信 HTML。
- 卡片正文是**唯一**纵向阅读区域；不得因 Markdown 再加一层滚动容器。
- 渲染 / 异步高亮 / 正文更新**不得**改变卡片外框或触发整板重排（呼应 F1）。
- 正文长度裁剪要检查：`MAX_CARD_TEXT_CHARS` 一类上限必须**显式**处理（截断标记可见，或如实报错），
  不得把围栏 / 列表 / 表格截成半段还声称完整。
- 结果撰写提示词必须允许用 Markdown 组织结果（程序负责解析）。

## 4. 冻结语义 F4：结果卡可继续参与协作（owner = C）

- `models.CHECKABLE_KINDS = ("text", "reply")`。
- `visible_except_deleted(card)`：
  - `hidden` → False（不变）；
  - `kind == "reply"` → **`bool(card.get("checked"))`**（默认 False，即 QIO 自己生成不等于获得许可）；
  - legacy code/url → False（不变）；其余按 kind 判定（不变）。
- 新结果卡默认 `checked = False`（`models.new_card` 保持），许可**持续有效**，提交后不自动取消；
  隐藏 / 删除 / 手动取消许可立即退出范围。
- `submission.diff_states` 不再无条件跳过 reply：
  - 新增可见结果卡 → 新表达式 `result_added`；已可见结果卡正文/来源变化 → `result_updated`；
  - 两者都加进 `models.EXPRESSION_KINDS` 与 `NON_INTENT_EXPRESSIONS`（不是用户提出的新工作）。
  - `result_added` / `result_updated` 的载荷必须带**正文与来源**（不能只给卡片编号或任务标题）。
- `understanding.build_context` 交付可见结果卡的完整 `text` 与来源
  （`meta.reply` 的 intentId / understandingId / executionId、`updatedAt`），
  使"根据上次这张结论继续分析"有据可依。
- 不得因此自动再次提交、自动派生无限任务、绕过审批，也不得把同一成果反复写入记忆。
- 前端：结果卡整卡出现"允许 QIO 查看"控制（与文字卡同一套勾选语义），勾选显示、快照、范围判断、
  材料解析、上下文与校验全部同步。**只改一个筛选条件不算完成。**

## 5. 冻结语义 F5：批次内带类型的临时引用（owner = C）

- 引用符号语法：`card:<symbol>` / `group:<symbol>`；`<symbol>` 匹配 `[A-Za-z][A-Za-z0-9_]{0,31}`。
  不带前缀的裸符号按**卡片**引用处理（向后兼容）。
- 声明：`add_reply_card` / `add_text_card` 可带 `ref`（声明一张新卡）；`group_cards` 可带 `ref`（声明一个新组）。
- 解析顺序 = **同一意图内**的操作顺序；只能引用**更早**操作声明的、**类型正确**的符号。
- 拒绝：不存在、重复声明、前向引用、跨意图、跨批次、类型错误、循环、未获批准依赖。
- 已存在对象仍按当前获准范围（`visibleCardIds` / `visibleGroupIds`）校验。
- `understanding._validate_operation` 必须先收集声明、再校验引用（**不能**只在执行器加 ref 而让理解校验仍拒绝）。
- 服务端在 `execution.build_plan` 稳定分配正式对象 id；重复批准 / 刷新 / 暂停继续 / 重启 / 重试不得重复创建。
- 前端审批预览必须显示同一份符号 → 正式对象映射。
- 同批已批准计划对新对象的操作 **≠** 允许下一次提交自动读取结果卡；后续提交仍遵守用户许可。

## 6. 冻结语义 F6：几何上下文（owner = C）

`understanding.build_context` 增加（且**只**覆盖本次获准范围内的对象）：

```json
"geometry": {
  "unit": "board",
  "view": { "x": 0, "y": 0, "w": 1200, "h": 800, "scale": 1 } ,
  "cards":  [{ "id": "c_x", "x": 0, "y": 0, "w": 260, "h": 170 }],
  "groups": [{ "id": "g_x", "x": 0, "y": 0, "w": 320, "h": 240 }]
}
```

- 单位：板面坐标（board units），原点在板面左上，x 向右、y 向下；必须在上下文里明说。
- `view` = 客户端冻结的可见区域（含 scale）；客户端没给时 `null`（不得编造）。
- 不允许泄露未许可对象的**身份或几何**。客户端布局层可以对**本机可见**的其他卡片做碰撞计算，但不得把这些对象交给模型。
- 纯缩放 / 平移 / 浮层变化不构成新意图、不调用模型、不使内容任务失效。

## 7. 冻结语义 F7：新建落点与相对摆放（owner = C，前端预览同规则）

- `add_reply_card` / `add_text_card`：
  - 尺寸：给 `w`/`h` 时夹进 `[CARD_MIN, CARD_MAX]`（后端同口径 180×96 / 1600×1200），并在需要时如实说明调整；
    省略时按内容类型取默认：reply `320×240`、text `260×170`。**不得**静默变成 1×1 或极小卡。
  - 给 `x`/`y`：校验为有限数并**真实生效**；与既有卡片冲突时确定性地找附近空位（复用 F2 的 free 策略）。
  - 省略 `x`/`y`：若给了 `place` 就按相对摆放算；否则在冻结 `view` 内找空白落点。
    **不得**默认落到 (0,0)。
  - `place`：`{"relativeTo": "<cardId|card:ref>", "side": "left|right|above|below", "gap": 24}`，
    由程序用确定性几何计算 x/y（`placeRelative`），不依赖模型猜坐标。
- **审批预览与执行必须使用同一份落点/尺寸**（同一函数、同一冻结 view）；预览若依据变化按既有版本校验处理。

## 8. 冻结语义 F8：两种排序（owner = C）

- `order_group` 保持纯逻辑排序语义（只改 `memberOrder`）。给用户/模型的文案必须明确说**只改逻辑顺序**。
- 新增可选 `arrange: "row" | "column"`：
  - `row` = 从左到右、`column` = 从上到下（一列）；程序在调整成员顺序之后**真实写入成员 x/y**，
    间距用板面既有分组间距；写完由 `board.normalize_state` 统一更新组框。
- 只做这两种排列、只针对一个组；不扩大为通用自动排版系统。
- 不得把"只更新 memberOrder"报告成"已经重新排列卡片"。

## 9. 文件归属（唯一写入者，禁止跨人改同一文件）

| 范围 | 负责人 |
| --- | --- |
| `frontend/src/interactive/geometry.ts`（新） | A |
| `frontend/src/interactive/cardLayout.ts`、`resize.ts`、`viewport.ts` | A |
| `frontend/src/components/interactive/BoardCanvas.vue`、`BoardResizeLayer.vue`、`ResizeHandles.vue`、`BoardLinkLayer.vue`、`BoardGroupFrame.vue`、`BoardToolbar.vue`、`AddMenu.vue` | A |
| `frontend/src/components/MarkdownContent.vue` | B |
| `frontend/src/components/interactive/BoardCard.vue` | B |
| `frontend/src/interactive/textCard.ts`、`TextCardEditor.vue`、`frontend/src/components/interactive/displayText.ts` | B |
| `frontend/src/components/**`（B/A 之外，含 `IntentPreviewCard.vue`、`IntentBatchTray.vue`、`SubmitCluster.vue`、`ChatDock.vue`） | C |
| `frontend/src/interactive/*.ts`（A 的三个文件之外，含 `types.ts`、`approval.ts`、`submission.ts`、`board.ts`、`materials.ts`、`overlayLayout.ts`） | C |
| `frontend/src/stores/interactive.ts` | C |
| `backend/src/agent/interactive/*.py`（含 `prompts.py`） | C |
| `docs/**`、`scripts/**`、契约文件 | Lead |
| 验证证据目录 / 独立反例 | D（只读源码，只写自己的证据目录） |

跨归属需求（例如 B 需要 BoardCard 上新增"允许 QIO 查看"的结果卡语义）由**归属者实现**，需求写进任务描述。

## 10. 测试与验收纪律

- 每个子分支：先跑自己受影响的定向测试；**不要**在子分支跑全量（避免重复触发全量 CI）。
- 全量只由 Lead 在集成完成后跑一次，随后只重跑失败项。
- 不允许"改测试期望来保留已被本任务否定的旧行为"。被否定的旧行为（展开放大 / 让位、视野夹取、
  reply 一律不可见、`(0,0)` 默认落点、纯 memberOrder 声称已重排）对应的断言必须**删除或改写为新的正确
  行为**，并在提交信息里说明。
- 验收必须包含 `real` 浏览器检查（D 负责）；没有环境的项必须如实标为未验证。
