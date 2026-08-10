# 对话页消息流自动避让输入框 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 输入框（Composer）贴边时消息流自动让出对应边空间（永不遮挡），并移除输入框自身的贴边隐藏；星球球/设置按钮及其隐藏机制完全不动。

**Architecture:** 新增纯函数 `computeAvoidance()`（输入浮动态 + 视口 → 四边避让增量），MessageStream 监听 `floatingState.composer` 计算避让量并叠加到 `.stream` 的默认 padding（CSS transition 平滑）。Composer 仅移除 `fw-hidden` 类与样式（通用隐藏机制保留给星球/设置）。设置页窗口 Tab 移除 Composer 的贴靠隐藏开关。

**Tech Stack:** Vue 3 + Pinia + vitest（前端）；沿用现有 `floatingState` 共享状态。

---

## Task 1: computeAvoidance 避让计算纯函数

**Files:**
- Create: `frontend/src/composables/dockAvoidance.ts`
- Test: `frontend/src/composables/__tests__/dockAvoidance.test.ts`

- [ ] **Step 1: 写失败测试**

新建 `frontend/src/composables/__tests__/dockAvoidance.test.ts`：

```ts
import { describe, expect, it } from "vitest";
import { computeAvoidance, type Avoidance } from "../dockAvoidance";

const VP = { width: 1024, height: 800 };
const ZERO: Avoidance = { top: 0, right: 0, bottom: 0, left: 0 };

describe("computeAvoidance 消息流避让", () => {
  it("贴底：底部让出 height+12", () => {
    expect(
      computeAvoidance({ dockedTo: "bottom", x: 230, y: 706, width: 560, height: 90 }, VP),
    ).toEqual({ ...ZERO, bottom: 102 });
  });
  it("贴右：右侧让出 width+12", () => {
    expect(
      computeAvoidance({ dockedTo: "right", x: 460, y: 350, width: 560, height: 90 }, VP),
    ).toEqual({ ...ZERO, right: 572 });
  });
  it("贴左：左侧让出 width+12", () => {
    expect(
      computeAvoidance({ dockedTo: "left", x: 4, y: 350, width: 560, height: 90 }, VP),
    ).toEqual({ ...ZERO, left: 572 });
  });
  it("贴顶：顶部让出 height+12", () => {
    expect(
      computeAvoidance({ dockedTo: "top", x: 230, y: 4, width: 560, height: 90 }, VP),
    ).toEqual({ ...ZERO, top: 102 });
  });
  it("未贴靠且贴近底部：按最近边估算为底部避让", () => {
    expect(
      computeAvoidance({ dockedTo: null, x: 230, y: 710, width: 560, height: 90 }, VP),
    ).toEqual({ ...ZERO, bottom: 102 });
  });
  it("未贴靠且贴近右侧：按最近边估算为右侧避让", () => {
    expect(
      computeAvoidance({ dockedTo: null, x: 900, y: 350, width: 120, height: 90 }, VP),
    ).toEqual({ ...ZERO, right: 132 });
  });
  it("尺寸未初始化（width/height 为 0）：不避让", () => {
    expect(
      computeAvoidance({ dockedTo: "bottom", x: 0, y: 0, width: 0, height: 0 }, VP),
    ).toEqual(ZERO);
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
node node_modules\vitest\vitest.mjs run src/composables/__tests__/dockAvoidance.test.ts
```

Expected: FAIL（模块不存在）。

- [ ] **Step 3: 最小实现**

新建 `frontend/src/composables/dockAvoidance.ts`：

```ts
import type { FloatingEntry } from "./floatingState";

export interface Avoidance {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

const GAP = 12;

/**
 * 计算消息流需要为浮动输入框让出的四条边增量（px）。
 * 贴靠边时精确避让；未贴靠（拖动中/自由态）按最近边估算，避免拖动瞬间遮挡消息。
 */
export function computeAvoidance(
  entry: Pick<FloatingEntry, "dockedTo" | "x" | "y" | "width" | "height">,
  viewport: { width: number; height: number },
  gap = GAP,
): Avoidance {
  const none: Avoidance = { top: 0, right: 0, bottom: 0, left: 0 };
  if (!entry.width || !entry.height) return none;
  switch (entry.dockedTo) {
    case "bottom":
      return { ...none, bottom: entry.height + gap };
    case "right":
      return { ...none, right: entry.width + gap };
    case "left":
      return { ...none, left: entry.width + gap };
    case "top":
      return { ...none, top: entry.height + gap };
    default:
      break;
  }
  const cx = entry.x + entry.width / 2;
  const cy = entry.y + entry.height / 2;
  const dTop = cy;
  const dBottom = viewport.height - cy;
  const dLeft = cx;
  const dRight = viewport.width - cx;
  const nearest = Math.min(dTop, dBottom, dLeft, dRight);
  if (nearest === dBottom) return { ...none, bottom: entry.height + gap };
  if (nearest === dRight) return { ...none, right: entry.width + gap };
  if (nearest === dLeft) return { ...none, left: entry.width + gap };
  return { ...none, top: entry.height + gap };
}
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
node node_modules\vitest\vitest.mjs run src/composables/__tests__/dockAvoidance.test.ts
```

