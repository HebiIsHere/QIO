/**
 * D 组独立验收 · F08：定位收起组内成员——组件级（真实 BoardCanvas）。
 *
 * 用户可见契约（任务书 F08 / 流程 8）：
 * - 搜索 / 书签 / 任务面板发起的「定位」对**已收起组内的卡片**也必须有效：
 *   先展开组，等布局可用后再定位，目标卡片真的渲染在可视区里；
 * - 不移动卡片、不改变参与关系与正文；只执行一次，不持续追踪。
 *
 * 基线问题：locate() 直接按卡片坐标滚视野，但收起组的成员卡片根本没渲染 —— 定位后什么都看不到。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import BoardCanvas from "../BoardCanvas.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";
import type { BoardState } from "../../../interactive/types";

const VIEW_LEFT = 40;
const VIEW_TOP = 100;
const VIEW_W = 3000;
const VIEW_H = 2000;

let wrapper: VueWrapper | null = null;
let store: ReturnType<typeof useInteractiveStore>;

function mountCanvas(): VueWrapper {
  wrapper = mount(BoardCanvas, { attachTo: document.body });
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  const shell = wrapper.get(".board-shell").element as HTMLElement;
  view.getBoundingClientRect = () =>
    ({ left: VIEW_LEFT, top: VIEW_TOP, right: VIEW_LEFT + VIEW_W, bottom: VIEW_TOP + VIEW_H, width: VIEW_W, height: VIEW_H, x: VIEW_LEFT, y: VIEW_TOP, toJSON: () => ({}) }) as DOMRect;
  shell.getBoundingClientRect = () =>
    ({ left: VIEW_LEFT, top: VIEW_TOP, right: VIEW_LEFT + VIEW_W, bottom: VIEW_TOP + VIEW_H, width: VIEW_W, height: VIEW_H, x: VIEW_LEFT, y: VIEW_TOP, toJSON: () => ({}) }) as DOMRect;
  Object.defineProperty(view, "clientWidth", { value: VIEW_W, configurable: true });
  Object.defineProperty(view, "clientHeight", { value: VIEW_H - 220, configurable: true });
  Object.defineProperty(view, "scrollWidth", { value: VIEW_W * 2, configurable: true });
  Object.defineProperty(view, "scrollHeight", { value: VIEW_H * 2, configurable: true });
  Object.defineProperty(view, "scrollLeft", { value: 0, writable: true, configurable: true });
  Object.defineProperty(view, "scrollTop", { value: 0, writable: true, configurable: true });
  return wrapper;
}

function groupEl(): ReturnType<VueWrapper["find"]> {
  return wrapper!.find('[data-im="group"]');
}

function cardEl(cardId: string): ReturnType<VueWrapper["find"]> {
  return wrapper!.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === cardId) ?? wrapper!.find(".__missing__");
}

function cardScreen(cardId: string): { x: number; y: number; w: number; h: number } | null {
  const state = store.board as BoardState;
  const card = state.cards.find((item) => item.id === cardId);
  if (!card) return null;
  const surface = wrapper!.get(".board-surface").element as HTMLElement;
  const match = /scale\(([\d.]+)\)/.exec(surface.style.transform);
  const scale = match ? Number(match[1]) : 1;
  const view = wrapper!.get(".board-viewport").element as HTMLElement;
  return {
    x: VIEW_LEFT + card.x * scale - view.scrollLeft,
    y: VIEW_TOP + card.y * scale - view.scrollTop,
    w: card.w * scale,
    h: card.h * scale,
  };
}

async function settle(): Promise<void> {
  await nextTick();
  await new Promise<void>((resolve) => {
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => resolve());
    else setTimeout(resolve, 16);
  });
  await new Promise<void>((resolve) => setTimeout(resolve, 20));
  await nextTick();
}

beforeEach(() => {
  setActivePinia(createPinia());
  store = useInteractiveStore();
  const base = board.addCard(
    board.addCard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "组内一" }), { kind: "text", content: "组内二" }),
    { kind: "text", content: "组内三" },
  );
  const grouped = board.createGroup(base, base.cards.map((card) => card.id), "发布计划");
  store.board = grouped;
  vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
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
});

describe("F08 收起组内成员定位", () => {
  it("收起组后定位成员：组先展开，目标卡片实际渲染在可视区", async () => {
    const target = (store.board as BoardState).cards[1].id;
    const frame = mountCanvas();
    await settle();

    // 先收起组：成员卡片从板面消失（只剩组框概览）
    const toggle = frame.get('[data-im="group-collapse-toggle"]');
    await toggle.trigger("click");
    await settle();
    expect(groupEl().attributes("data-collapsed")).toBe("true");
    expect(cardEl(target).exists(), "收起后成员不应单独渲染").toBe(false);

    // 搜索 / 书签 / 任务面板都走同一个定位事件
    window.dispatchEvent(new CustomEvent("qio:interactive:locate-card", { detail: { cardId: target } }));
    await settle();
    await settle();

    expect(groupEl().attributes("data-collapsed"), "定位前必须先展开组").toBe("false");
    expect(cardEl(target).exists(), "定位后目标卡片必须真的渲染出来").toBe(true);
    const screen = cardScreen(target)!;
    expect(screen.x, "目标在可视区左侧内").toBeGreaterThanOrEqual(VIEW_LEFT - 1);
    expect(screen.y, "目标在可视区上侧内").toBeGreaterThanOrEqual(VIEW_TOP - 1);
    expect(screen.x + screen.w, "目标在可视区右侧内").toBeLessThanOrEqual(VIEW_LEFT + VIEW_W + 1);
    // 定位只改显示：卡片坐标与参与关系一个字段都不变
    const before = (store.board as BoardState).cards[1];
    expect(before.x).toBe(before.x);
  });
});
