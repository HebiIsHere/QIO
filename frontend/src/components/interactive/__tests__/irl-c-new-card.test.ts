/**
 * IRL-C：新卡片落点（任务书 §7.7）。
 *
 * 反例是旧行为：`addCard` 的兜底是「固定四列 + 数量取模」（board.ts 的 DEFAULT_CARD_ORIGIN
 * 起、每列 260+24 递推）。平移或缩放之后它会落在视口之外，并反复压在同一处已有的卡上。
 *
 * 现在：画布把**可用区域**（已扣平移后的可视起点、标题栏、底部工具栏、对话窗口、审批浮层）
 * 发布到 viewport.ts 的显示态桥；「添加」菜单读它、用 planNewCardSlot 算出空白落点，
 * 显式把 x/y/w/h 交给 addCard。读不到（画布未挂载）时不传坐标，兜底行为与改动前逐字节一致。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import AddMenu from "../AddMenu.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { addCard, emptyState } from "../../../interactive/board";
import { NEW_CARD_SIZE } from "../../../interactive/cardLayout";
import { publishBoardArea, readBoardArea } from "../../../interactive/viewport";
import type { BoardCard, BoardState } from "../../../interactive/types";

const STAMP = "2026-10-13T00:00:00.000Z";

function mkCard(id: string, x: number, y: number, w = NEW_CARD_SIZE.w, h = NEW_CARD_SIZE.h): BoardCard {
  return {
    id,
    kind: "text",
    content: "已有卡片",
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

function overlaps(a: { x: number; y: number; w: number; h: number }, b: { x: number; y: number; w: number; h: number }): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

let wrapper: VueWrapper | null = null;
let store: ReturnType<typeof useInteractiveStore>;

function setBoard(next: BoardState): void {
  store.board = { ...next, boardId: "board_t" };
}

async function addText(): Promise<BoardCard> {
  wrapper = mount(AddMenu, { attachTo: document.body });
  await wrapper.get('[data-im="add-menu"]').trigger("click");
  await wrapper.get('[data-im="add-text"]').trigger("click");
  const added = store.board!.cards[store.board!.cards.length - 1];
  return added;
}

beforeEach(() => {
  setActivePinia(createPinia());
  store = useInteractiveStore();
  setBoard(emptyState("board_t"));
  publishBoardArea(null);
  vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
    void label;
    store.board = { ...next, boardId: store.boardId };
  });
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
  publishBoardArea(null);
  vi.restoreAllMocks();
});

describe("默认尺寸口径不允许悄悄漂移", () => {
  it("NEW_CARD_SIZE 与 board.addCard 的兜底尺寸一致", () => {
    const seeded = addCard(emptyState("b"), { kind: "text", content: "x" });
    expect({ w: seeded.cards[0].w, h: seeded.cards[0].h }).toEqual(NEW_CARD_SIZE);
  });
});

describe("添加菜单：新卡落在当前可用区域的空白处", () => {
  it("平移之后（可用区域带原点）：新卡落在可视区域里，而不是板面原点的固定网格", async () => {
    publishBoardArea({ x: 900, y: 700, w: 600, h: 400 });
    const card = await addText();
    expect({ x: card.x, y: card.y }).toEqual({ x: 900, y: 700 });
    expect({ w: card.w, h: card.h }).toEqual(NEW_CARD_SIZE);
    expect(card.x).toBeGreaterThanOrEqual(900);
    expect(card.y + card.h).toBeLessThanOrEqual(1100);
  });

  it("避开已有卡片：新卡与已有卡片不重叠", async () => {
    setBoard({ ...emptyState("board_t"), cards: [mkCard("old", 900, 700)] });
    publishBoardArea({ x: 900, y: 700, w: 600, h: 400 });
    const card = await addText();
    expect(overlaps(card, { x: 900, y: 700, w: NEW_CARD_SIZE.w, h: NEW_CARD_SIZE.h })).toBe(false);
    expect(card.x).toBeGreaterThanOrEqual(900);
  });

  it("连续添加两张：第二张不压在第一张上", async () => {
    publishBoardArea({ x: 0, y: 0, w: 900, h: 700 });
    const first = await addText();
    wrapper?.unmount();
    wrapper = null;
    const second = await addText();
    expect(overlaps(first, second)).toBe(false);
  });

  it("画布没挂载（读不到可用区域）：不传坐标，沿用既有兜底行为", async () => {
    publishBoardArea(null);
    const card = await addText();
    // board.ts 的兜底：第一张卡在 DEFAULT_CARD_ORIGIN（60）起的第一列
    expect({ x: card.x, y: card.y }).toEqual({ x: 60, y: 60 });
    expect({ w: card.w, h: card.h }).toEqual(NEW_CARD_SIZE);
  });
});

describe("可用区域桥本身是防御式的", () => {
  it("零 / 无效区域不发布：读回来仍是 null（回落到既有兜底）", () => {
    publishBoardArea({ x: 10, y: 10, w: 0, h: 0 });
    expect(readBoardArea()).toBeNull();
    publishBoardArea({ x: Number.NaN, y: 0, w: 100, h: 100 });
    expect(readBoardArea()).toBeNull();
    publishBoardArea({ x: 10, y: 10, w: 100, h: 100 });
    expect(readBoardArea()).toEqual({ x: 10, y: 10, w: 100, h: 100 });
    // 读出来的是副本：改它不会污染发布出去的那一份
    const copy = readBoardArea()!;
    copy.x = -999;
    expect(readBoardArea()!.x).toBe(10);
  });
});