Expected: 7 个用例 PASS。

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/composables/dockAvoidance.ts frontend/src/composables/__tests__/dockAvoidance.test.ts
git commit -m "feat: computeAvoidance 避让计算（贴边/最近边估算）"
```

## Task 2: MessageStream 应用避让

**Files:**
- Modify: `frontend/src/components/MessageStream.vue`
- Test: `frontend/src/views/__tests__/ConversationView.test.ts`

- [ ] **Step 1: 写失败测试**

在 `frontend/src/views/__tests__/ConversationView.test.ts` 的 `describe("ConversationView 状态条移除与异常提示", ...)` 内追加：

```ts
it("输入框贴底时消息流底部让出输入框高度，恢复后还原", async () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mountView(pinia, makeRouter());
  await flushPromises();
  const { floatingState } = await import("../../composables/floatingState");
  floatingState.composer.width = 560;
  floatingState.composer.height = 90;
  floatingState.composer.dockedTo = "bottom";
  await nextTick();
  const stream = w.find(".stream").element as HTMLElement;
  expect(stream.style.paddingBottom).toBe("122px"); // 默认 20 + (90 + 12)
  expect(stream.style.paddingTop).toBe("34px");
  floatingState.composer.dockedTo = null;
  await nextTick();
  expect(stream.style.paddingBottom).toBe("20px");
  w.unmount();
});
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
node node_modules\vitest\vitest.mjs run src/views/__tests__/ConversationView.test.ts
```

Expected: 新用例 FAIL（`.stream` 无内联 padding）。

- [ ] **Step 3: 最小实现**

`frontend/src/components/MessageStream.vue`：

- script 顶部 import 增加：

```ts
import { computeAvoidance } from "../composables/dockAvoidance";
import { floatingState } from "../composables/floatingState";
```

- 新增（放在 `showTyping` computed 之后）：

```ts
/** 消息流默认内边距（与 .stream CSS 一致）；避让量叠加在其上 */
const DEFAULT_PADDING = { top: 34, right: 44, bottom: 20, left: 44 };
const streamStyle = computed(() => {
  const a = computeAvoidance(
    {
      dockedTo: floatingState.composer.dockedTo,
      x: floatingState.composer.x,
      y: floatingState.composer.y,
      width: floatingState.composer.width,
      height: floatingState.composer.height,
    },
    { width: window.innerWidth, height: window.innerHeight },
  );
  return {
    paddingTop: `${DEFAULT_PADDING.top + a.top}px`,
    paddingRight: `${DEFAULT_PADDING.right + a.right}px`,
    paddingBottom: `${DEFAULT_PADDING.bottom + a.bottom}px`,
    paddingLeft: `${DEFAULT_PADDING.left + a.left}px`,
  };
});
```

- 模板 `.stream` 加 style 绑定：

```html
<div ref="containerRef" class="stream" :style="streamStyle" @scroll.passive="onScroll">
```

- `.stream` CSS 加过渡：

```css
.stream {
  flex: 1;
  overflow-y: auto;
  padding: 34px 44px 20px;
  transition: padding 0.25s cubic-bezier(0.22, 0.8, 0.24, 1);
  scrollbar-width: thin;
  background: var(--bg-base);
}
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
node node_modules\vitest\vitest.mjs run src/views/__tests__/ConversationView.test.ts
```

Expected: 新用例 + 既有用例全部 PASS。

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/components/MessageStream.vue frontend/src/views/__tests__/ConversationView.test.ts
git commit -m "feat: 消息流根据输入框贴边自动让出空间（平滑过渡）"
```

## Task 3: Composer 移除贴边隐藏

**Files:**
- Modify: `frontend/src/components/Composer.vue`
- Test: `frontend/src/components/__tests__/Composer.test.ts`

- [ ] **Step 1: 改失败测试（行为变更）**

`frontend/src/components/__tests__/Composer.test.ts`：把「贴靠隐藏：hidden 时挂 fw-hidden + 贴靠方向类」用例整体替换为：

```ts
it("贴边隐藏已移除：hidden 状态不影响输入框显示（无 fw-hidden）", async () => {
  const { floatingState } = await import("../../composables/floatingState");
  const { w } = await mountComposer();
  floatingState.composer.hidden = true;
  floatingState.composer.dockedTo = "top";
  await nextTick();
  const el = w.find(".composer");
  expect(el.classes()).not.toContain("fw-hidden");
  floatingState.composer.hidden = false;
  floatingState.composer.dockedTo = null;
  await nextTick();
  w.unmount();
});
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
node node_modules\vitest\vitest.mjs run src/components/__tests__/Composer.test.ts
```

Expected: 新用例 FAIL（`.composer` 仍挂 `fw-hidden`）。

- [ ] **Step 3: 最小实现**

`frontend/src/components/Composer.vue`：
- 模板根元素 class 数组去掉 `{ 'fw-hidden': float.entry.hidden }` 一项：

