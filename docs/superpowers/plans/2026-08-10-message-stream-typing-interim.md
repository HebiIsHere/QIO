# 消息流「打字指示器 + 中间消息」与记忆滑块移除 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 Composer 的记忆强度滑块；发送消息后用户气泡先落屏并显示三圆点跳动打字指示器；agent 一轮可发多条消息——工具调用前的可见评论作为「过程」中间助手消息展示，最后一条为最终消息。

**Architecture:** 沿用现有 SSE 事件流。后端在 native 模式下，当一次 planning completion 同时带可见 content 与 tool_calls 时，新增 `ASSISTANT` 事件（data.content + interim=true）在 TOOL_START 之前发出；前端 events store 将其路由为带 `interim` 标记的助手消息，MessageItem 以弱化样式展示；MessageStream 在 turnRunning 且当前轮次尚无助手消息时渲染三圆点打字指示器（CSS animation，位于虚拟列表外，避免虚拟化重测量问题）。

**Tech Stack:** Vue 3 + Pinia + @tanstack/vue-virtual（前端）；FastAPI + pydantic + asyncio（后端 SSE）；vitest（前端测试）；pytest（后端测试）。

---

## Task 1: 后端新增 ASSISTANT 事件类型并在 loop 中发出中间助手消息

**Files:**
- Modify: `backend/src/agent/api/events.py`（EventType 枚举加 `ASSISTANT`）
- Modify: `backend/src/agent/core/loop.py`（planning 完成后发中间助手消息）
- Test: `backend/tests/test_loop.py`（两个新用例）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_loop.py` 末尾追加：

```python
async def test_interim_assistant_event_emitted_for_native_commentary():
    client = ScriptedClient(
        [FakeCompletion([FakeChoice(FakeMessage("我先查一下仓库", [_tc("c1", "echo", '{"text": "hi"}')]))])]
    )
    loop, bus = _make_loop(client)

    collected: list[str] = []

    async def consumer():
        async for chunk in bus.stream():
            collected.append(chunk)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)
    await loop.run("查一下仓库")
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    joined = "\n".join(collected)
    assert "event: ASSISTANT" in joined
    assert "我先查一下仓库" in joined
    assert joined.index("我先查一下仓库") < joined.index("TURN_END")


async def test_plain_text_turn_no_interim_assistant_event():
    client = ScriptedClient([])
    loop, bus = _make_loop(client)

    collected: list[str] = []

    async def consumer():
        async for chunk in bus.stream():
            collected.append(chunk)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)
    await loop.run("hello")
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert "event: ASSISTANT" not in "\n".join(collected)
```

- [ ] **Step 2: 运行测试确认失败**

Run（在 `qio/backend` 下，`PYTHONPATH=src`，用项目本地 python）：

```powershell
$env:PYTHONPATH='C:\Users\zxy\Documents\Front agent\qio\backend\src'; & 'C:\Users\zxy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_loop.py::test_interim_assistant_event_emitted_for_native_commentary tests/test_loop.py::test_plain_text_turn_no_interim_assistant_event -v
```

Expected: 第一个用例 FAIL（没有 `event: ASSISTANT`）；第二个用例 PASS。

- [ ] **Step 3: 最小实现**

`backend/src/agent/api/events.py` 的 `EventType` 枚举，在 `TOOL_END = "TOOL_END"` 之后、`WARNING = "WARNING"` 之前加一行：

```python
    ASSISTANT = "ASSISTANT"
```

`backend/src/agent/core/loop.py` 的 `run()` 中，在 `completion = await self._plan(messages)` 之后、`if not completion.tool_calls:` 之前插入：

```python
            # 中间助手消息：native 模型在请求工具前写下的可见评论，前端作为「过程」气泡展示
            if (
                self.adapter.mode == AdapterMode.NATIVE
                and completion.tool_calls
                and completion.message.content
                and completion.message.content.strip()
            ):
                await self._emit(
                    EventType.ASSISTANT,
                    {"content": completion.message.content, "interim": True},
                )
