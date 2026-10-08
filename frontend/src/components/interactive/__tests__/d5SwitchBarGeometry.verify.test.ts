/**
 * 独立验收 D5-F：窄窗口下切换条必须有真实布局（契约 §11.7）。
 *
 * 由独立验收子智能体 D 编写，**不修改任何产品代码**。
 * 场景：480px 窄窗口里用户关掉一个面板（只剩一个面板）→ 切换条仍然要占住自己那一行，
 * **不许压在剩下的面板上**（面板底边必须让开切换条）。
 * jsdom 不排版，所以这里量的是组件写进 DOM 的几何变量与切换条自己的定位；
 * 「真实矩形与 computed style」由浏览器探针（d5-recovery-probe.mjs）量。
 *
 * 标注：【组件/DOM（几何变量）】
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount } from "@vue/test-utils";
import IntentBatchTray from "../IntentBatchTray.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { OVERLAY_EDGE, OVERLAY_SWITCH_BAR_HEIGHT } from "../../../interactive/overlayLayout";

const VIEWPORT = { width: 480, height: 600 };
const STAGE = { top: 0, height: 600 };
const TOOLBAR = { top: 520, height: 60 };
const TOGGLE = { top: 540, height: 34 };

interface FakeRect {
  top: number;
  height: number;
}

function rectLike(r: FakeRect): DOMRect {
  return {
    x: 0,
    y: r.top,
    top: r.top,
    bottom: r.top + r.height,
    left: 0,
    right: VIEWPORT.width,
    width: VIEWPORT.width,
    height: r.height,
    toJSON: () => ({}),
  } as unknown as DOMRect;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  Object.defineProperty(window, "innerWidth", { value: VIEWPORT.width, configurable: true });
  Object.defineProperty(window, "innerHeight", { value: VIEWPORT.height, configurable: true });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe("场景 8：480px 关闭一个面板后的切换条布局（§11.7）", () => {
  it("【组件/DOM】切换条要给剩下的面板预留真实高度", async () => {
    // 底部工具栏与聊天入口按钮：几何控制器只量这些不受浮层尺寸影响的元素
    const toolbar = document.createElement("div");
    toolbar.setAttribute("data-im", "board-toolbar");
    const toggle = document.createElement("button");
    toggle.setAttribute("data-im", "chat-toggle");
    document.body.append(toolbar, toggle);

    // 舞台是组件挂载点本身（真实应用里是板面舞台），挂载后再把它的矩形登记进来
    const rects = new Map<Element, FakeRect>([
      [toolbar, TOOLBAR],
      [toggle, TOGGLE],
    ]);
    const native = Element.prototype.getBoundingClientRect;
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      const fake = rects.get(this);
      if (fake) return rectLike(fake);
      return native.call(this);
    });

    const store = useInteractiveStore();
    // 用户先开过两个面板，然后关掉了批量列表：480px 下空间依然不足（切换条必须常驻）
    store.chatOpen = true;
    store.batchOpen = false;

    vi.useFakeTimers();
    const wrapper = mount(IntentBatchTray, { attachTo: document.body });
    const tray = wrapper.find('[data-im="batch-area"]').element;
    const stage = tray.parentElement as HTMLElement;
    expect(stage, "找不到板面舞台（组件挂载点）").toBeTruthy();
    rects.set(stage, STAGE);
    // 第一次测量用的是还没有尺寸的舞台：这里让几何控制器按真实舞台再量一次
    await vi.advanceTimersByTimeAsync(50);
    await wrapper.vm.$nextTick();

    const bar = wrapper.find('[data-im="overlay-switch"]');
    expect(bar.exists(), "480px 下关掉一个面板后，切换条整个消失了").toBe(true);

    const bottom = Number.parseFloat((bar.element as HTMLElement).style.bottom || "NaN");
    const height = Number.parseFloat((bar.element as HTMLElement).style.height || "NaN");
    expect(Number.isFinite(bottom), "切换条没有真实定位（没有 bottom）").toBe(true);
    expect(height, "切换条没有真实高度").toBe(OVERLAY_SWITCH_BAR_HEIGHT);

    const barTop = VIEWPORT.height - bottom - height;
    const chatMaxHeight = Number.parseFloat(stage.style.getPropertyValue("--im-geo-chat-max-h") || "NaN");
    expect(Number.isFinite(chatMaxHeight), "几何控制器没有给聊天面板写高度上限").toBe(true);
    const chatBottom = STAGE.top + OVERLAY_EDGE + chatMaxHeight;

    expect(
      chatBottom,
      "切换条压在剩下的面板上：面板底边 " + chatBottom + "px，切换条顶边 " + barTop + "px（没有为切换条预留高度）",
    ).toBeLessThanOrEqual(barTop + 0.5);
  });
});