```html
    :class="[
      float.entry.dockedTo ? 'dock-' + float.entry.dockedTo : '',
    ]"
```

- CSS 删除以下规则（整段删除）：

```css
/* 贴靠隐藏：收起为贴靠边 10px 细边；hover 展开、移出再隐藏（CSS :hover 逐帧几何命中） */
.composer.fw-hidden {
  transform: translateY(calc(100% - 10px));
}
.composer.fw-hidden.dock-top {
  transform: translateY(calc(-100% + 10px));
}
.composer.fw-hidden.dock-left {
  transform: translateX(calc(-100% + 10px));
}
.composer.fw-hidden.dock-right {
  transform: translateX(calc(100% - 10px));
}
.composer.fw-hidden:hover {
  transform: none;
}
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
node node_modules\vitest\vitest.mjs run src/components/__tests__/Composer.test.ts
```

Expected: 全部 PASS（含新用例）。

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/components/Composer.vue frontend/src/components/__tests__/Composer.test.ts
git commit -m "feat: 移除输入框贴边隐藏（细边 + hover 展开），贴边后始终完整显示"
```

## Task 4: 设置页移除输入框贴靠隐藏开关

**Files:**
- Modify: `frontend/src/views/SettingsView.vue`
- Test: `frontend/src/views/__tests__/SettingsView.test.ts`

- [ ] **Step 1: 改失败测试（行为变更：3 个开关 → 2 个）**

`frontend/src/views/__tests__/SettingsView.test.ts`：

1. 「窗口 tab 展示三个贴靠隐藏开关与还原布局按钮」改为「窗口 tab 展示两个贴靠隐藏开关（星球/设置）与还原布局按钮」，断言 `switches.length` 为 2。
2. 「切换「输入框」贴靠隐藏：写入 shared state 并持久化 localStorage」整段改为：

```ts
it("切换「话题星球入口」贴靠隐藏：写入 shared state 并持久化 localStorage", async () => {
  const w = mount(SettingsView, { global: { stubs: { RouterLink: true } } });
  await w.findAll(".tab")[2].trigger("click");
  await nextTick();
  expect(floatingState["planet-dock"].hideEnabled).toBe(false);
  // 第一个开关 = 话题星球入口（WINDOW_ITEMS 顺序：planet-dock / settings-float）
  const sw = w.findAll(".panel:not([style*='display: none']) .qio-switch")[0];
  await sw.trigger("click");
  await nextTick();
  expect(floatingState["planet-dock"].hideEnabled).toBe(true);
  expect(sw.attributes("aria-checked")).toBe("true");
  const saved = JSON.parse(localStorage.getItem("qio-float-hide") || "{}");
  expect(saved["planet-dock"]).toBe(true);
  w.unmount();
});
```

3. 「还原默认布局」用例：保留，但把「遍历所有开关点击」逻辑不变（现在 2 个开关），断言保持 `floatingState.composer.hideEnabled` false（reset 后恒 false）。

- [ ] **Step 2: 运行测试确认失败**

```powershell
node node_modules\vitest\vitest.mjs run src/views/__tests__/SettingsView.test.ts
```

Expected: 至少 2 个用例 FAIL（仍渲染 3 个开关）。

- [ ] **Step 3: 最小实现**

`frontend/src/views/SettingsView.vue`：`WINDOW_ITEMS` 数组删除 composer 一项：

```ts
const WINDOW_ITEMS: { id: DockId; title: string; desc: string }[] = [
  { id: "planet-dock", title: "话题星球入口", desc: "贴靠后淡化隐藏，悬停展开、移出再隐藏" },
  { id: "settings-float", title: "设置入口", desc: "贴角后淡化隐藏，悬停展开、移出再隐藏" },
];
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
node node_modules\vitest\vitest.mjs run src/views/__tests__/SettingsView.test.ts
```

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/views/SettingsView.vue frontend/src/views/__tests__/SettingsView.test.ts
git commit -m "feat: 设置页窗口 Tab 移除输入框贴靠隐藏开关（保留星球/设置）"
```

## Task 5: 全量验证

- [ ] **Step 1: 前端全量测试**

```powershell
node node_modules\vitest\vitest.mjs run
```

Expected: 全绿（现有 133 + 新增约 8 个用例）。注意 chunk>500kB 警告不影响 exit code，用 `$LASTEXITCODE` 复核。

- [ ] **Step 2: 类型检查与构建**

```powershell
node node_modules\vue-tsc\bin\vue-tsc.js --noEmit
node node_modules\vite\bin\vite.js build
```

Expected: 无类型错误、构建成功。

- [ ] **Step 3: 手动冒烟（IAB 用 ?fresh=N 强制刷新）**

验证：拖动输入框贴四边 → 消息流对应边自动让出（含 12px 呼吸间距）、无遮挡；输入框增高时避让量同步；输入框不再有细边隐藏；星球球/设置按钮外观与隐藏机制不变；设置页窗口 Tab 只有 2 个开关。
