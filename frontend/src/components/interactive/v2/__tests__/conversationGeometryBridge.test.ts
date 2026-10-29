/**
 * C01：新版互动对话窗 → 跨浮层几何桥（chatFrameState）的生产者定向测试。
 *
 * 基线缺陷：BoardCanvas 消费 chatObstacleFrame()，但唯一生产者是未挂载的旧 ChatDock，
 * 新对话窗从不发布 → 桥永远读到 null（卡片展开 / 添加菜单的可用区域算错）。
 *
 * 这里验的是**生产者契约**（消费方 BoardCanvas 的行为由 irl-c-canvas 等既有测试负责）：
 * - 展开：发布「消息面板 + 输入区」的真实外接矩形（屏幕坐标）；
 * - 只按可见内容，不按外层全宽容器（否则整块板面会被误判成不可用）；
 * - 收起 / 卸载发布 null（不留幽灵障碍）；
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
  liveChatFrame,
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
  if (!element) throw new Error("要测量的元素不存在");
  if (!box) {
    element.getBoundingClientRect = () => new DOMRect(0, 0, 0, 0);
    return;
  }
  element.getBoundingClientRect = () =>
    new DOMRect(box.left, box.top, box.width, box.height);
}

async function waitForPublish(): Promise<void> {
  window.dispatchEvent(new Event("resize"));
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, 40));
  await nextTick();
}

const PARTS = {
  stream: { left: 300, top: 200, width: 860, height: 300 },
  bar: { left: 302, top: 512, width: 856, height: 24 },
  entries: { left: 320, top: 540, width: 820, height: 32 },
  toolbar: { left: 330, top: 576, width: 800, height: 32 },
  composer: { left: 350, top: 612, width: 760, height: 60 },
} as const;

/** 把页面里那几块真实可见区域量成可控数字；外层根容器故意给一个更大的框。 */
function stubVisibleParts(w: VueWrapper, parts: Partial<Record<keyof typeof PARTS, FakeBox>> = PARTS): void {
  const root = w.get('[data-ic="conversation"]').element;
  stubBox(root, { left: 20, top: 100, width: 1400, height: 700 });
  stubBox(root.querySelector("[data-ic-stream]"), parts.stream ?? null);
  stubBox(root.querySelector("[data-ic-bar]"), parts.bar ?? null);
  stubBox(root.querySelector(".ic-entries"), parts.entries ?? null);
  stubBox(root.querySelector("[data-ic-toolbar]"), parts.toolbar ?? null);
  stubBox(root.querySelector(".ic-composer"), parts.composer ?? null);
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
  it("消息面板 + 输入区的外接矩形（屏幕坐标），不是外层全宽容器", async () => {
    const w = mountConversation({ expanded: true, height: 520 });
    stubVisibleParts(w);
    await waitForPublish();

    // 期望：左=300、上=200、右=1160、下=672
    expect(readChatFrame()).toEqual({ x: 300, y: 200, w: 860, h: 472 });
    // 消费方拿到的是同一份几何（屏幕坐标）
    expect(chatObstacleFrame()).toEqual({ x: 300, y: 200, w: 860, h: 472 });
    expect(liveChatCollapsed.value).toBe(false);
    // 外层容器宽 1400，发布宽度远小于它 —— 证明没有按全宽容器误判
    expect(readChatFrame()!.w).toBeLessThan(1400);
  });

  it("高度调整后重新测量并更新矩形（展开区域变高）", async () => {
    const w = mountConversation({ expanded: true, height: 520 });
    stubVisibleParts(w);
    await waitForPublish();
    expect(readChatFrame()!.h).toBe(472);

    // 用户把对话拖高 200px：消息面板随之上移、变高
    const next = {
      ...PARTS,
      stream: { left: 300, top: 0, width: 860, height: 500 },
    };
    stubVisibleParts(w, next);
    await w.setProps({ height: 720 });
    await waitForPublish();

    expect(readChatFrame()).toEqual({ x: 300, y: 0, w: 860, h: 672 });
  });

  it("收起后发布 null（板面恢复可用），再次展开重新发布", async () => {
    const w = mountConversation({ expanded: true, height: 520 });
    stubVisibleParts(w);
    await waitForPublish();
    expect(chatObstacleFrame()).not.toBeNull();

    await w.get('[data-ic-toggle]').trigger("click");
    await waitForPublish();
    expect(w.get('[data-ic="conversation"]').attributes("data-ic-expanded")).toBe("false");
    expect(readChatFrame()).toBeNull();
    expect(chatObstacleFrame()).toBeNull();
    expect(liveChatCollapsed.value).toBe(true);

    await w.get('[data-ic-toggle]').trigger("click");
    await waitForPublish();
    expect(chatObstacleFrame()).toEqual({ x: 300, y: 200, w: 860, h: 472 });
  });

  it("卸载后桥里不留幽灵矩形", async () => {
    const w = mountConversation({ expanded: true, height: 520 });
    stubVisibleParts(w);
    await waitForPublish();
    expect(liveChatFrame.value).not.toBeNull();

    w.unmount();
    wrapper = null;
    expect(liveChatFrame.value).toBeNull();
    expect(chatObstacleFrame()).toBeNull();
  });

  it("默认收起：挂载后不发布矩形（不按输入框单独占一块不存在的大区域）", async () => {
    mountConversation();
    await waitForPublish();
    expect(readChatFrame()).toBeNull();
    expect(chatObstacleFrame()).toBeNull();
    expect(liveChatCollapsed.value).toBe(true);
  });

  it("量不到真实几何（jsdom 零矩形）时发布 null，不编造假显示框", async () => {
    const w = mountConversation({ expanded: true, height: 520 });
    // 不装任何矩形：所有 getBoundingClientRect 都是 0
    await waitForPublish();
    expect(readChatFrame()).toBeNull();
    expect(chatObstacleFrame()).toBeNull();
  });
});
