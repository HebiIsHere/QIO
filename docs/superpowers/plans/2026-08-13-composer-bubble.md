# 输入框贴右下角大气泡（移除消息流避让）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 输入框改为贴右下角的「大气泡」（浅色表面 + 玫红描边 + 右下小圆角），固定不可拖动；彻底移除消息流避让逻辑。

**Architecture:** Composer 移除 `useFloatingWindow` 依赖，改用 CSS `position: fixed; right/bottom` 固定右下角（高度增高靠 bottom 定位自然向上生长，无需 JS 调 top）；MessageStream 移除 `computeAvoidance`/`streamStyle`，恢复固定 padding；删除 `dockAvoidance.ts` 及其测试。

**Tech Stack:** Vue 3 + Pinia + vitest（前端）。

---

## Task 1: 移除消息流避让

**Files:**
- Modify: `frontend/src/components/MessageStream.vue`
- Delete: `frontend/src/composables/dockAvoidance.ts`、`frontend/src/composables/__tests__/dockAvoidance.test.ts`
- Modify: `frontend/src/views/__tests__/ConversationView.test.ts`

- [ ] **Step 1: 删除避让测试用例**

`frontend/src/views/__tests__/ConversationView.test.ts` 中删除整个用例「输入框贴底时消息流底部让出输入框高度，恢复后还原」。

- [ ] **Step 2: 实现（移除避让）**

`frontend/src/components/MessageStream.vue`：
- 删除 import：`computeAvoidance`、`floatingState` 两行。
- 删除 `DEFAULT_PADDING` 与 `streamStyle` 两个常量/computed 块。
- 模板 `.stream` 去掉 `:style="streamStyle"`。
- CSS `.stream` 去掉 `transition: padding 0.25s cubic-bezier(0.22, 0.8, 0.24, 1);` 一行。

删除文件（确认路径在项目内后执行）：

```powershell
Remove-Item -LiteralPath 'C:\Users\zxy\Documents\Front agent\qio\frontend\src\composables\dockAvoidance.ts' -Force
Remove-Item -LiteralPath 'C:\Users\zxy\Documents\Front agent\qio\frontend\src\composables\__tests__\dockAvoidance.test.ts' -Force
```

- [ ] **Step 3: 运行测试确认通过**

```powershell
node node_modules\vitest\vitest.mjs run src/views/__tests__/ConversationView.test.ts
```

Expected: 全部 PASS（避让用例已删，其余不变）。

- [ ] **Step 4: Commit**

```powershell
git add -A
git commit -m "refactor: 移除消息流避让（删除 computeAvoidance 与 streamStyle）"
```

## Task 2: Composer 改为固定右下角大气泡

**Files:**
- Modify: `frontend/src/components/Composer.vue`
- Test: `frontend/src/components/__tests__/Composer.test.ts`

- [ ] **Step 1: 改失败测试（行为变更）**

`frontend/src/components/__tests__/Composer.test.ts` 替换整个 describe 内的用例：

```ts
describe("Composer 输入框（右下角大气泡）", () => {
  it("渲染：气泡 + textarea + 发送按钮（无拖拽把手）", async () => {
    const { w } = await mountComposer();
    expect(w.find(".composer").exists()).toBe(true);
    expect(w.find(".topicbar.fw-handle").exists()).toBe(false);
    expect(w.find("textarea.qio-input").exists()).toBe(true);
    expect(w.find(".send-btn").exists()).toBe(true);
    w.unmount();
  });

  it("输入并发送：保留 send 数据流（api.sendTurn）", async () => {
    const { w, pinia } = await mountComposer();
    await w.find("textarea").setValue("hello qio");
    await w.find(".send-btn").trigger("click");
    await flushPromises();
    expect(mocks.sendTurn).toHaveBeenCalledWith("hello qio", null);
    const session = useSessionStore();
    expect(session.messages[session.messages.length - 1]?.content).toBe("hello qio");
    expect(w.find("textarea").element as HTMLTextAreaElement).toHaveProperty("value", "");
    w.unmount();
    void pinia;
  });

  it("固定右下角：position fixed + right/bottom 像素定位", async () => {
    const { w } = await mountComposer();
    const el = w.find(".composer").element as HTMLElement;
    const cs = getComputedStyle(el);
    expect(cs.position).toBe("fixed");
    expect(cs.right).toBe("16px");
    expect(cs.bottom).toBe("16px");
    w.unmount();
  });

  it("增高时设置 textarea 高度（bottom 定位自然向上生长）", async () => {
    const { w } = await mountComposer();
    const ta = w.find("textarea").element as HTMLTextAreaElement;
    Object.defineProperty(ta, "scrollHeight", { value: 160, configurable: true });
    await w.find("textarea").setValue("多行\n内容\n内容");
    expect(ta.style.height).toBe("160px");
    w.unmount();
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
node node_modules\vitest\vitest.mjs run src/components/__tests__/Composer.test.ts
```

