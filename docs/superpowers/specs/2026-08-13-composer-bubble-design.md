# 输入框「贴右下角大气泡」设计

> 日期：2026-08-13 ｜ 状态：已确认（材质 B / 右下角 / 固定不可拖动 / 移除避让）

## 背景与目标

对话页输入框此前是「可拖动浮动窗口 + 消息流避让」。避让在贴侧边时把宽输入框的整条边让出，导致消息被挤压到极窄区域，体验不佳。

**新方向（用户确认）**：放弃消息流避让，把输入框样式改成**贴右下角的「大气泡」**——浅色表面 + 玫红描边 + 右下小圆角，像一条放大的「用户消息」气泡，直接融入对话视觉；**固定右下角，不可拖动**。

## 范围

### 1. 输入框外观（材质 B）

- 大圆角气泡：`--bg-surface` 底 + `1.5px --accent` 玫红细描边；圆角 `18px 18px 4px 18px`（右下小圆角，呼应用户消息气泡 `14px 14px 4px 14px`）。
- 内部保持：输入框（透明底、`--text-primary` 文字、autosize 增高向上生长、Enter 发送）+ 玫红圆形发送按钮（`turnRunning` 时变 `…` 禁用）。
- 顶部保留话题信息条：话题名（衬线）+ 锚点片段（等宽）+ `/tool /topic /memory` 命令提示，小字融入气泡。

### 2. 位置与交互

- **固定右下角**：`position: fixed; right: 16px; bottom: 16px;`，窗口缩放时跟随右下角。
- **移除拖动**：Composer 不再接入 `useFloatingWindow`（无拖拽把手、无贴边、无贴靠隐藏、无 `fw-hidden`）。

### 3. 移除「消息流避让」

- 删除 `frontend/src/composables/dockAvoidance.ts` 及 `__tests__/dockAvoidance.test.ts`。
- `MessageStream.vue` 移除 `streamStyle` / `DEFAULT_PADDING` / `computeAvoidance` / `floatingState.composer` 监听，`.stream` 恢复固定 padding 与无 padding transition。
- 删除 `ConversationView.test.ts` 中避让用例。

### 4. 其他浮动组件

- 星球球（PlanetDock）、设置按钮（SettingsFloat）**完全不动**（可拖动、贴边/贴角、贴靠隐藏保持）。
- 星球球与输入框重叠时：输入框 `z-index` 高于星球球；星球球仍可拖动移开。

### 5. 设置页

- 窗口 Tab 维持现状（星球/设置两项开关 + 还原布局），不做改动。

## 不做的事

- 不做任何消息流避让/让位逻辑。
- 不删 `floatingState` 通用机制（星球/设置仍用）；`composer` 条目保留但不再被组件使用。
- 不改 PlanetDock / SettingsFloat。

## 测试计划

- 删除：`dockAvoidance.test.ts`、ConversationView 避让用例。
- 更新 `Composer.test.ts`：
  - 固定右下角定位（`position: fixed` + `right/bottom` 像素）；
  - 无 `fw-hidden`、无拖拽把手（无 `.fw-handle`）、`moved` 逻辑移除；
  - 发送流程、autosize 向上生长、`turnRunning` 禁用保留。
- `floatingState.test.ts` / `useFloatingWindow.test.ts` / `SettingsView.test.ts` 保留（通用机制未动）。
- 手动验证：窗口缩放输入框始终贴右下；输入增高向上生长；发送禁用态正常；星球球/设置按钮行为不变；消息流固定 padding 不再变化。

## 验收标准

- 输入框为贴右下角大气泡（浅色表面 + 玫红描边 + 右下小圆角），不可拖动。
- 消息流不再因输入框改变 padding。
- 星球球 / 设置按钮外观与交互不变。
- 前端全量测试通过、类型检查通过、构建通过。
