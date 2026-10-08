/**
 * 独立验收 D2-C：480px 切换条与面板几何（契约 §11.7）。
 *
 * 由独立验收子智能体 D2 编写，**不修改任何产品代码**。
 * 两条互补的反例：
 * 1.【纯几何】按实机量到的 480×600（工具栏顶边 484、舞台 top 48）直接算一遍：
 *    切换条必须在视口内；两个面板的最坏矩形都不许压在切换条上；关掉一个面板不改变同一布局状态；
 * 2.【组件/DOM】只开批量列表时，写进舞台的 --im-geo-batch-max-h 也要为切换条留出真实高度。
 *
 * 标注：【纯几何】【组件/DOM（几何变量）】；真实矩形与 computed style 由探针在真实浏览器里量。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount } from "@vue/test-utils";
import IntentBatchTray from "../IntentBatchTray.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import {
  OVERLAY_EDGE,
  OVERLAY_GAP,
  OVERLAY_SWITCH_BAR_HEIGHT,
  intersectionArea,
  overlayRects,
  planOverlayGeometry,
  type OverlayInput,
} from "../../../interactive/overlayLayout";

/** 实机 480×600 量到的数字（工具栏 rect top=484；舞台是浮层的定位父级） */
const VIEWPORT = { width: 480, height: 600 };
const STAGE = { top: 48, height: 552 };
const TOOLBAR_TOP = 484;
const CHAT_MIN = { width: 320, height: 240 };
const BATCH_MIN = { width: 300, height: 220 };

