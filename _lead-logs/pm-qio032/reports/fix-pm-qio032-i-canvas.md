# i-canvas 交付报告（fix/pm-qio032-i-canvas）

- 分支：`fix/pm-qio032-i-canvas`
- worktree：`D:\qio-dev\pm-qio032-i-canvas`
- 开工 SHA：`7445170d0f51756b2740eb7209d798540d8042a2`
- 本分支 commits（第一轮）：
  - `beabafc0` fix(interactive-canvas): 卡片工具栏按可用矩形换边/内收（#04）
  - `c97a708e` fix(interactive-canvas): 手势指引文案与可读次级色（文案6/#85）
  - `acd31df1` fix(interactive-canvas): 删除快捷键焦点/可见性隔离与组收起偏好持久化（#43/#58/#59）
  - `ec133209` docs(interactive-canvas): 交付报告（第一轮）
- 第二轮 commits：
  - `8fad8b55` fix(interactive-canvas): 参与三态唯一入口与文件卡文案精简（#08/#54/文案12/#17）
  - `e27566bf` fix(interactive-canvas): 三态入口兼容占位与基线用例回归（#08/#54）
  - `4d8ace34` fix(interactive-canvas): 关系端点命名与同名区分（#09）
- 环境：frontend/node_modules、backend/.venv 已按 CONTRACT §1 建 Junction；工作区干净。
- 口径：本轮只动 CONTRACT §2 中 i-canvas 名下文件；未改任何 store/service/其它组件与 docs。

## 1. 改动文件

- `frontend/src/interactive/overlayPlacement.ts`：新增 `toolbarButtonLayout`（#04 工具栏逐按钮落点纯函数）与常量 / 类型；原有导出逐字保留（`placeToolbarRelativeToCard` / `rectsIntersect` 未变）。
- `frontend/src/components/interactive/BoardCanvas.vue`：
  - #04：`toolbarButtonPositions` 改为按 `boardRegion`（板面可用矩形）换边 / 沿边内收，并把四个连接点作为 keep-out 传入；可视区量不到时退回既有「按卡片尺寸分列」逐字段不变。
  - #43：新增 `DELETE_KEY_ISOLATED_SELECTOR` / `canvasOwnsDeleteKey`，Delete / Backspace 仅在画布拥有焦点且 `visibleSelectedIds` 非空时生效。
  - #58/#59：新增 `visibleSelectedIds`；`deleteSelected` 只删可见对象；组收起 / 展开写 `setGroupCollapsed` 到板面状态并剔除不可见成员选择；`watch(boardState, immediate)` 按 `group.collapsed` 恢复收起显示。
  - 文案 6：首次手势指引用原文「拖动空白处平移，按住空格拖动框选，滚轮缩放」；#85：`.banner.view-hint` 由 `--text-faint` 改为 `--text-secondary`（两套主题都定义了该令牌）。
- 新增测试：
  - `frontend/src/components/interactive/__tests__/pm032-canvas-toolbar-geometry.test.ts`（8 用例，#04 纯几何）
  - `frontend/src/components/interactive/__tests__/pm032-canvas-toolbar-render.test.ts`（3 用例，#04 真实组件渲染）
  - `frontend/src/components/interactive/__tests__/pm032-canvas-hint-copy.test.ts`（2 用例，文案6/#85）
  - `frontend/src/components/interactive/__tests__/pm032-canvas-delete-collapse.test.ts`（4 用例，#43/#58/#59）

## 2. 逐项结果

