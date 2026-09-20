# QIO 第四阶段（视觉语言 / 交互收口 / 动效语言）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 QIO 前三阶段已建立的能力，用同一套视觉、交互与动效语言表达出来（不新增后端能力、不改信息架构）。

**Architecture:** 先在 `tokens.css` / `base.css` 建立第三版设计令牌（三层动效、晶体玻璃、位移、组件原语），再按「Planet 连续体」「阅读态重构」「确认与反馈统一」「对话页表面统一」四条工作流并行落地；每条工作流自带测试，最后统一跑 typecheck + vitest + 真实界面视觉验证。

**Tech Stack:** Vue 3 + TypeScript + Vite + Vitest；星球为 three.js WebGL；无头验证用 `scripts/visual_probe.mjs`（Chrome CDP）。

**Spec:** `docs/superpowers/specs/2026-09-15-visual-language-phase4-design.md`

## Global Constraints

- 不推翻前三阶段的状态语义、安全边界与业务流程；视觉修改不得掩盖真实状态。
- 不引入程序化构筑物（Topic 建筑 / 城市 / 生长体）。
- 颜色必须来自 `var(--*)`；新增时长/曲线/位移必须来自令牌，禁止在组件里散落硬编码。
- 不新增以真实 API Key 或联网为前提的测试。
- 每个任务结束时：`npx vue-tsc --noEmit` 与 `npm test` 必须通过（在 `frontend/` 下执行）。
- 文件所有权：同一文件同一时间只有一个 agent 在改（见各任务 Files 段）。

---

### Task 1: 设计令牌 v3（三层动效 + 晶体玻璃 + 位移）

**Files:**
- Modify: `frontend/src/styles/tokens.css`
- Test: `frontend/src/styles/tokens.test.ts`

**Interfaces (Produces):**
- 时长规范名：`--mo-1-press/-state/-fast/-exit/-menu/-wide`、`--mo-2-in/-move/-out/-wide`、`--mo-3-prepare/-expand/-settle/-collapse`
- 曲线：`--ease-1`、`--ease-1-out`、`--ease-2`、`--ease-2-out`、`--ease-3-in`、`--ease-3-settle`、`--ease-3-out`
- 位移：`--shift-1/-2/-4/-8/-panel`
- 玻璃：`--glass-bg`、`--glass-bg-strong`、`--glass-blur`、`--glass-border`、`--glass-edge-top`、`--glass-inner`、`--glass-tint-warm`、`--glass-tint-cool`、`--glass-shadow`
- 既有 `--dur-*` / `--ease` 全部保留为别名（后者不再各处硬编码）

- [ ] **Step 1: 改测试（红）** — 在 `tokens.test.ts` 增加断言：三层时长令牌存在；`--ease-3-settle` 的 y2 > 1（存在柔性过冲但非 bounce）；`--dur-planet-in` 等于 `var(--mo-3-expand)`；玻璃令牌存在且暗/亮两套都定义。
- [ ] **Step 2: 跑测试确认失败** — `npx vitest run src/styles/tokens.test.ts`，预期 FAIL（令牌缺失）。
- [ ] **Step 3: 实现令牌** — 按 spec §1、§3 写入两个主题块。
- [ ] **Step 4: 跑测试确认通过** — `npx vitest run src/styles/tokens.test.ts`。

### Task 2: 组件原语与 reduced-motion 语义（base.css）

**Files:**
- Modify: `frontend/src/styles/base.css`
- Test: `frontend/src/styles/tokens.test.ts`（CSS 文本断言，避免引入浏览器依赖）

**Interfaces (Produces):**
- 类：`.qio-card`（`--quiet/--focus`）、`.qio-row`（`.is-active/.is-selected`）、`.qio-toolbar`、`.qio-tag`、`.qio-state`（`ok/warn/err/info`）、`.qio-inline-edit`、`.qio-confirm`、`.qio-glass`（`--panel/--chip`）、`.qio-feedback`、`.qio-floating`
- 过渡工具类：`.qio-fade-enter-active/.qio-fade-leave-active`、`.qio-rise-*`、`.qio-collapse-*`（供 `<Transition>` 使用）

- [ ] **Step 1: 改测试（红）** — 断言 base.css 含上述类名，且 reduced-motion 段落**不再**把 `transition-duration` 归零为 `.001ms`（改为保留淡入淡出的压缩时长）。
- [ ] **Step 2: 实现** — 新增原语；reduced-motion 改为：时长压到 90ms、`--shift-*` 归零、`--ease-3-*` 退化为 `--ease-1`（保留 opacity 过渡）。
- [ ] **Step 3: 跑 `npx vue-tsc --noEmit` + `npm test`** — 确认既有 454 个用例不回归。

### Task 3: Planet 连续体（入口小球 = Planet 压缩态）