function inputOf(chatOpen: boolean, batchOpen: boolean, switched?: boolean): OverlayInput {
  return {
    viewport: VIEWPORT,
    stage: STAGE,
    toolbarTop: TOOLBAR_TOP,
    chatOpen,
    batchOpen,
    chatMin: CHAT_MIN,
    batchMin: BATCH_MIN,
    switched,
  };
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  // jsdom 默认是 1024×768，不覆盖的话 480px 的窄窗口判定（cramped）根本不会成立
  Object.defineProperty(window, "innerWidth", { value: VIEWPORT.width, configurable: true });
  Object.defineProperty(window, "innerHeight", { value: VIEWPORT.height, configurable: true });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe("D2 反例：480×600 的切换条与面板几何（§11.7）", () => {
  it("【纯几何】两个面板都请求展开：切换条在视口内，两个面板的最坏矩形都不压在它上面", () => {
    const plan = planOverlayGeometry(inputOf(true, true));
    expect(plan.mode, "480px 放不下两个面板，却算成了并排/上下叠放").toBe("switched");

    const bar = plan.switchBar;
    expect(bar.height, "切换条没有真实高度（等于没有为它预留位置）").toBe(OVERLAY_SWITCH_BAR_HEIGHT);
    expect(bar.y, "切换条跑到视口上方").toBeGreaterThanOrEqual(STAGE.top + OVERLAY_EDGE - 0.5);
    expect(bar.y + bar.height, "切换条底部越过了底部工具栏").toBeLessThanOrEqual(TOOLBAR_TOP);
    expect(bar.y + bar.height, "切换条底部越过了视口").toBeLessThanOrEqual(VIEWPORT.height);

    const rects = overlayRects(inputOf(true, true), plan);
    const chatBottom = rects.chat.y + rects.chat.height;
    const batchBottom = rects.batch.y + rects.batch.height;
    expect(chatBottom, "聊天面板的最坏底边压在切换条顶边上").toBeLessThanOrEqual(bar.y + 0.5);
    expect(batchBottom, "批量列表的最坏底边压在切换条顶边上").toBeLessThanOrEqual(bar.y + 0.5);
    expect(intersectionArea(rects.chat, bar), "聊天面板与切换条相交").toBe(0);
    expect(intersectionArea(rects.batch, bar), "批量列表与切换条相交").toBe(0);
    expect(rects.chat.y, "聊天面板顶边跑到舞台可用区之外").toBeGreaterThanOrEqual(STAGE.top + OVERLAY_EDGE - 0.5);
    expect(rects.batch.y, "批量列表顶边跑到舞台可用区之外").toBeGreaterThanOrEqual(STAGE.top + OVERLAY_EDGE - 0.5);
  });

  it("【纯几何】关掉一个面板（或两个都关）不改变「需要切换显示」这个布局状态：切换条该占的那一行始终留着", () => {
    const both = planOverlayGeometry(inputOf(true, true, true));
    const onlyChat = planOverlayGeometry(inputOf(true, false, true));
    const onlyBatch = planOverlayGeometry(inputOf(false, true, true));
    const none = planOverlayGeometry(inputOf(false, false, true));

    for (const [name, plan] of [
      ["只开聊天", onlyChat],
      ["只开批量", onlyBatch],
      ["两个都关", none],
    ] as const) {
      expect(plan.mode, name + "：布局状态被改成了 " + plan.mode).toBe("switched");
      expect(plan.switchBar.height, name + "：切换条那一行没有被预留").toBe(OVERLAY_SWITCH_BAR_HEIGHT);
      expect(plan.switchBar.y, name + "：切换条位置与两个都开时不一致").toBe(both.switchBar.y);
      expect(plan.chatMaxHeight, name + "：聊天面板可用高度与两个都开时不一致").toBe(both.chatMaxHeight);
      expect(plan.batchMaxHeight, name + "：批量列表可用高度与两个都开时不一致").toBe(both.batchMaxHeight);
    }
  });
});

describe("D2 反例：只开批量列表时也要给切换条留高度（§11.7）", () => {
  it("【组件/DOM】--im-geo-batch-max-h 算出来的面板底边不许越过切换条顶边", async () => {
    const toolbar = document.createElement("div");
    toolbar.setAttribute("data-im", "board-toolbar");
    const toggle = document.createElement("button");
    toggle.setAttribute("data-im", "chat-toggle");
    document.body.append(toolbar, toggle);

    interface FakeRect {
      top: number;
      height: number;
    }
    const rectLike = (r: FakeRect): DOMRect =>
      ({
        x: 0,
        y: r.top,
        top: r.top,
        bottom: r.top + r.height,
        left: 0,
        right: VIEWPORT.width,
        width: VIEWPORT.width,
        height: r.height,
        toJSON: () => ({}),
      }) as unknown as DOMRect;
    const rects = new Map<Element, FakeRect>([
      [toolbar, { top: TOOLBAR_TOP, height: 60 }],
      [toggle, { top: TOOLBAR_TOP + 20, height: 34 }],
    ]);
    const native = Element.prototype.getBoundingClientRect;
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      const fake = rects.get(this);
      if (fake) return rectLike(fake);
      return native.call(this);
    });

    const store = useInteractiveStore();
    // 用户只开着批量列表（聊天是收起的）：480px 下切换条仍然要占住自己那一行
    store.chatOpen = false;
    store.batchOpen = true;

    vi.useFakeTimers();
    const wrapper = mount(IntentBatchTray, { attachTo: document.body });
    const tray = wrapper.find('[data-im="batch-area"]').element;
    const stage = tray.parentElement as HTMLElement;
    expect(stage, "找不到板面舞台（组件挂载点）").toBeTruthy();
    rects.set(stage, { top: STAGE.top, height: STAGE.height });
    await vi.advanceTimersByTimeAsync(50);
    await wrapper.vm.$nextTick();

    const bar = wrapper.find('[data-im="overlay-switch"]');
    expect(bar.exists(), "只开批量列表时切换条整个消失了").toBe(true);
    const barBottom = Number.parseFloat((bar.element as HTMLElement).style.bottom || "NaN");
    const barHeight = Number.parseFloat((bar.element as HTMLElement).style.height || "NaN");
    expect(Number.isFinite(barBottom) && Number.isFinite(barHeight), "切换条没有真实定位/高度").toBe(true);
    const barTop = VIEWPORT.height - barBottom - barHeight;

    const batchMaxHeight = Number.parseFloat(stage.style.getPropertyValue("--im-geo-batch-max-h") || "NaN");
    expect(Number.isFinite(batchMaxHeight), "几何控制器没有给批量列表写高度上限").toBe(true);
    const batchBottom = STAGE.top + OVERLAY_EDGE + batchMaxHeight;
    expect(
      batchBottom,
      "切换条压在批量列表上：列表底边 " + batchBottom + "px，切换条顶边 " + barTop + "px（间距应为 " + OVERLAY_GAP + "px）",
    ).toBeLessThanOrEqual(barTop + 0.5);
  });
});
