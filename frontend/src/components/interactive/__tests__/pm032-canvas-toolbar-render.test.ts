/**
 * #04 卡片工具栏几何（PM-QIO032）· 真实组件渲染后的验收：
 * 卡片贴上边 / 贴左边 / 放大到比可视区还大时，编辑等按钮的屏幕外框必须完整落在
 * 板面可视区里（完整可点），不能因为固定贴上边被宿主裁掉。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount, type VueWrapper } from "@vue/test-utils";
import { nextTick } from "vue";
import BoardCanvas from "../BoardCanvas.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
import type { BoardCard as BoardCardModel, BoardState } from "../../../interactive/types";
import { installCardToolbarLayout, type CardToolbarHarness } from "./cardToolbarLayoutHarness";

const VIEW = { left: 40, top: 100, width: 1200, height: 800 };
const SURFACE = { w: 4000, h: 2400 };
const TOOLBAR = { w: 200, h: 30 };
const STAMP = "2026-10-14T00:00:00.000Z";

function mkCard(id: string, x: number, y: number, w = 260, h = 170): BoardCardModel {
  return {
    id,
    kind: "text",
    content: "卡片 " + id,
    meta: {},
    x,
    y,
    w,
    h,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

function board(cards: BoardCardModel[], patch: Partial<BoardState> = {}): BoardState {
  return { ...emptyState("board_t"), cards, ...patch };
}

let wrapper: VueWrapper | null = null;
let harness: CardToolbarHarness | null = null;
let store: ReturnType<typeof useInteractiveStore>;

async function flushFrame(times = 2): Promise<void> {
  for (let i = 0; i < times; i += 1) {
    await nextTick();
    await new Promise<void>((resolve) => {
      if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => resolve());
      else setTimeout(resolve, 16);
    });
    await new Promise<void>((resolve) => setTimeout(resolve, 20));
    await nextTick();
  }
}

function mountCanvas(): void {
  wrapper = mount(BoardCanvas, { attachTo: document.body });
  harness = installCardToolbarLayout(wrapper.get(".board-shell").element as HTMLElement, {
    viewport: VIEW,
    surfaceSize: SURFACE,
    toolbarSize: TOOLBAR,
  });
}

function buttonScreenRect(hook: string): { left: number; top: number; right: number; bottom: number } {
  const el = wrapper!.get(`[data-im="${hook}"]`).element as HTMLElement;
  const left = Number.parseFloat(el.style.left);
  const top = Number.parseFloat(el.style.top);
  const cardRect = harness!.measure().cardRect!;
  return { left: cardRect.left + left, top: cardRect.top + top, right: cardRect.left + left + 30, bottom: cardRect.top + top + 30 };
}

function expectButtonsInsideViewport(): void {
  for (const hook of ["card-icon-edit", "card-icon-copy", "card-icon-hide", "card-icon-bookmark", "card-icon-delete"]) {
    const rect = buttonScreenRect(hook);
    expect(rect.left, hook + " 左缘被可视区裁掉").toBeGreaterThanOrEqual(VIEW.left);
    expect(rect.top, hook + " 上缘被可视区裁掉").toBeGreaterThanOrEqual(VIEW.top);
    expect(rect.right, hook + " 右缘越出可视区").toBeLessThanOrEqual(VIEW.left + VIEW.width);
    expect(rect.bottom, hook + " 下缘越出可视区").toBeLessThanOrEqual(VIEW.top + VIEW.height);
  }
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  store = useInteractiveStore();
});

afterEach(() => {
  harness?.restore();
  harness = null;
  try {
    wrapper?.unmount();
  } catch {
    /* 已卸载 */
  }
  wrapper = null;
  document.body.innerHTML = "";
});

describe("#04 真实组件：贴边 / 放大后编辑按钮完整可点", () => {
  it("卡片贴板面上边：工具栏换到下方，5 个按钮都在可视区内", async () => {
    store.board = board([mkCard("c1", 300, 0)], { selection: ["c1"] });
    mountCanvas();
    await flushFrame();
    expectButtonsInsideViewport();
    // 换到下方 = 按钮 top 相对卡片为正（不再是 -42 的上方行）
    const edit = wrapper!.get('[data-im="card-icon-edit"]').element as HTMLElement;
    expect(Number.parseFloat(edit.style.top)).toBeGreaterThan(0);
  });

  it("卡片贴左边且很宽：整排按钮沿边内收，全部在可视区内", async () => {
    store.board = board([mkCard("c1", -200, 300, 1100, 170)], { selection: ["c1"] });
    mountCanvas();
    await flushFrame();
    expectButtonsInsideViewport();
  });

  it("放大到卡片比可视区还大（缩放后）：按钮向内调整，编辑按钮仍在可视区内", async () => {
    store.board = board([mkCard("c1", 300, 260, 2600, 1700)], { selection: ["c1"] });
    mountCanvas();
    await flushFrame();
    expectButtonsInsideViewport();
  });
});
