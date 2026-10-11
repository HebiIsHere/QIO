/**
 * 收尾轮 · D 的反例测试（20：空格框选状态在失焦后残留）。
 *
 * 反例：按住空格 → 切换窗口（window blur）→ 在别处松开 → 返回后按普通拖动空白处，
 * 结果仍被当框选（spaceDown 只由 window keydown / keyup 维护，失焦期间的 keyup 收不到）。
 * 应有结果（契约 M8）：失焦 / 页面隐藏 / 取消手势 / 卸载时重置临时按键与拖动状态，
 * 返回后普通拖动恢复为平移；输入框 / 中文输入法 / 控件激活键 / Escape 不被劫持。
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

function pointer(type: string, x: number, y: number, extra: PointerEventInit = {}): MouseEvent {
  return new MouseEvent(type, { clientX: x, clientY: y, bubbles: true, cancelable: true, ...extra });
}

describe("空格框选状态复位（20）", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  let labels: string[];
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
    labels = [];
    vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
      labels.push(label);
      store.board = { ...next, boardId: store.boardId };
    });
  });

  function mountCanvas(): VueWrapper {
    const wrapper = mount(BoardCanvas, { attachTo: document.body });
    mounted.push(wrapper);
    stubGeometry(wrapper);
    return wrapper;
  }

  function holdSpace() {
    window.dispatchEvent(new KeyboardEvent("keydown", { key: " ", code: "Space", bubbles: true, cancelable: true }));
  }

  /** 普通拖动空白处：返回是否发生了框选（.select-rect 出现）。 */
  async function plainDrag(wrapper: VueWrapper): Promise<boolean> {
    wrapper.get(".board-viewport").element.dispatchEvent(pointer("pointerdown", 500, 400));
    window.dispatchEvent(pointer("pointermove", 560, 450));
    await wrapper.vm.$nextTick();
    const framed = wrapper.find(".select-rect").exists();
    window.dispatchEvent(pointer("pointerup", 560, 450));
    await wrapper.vm.$nextTick();
    return framed;
  }

  it("按住空格切换窗口（blur），在别处松开 → 返回后普通拖动恢复为平移", async () => {
    const wrapper = mountCanvas();
    holdSpace();
    await wrapper.vm.$nextTick();
    window.dispatchEvent(new Event("blur"));
    await wrapper.vm.$nextTick();
    // 在另一个窗口里松开（本窗口收不到 keyup）：直接普通拖动
    const framed = await plainDrag(wrapper);
    expect(framed).toBe(false);
    expect(labels.filter((label) => label.includes("框选"))).toHaveLength(0);
    // 返回后再补的 keyup 也不应把状态弄乱
    window.dispatchEvent(new KeyboardEvent("keyup", { key: " ", code: "Space", bubbles: true }));
    wrapper.unmount();
  });

  it("页面隐藏（visibilitychange hidden）→ 临时按键与进行中的框选一并复位", async () => {
    const wrapper = mountCanvas();
    holdSpace();
    await wrapper.vm.$nextTick();
    wrapper.get(".board-viewport").element.dispatchEvent(pointer("pointerdown", 500, 400));
    window.dispatchEvent(pointer("pointermove", 560, 450));
    await wrapper.vm.$nextTick();
    expect(wrapper.find(".select-rect").exists()).toBe(true);
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    await wrapper.vm.$nextTick();
    expect(wrapper.find(".select-rect").exists()).toBe(false);
    const framed = await plainDrag(wrapper);
    expect(framed).toBe(false);
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    wrapper.unmount();
  });

  it("Escape 取消手势：临时按键复位，后续拖动是平移", async () => {
    const wrapper = mountCanvas();
    holdSpace();
    await wrapper.vm.$nextTick();
    wrapper.get(".board-viewport").element.dispatchEvent(pointer("pointerdown", 500, 400));
    window.dispatchEvent(pointer("pointermove", 560, 450));
    await wrapper.vm.$nextTick();
    expect(wrapper.find(".select-rect").exists()).toBe(true);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await wrapper.vm.$nextTick();
    expect(wrapper.find(".select-rect").exists()).toBe(false);
    const framed = await plainDrag(wrapper);
    expect(framed).toBe(false);
    wrapper.unmount();
  });

  it("输入框空格正常输入（豁免不回退）；中文输入法选字组合不劫持", async () => {
    const wrapper = mountCanvas();
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    const typing = new KeyboardEvent("keydown", { key: " ", code: "Space", bubbles: true, cancelable: true });
    input.dispatchEvent(typing);
    expect(typing.defaultPrevented).toBe(false);
    // 中文输入法：keydown 带 isComposing（选字确认），即使目标不在输入框也不劫持
    const composing = new KeyboardEvent("keydown", { key: " ", code: "Space", bubbles: true, cancelable: true, isComposing: true });
    window.dispatchEvent(composing);
    await wrapper.vm.$nextTick();
    const framed = await plainDrag(wrapper);
    expect(framed).toBe(false);
    input.remove();
    wrapper.unmount();
  });

  it("控件激活键（按钮上的空格）不被劫持", () => {
    const wrapper = mountCanvas();
    const button = document.createElement("button");
    document.body.appendChild(button);
    const event = new KeyboardEvent("keydown", { key: " ", code: "Space", bubbles: true, cancelable: true });
    button.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    button.remove();
    wrapper.unmount();
  });
});