```

`AdapterMode` 已在 loop.py 顶部 import（`from agent.adapters.base import AdapterMode, BaseAdapter, ChatMessage, Completion`），无需新增。

- [ ] **Step 4: 运行测试确认通过**

```powershell
$env:PYTHONPATH='C:\Users\zxy\Documents\Front agent\qio\backend\src'; & 'C:\Users\zxy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_loop.py -v
```

Expected: 两个新用例 + 既有用例全部 PASS；`tests/test_events.py::test_all_event_types_serialize` 也通过（枚举迭代自动覆盖）。

- [ ] **Step 5: Commit**

```powershell
git add backend/src/agent/api/events.py backend/src/agent/core/loop.py backend/tests/test_loop.py
git commit -m "feat: 后端新增 ASSISTANT 事件（工具调用前的中间助手评论）"
```

## Task 2: 前端协议 + session store 支持 interim 助手消息

**Files:**
- Modify: `frontend/src/services/events.ts`（EVENT_TYPES 加 `"ASSISTANT"`）
- Modify: `frontend/src/stores/session.ts`（StreamMessage 加 `interim?: boolean`；pushAssistant 加第三参）
- Test: `frontend/src/stores/__tests__/events.test.ts`（新建，两个用例）

- [ ] **Step 1: 写失败测试**

新建 `frontend/src/stores/__tests__/events.test.ts`：

```ts
import { describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "",
      topic_name: null,
      anchor_fragment: null,
      messages: [],
    })),
    sendTurn: vi.fn(async () => ({})),
  },
}));

