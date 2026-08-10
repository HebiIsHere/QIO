# 对话页消息流自动避让输入框 设计

> 日期：2026-08-10 ｜ 状态：待用户审查

## 背景与目标

对话页的输入框（Composer）是可拖动浮动窗口（贴边模式）。浮动窗口悬浮在消息流之上，会遮挡消息；现有「贴边隐藏（细边 + hover 展开）」虽能避让，但带来额外操作成本。

**目标**：输入框保持「可拖动 + 贴边」的自由，同时**消息流根据输入框贴靠的边自动让出空间，永不遮挡消息**；并移除输入框自身的贴边隐藏交互。

**明确不做**（用户确认）：
- 话题星球入口（PlanetDock）、设置入口（SettingsFloat）**完全保持现状**，包括它们现有的「贴靠后淡化隐藏、悬停展开」机制。
- 不处理输入框悬浮在消息中间的情况（输入框设计上贴边停靠）。

## 范围

### 1. 新增：消息流自动避让（核心）

- 避让逻辑放在 `MessageStream.vue`（或抽成 `useDockAvoidance()` composable，最终以实现计划为准），监听 `floatingState.composer` 的贴靠方向与尺寸。
- 当输入框贴靠某条边时，`.stream` 的对应内边距动态让出输入框占用尺寸：
  - 贴底（`dockedTo === "bottom"`）→ `padding-bottom` = 输入框高度 + 间距
  - 贴右（`"right"`）→ `padding-right` = 输入框宽度 + 间距
  - 贴左（`"left"`）→ `padding-left` = 输入框宽度 + 间距
  - 贴顶（`"top"`）→ `padding-top` = 输入框高度 + 间距
- 间距取 12px（输入框贴边时已有 4px 吸附余量，避让量 = 尺寸 + 12px 呼吸间距）。
- **覆盖规则**：被避让的那条边用「避让量」**覆盖**该边默认 padding（`.stream` 默认 `padding: 34px 44px 20px`），其余边保持默认值；例如贴底时 `padding-bottom = 输入框高度 + 12px`（替换默认 20px），`padding-left/right/top` 不变。
- **未贴靠（拖动中/自由态）**：按「距离最近的那条边」估算避让量（用 `composer.x/y/width/height` 与视口比较），拖动结束贴靠后切到精确值；避免拖动瞬间消息被短暂遮挡。
- **平滑过渡**：`.stream` 加 `transition: padding 0.25s cubic-bezier(...)`，避让量变化不跳动。
- **更新时机**：`dockedTo / width / height` 变化立即重算；`x / y` 变化（拖拽中）用 rAF 节流更新最近边估算，避免每帧重复触发。

### 2. 移除：输入框（Composer）贴边隐藏

- `Composer.vue`：移除 `fw-hidden` 相关类绑定与 CSS（细边收起 / hover 展开），贴边后正常完整显示。
- `floatingState` 与 `useFloatingWindow` 的通用隐藏机制**保留**（PlanetDock / SettingsFloat 继续使用），仅 Composer 不再启用（`composer.hideEnabled` 恒为 false，不参与隐藏）。

### 3. 设置页「窗口」Tab 调整

- `SettingsView.vue` 的 `WINDOW_ITEMS` 移除 `composer` 一项，只保留「话题星球入口」「设置入口」两个贴靠隐藏开关。
- 「还原默认布局」按钮保留。

### 4. 滚动与虚拟化适配

- 消息流「跟随底部」逻辑对 padding 变化自适应（`scrollHeight` 变化后仍能滚到最后一条）。
- 虚拟滚动不受影响：padding 加在 `.stream` 容器上，虚拟列表 spacer 高度不变。

## 不做的事

- 不改 PlanetDock / SettingsFloat 的任何行为与样式。
- 不删除通用贴靠隐藏机制（`hideEnabled / hidden` 字段、`setHideEnabled`、持久化键）。
- 不做「输入框悬浮中间时的像素级绕行」。

## 测试计划

- `ConversationView.test.ts`：设置 `floatingState.composer.dockedTo = "bottom"` 且 `height = 90` → `.stream` 的 `padding-bottom` 变大；`dockedTo = null` → 恢复默认。
- `Composer.test.ts`：移除「贴靠隐藏挂 `fw-hidden`」相关断言；新增「贴边后不再隐藏、始终完整显示」断言。
- `SettingsView.test.ts`：窗口 Tab 只渲染 2 个贴靠隐藏开关（不再有输入框一项）。
- `floatingState.test.ts` / `useFloatingWindow.test.ts`：保留（通用机制未删），如断言受影响则同步修正。
- 手动验证：拖动输入框贴四边，消息流对应边让出、无遮挡；高度随 autosize 变化时避让量同步；消息流滚动到底正常。

## 验收标准

- 输入框贴任意一条边时，消息流对应边自动让出输入框尺寸 + 12px，消息不被遮挡。
- 输入框不再有「细边收起 / hover 展开」交互。
- 星球球 / 设置按钮外观与交互不变。
- 设置页窗口 Tab 只有 2 个贴靠隐藏开关。
- 前端全量测试通过、类型检查通过、构建通过。
