/**
 * C01：新版互动对话窗 → 跨浮层几何桥（chatFrameState）的生产者定向测试。
 *
 * 2026-11 底部操作区重做后，对话卡就是实际可见面板本身（操作行 + 消息区 + 输入区），
 * 因此发布口径是「展开时消息区 + 操作行 + 输入区的外接矩形（屏幕坐标）」；
 * 不再按外层全宽容器发布，也不会在收起时留下幽灵矩形。
 *
 * 这里验的是**生产者契约**（消费方 BoardCanvas 的行为由 irl-c-canvas 等既有测试负责）：
 * - 展开：发布真实可见矩形；收起 / 卸载发布 null；
 * - 高度 / 窗口变化后重新测量并更新；
 * - 量不到真实几何（jsdom 默认零矩形）时发布 null，不编造假框。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { defineComponent, h, nextTick } from "vue";
import InteractiveConversation from "../InteractiveConversation.vue";
import {
  chatObstacleFrame,
  liveChatCollapsed,
  readChatFrame,
  resetChatFrameState,
} from "../../../../interactive/chatFrameState";

const StreamStub = defineComponent({
  name: "MessageStream",
  setup() {
    return () => h("div", { "data-stub": "stream" });
  },
});

const EntriesStub = defineComponent({
  name: "ComposerEntries",
  setup() {
    return () => h("div", { "data-stub": "entries" });
  },
});

const ComposerStub = defineComponent({
  name: "Composer",
  props: { chatBinding: { type: Object, default: undefined } },
  setup() {
    return () => h("div", { "data-stub": "composer" });
  },
});

let wrapper: VueWrapper | null = null;

function mountConversation(props: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  wrapper = mount(InteractiveConversation, {
    props,
    global: {
      plugins: [pinia],
      stubs: { MessageStream: StreamStub, ComposerEntries: EntriesStub, Composer: ComposerStub },
    },
    attachTo: document.body,
  });
  return wrapper;
}

interface FakeBox {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** 给真实 DOM 节点装一个可控的屏幕矩形（jsdom 不做布局，默认全是 0）。 */
function stubBox(element: Element | null, box: FakeBox | null): void {
  if (!element) return;
  element.getBoundingClientRect = () =>
    box ? new DOMRect(box.left, box.top, box.width, box.height) : new DOMRect(0, 0, 0, 0);
}

async function waitForPublish(): Promise<void> {
  window.dispatchEvent(new Event("resize"));
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, 45));
  await nextTick();
}

const PARTS = {
  stream: { left: 300, top: 200, width: 860, height: 300 },
  rail: { left: 300, top: 508, width: 860, height: 36 },
  entries: { left: 320, top: 544, width: 820, height: 32 },
  composer: { left: 312, top: 576, width: 836, height: 60 },
} as const;

/** 把页面里那几块真实可见区域量成可控数字；外层根容器故意给一个更大的框。 */
function stubVisibleParts(
  w: VueWrapper,
  parts: Partial<Record<keyof typeof PARTS, FakeBox | null>> = PARTS,
): void {
  const root = w.get('[data-ic="conversation"]').element;
  stubBox(root, { left: 20, top: 100, width: 1200, height: 700 });
  stubBox(root.querySelector("[data-ic-stream]"), parts.stream ?? null);
  stubBox(root.querySelector("[data-ic-rail]"), parts.rail ?? null);
  stubBox(root.querySelector("[data-ic-entries]"), parts.entries ?? null);
  stubBox(root.querySelector("[data-ic-composer]"), parts.composer ?? null);
}

beforeEach(() => {
  resetChatFrameState();
  localStorage.clear();
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  resetChatFrameState();
  document.body.innerHTML = "";
});

describe("展开时发布真实可见矩形", () => {
  it("消息区 + 操作行 + 输入区的外接矩形（屏幕坐标）", async () => {
    const w = mountConversation({ expanded: true });
    stubVisibleParts(w);
    await waitForPublish();
    expect(readChatFrame()).toEqual({ x: 300, y: 200, w: 860, h: 436 });
    expect(liveChatCollapsed.value).toBe(false);
    expect(chatObstacleFrame()).toEqual({ x: 300, y: 200, w: 860, h: 436 });
  });

  it("高度调整后重新测量并更新矩形（展开区域变高）", async () => {
    const w = mountConversation({ expanded: true });
    stubVisibleParts(w);
    await waitForPublish();
    const before = readChatFrame();
    expect(before).not.toBeNull();

    stubVisibleParts(w, {
      ...PARTS,
      stream: { left: 300, top: 100, width: 860, height: 420 },
    });
    await waitForPublish();
    const after = readChatFrame();
    expect(after).not.toBeNull();
    expect(after?.y).toBe(100);
    expect(after?.h).toBeGreaterThan(before?.h ?? 0);
  });

  it("收起后发布 null（板面恢复可用），再次展开重新发布", async () => {
    const w = mountConversation({ expanded: true });
    stubVisibleParts(w);
    await waitForPublish();
    expect(readChatFrame()).not.toBeNull();

    await w.get('[data-ic-toggle]').trigger("click");
    await waitForPublish();
    expect(readChatFrame()).toBeNull();
    expect(liveChatCollapsed.value).toBe(true);
    expect(chatObstacleFrame()).toBeNull();

    await w.get('[data-ic-toggle]').trigger("click");
    await waitForPublish();
    expect(readChatFrame()).not.toBeNull();
  });

  it("卸载后桥里不留幽灵矩形", async () => {
    const w = mountConversation({ expanded: true });
    stubVisibleParts(w);
    await waitForPublish();
    expect(readChatFrame()).not.toBeNull();
    w.unmount();
    wrapper = null;
    expect(readChatFrame()).toBeNull();
  });

  it("默认收起：挂载后不发布矩形（不按输入框单独占一块不存在的大区域）", async () => {
    const w = mountConversation();
    stubVisibleParts(w, { stream: null });
    await waitForPublish();
    expect(readChatFrame()).toBeNull();
  });

  it("量不到真实几何（jsdom 零矩形）时发布 null，不编造假显示框", async () => {
    const w = mountConversation({ expanded: true });
    stubVisibleParts(w, { stream: null, rail: null, entries: null, composer: null });
    await waitForPublish();
    expect(readChatFrame()).toBeNull();
  });
});