**Files:**
- Create: `frontend/src/components/planet/PlanetOrb.vue`（2D canvas 压缩态，复用 `planet/sdfRings.ts` 的 `levelField`/`smin`）
- Modify: `frontend/src/components/PlanetDock.vue`
- Modify: `frontend/src/components/PlanetBoot.vue`
- Modify: `frontend/src/views/PlanetView.vue`
- Modify: `frontend/src/composables/usePlanetScene.ts`（仅新增「按尺度控制信息密度」与「构图度量」的接口）
- Test: `frontend/src/components/__tests__/PlanetDock.test.ts`、`frontend/src/views/__tests__/PlanetView.test.ts`、新增 `frontend/src/components/planet/__tests__/PlanetOrb.test.ts`

**Interfaces (Produces):**
- `PlanetOrb.vue` props: `{ size?: number; detail?: number; activity?: boolean }`，emit `activated`
- `usePlanetScene` 新增：`setReveal(t: number)`（0 = 抽象态，1 = 完整 Planet；控制话题点/网格/标签透明度）、`sphereScreenRect()`（返回球体在屏幕上的中心与直径，供转场对齐）
- 转场状态：`entering | entered | collapsing | closed`，由 `PlanetView` 内部状态机驱动

- [ ] **Step 1: 测试（红）** — Dock 断言：渲染 canvas（不再是纯 SVG 椭圆环）、存在「压缩态」标记属性；PlanetView 断言：进入分三阶段（激活 → 体量 → 接管）、退出分三阶段且反向。
- [ ] **Step 2: 实现 PlanetOrb** — 用 `levelField` 在离屏 canvas 上算 3 层环（2× 超采样），主题色来自 CSS 变量，静止时 4fps 呼吸 + 轻微内部流动。
- [ ] **Step 3: 改造 Dock** — 用 `PlanetOrb` 替换 SVG；保持 `useFloatingWindow` 拖动/贴边行为不变。
- [ ] **Step 4: 改造 PlanetView 转场** — 阶段 A 激活（对话页退后）；阶段 B 星球 canvas 从入口矩形连续长大到全屏球体尺寸（同一对象，不是淡入）；阶段 C 玻璃浮层与话题密度接管。退出严格反向。
- [ ] **Step 5: reduced-motion** — 直接落到终态（保留一次很短的淡入），不跑三段编排。
- [ ] **Step 6: 跑测试 + typecheck**

### Task 4: Planet 浮层材质（晶体玻璃）与退出控件

**Files:**
- Modify: `frontend/src/views/PlanetView.vue`（`.close-btn`、`.topic-hint`、`.hud`、`.load-error`、面板外框）
- Modify: `frontend/src/components/SettingsFloat.vue`（浮动入口纳入同一浮层语言）

**Interfaces:** 复用 Task 1 的 `--glass-*` 与 Task 2 的 `.qio-glass`。

- [ ] **Step 1: 测试（红）** — 断言退出控件是一枚玻璃浮层（含 `.qio-glass` 类）且带可理解的 `aria-label`。
- [ ] **Step 2: 实现** — 退出控件改为右上角玻璃胶囊（hover 反馈纳入高频层），话题信息卡与错误条统一为玻璃浮层；面板外框不做重雾化。
- [ ] **Step 3: 跑测试 + typecheck**

### Task 5: Topic Detail 阅读态重构（PlanetView 面板）

**Files:**
- Modify: `frontend/src/views/PlanetView.vue`

**Interfaces:** 复用 `.qio-row`、`.qio-tag`、`.qio-state`、`.qio-inline-edit`、`.qio-feedback`。

- [ ] **Step 1: 测试（红）** — 断言：片段条目默认不显示「归档/修正」这类管理按钮；进入编辑后出现，退出后消失；Tab 切换有过渡类。
- [ ] **Step 2: 实现** — 阅读态优先：标题 → 摘要 → 事实 → 片段历史 → 实体 → 知识；管理动作收进「⋯」次级入口；知识条目默认只读，点「修正」就地进入 `.qio-inline-edit`。
- [ ] **Step 3: 列表连续性** — 话题列表/片段列表/知识列表用 `.qio-row` 并接入过渡类（筛选变化不跳变）。
- [ ] **Step 4: 跑测试 + typecheck**

### Task 6: Knowledge / Entity 阅读态与局部编辑

**Files:**
- Modify: `frontend/src/components/planet/KnowledgePanel.vue`
- Modify: `frontend/src/components/planet/EntityPanel.vue`
- Test: `frontend/src/components/planet/__tests__/KnowledgePanel.test.ts`、`EntityPanel.test.ts`

- [ ] **Step 1: 测试（红）** — 断言：默认阅读态下不渲染 `textarea`；进入编辑后才渲染；归档走确认层而不是 `window.confirm`。
- [ ] **Step 2: 实现** — 阅读态：内容为主、状态徽章降调；编辑就地进入（`.qio-inline-edit`）；归档/删除走 `QConfirm`（Task 7）。
- [ ] **Step 3: 跑测试 + typecheck**

### Task 7: 确认层（替换所有原生 confirm / alert）

