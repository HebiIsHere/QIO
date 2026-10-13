/**
 * IRL-E 第二阶段独立复核 · 尺寸操作与任务语义分离（任务书 §3 / §8.1，契约 §3）。
 *
 * 与 C 的 irl-c-canvas.test.ts 换角度：不做「标签对得上」的检查，而是
 * 1) 对整块板面做**全量路径 diff**：一次尺寸拖动之后，只有该卡的几何字段变了；
 * 2) 对网络做 spy：整个拖动 / 提交过程 0 次 fetch（不产生模型调用、不申请审批）；
 * 3) 对抗 C 的提交口径：被**让位**的展开卡（槽位 ≠ 正式位置）用西把手调尺寸，
 *    临时槽位坐标绝不写回板面，收起后停在正式位置 + 用户尺寸；
 * 4) 对照分支：常态卡的西把手必须写回 x（对边固定）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount, type VueWrapper } from "@vue/test-utils";
import { nextTick } from "vue";
import BoardCanvas from "../BoardCanvas.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
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

/** 两个 JSON 值之间的叶子路径差异：返回 "a.b.0.c" 这样的路径集合。 */
function diffPaths(before: unknown, after: unknown, prefix = ""): string[] {
  if (Object.is(before, after)) return [];
  if (Array.isArray(before) && Array.isArray(after)) {
    if (before.length !== after.length) return [prefix + ".(length)"];
    return before.flatMap((item, index) => diffPaths(item, after[index], prefix + "." + index));
  }
  if (before && after && typeof before === "object" && typeof after === "object") {
    const keys = new Set([...Object.keys(before as object), ...Object.keys(after as object)]);
    return [...keys].flatMap((key) =>
      diffPaths((before as Record<string, unknown>)[key], (after as Record<string, unknown>)[key], prefix ? prefix + "." + key : key),
    );
  }
  return [prefix];
}

function stubGeometry(wrapper: VueWrapper): void {
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  const shell = wrapper.get(".board-shell").element as HTMLElement;
  const rect = () =>
    ({ left: VIEW_LEFT, top: VIEW_TOP, right: VIEW_LEFT + VIEW_W, bottom: VIEW_TOP + VIEW_H, width: VIEW_W, height: VIEW_H, x: VIEW_LEFT, y: VIEW_TOP, toJSON: () => ({}) }) as DOMRect;
  view.getBoundingClientRect = rect;
  shell.getBoundingClientRect = rect;
  Object.defineProperty(view, "clientWidth", { value: VIEW_W, configurable: true });
  Object.defineProperty(view, "clientHeight", { value: VIEW_H, configurable: true });
}

let wrapper: VueWrapper | null = null;
let store: ReturnType<typeof useInteractiveStore>;
let labels: string[];
let payloads: BoardState[];

function mountCanvas(): VueWrapper {
  wrapper = mount(BoardCanvas, { attachTo: document.body });
  stubGeometry(wrapper);
  return wrapper;
}

/** 在缩放 1 的情况下，把「板面坐标」换算成派发事件用的屏幕坐标 */
const at = (x: number, y: number): { x: number; y: number } => ({ x: VIEW_LEFT + x, y: VIEW_TOP + y });

