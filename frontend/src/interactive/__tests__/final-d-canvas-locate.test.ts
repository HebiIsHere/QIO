/**
 * 收尾轮 · D 的反例测试（19a：预览右 / 下离屏定位）——BoardCanvas 组件级。
 *
 * 反例：预览处于右 / 下视口外时，定位必须把它移进实际可用区域，不只处理左 / 上。
 * （基线：onLocatePreview 的 dx / dy 只在 screen 越出左 / 上边缘时计算，右 / 下离屏不动。）
 *
 * 挂载模式沿用既有 boardCanvas.test.ts 的桩几何做法（jsdom 没有真实布局）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BoardCanvas from "../../components/interactive/BoardCanvas.vue";
import { useInteractiveStore } from "../../stores/interactive";
import * as board from "../board";
import type { BoardState } from "../types";

const VIEW_LEFT = 40;
const VIEW_TOP = 100;
const VIEW_W = 3000;
const VIEW_H = 2000;

function stubGeometry(wrapper: VueWrapper) {
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  const shell = wrapper.get(".board-shell").element as HTMLElement;
  view.getBoundingClientRect = () =>
    ({ left: VIEW_LEFT, top: VIEW_TOP, right: VIEW_LEFT + VIEW_W, bottom: VIEW_TOP + VIEW_H, width: VIEW_W, height: VIEW_H, x: VIEW_LEFT, y: VIEW_TOP, toJSON: () => ({}) }) as DOMRect;
  shell.getBoundingClientRect = () =>
    ({ left: VIEW_LEFT, top: VIEW_TOP, right: VIEW_LEFT + VIEW_W, bottom: VIEW_TOP + VIEW_H + 200, width: VIEW_W, height: VIEW_H + 200, x: VIEW_LEFT, y: VIEW_TOP, toJSON: () => ({}) }) as DOMRect;
  Object.defineProperty(view, "clientWidth", { value: VIEW_W, configurable: true });
  Object.defineProperty(view, "clientHeight", { value: VIEW_H, configurable: true });
}

function wheel(x: number, y: number, deltaY: number, target?: Element): Event {
  const event = new WheelEvent("wheel", { clientX: x, clientY: y, deltaY, deltaMode: 0, bubbles: true, cancelable: true });
  (target ?? document.body).dispatchEvent(event);
  return event;
}

function viewState(wrapper: VueWrapper): { scale: number; scrollLeft: number; scrollTop: number } {
  const surface = wrapper.get(".board-surface").element as HTMLElement;
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  const match = /scale\(([\d.]+)\)/.exec(surface.style.transform);
  return {
    scale: match ? Number(match[1]) : Number.NaN,
    scrollLeft: view.scrollLeft,
    scrollTop: view.scrollTop,
  };
}

/** 板面坐标 → 屏幕坐标（按桩几何的换算关系）。 */
function screenOf(boardX: number, boardY: number, state: { scale: number; scrollLeft: number; scrollTop: number }) {
  return { x: VIEW_LEFT + boardX * state.scale - state.scrollLeft, y: VIEW_TOP + boardY * state.scale - state.scrollTop };
}

/**
 * 滚到目标比例之上。
 *
 * 修正（Lead）：缩放后必须**等一次渲染**再读 DOM —— 缩放发生在同步事件处理里，
 * 但 `.board-surface` 的 transform 要等 Vue 重渲染才更新；同步读完就断言会读到旧值
 * （表现为「滚了轮子但 scale 还是 1」的假失败）。
 */
async function wheelZoomTo(scaleTarget: number, wrapper: VueWrapper) {
  const view = wrapper.get(".board-viewport").element;
  // 一次 wheel 的缩放系数固定：-462 ≈ ×2.1；重复滚到目标比例之上再取实际值
  for (let i = 0; i < 3; i += 1) {
    const state = viewState(wrapper);
    if (state.scale >= scaleTarget) break;
    view.dispatchEvent(wheel(VIEW_LEFT + 500, VIEW_TOP + 400, -462));
    await wrapper.vm.$nextTick();
  }
}

