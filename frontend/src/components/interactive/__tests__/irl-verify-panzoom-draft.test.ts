/**
 * IRL-E 第二阶段补测（Lead 点名的两条未验证项）：
 * 1) §8.6 平移 **+ 缩放** 组合之后的展开位置仍可见（C 的用例只测了平移，这里加缩放）；
 * 2) §8.4 尺寸操作不丢草稿：调整尺寸 / 展开收起一轮之后，本机草稿原文与归属不变。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount, type VueWrapper } from "@vue/test-utils";
import { nextTick } from "vue";
import BoardCanvas from "../BoardCanvas.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
import { cardDraftKey } from "../../../interactive/drafts";
import type { BoardCard as BoardCardModel, BoardState } from "../../../interactive/types";

const VIEW_LEFT = 40;
const VIEW_TOP = 100;
const VIEW_W = 3000;
const VIEW_H = 2000;
const STAMP = "2026-10-13T00:00:00.000Z";

function mkCard(id: string, x: number, y: number, w = 120, h = 80, patch: Partial<BoardCardModel> = {}): BoardCardModel {
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
    ...patch,
  };
}

function board(cards: BoardCardModel[], patch: Partial<BoardState> = {}): BoardState {
  return { ...emptyState("board_t"), cards, ...patch };
}

function pointer(type: string, x: number, y: number): MouseEvent {
  return new MouseEvent(type, { clientX: x, clientY: y, bubbles: true, cancelable: true });
}

function styleOf(element: Element | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of (element?.getAttribute("style") ?? "").split(";")) {
    const at = part.indexOf(":");
    if (at < 0) continue;
    out[part.slice(0, at).trim()] = part.slice(at + 1).trim();
  }
  return out;
}

const px = (value: string | undefined): number => Number.parseFloat((value ?? "").replace("px", ""));

function rectOfCard(wrapper: VueWrapper, cardId: string): { x: number; y: number; w: number; h: number } {
  const found = wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === cardId);
  const style = styleOf(found?.element);
  return { x: px(style.left), y: px(style.top), w: px(style.width), h: px(style.height) };
}

function stubGeometry(wrapper: VueWrapper): HTMLElement {
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  const shell = wrapper.get(".board-shell").element as HTMLElement;
  const rect = () =>
    ({ left: VIEW_LEFT, top: VIEW_TOP, right: VIEW_LEFT + VIEW_W, bottom: VIEW_TOP + VIEW_H, width: VIEW_W, height: VIEW_H, x: VIEW_LEFT, y: VIEW_TOP, toJSON: () => ({}) }) as DOMRect;
  view.getBoundingClientRect = rect;
  shell.getBoundingClientRect = rect;
  Object.defineProperty(view, "clientWidth", { value: VIEW_W, configurable: true });
  Object.defineProperty(view, "clientHeight", { value: VIEW_H, configurable: true });
  return view;
}

let wrapper: VueWrapper | null = null;
let store: ReturnType<typeof useInteractiveStore>;

function mountCanvas(): VueWrapper {
  wrapper = mount(BoardCanvas, { attachTo: document.body });
  stubGeometry(wrapper);
  return wrapper;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  store = useInteractiveStore();
  vi.spyOn(store, "commit").mockImplementation((next: BoardState, _label: string) => {
    store.board = { ...next, boardId: store.boardId };
  });
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("平移 + 缩放之后的展开位置（§8.6）", () => {
  it("先平移再滚轮放大：展开槽位仍落在可见板面范围内，未展开卡不动，收起后回到正式位置", async () => {
    store.board = board([mkCard("c1", 100, 100), mkCard("c2", 100, 900)]);
    const view = mountCanvas();
    const element = view.get(".board-viewport").element as HTMLElement;
    Object.defineProperty(element, "scrollWidth", { value: VIEW_W * 2, configurable: true });
    Object.defineProperty(element, "scrollHeight", { value: VIEW_H * 2, configurable: true });
    Object.defineProperty(element, "scrollLeft", { value: 800, writable: true, configurable: true });
    Object.defineProperty(element, "scrollTop", { value: 600, writable: true, configurable: true });
    element.dispatchEvent(new Event("scroll"));
    await nextTick();
    element.dispatchEvent(
      new WheelEvent("wheel", {
        clientX: VIEW_LEFT + VIEW_W / 2,
        clientY: VIEW_TOP + VIEW_H / 2,
        deltaY: -462,
        deltaMode: 0,
        bubbles: true,
        cancelable: true,
      }),
    );
    await nextTick();

    const scale = Number(/scale\(([\d.]+)\)/.exec(styleOf(view.get(".board-surface").element).transform ?? "")?.[1] ?? "1");
    expect(scale).toBeGreaterThan(1.5);
    const official = rectOfCard(view, "c2");

    store.toggleCardPreview("c1");
    await nextTick();
    const slot = rectOfCard(view, "c1");
    const visibleW = VIEW_W / scale;
    const visibleH = VIEW_H / scale;
    // 展开槽位落在当前可见板面范围内（平移 800/600 + 放大之后仍然看得见）
    expect(slot.x).toBeGreaterThanOrEqual(800 - 1);
    expect(slot.y).toBeGreaterThanOrEqual(600 - 1);
    expect(slot.x + slot.w).toBeLessThanOrEqual(800 + visibleW + 1);
    expect(slot.y + slot.h).toBeLessThanOrEqual(600 + visibleH + 1);
    // 未展开的卡不受影响
    expect(rectOfCard(view, "c2")).toEqual(official);
    // 收起后回到正式位置
    store.collapseAllPreviews();
    await nextTick();
    expect(rectOfCard(view, "c1")).toEqual({ x: 100, y: 100, w: 120, h: 80 });
  });
});

describe("尺寸操作不丢草稿（§8.4 的一半）", () => {
  it("调整尺寸之后：本机草稿原文与归属不变，正文也不变", async () => {
    store.board = board([mkCard("c1", 100, 100, 200, 120, { content: "已写入的正文" })], { selection: ["c1"] });
    store.setCardDraftInput("c1", { text: "还没提交的草稿内容" });
    const view = mountCanvas();
    const key = cardDraftKey("c1");
    expect(store.draftFor(key)).toBe("还没提交的草稿内容");

    const handle = view.get('[data-resize-card-id="c1"] [data-resize-handle="se"]').element;
    handle.dispatchEvent(pointer("pointerdown", VIEW_LEFT + 300, VIEW_TOP + 220));
    document.dispatchEvent(pointer("pointermove", VIEW_LEFT + 360, VIEW_TOP + 260));
    document.dispatchEvent(pointer("pointerup", VIEW_LEFT + 360, VIEW_TOP + 260));
    await nextTick();

    expect(store.board!.cards[0]).toMatchObject({ x: 100, y: 100, w: 260, h: 160, content: "已写入的正文" });
    expect(store.draftFor(key)).toBe("还没提交的草稿内容");
  });

  it("展开 → 收起一轮之后再调尺寸：草稿仍在（重新渲染不丢草稿）", async () => {
    store.board = board([mkCard("c1", 100, 100)], { selection: ["c1"] });
    store.setCardDraftInput("c1", { text: "展开期间的草稿" });
    const view = mountCanvas();
    const key = cardDraftKey("c1");

    store.toggleCardPreview("c1");
    await nextTick();
    expect(store.draftFor(key)).toBe("展开期间的草稿");
    store.collapseAllPreviews();
    await nextTick();
    expect(store.draftFor(key)).toBe("展开期间的草稿");

    await view.get('[data-resize-card-id="c1"] [data-resize-key="w+"]').trigger("click");
    expect(store.draftFor(key)).toBe("展开期间的草稿");
    expect(store.board!.cards[0].content).toBe("卡片 c1");
  });
});