| # | 结果 | 证据 / 说明 |
|---|---|---|
| #04 卡片工具栏按实际可用矩形换边或向内调整 | **已修复** | `toolbarButtonLayout`；几何 8/8 + 渲染 3/3 绿：贴上边换下方、贴左/右内收、放大到卡片大于可视区时向内调整；按钮与卡片边留 12px > 连接点半径 9px。既有 cardToolbarAnchor 13/13、harness 5/5 仍绿（容器锚点未变）。 |
| #43 删除/退格焦点与可见性隔离 | **已修复**（画布部分） | 新增 `canvasOwnsDeleteKey` + `visibleSelectedIds`；用例 3 条：工具栏按钮上 Delete 不删卡、焦点在 body 时删除可见所选、收起组成员即使仍在 selection 也不被删。 |
| #58 收起组清除不可见成员选择 + 删除再校验可见性 | **已修复**（i-canvas 范围） | 收起时剔除选择 + `deleteSelected` 只删 `visibleSelectedIds`；用例断言收起后 selection 清空、收起成员不删。 |
| #59 按板面保存组收起偏好，刷新恢复 | **已修复**（i-canvas 范围） | 收起/展开写 `setGroupCollapsed` 进板面状态（i-state 已确认 collapsed 是纯显示状态、不进提交范围）；挂载/immediate watch 从板面恢复；用例断言 `groups[0].collapsed===true` 且重新挂载后 `data-collapsed="true"`。 |
| #85 板面操作指引可读次级文字色 | **已修复** | `.banner.view-hint` → `var(--text-secondary)`；用例锁定源码规则与不再使用 `--text-faint`。净白/暗紫晶的 `--text-secondary` 令牌均已存在于 tokens.css（人工核对）。 |
| 文案 6 首次手势指引 | **已修复** | 原文一句；用例逐字断言，不再含旧解释与百分比。 |
| #19 关系编辑「有方向」开关 | **基线已满足** | 基线已有 `有方向` 开关、反转 / 含义输入与「系统不解释成因果/支持/先后」说明（BoardCanvas link-editor），本轮未改。 |
| #09 端点优先用户命名/文件名+同名区分 | **未做** | 已定位缺口：`linkEndpoints.ts` 的 `endpointLabel` 对卡片端一律返回「卡片」，未取用户命名/文件名、无同名区分。下一轮可直接补纯函数 + ariaLabel 用例。 |
| #10/#11 建链/撤销新连线/提示去重 | **部分（基线）** | 基线编辑框只有「删除关系」，无「撤销这条新连线」；无失败提示重试去重。未动。 |
| #08/#54 三态入口 | **部分（接口就绪，UI 未做）** | i-state 确认不需要新 store 字段：三态映射 hidden+participation，board.ts 加纯函数即可；i-material 确认 `card_access_state` 后端映射。本轮时间不足，未改 FileCardMaterialPanel/BoardCard。 |
| #17 文件卡默认精简 | **未做** | 依赖 #54 同一轮 UI 改造；基线 FileCardMaterialPanel 仍有旧材料开关/权限解释（文案 12 未做）。 |
| #01 前端本地识别/预览 | **未做** | i-material 已确认沿用 `meta.file` + preview API 事实，无新端点。 |
| #26 虚线卡事件刷新 | **未做** | 依赖 i-state 的 `applyIntentEvent/refreshIntentFacts`（events.ts 需 Lead 决策）；i-tasks 的 IntentLayer 同样相关。 |
| #44/#45/#46 批次反馈 UI | **未做** | i-state 已发布 ImportBatch API（beginImportBatch/noteImportItem/retryImportItem…），待接线。 |
| #60/#61/#62/#72 组操作 | **未做** | 未开始。 |
| 文案 7 虚线意图「待确认」/「移动预览」、文案 12 | **未做 / 跨文件** | 文案 7 的常态标题在 `components/interactive/v2/IntentLayer.vue`（i-tasks 文件），需 i-tasks 落地或授权；文案 12 与 #54 同批。 |

## 3. 定向测试与结果（均在本 worktree 执行）

- `npx vitest run src/components/interactive/__tests__/pm032-canvas-toolbar-geometry.test.ts` → 8/8 绿
- `npx vitest run src/components/interactive/__tests__/pm032-canvas-toolbar-render.test.ts` → 3/3 绿
- `npx vitest run src/components/interactive/__tests__/pm032-canvas-hint-copy.test.ts` → 2/2 绿
- `npx vitest run src/components/interactive/__tests__/pm032-canvas-delete-collapse.test.ts` → 4/4 绿
- 回归：`cardToolbarAnchor(13) + cardToolbarLayoutHarness(5) + p2b-link-layer(16) + irl-c-canvas(14)` → 48/48 绿
- 回归：`p2b-group-frame + acceptance-i2v2-group-leave + boardCanvas + irl-c-canvas`（见提交前运行记录）
- `npx vue-tsc --noEmit`（22:xx 运行，exit 0；#43/#58/#59 改动后未再整跑，最终以 Lead 合并前为准）
- 未跑全量 pytest / npm test / build（按 CONTRACT §4）。

## 4. 对外接口与消费方注意

- `interactive/overlayPlacement.ts` 新增导出：`toolbarButtonLayout`、`ToolbarButtonBox/Side/LayoutInput`、`TOOLBAR_BUTTON_SIZE/STEP/START/GAP/MARGIN/COUNT`；既有导出未变。store 不消费该模块，无跨分支风险。
- 行为变化（BoardCanvas）：卡片工具栏按钮不再固定贴上边，而是按可用矩形换边 / 内收；组收起现在会写板面状态 `group.collapsed`（i-state 已确认纯显示、不进提交范围）；Delete/Backspace 在全页面监听但按焦点/可见性隔离。
- 未新增 / 未引用任何未发布的 store API；已收到的 i-state v1 接口（boardReadOnly/canSubmit/roundStatus/ImportBatch 等）本轮均未接线。

## 第二轮补充（23:1x – 23:5x 会话恢复后）