function dragHandle(view: VueWrapper, cardId: string, handle: string, from: { x: number; y: number }, delta: { x: number; y: number }): void {
  const element = view.get('[data-resize-card-id="' + cardId + '"] [data-resize-handle="' + handle + '"]').element;
  const start = at(from.x, from.y);
  element.dispatchEvent(pointer("pointerdown", start.x, start.y));
  document.dispatchEvent(pointer("pointermove", start.x + delta.x, start.y + delta.y));
  document.dispatchEvent(pointer("pointerup", start.x + delta.x, start.y + delta.y));
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  store = useInteractiveStore();
  labels = [];
  payloads = [];
  vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
    labels.push(label);
    payloads.push(JSON.parse(JSON.stringify(next)) as BoardState);
    store.board = { ...next, boardId: store.boardId };
  });
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("尺寸操作 ↔ 任务语义分离", () => {
  it("全量路径 diff：一次尺寸拖动只改该卡的几何字段，其余一个都不动", async () => {
    store.board = board(
      [
        mkCard("c1", 100, 100, 120, 80, { checked: true, bookmarked: true }),
        mkCard("c2", 500, 300, 200, 150, { checked: true }),
      ],
      {
        selection: ["c1"],
        groups: [
          {
            id: "g1",
            name: "组一",
            defaultName: false,
            ordered: false,
            x: 90,
            y: 90,
            w: 620,
            h: 380,
            members: ["c1", "c2"],
            deleted: false,
            createdAt: STAMP,
            updatedAt: STAMP,
          },
        ],
        links: [
          {
            id: "l1",
            src: "c1",
            dst: "c2",
            direction: true,
            meaning: "相关",
            deleted: false,
            createdAt: STAMP,
            updatedAt: STAMP,
          },
        ],
      },
    );
    const view = mountCanvas();
    const before = JSON.parse(JSON.stringify(store.board)) as BoardState;
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    fetchSpy.mockClear();

    dragHandle(view, "c1", "se", { x: 220, y: 180 }, { x: 60, y: 40 });
    await nextTick();

    const after = JSON.parse(JSON.stringify(store.board)) as BoardState;
    const changed = diffPaths(before, after).sort();
    expect(labels).toEqual(["调整卡片尺寸"]);
    // 允许出现的变化只有三类**布局几何**：被拉那张卡的几何、组框跟着成员重算的框、
    // 以及时间戳。（组框 x/y/w/h 是显示几何：后端 diff_states 只比 members/name/ordered，
    // 不看框坐标，见 backend/src/agent/interactive/submission.py:436-464。）
    const allowed = new Set([
      "boardId",
      "updatedAt",
      "cards.0.x",
      "cards.0.y",
      "cards.0.w",
      "cards.0.h",
      "cards.0.updatedAt",
      "groups.0.x",
      "groups.0.y",
      "groups.0.w",
      "groups.0.h",
      "groups.0.updatedAt",
    ]);
    expect(changed.filter((path) => !allowed.has(path))).toEqual([]);
    expect(changed.some((path) => path.endsWith(".w") || path.endsWith(".h"))).toBe(true);
    // 语义字段一个都不能动
    expect(after.cards[0].checked).toBe(true);
    expect(after.cards[0].bookmarked).toBe(true);
    expect(after.cards[0].content).toBe(before.cards[0].content);
    expect(after.cards[0].meta).toEqual(before.cards[0].meta);
    expect(after.cards[0].deleted).toBe(false);
    expect(after.cards[1]).toEqual(before.cards[1]);
    expect(after.groups[0].members).toEqual(["c1", "c2"]);
    expect(after.groups[0].name).toBe("组一");
    expect(after.groups[0].ordered).toBe(false);
    expect(after.links).toEqual(before.links);
    expect(after.selection).toEqual(["c1"]);
    expect(store.expandedCardIds).toEqual([]);
    expect(store.intents).toEqual([]);
    expect(fetchSpy, "调整尺寸不得有任何网络调用（模型 / 提交 / 审批）").not.toHaveBeenCalled();
  });

  it("拖动中 0 次提交、0 次网络；松手 1 次提交；没有第二次", async () => {
    store.board = board([mkCard("c1", 100, 100)], { selection: ["c1"] });
    const view = mountCanvas();
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    fetchSpy.mockClear();
    const element = view.get('[data-resize-card-id="c1"] [data-resize-handle="se"]').element;
    element.dispatchEvent(pointer("pointerdown", VIEW_LEFT + 220, VIEW_TOP + 180));
    document.dispatchEvent(pointer("pointermove", VIEW_LEFT + 260, VIEW_TOP + 220));
    document.dispatchEvent(pointer("pointermove", VIEW_LEFT + 300, VIEW_TOP + 260));
    await nextTick();
    expect(labels).toEqual([]);
    expect(fetchSpy).not.toHaveBeenCalled();
    document.dispatchEvent(pointer("pointerup", VIEW_LEFT + 300, VIEW_TOP + 260));
    await nextTick();
    expect(labels).toEqual(["调整卡片尺寸"]);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("对照分支：常态卡的西把手写回 x（对边固定），不是只写 w/h", async () => {
    // 卡宽 200：往右拉 40 → 160，会被 CARD_MIN_SIZE.w(180) 夹住；
    // 关键是「右边界不动」+「x 跟着回算」，这条只有常态分支才允许写 x。
    store.board = board([mkCard("c1", 100, 100, 200, 120)], { selection: ["c1"] });
    const view = mountCanvas();
    dragHandle(view, "c1", "w", { x: 100, y: 160 }, { x: 40, y: 0 });
    await nextTick();
    expect({ labels, card: store.board!.cards[0] }).toMatchObject({
      labels: ["调整卡片尺寸"],
      card: { x: 120, y: 100, w: 180, h: 120 },
    });
    expect(store.board!.cards[0].x + store.board!.cards[0].w).toBe(300);
  });
});

describe("展开中的尺寸调整：临时槽位坐标绝不写回板面（对抗反例）", () => {
  it("被让位的展开卡用西把手调尺寸：x/y 一个字都不写回，尺寸落到 card.w/h", async () => {
    store.board = board([mkCard("a", 0, 0, 300, 200), mkCard("b", 200, 0, 300, 200)], { selection: ["b"] });
    const view = mountCanvas();
    store.toggleCardPreview("a");
    store.toggleCardPreview("b");
    await nextTick();
    const slotB = rectOfCard(view, "b");
    // 前提：b 确实被让位（槽位不在正式位置上）——否则这条用例不构成反例
    expect(slotB.x).not.toBe(200);
    expect(slotB.w).toBe(560);
    expect(slotB.h).toBe(420);

    const fetchSpy = vi.spyOn(globalThis, "fetch");
    fetchSpy.mockClear();
    // 西把手：常态下它会改 x；临时显示下只允许改 w
    dragHandle(view, "b", "w", { x: slotB.x, y: slotB.y + slotB.h / 2 }, { x: 40, y: 0 });
    await nextTick();

    expect(labels).toEqual(["调整卡片尺寸"]);
    const payload = payloads[0].cards.find((item) => item.id === "b")!;
    expect(payload.x).toBe(200); // 板面里的正式位置没被动
    expect(payload.y).toBe(0);
    expect(payload.w).toBe(520); // 560 - 40
    expect(payload.h).toBe(420);
    expect(store.expandedCardIds).toEqual(["a", "b"]);
    expect(fetchSpy).not.toHaveBeenCalled();

    // 收起：停在正式位置 + 用户尺寸（不被旧快照覆盖）
    store.collapseAllPreviews();
    await nextTick();
    expect(rectOfCard(view, "b")).toEqual({ x: 200, y: 0, w: 520, h: 420 });
  });

  it("展开中键盘 ＋：同样只提交 {w,h}，展开集合与位置不变", async () => {
    store.board = board([mkCard("c1", 100, 100)], { selection: ["c1"] });
    const view = mountCanvas();
    store.toggleCardPreview("c1");
    await nextTick();
    await view.get('[data-resize-card-id="c1"] [data-resize-key="w+"]').trigger("click");
    const payload = payloads[0].cards.find((item) => item.id === "c1")!;
    expect(payload.x).toBe(100);
    expect(payload.y).toBe(100);
    expect(payload.w).toBe(560 + 24);
    expect(payload.h).toBe(420);
    expect(store.expandedCardIds).toEqual(["c1"]);
    store.collapseAllPreviews();
    await nextTick();
    expect(rectOfCard(view, "c1")).toEqual({ x: 100, y: 100, w: 584, h: 420 });
  });

  it("收起之后再次调整：这次走常态分支，x/y 才被允许写回", async () => {
    store.board = board([mkCard("c1", 100, 100)], { selection: ["c1"] });
    const view = mountCanvas();
    store.toggleCardPreview("c1");
    await nextTick();
    await view.get('[data-resize-card-id="c1"] [data-resize-key="h+"]').trigger("click");
    store.collapseAllPreviews();
    await nextTick();
    const slot = rectOfCard(view, "c1");
    dragHandle(view, "c1", "nw", { x: slot.x, y: slot.y }, { x: 10, y: 10 });
    await nextTick();
    const card = store.board!.cards[0];
    expect(card.x).toBe(110);
    expect(card.y).toBe(110);
    expect(card.w).toBe(slot.w - 10);
    expect(card.h).toBe(slot.h - 10);
  });
});