**Files:**
- Create: `frontend/src/components/ui/QConfirm.vue`（inline / popover / layer 三档）
- Modify: `frontend/src/views/PlanetView.vue`、`frontend/src/views/SettingsView.vue`、`frontend/src/components/planet/KnowledgePanel.vue`、`frontend/src/components/planet/EntityPanel.vue`
- Test: 新增 `frontend/src/components/ui/__tests__/QConfirm.test.ts`

**Interfaces (Produces):**
- `QConfirm` props: `{ open, title, detail?, confirmText?, tone?: "normal" | "danger", variant?: "inline" | "popover" | "layer" }`；emits `confirm` / `cancel`
- 约束：`Esc` 取消；`layer` 档做焦点陷阱与归还；`danger` 档确认按钮为中性实心（不用品牌色）

- [ ] **Step 1: 测试（红）** — `QConfirm` 的行为测试（open/confirm/cancel/Esc/焦点归还）。
- [ ] **Step 2: 实现 QConfirm** — 三档形态共用一套视觉语言；`layer` 档用 `<Transition>` + `.qio-fade-*`。
- [ ] **Step 3: 替换 6 处原生确认** — `PlanetView.vue`（删除知识）、`SettingsView.vue`（删除凭据 / 覆盖保存 / 清除博查 Key）、`KnowledgePanel.vue`（归档）、`EntityPanel.vue`（归档）。
- [ ] **Step 4: 加守卫测试** — 源码扫描断言 `frontend/src` 内不再出现 `window.confirm|window.alert|window.prompt`。
- [ ] **Step 5: 跑测试 + typecheck**

### Task 8: 卡片家族统一（Approval / Tool / Subagent / Knowledge Candidate）

**Files:**
- Modify: `frontend/src/components/ApprovalModal.vue`、`frontend/src/components/ToolCreationCard.vue`、`frontend/src/components/KnowledgeCandidateCard.vue`、`frontend/src/components/MessageStream.vue`（工具卡与子 agent 卡）
- Test: 对应 `__tests__` 文件

**Interfaces:** 四类卡片共用 `.qio-card` 骨架 + `.qio-state` 状态徽章 + `.qio-feedback` 反馈 + 统一「展开详情」入口文案。

- [ ] **Step 1: 测试（红）** — 断言四类卡片使用统一骨架类，且状态推进为原位更新（不新增卡片）。
- [ ] **Step 2: 实现** — 统一标题层级（对象名 → 状态 → 一行说明 → 可展开详情）、统一状态徽章、统一成功/失败表达与停留时长。
- [ ] **Step 3: 跑测试 + typecheck**

### Task 9: 对话页表面统一（消息流 / 输入区 / 状态行）

**Files:**
- Modify: `frontend/src/components/MessageStream.vue`、`MessageItem.vue`、`Composer.vue`、`QueueChip.vue`、`ContinueBar.vue`、`TopicSwitchPrompt.vue`、`views/ConversationView.vue`

- [ ] **Step 1: 测试（红）** — 断言状态行/候选/提示使用统一 `.qio-feedback` 与状态行原语，且主内容优先级不被小徽章抢。
- [ ] **Step 2: 实现** — 状态行降到安静层级；工具状态不得抢过回答；Composer 状态变化（准备中/停止/已停止）连续。
- [ ] **Step 3: 跑测试 + typecheck**

### Task 10: 文档同步与验收报告

**Files:**
- Modify: `docs/status.md`、`docs/frontend-design.md`、`docs/frontend-components.md`、`docs/superpowers/specs/2026-08-09-qio-frontend-design.md`（追加 v2.3 修订段）
- Create: `docs/release-phase4.md`

- [ ] **Step 1: 同步设计文档** — 动效层级、三层定义、Planet 连续体、入口小球、玻璃规则、卡片统一、反馈层级、阅读态、reduced-motion。
- [ ] **Step 2: 同步 status.md** — 只写真实状态，不写会过期的硬编码数字。
- [ ] **Step 3: 跑 `python scripts/check_docs.py`**
- [ ] **Step 4: 写 `docs/release-phase4.md`** — 按 spec §八十 的十节结构输出（含未运行项与剩余问题）。

### Task 11: 真实视觉验证

**Files:**
- Create: `scripts/baseline/qa/phase4.mjs`（驱动 `scripts/visual_probe.mjs` 的场景脚本）

- [ ] **Step 1: 起服务** — `python scripts/e2e_up.py`
- [ ] **Step 2: 场景 1–7** — 普通对话 / 打开 Planet / Planet 内浏览 / Knowledge-Entity / Approval / Tool Creation / reduced-motion，逐条截图 + 客观几何证据。
- [ ] **Step 3: 对照度与键盘** — 复用 `scripts/baseline/qa/contrast.mjs`，并检查新增浮层的焦点环。
- [ ] **Step 4: 如实报告通过 / 失败 / 未运行**（不得把未运行写成通过）。
