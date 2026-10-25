/**
 * D 组独立验收 · F04：重复拖线不得覆盖已有关系——组件级（真实 BoardCanvas 手势）。
 *
 * 用户可见契约（任务书 F04 / 流程 6）：
 * - 两个端点之间已经有关系时，再拖一次不能把原有的含义 / 方向 / 曲线清空；
 * - 应选中 / 打开原关系（同一个 id），取消编辑不产生修改；只有明确保存才更新；
 * - 卡片—卡片、卡片—组一致；删除后可以重建，撤销重做有效。
 *
 * 基线问题：finishLinkDraft 无条件 addLink(..., false, "")，addLink 对已存在的端点对
 * 直接把 direction/meaning 覆盖成默认值 —— 用户写下的含义和方向被清空。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import BoardCanvas from "../BoardCanvas.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
import type { BoardCard as BoardCardModel, BoardGroup, BoardLink, BoardState } from "../../../interactive/types";

const VIEW_LEFT = 40;
const VIEW_TOP = 100;
const VIEW_W = 3000;
const VIEW_H = 2000;
const STAMP = "2026-10-25T00:00:00.000Z";

function mkCard(id: string, x: number, y: number, w = 120, h = 80): BoardCardModel {
  return { id, kind: "text", content: "卡片 " + id, meta: {}, x, y, w, h, checked: false, hidden: false, folded: false, bookmarked: false, deleted: false, createdAt: STAMP, updatedAt: STAMP };
}

function mkGroup(id: string, members: string[]): BoardGroup {
  return { id, name: "发布计划", defaultName: false, ordered: false, x: 80, y: 60, w: 360, h: 200, members, deleted: false, createdAt: STAMP, updatedAt: STAMP };
}

function mkLink(id: string, src: string, dst: string): BoardLink {
  return { id, src, dst, direction: true, meaning: "属于", curve: { x: 20, y: 10 }, deleted: false, createdAt: STAMP, updatedAt: STAMP };
}

function pointer(type: string, x: number, y: number): MouseEvent {
  return new MouseEvent(type, { clientX: x, clientY: y, bubbles: true, cancelable: true });
}

let wrapper: VueWrapper | null = null;
let store: ReturnType<typeof useInteractiveStore>;
let labels: string[];

function mountCanvas(): VueWrapper {
  wrapper = mount(BoardCanvas, { attachTo: document.body });
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  const shell = wrapper.get(".board-shell").element as HTMLElement;
  view.getBoundingClientRect = () =>
    ({ left: VIEW_LEFT, top: VIEW_TOP, right: VIEW_LEFT + VIEW_W, bottom: VIEW_TOP + VIEW_H, width: VIEW_W, height: VIEW_H, x: VIEW_LEFT, y: VIEW_TOP, toJSON: () => ({}) }) as DOMRect;
  shell.getBoundingClientRect = () =>
    ({ left: VIEW_LEFT, top: VIEW_TOP, right: VIEW_LEFT + VIEW_W, bottom: VIEW_TOP + VIEW_H, width: VIEW_W, height: VIEW_H, x: VIEW_LEFT, y: VIEW_TOP, toJSON: () => ({}) }) as DOMRect;
  Object.defineProperty(view, "clientWidth", { value: VIEW_W, configurable: true });
  Object.defineProperty(view, "clientHeight", { value: VIEW_H, configurable: true });
  return wrapper;
}

function viewState(): { scale: number; scrollLeft: number; scrollTop: number } {
  const surface = wrapper!.get(".board-surface").element as HTMLElement;
  const view = wrapper!.get(".board-viewport").element as HTMLElement;
  const match = /scale\(([\d.]+)\)/.exec(surface.style.transform);
  return { scale: match ? Number(match[1]) : 1, scrollLeft: view.scrollLeft, scrollTop: view.scrollTop };
}

function screenOf(boardX: number, boardY: number) {
  const state = viewState();
  return { x: VIEW_LEFT + boardX * state.scale - state.scrollLeft, y: VIEW_TOP + boardY * state.scale - state.scrollTop };
}

function connectPoints(): ReturnType<VueWrapper["findAll"]> {
  return wrapper!.findAll('[data-im="connect-point"]');
}

async function dragConnect(fromBoard: { x: number; y: number }, toBoard: { x: number; y: number }): Promise<void> {
  const points = connectPoints();
  expect(points.length, "选中一张卡片后应有连接点").toBeGreaterThan(0);
  const start = screenOf(fromBoard.x, fromBoard.y);
  points[0].element.dispatchEvent(pointer("pointerdown", start.x, start.y));
  await nextTick();
  const end = screenOf(toBoard.x, toBoard.y);
  window.dispatchEvent(pointer("pointermove", end.x, end.y));
  await nextTick();
  window.dispatchEvent(pointer("pointerup", end.x, end.y));
  await nextTick();
}

beforeEach(() => {
  setActivePinia(createPinia());
  store = useInteractiveStore();
  labels = [];
  vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
    labels.push(label);
    store.board = { ...next, boardId: store.boardId };
  });
});

afterEach(() => {
  try {
    wrapper?.unmount();
  } catch {
    /* 已经卸载 */
  }
  wrapper = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("F04 已有关系再拖线", () => {
  it("卡片—卡片：重复连接不新建第二份、不清空已写含义与方向（同一个 id）", async () => {
    const a = mkCard("a", 100, 100);
    const b = mkCard("b", 500, 100);
    const existing = mkLink("l1", "a", "b");
    store.board = { ...emptyState("board_t"), cards: [a, b], links: [existing], selection: ["a"] } as BoardState;
    const frame = mountCanvas();
    await nextTick();

    await dragConnect({ x: 160, y: 100 }, { x: 500, y: 140 });

    const links = (store.board as BoardState).links.filter((link) => !link.deleted);
    expect(links, "同一对端点只能有一条关系").toHaveLength(1);
    expect(links[0].id, "应打开原关系而不是替换它").toBe("l1");
    expect(links[0].meaning, "已有关系的含义不能被拖线清空").toBe("属于");
    expect(links[0].direction, "已有关系的方向不能被拖线重置").toBe(true);
    expect(links[0].curve).toEqual({ x: 20, y: 10 });
    expect(frame.find('[data-im="link-draft"]').exists()).toBe(false);
  });

  it("卡片—组：重复连接到组同样保留原关系", async () => {
    const a = mkCard("a", 100, 100);
    const b = mkCard("b", 500, 100);
    const c = mkCard("c", 100, 400);
    const group = mkGroup("g1", ["b"]);
    const existing = mkLink("l2", "a", "group:g1");
    store.board = { ...emptyState("board_t"), cards: [a, b, c], groups: [group], links: [existing], selection: ["a"] } as BoardState;
    mountCanvas();
    await nextTick();

    await dragConnect({ x: 160, y: 100 }, { x: 260, y: 120 });

    const links = (store.board as BoardState).links.filter((link) => !link.deleted);
    expect(links).toHaveLength(1);
    expect(links[0].id).toBe("l2");
    expect(links[0].meaning).toBe("属于");
    expect(links[0].direction).toBe(true);
  });
});