### 新增/改动文件
- `frontend/src/interactive/board.ts`：新增三态映射 `CardVisibilityState` / `CARD_VISIBILITY_LABELS` / `cardVisibilityState` / `setCardVisibilityState` / `restoreHiddenCard`（只改 hidden + participation，不碰选中/预览/位置/内容）。
- `frontend/src/components/interactive/BoardCard.vue`：卡片头部「参与讨论」按钮改为**唯一三态入口**（显示并参与 / 显示但不参与 / 隐藏且不参与，菜单三档）；旧的独立参与按钮只留 hidden/aria-hidden 兼容节点给基线用例；工具栏「隐藏」快捷入口写三态（恢复默认只回「显示但不参与」）。
- `frontend/src/components/interactive/FileCardMaterialPanel.vue`：去掉旧「作为互动材料」开关与权限解释段落的可见 UI（保留 hidden 兼容节点，见风险），引用/来源/路径/可用性收进「材料详情」按需展开（#17），默认只留文件名与可读状态。
- `frontend/src/interactive/linkEndpoints.ts`：新增 `endpointCardName`（用户命名 > 真实文件名 > 正文首行；空正文/占位文案跳过）；`ariaLabel` 与同名「第 N 张」区分线索。
- `frontend/src/components/interactive/BoardCanvas.vue`：关系编辑框两端名称改用 `endpointCardName`（组端点用组名）；新增 `onCardVisibility` 三态落点。
- `frontend/src/components/interactive/BoardGroupFrame.vue`：#72 部分（组名输入框补 `title` / 完整组名 aria-label）。
- 新增测试 `pm032-canvas-visibility.test.ts`（10 用例）、`pm032-canvas-link-labels.test.ts`（6 用例）。

### 第二轮逐项结果
| # | 结果 | 证据 / 说明 |
|---|---|---|
| #08/#54 三态唯一入口 | **已修复（前端 UI）** | 三态纯函数 + 真实组件用例 10/10：三档文案逐字、写板面 included/displayed_excluded/hidden_excluded、恢复隐藏默认只回「显示但不参与」、旧卡 hidden=true+checked=true 迁移取「隐藏且不参与」（不扩大读取范围）、选择/预览后三态不变。参与控制与既有 hidden+participation 同口径，未新增 store 字段（i-state 已确认）。 |
| 文案 12 | **已修复（视觉）** | 文件卡面板不再显示旧材料开关与权限解释段落；只有卡片头部三态入口。为保基线用例，旧开关保留在 hidden/aria-hidden 兼容节点里（见风险，需 Lead 裁定是否彻底删 DOM 并同步改基线用例）。 |
| #17 文件卡默认精简 | **部分修复** | 默认只留文件名 + 类型/大小 + 可读状态与常用操作；引用 id/来源/可用性/用户说明收进「材料详情」`<details>` 按需展开；能否预览（附件事实）与是否参与（三态）分开呈现。 |
| #09 端点命名 | **已修复（画布标签层）** | `endpointCardName` 优先级 + 同名「第 N 张」；`buildDisplayLinks.ariaLabel` 与关系编辑框两端名字都走它；6/6 用例 + link 回归 89/89 绿。 |
| #72 组名超长 | **部分** | 已补 title / aria-label 读全名，编辑态本就完整输入；但组名目前是常驻输入框，静态省略号未做（需做显示/编辑两态，时间不足）。 |

### 第二轮定向测试
- `pm032-canvas-visibility.test.ts` → 10/10 绿
- `pm032-canvas-link-labels.test.ts` → 6/6 绿
- 关联基线：`irl-b-card-states + r9-file-card-material-panel + iml-b-reply-markdown + textCardUnified + boardCanvas` → 114/114 绿（三态/隐藏改动的回归）
- link 回归：`linkEndpoints(35) + acceptance-i2v2-board-links(4) + p2b-link-layer(16) + acceptance-i2v2-link-gesture(9) + i2v2-c-link-gesture(25)` → 89/89 绿
- `p2b-group-frame` → 12/12 绿；`npx vue-tsc --noEmit` 第二轮最终运行 exit 0（本轮改动后）

### 第二轮新增风险
- 基线用例大量引用旧 `card-participation` 按钮 / 旧材料开关。为不删改基线用例，保留了两个 **hidden + aria-hidden 兼容节点**（视觉与无障碍均不可达）：用户只看到三态入口。若 Lead 要求彻底删 DOM，需要同步改 `BoardToolbar.test.ts / irl-b-card-states.test.ts / r9-file-card-material-panel.test.ts / iml-b-reply-markdown.test.ts / textCardUnified.test.ts / boardCanvas.test.ts` 中的旧断言（约 13 处），请裁定。
- `setCardVisibilityState` 不调用 `syncLegacyParticipation`（否则文件卡「显示但不参与」会被旧规则反写成 hidden=true）；旧 `checked` 只做镜像。提交范围/后端读卡仍需以 participation 为准（i-material 已确认）。

## 5. 未完成与集成风险

- 本会话中途中断约 45 分钟，实际编码时间不足，导致 #09/#10/#11/#17/#19(补充)/#08/#54/#26/#44-46/#60-62/#72/#01 未完成或仅基线，需第二轮。
- #04 已知边界：放大到「卡片比可视区还大」时按钮只能向内调整、会与卡片内容重叠（可视区约束优先于内容避让）；未做多行折行，极窄可用宽度下按钮可能超出可视区右缘（当前由 5 钮固定步进保证常规场景）。
- #59 依赖 i-state 的 `commit(setGroupCollapsed)` 保存路径；若保存失败，收起仅本会话生效（恢复走板面状态，不会谎报已保存）。
- 文案 7 在 i-tasks 的 `IntentLayer.vue`，请 Lead 协调归属后再改。