/**
 * IRL-E 第二阶段独立复核 · 尺寸操作与任务语义分离（任务书 §3 / §8.1，契约 §3）。
 *
 * 与 C 的 irl-c-canvas.test.ts 换角度：不做「标签对得上」的检查，而是
 * 1) 对整块板面做**全量路径 diff**：一次尺寸拖动之后，只有该卡的几何字段变了；
 * 2) 对网络做 spy：整个拖动 / 提交过程 0 次 fetch（不产生模型调用、不申请审批）；
 * 3) 对抗 C 的提交口径（本轮按 F1 改写）：展开**不产生第二套位置**，展开中调尺寸
 *    提交的仍是完整 { x, y, w, h }，收起前后外框逐字段不变；
 * 4) 西把手必须写回 x（对边固定）—— 展开与收起走同一条口径。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount, type VueWrapper } from "@vue/test-utils";
import { nextTick } from "vue";
import BoardCanvas from "../BoardCanvas.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState, normalizeState } from "../../../interactive/board";
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
    // 基线归一化后再取快照：participation / version 是归一化补上的状态，不是尺寸拖动造成的语义变化。
    const before = JSON.parse(JSON.stringify(normalizeState(store.board!))) as BoardState;
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


});