Expected: 至少 3 个用例 FAIL（旧结构不匹配新断言）。

- [ ] **Step 3: 最小实现**

重写 `frontend/src/components/Composer.vue`：

```vue
<script setup lang="ts">
import { computed, ref } from "vue";
import { useSessionStore } from "../stores/session";

const session = useSessionStore();
const text = ref("");
const inputRef = ref<HTMLTextAreaElement | null>(null);

const topicText = computed(() => session.topicName || (session.currentTopicId ? "当前话题" : "默认话题"));

const anchorText = computed(() => {
  const f = session.anchorFragment;
  if (f?.title) return `anchor · ${f.title}`;
  if (session.anchorFragmentId) return `anchor · 片段 #${session.anchorFragmentId.slice(-4)}`;
  return "";
});

function submit() {
  const value = text.value.trim();
  if (!value) return;
  session.send(value);
  text.value = "";
  if (inputRef.value) inputRef.value.style.height = "auto";
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submit();
  }
}

function autosize() {
  const el = inputRef.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 160) + "px";
}
</script>

<template>
  <div class="composer bubble">
    <div class="topicbar">
      <span class="tname serif" :title="session.currentTopicId ?? undefined">{{ topicText }}</span>
      <span v-if="anchorText" class="anchor mono">{{ anchorText }}</span>
      <span class="spacer"></span>
      <span class="slash mono">/tool /topic /memory</span>
    </div>

    <div class="input-row">
      <textarea
        ref="inputRef"
        v-model="text"
        class="qio-input"
        placeholder="和 QIO 说点什么…"
        :disabled="session.turnRunning"
        @keydown="onKeydown"
        @input="autosize"
      ></textarea>
      <button
        class="send-btn"
        type="button"
        :disabled="!text.trim() || session.turnRunning"
        :aria-label="session.turnRunning ? '运行中' : '发送'"
        :title="session.turnRunning ? '运行中' : '发送（Enter）'"
        @click="submit"
      >
        <span v-if="session.turnRunning">…</span>
        <span v-else>↑</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 贴右下角大气泡：浅色表面 + 玫红描边 + 右下小圆角，固定不可拖动 */
.composer {
  position: fixed;
  right: 16px;
  bottom: 16px;
  z-index: 12;
  width: min(560px, calc(100vw - 32px));
  border: 1.5px solid var(--accent);
  border-radius: 18px 18px 4px 18px;
  padding: 10px 18px 16px;
  background: var(--bg-surface);
  box-shadow: 0 12px 34px rgba(0, 0, 0, 0.4);
}
.topicbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  font-size: 12px;
}
.tname {
  font-weight: 600;
  color: var(--text-strong);
  font-size: 15px;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.anchor {
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.spacer {
  flex: 1;
}
.slash {
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.05em;
  white-space: nowrap;
}
.input-row {
  display: flex;
  align-items: flex-end;
  gap: 10px;
}
.input-row textarea.qio-input {
  flex: 1;
  min-height: 46px;
  max-height: 160px;
  resize: none;
}
.send-btn {
  width: 38px;
  height: 38px;
  flex-shrink: 0;
  border: none;
  border-radius: 50%;
  background: var(--accent);
  color: var(--on-accent);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  transition: background 0.18s, transform 0.15s;
}
.send-btn:hover:not(:disabled) {
  background: var(--accent-hover);
  transform: translateY(-1px);
}
.send-btn:disabled {
  opacity: 0.45;
  cursor: default;
}
</style>
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
node node_modules\vitest\vitest.mjs run src/components/__tests__/Composer.test.ts
```

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/components/Composer.vue frontend/src/components/__tests__/Composer.test.ts
git commit -m "feat: 输入框改为贴右下角大气泡（浅色表面+玫红描边，固定不可拖动）"
```

## Task 3: 全量验证

- [ ] **Step 1: 前端全量测试**

```powershell
node node_modules\vitest\vitest.mjs run
```

Expected: 全绿（删除 dockAvoidance 2 用例、ConversationView 1 用例，Composer 调整为 4 用例；总数约 138）。

- [ ] **Step 2: 类型检查与构建**

```powershell
node node_modules\vue-tsc\bin\vue-tsc.js --noEmit
node node_modules\vite\bin\vite.js build
```

Expected: 无类型错误、构建成功。

- [ ] **Step 3: 手动冒烟（IAB 用 ?fresh=N 强制刷新）**

验证：输入框贴右下角（右 16px / 底 16px）、窗口缩放跟随右下、增高向上生长、无拖拽把手、发送禁用态正常；消息流 padding 固定不再变化；星球球/设置按钮行为不变。