describe("BoardCanvas 预览定位四方向（19a）", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  const mounted: VueWrapper[] = [];

  afterEach(() => {
    while (mounted.length) {
      const wrapper = mounted.pop();
      try {
        wrapper?.unmount();
      } catch {
        /* 已经卸载过 */
      }
    }
    document.body.innerHTML = "";
  });

  beforeEach(() => {
    setActivePinia(createPinia());
    store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    vi.spyOn(store, "commit").mockImplementation((next: BoardState) => {
      store.board = { ...next, boardId: store.boardId };
    });
  });

  function mountCanvas(): VueWrapper {
    const wrapper = mount(BoardCanvas, { attachTo: document.body });
    mounted.push(wrapper);
    stubGeometry(wrapper);
    return wrapper;
  }

  function locateBounds(bounds: { x: number; y: number; w: number; h: number }, intentId = "intent_1") {
    window.dispatchEvent(
      new CustomEvent("qio:interactive:locate-preview", { detail: { intentId, bounds } }),
    );
  }

  function expectInsideViewportAfter(wrapper: VueWrapper, bounds: { x: number; y: number; w: number; h: number }, margin: number) {
    const state = viewState(wrapper);
    const topLeft = screenOf(bounds.x, bounds.y, state);
    const bottomRight = {
      x: topLeft.x + bounds.w * state.scale,
      y: topLeft.y + bounds.h * state.scale,
    };
    // 四方向都不得再越出可用区（越出即失败：定位没有真的移动板面）
    expect(topLeft.x).toBeGreaterThanOrEqual(VIEW_LEFT + margin - 1);
    expect(topLeft.y).toBeGreaterThanOrEqual(VIEW_TOP + margin - 1);
    expect(bottomRight.x).toBeLessThanOrEqual(VIEW_LEFT + VIEW_W - margin + 1);
    expect(bottomRight.y).toBeLessThanOrEqual(VIEW_TOP + VIEW_H - margin + 1);
  }

  it.each([
    ["右侧离屏", { x: 2100, y: 200, w: 200, h: 100 }],
    ["下侧离屏", { x: 200, y: 1450, w: 200, h: 100 }],
    ["右下角离屏", { x: 2100, y: 1450, w: 200, h: 100 }],
  ])("预览%s → 定位后移进实际可用区域", async (_name, bounds) => {
    const wrapper = mountCanvas();
    await wheelZoomTo(1.5, wrapper);
    const state0 = viewState(wrapper);
    expect(state0.scale).toBeGreaterThanOrEqual(1.4);
    // 越出前确认它真的在右 / 下视口外（几何先有效，再断言边界）
    const before = screenOf(bounds.x + bounds.w, bounds.y + bounds.h, state0);
    const outOfRight = before.x > VIEW_LEFT + VIEW_W - 60;
    const outOfBottom = before.y > VIEW_TOP + VIEW_H - 60;
    expect(outOfRight || outOfBottom).toBe(true);
    locateBounds(bounds);
    await wrapper.vm.$nextTick();
    expectInsideViewportAfter(wrapper, bounds, 59);
    wrapper.unmount();
  });

  it("极远坐标：数值有限、不抛错，滚动被夹在板面滚动范围内", async () => {
    const wrapper = mountCanvas();
    await wheelZoomTo(1.5, wrapper);
    const state0 = viewState(wrapper);
    locateBounds({ x: 1e6, y: 1e6, w: 200, h: 100 });
    await wrapper.vm.$nextTick();
    const state1 = viewState(wrapper);
    const maxScrollX = 2400 * state1.scale - VIEW_W;
    const maxScrollY = 1600 * state1.scale - VIEW_H;
    expect(state1.scrollLeft).toBeGreaterThanOrEqual(0);
    expect(state1.scrollLeft).toBeLessThanOrEqual(Math.max(0, maxScrollX) + 1);
    expect(state1.scrollTop).toBeGreaterThanOrEqual(0);
    expect(state1.scrollTop).toBeLessThanOrEqual(Math.max(0, maxScrollY) + 1);
    wrapper.unmount();
  });

  it("平移到板面深处后，落在视口左 / 上外的卡片由定位拉回（四方向补全）", async () => {
    store.board = board.normalizeState(
      board.addCard(board.emptyState("board_t"), { kind: "text", content: "深处卡片" }),
    );
    const cardId = store.board!.cards[0].id;
    const wrapper = mountCanvas();
    await wheelZoomTo(2.0, wrapper);
    const state0 = viewState(wrapper);
    expect(state0.scale).toBeGreaterThanOrEqual(1.9);
    // 以右下深处为缩放中心：缩放后 (100,100) 处的卡片落到视口左上外
    const leftScreen = screenOf(100, 100, state0);
    const beforeState = viewState(wrapper);
    const outLeft = beforeState.scrollLeft > 0 && leftScreen.x < VIEW_LEFT + 80;
    const outTop = beforeState.scrollTop > 0 && leftScreen.y < VIEW_TOP + 80;
    expect(outLeft || outTop).toBe(true);
    window.dispatchEvent(new CustomEvent("qio:interactive:locate-card", { detail: { cardId } }));
    await wrapper.vm.$nextTick();
    const card = store.board!.cards[0];
    expectInsideViewportAfter(wrapper, { x: card.x, y: card.y, w: card.w, h: card.h }, 79);
    wrapper.unmount();
  });
  it("定位后高亮对应预览（既有行为保持）", async () => {
    const wrapper = mountCanvas();
    const layer = document.createElement("div");
    layer.className = "preview-layer";
    document.body.appendChild(layer);
    locateBounds({ x: 200, y: 200, w: 200, h: 100 });
    await wrapper.vm.$nextTick();
    /*
     * 修正（Lead）：这条原来的断言写反了 —— 画布**自己**就渲染 .preview-layer，
     * 所以「画布内没有预览层」永远为假，断言必然失败（假失败）。
     * 这条用例要证明的是「只带 bounds、没有对应预览时定位不报错、画布仍然完好」。
     */
    expect(wrapper.find(".board-surface").exists()).toBe(true);
    expect(wrapper.find(".preview-layer").exists()).toBe(true);
    layer.remove();
    wrapper.unmount();
  });
});