describe("events store 路由", () => {
  it("ASSISTANT 事件 → 追加 interim 助手消息", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({
      type: "ASSISTANT",
      id: "e1",
      ts: "2026-08-10T00:00:00Z",
      data: { content: "我先查一下仓库", interim: true },
    });
    const last = session.messages[session.messages.length - 1];
    expect(last?.role).toBe("assistant");
    expect(last?.content).toBe("我先查一下仓库");
    expect(last?.interim).toBe(true);
  });

  it("ASSISTANT 空内容不追加消息", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({
      type: "ASSISTANT",
      id: "e2",
      ts: "2026-08-10T00:00:00Z",
      data: { content: "  " },
    });
    expect(session.messages.length).toBe(0);
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run（在 `qio/frontend` 下）：

```powershell
node node_modules\vitest\vitest.mjs run src/stores/__tests__/events.test.ts
```

Expected: 两个用例 FAIL（route 无 ASSISTANT 分支 → 未追加消息）。

- [ ] **Step 3: 最小实现**

`frontend/src/services/events.ts` 的 `EVENT_TYPES` 数组，在 `"TOOL_END"` 之后加 `"ASSISTANT",`。

`frontend/src/stores/session.ts`：
- `StreamMessage` 接口加字段：`/** 中间助手消息（工具调用前的可见评论，区别于最终答复） */ interim?: boolean;`
- `pushAssistant` 改为：

```ts
pushAssistant(text: string, memoryInject?: StreamMessage["memoryInject"], interim = false) {
  this.pushMessage({
    role: "assistant",
    content: text,
    contentType: "text",
    memoryInject,
    ...(interim ? { interim: true } : {}),
  });
}
```

`frontend/src/stores/events.ts` 的 `route()` switch，在 `case "MEMORY_INJECT":` 之后加：

```ts
case "ASSISTANT": {
  const d = event.data as Record<string, unknown>;
  const content = String(d.content ?? "");
  if (content.trim()) {
    session.pushAssistant(content, undefined, true);
  }
  break;
}
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
node node_modules\vitest\vitest.mjs run src/stores/__tests__/events.test.ts
```

Expected: 两个用例 PASS。

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/services/events.ts frontend/src/stores/session.ts frontend/src/stores/events.ts frontend/src/stores/__tests__/events.test.ts
git commit -m "feat: 前端协议与 store 支持 ASSISTANT 中间助手消息"
```

## Task 3: MessageItem 弱化展示中间助手消息

**Files:**
- Modify: `frontend/src/components/MessageItem.vue`
- Test: `frontend/src/components/__tests__/MessageItem.test.ts`

- [ ] **Step 1: 写失败测试**

在 `frontend/src/components/__tests__/MessageItem.test.ts` 的 describe 内追加：

```ts
it("interim 助手消息渲染「过程」标签与弱化气泡", () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mountItem(
    makeMessage({ role: "assistant", content: "我先查一下仓库", interim: true }),
    pinia,
  );
  expect(w.find(".interim-tag").exists()).toBe(true);
  expect(w.find(".assist-bubble.interim").exists()).toBe(true);
  w.unmount();
});
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
node node_modules\vitest\vitest.mjs run src/components/__tests__/MessageItem.test.ts
```

Expected: 新用例 FAIL（无 `.interim-tag`）。

- [ ] **Step 3: 最小实现**

`frontend/src/components/MessageItem.vue` 助手分支模板改为：

```html
<div class="bubble assist-bubble" :class="{ interim: message.interim }">
  <div v-if="message.interim" class="interim-tag mono">◈ 过程</div>
  <div v-if="showTopic" class="tname serif">{{ topicLine }}</div>
  <div v-if="message.memoryInject" class="inject-tag">◈ {{ message.memoryInject.label }}</div>
  <MarkdownContent :source="message.content" />
</div>
```

CSS 追加：

```css
.assist-bubble.interim {
  background: transparent;
  border-style: dashed;
}
.interim-tag {
  display: inline-flex;
  align-items: center;
  margin-bottom: 6px;
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.05em;
}
```

- [ ] **Step 4: 运行测试确认通过**

```powershell
node node_modules\vitest\vitest.mjs run src/components/__tests__/MessageItem.test.ts
```

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/components/MessageItem.vue frontend/src/components/__tests__/MessageItem.test.ts
git commit -m "feat: 中间助手消息弱化样式（过程标签 + 虚线气泡）"
```

## Task 4: MessageStream 三圆点打字指示器

**Files:**
- Modify: `frontend/src/components/MessageStream.vue`
- Test: `frontend/src/views/__tests__/ConversationView.test.ts`

- [ ] **Step 1: 写失败测试**

在 `frontend/src/views/__tests__/ConversationView.test.ts` 顶部 import 加 `nextTick`：

```ts
import { nextTick } from "vue";
```

在 describe 内追加：

```ts
it("等待回复时用户消息下方显示三圆点跳动指示，收到助手消息后消失", async () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mountView(pinia, makeRouter());
  await flushPromises();
  const session = useSessionStore();
  session.pushUser("hello");
  session.turnStarted();
  await nextTick();
  expect(w.find(".typing-bubble").exists()).toBe(true);
  expect(w.findAll(".typing-bubble .dot").length).toBe(3);
  session.pushAssistant("final answer");
  session.turnEnded();
  await nextTick();
  expect(w.find(".typing-bubble").exists()).toBe(false);
  w.unmount();
});
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
node node_modules\vitest\vitest.mjs run src/views/__tests__/ConversationView.test.ts
```

Expected: 新用例 FAIL（无 `.typing-bubble`）。

- [ ] **Step 3: 最小实现**

`frontend/src/components/MessageStream.vue` script 内新增：

```ts
/** 打字指示器：turn 运行中且当前轮次尚无助手消息时显示三圆点 */
const showTyping = computed(() => {
  if (!session.turnRunning) return false;
  const last = turns.value[turns.value.length - 1];
  if (!last) return false;
  return !last.items.some((m) => m.role === "assistant");
});
```

把 `watch(() => session.messages.length, ...)` 的实现改为滚到容器最底（把打字指示器也滚入视野）：

```ts
watch(
  () => session.messages.length,
  async () => {
    if (followBottom.value) {
      await nextTick();
      containerRef.value?.scrollTo({ top: containerRef.value.scrollHeight });
    }
  },
);
```

把 `watch(() => session.turnRunning, ...)` 改为：

```ts
watch(
  () => session.turnRunning,
  async (running) => {
    if (running) {
      followBottom.value = true;
      await nextTick();
      containerRef.value?.scrollTo({ top: containerRef.value.scrollHeight });
    }
  },
);
```

模板在 `.spacer` div 之后、`.empty` 之前插入：

```html
<div v-if="showTyping" class="typing" role="status" aria-label="QIO 正在回复">
  <div class="typing-bubble">
    <span class="dot"></span><span class="dot"></span><span class="dot"></span>
  </div>
</div>
```

CSS 追加：

```css
/* ---- 打字指示器：三圆点来回跳动 ---- */
.typing {
  display: flex;
  justify-content: flex-start;
  margin-top: 4px;
}
.typing-bubble {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 12px 16px;
  border-radius: 14px 14px 14px 4px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
}
.typing-bubble .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-secondary);
  animation: typing-bounce 1.2s infinite ease-in-out;
}
.typing-bubble .dot:nth-child(2) {
  animation-delay: 0.15s;
}
.typing-bubble .dot:nth-child(3) {
  animation-delay: 0.3s;
}
@keyframes typing-bounce {
  0%, 60%, 100% {
    transform: translateY(0);
    opacity: 0.4;
  }
  30% {
    transform: translateY(-4px);
    opacity: 1;
  }
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
git commit -m "feat: 等待回复时三圆点打字指示器（收到助手消息后消失）"
```

## Task 5: 删除 Composer 记忆强度滑块

**Files:**
- Modify: `frontend/src/components/Composer.vue`
- Test: `frontend/src/components/__tests__/Composer.test.ts`

- [ ] **Step 1: 改失败测试（行为变更：滑块应不存在）**

`frontend/src/components/__tests__/Composer.test.ts` 第一个用例改为：

```ts
it("渲染：header 拖拽把手 + textarea + 发送按钮（记忆滑块已移除）", async () => {
  const { w } = await mountComposer();
  expect(w.find(".composer").exists()).toBe(true);
  expect(w.find(".topicbar.fw-handle").exists()).toBe(true);
  expect(w.find("textarea.qio-input").exists()).toBe(true);
  expect(w.find(".send-btn").exists()).toBe(true);
  expect(w.find(".strength-slider").exists()).toBe(false);
  w.unmount();
});
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
node node_modules\vitest\vitest.mjs run src/components/__tests__/Composer.test.ts
```

Expected: 第一个用例 FAIL（`.strength-slider` 仍存在）。

- [ ] **Step 3: 最小实现**

`frontend/src/components/Composer.vue`：
- 删除 `import QSlider from "./ui/QSlider.vue";`
- 删除 `const memoryStrength = ref(0.38);` 及上方注释
- 删除模板中整个 `.mem-row` div
- 删除 CSS 中 `.mem-row`、`.mem-label`、`.strength-slider`、`.mem-val` 四段

- [ ] **Step 4: 运行测试确认通过**

```powershell
node node_modules\vitest\vitest.mjs run src/components/__tests__/Composer.test.ts
```

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/components/Composer.vue frontend/src/components/__tests__/Composer.test.ts
git commit -m "feat: 删除 Composer 记忆强度滑块"
```

## Task 6: 全量验证

- [ ] **Step 1: 前端全量测试**

```powershell
node node_modules\vitest\vitest.mjs run
```

Expected: 129+ 用例全绿（新增约 6 个用例）。注意：chunk>500kB 警告不影响 exit code，用 `$LASTEXITCODE` 复核。

- [ ] **Step 2: 前端类型与构建**

```powershell
node node_modules\vue-tsc\bin\vue-tsc.js --noEmit
node node_modules\vite\bin\vite.js build
```

Expected: 无类型错误、构建成功。

- [ ] **Step 3: 后端全量测试**

```powershell
$env:PYTHONPATH='C:\Users\zxy\Documents\Front agent\qio\backend\src'; & 'C:\Users\zxy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest
```

Expected: 全绿。

- [ ] **Step 4: 重启后端 + 手动冒烟（IAB 用 ?fresh=N 强制刷新）**

按 AGENTS.md 环境注意：改后端必须重启 uvicorn（参考 `qio/scripts/e2e_up.py`）；前端刷新用 `http://127.0.0.1:5199/?fresh=1#/`。手动验证：发送消息 → 用户气泡先落屏 + 三圆点跳动；工具卡出现；最终答复替换指示器；interim 评论以虚线「过程」气泡展示。

## Task 7（后续，独立调试任务）: 排查星球页右侧边栏开合时左侧星球「闪一下」

完成 Task 1-6 后，按 superpowers:systematic-debugging 流程单独处理：
1. 读 `PlanetView.vue` 的 `.panel` width 过渡（0.32s）与 `ResizeObserver → planet.resize()` 链路；
2. 读 `usePlanetScene.ts` 的 `resize()`（`camera.aspect` 更新 + `renderer.setSize`）与 `init()` 的 renderer 构造参数；
3. 形成根因假设（疑似：`renderer.setSize` 默认 `updateStyle=true` 会把 canvas 的 style.width/height 钉死为像素值，与 flex 布局/宽度过渡冲突，或 RO 在过渡期间触发 setSize 导致缓冲重建闪帧），最小改动验证后再修。
