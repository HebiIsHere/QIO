/**
 * 无限点阵画布（2026-11-25）· 组件级定向回归。
 *
 * 这一轮把「有限滚动模型」换成「视口 + 世界层」：
 * - 唯一视图状态是 Viewport { x, y, scale }，视口自己裁切，内容层只做一次 translate + scale；
 * - 空板也能向四个方向连续平移（旧模型靠容器滚动，负方向立刻到边界）；
 * - 板面坐标有符号：拖动 A 跨过原点只改 A，独立卡片 B 的坐标 / 更新时间 / 撤销记录都不变；
 * - 平移 / 缩放只改查看状态：不 commit、不排保存、不进卡片撤销栈；
 * - 「查看全部」把可展示内容（含负坐标）放进扣掉遮挡的可用区，空板回到初始视野与 100%。
 *
 * jsdom 没有真实布局：容器几何是打桩的（与 boardCanvas.test.ts 同一套做法）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import BoardCanvas from "../BoardCanvas.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
import type { BoardCard as BoardCardModel, BoardState } from "../../../interactive/types";

const VIEW_LEFT = 40;
const VIEW_TOP = 100;
const VIEW_W = 1200;
const VIEW_H = 800;
const STAMP = "2026-11-25T00:00:00.000Z";

function mkCard(id: string, x: number, y: number, w = 260, h = 170, patch: Partial<BoardCardModel> = {}): BoardCardModel {
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
  return new MouseEvent(type, { clientX: x, clientY: y, button: 0, bubbles: true, cancelable: true });
}

interface ViewState {
  scale: number;
  offsetX: number;
  offsetY: number;
}

function viewState(wrapper: VueWrapper): ViewState {
  const surface = wrapper.get(".board-surface").element as HTMLElement;
  const transform = surface.style.transform;
  const scaleMatch = /scale\(([\d.]+)\)/.exec(transform);
  const translateMatch = /translate\((-?[\d.]+)px,\s*(-?[\d.]+)px\)/.exec(transform);
  return {
    scale: scaleMatch ? Number(scaleMatch[1]) : Number.NaN,
    offsetX: translateMatch ? Number(translateMatch[1]) : 0,
    offsetY: translateMatch ? Number(translateMatch[2]) : 0,
  };
}

function screenOf(boardX: number, boardY: number, state: ViewState): { x: number; y: number } {
  return { x: VIEW_LEFT + boardX * state.scale + state.offsetX, y: VIEW_TOP + boardY * state.scale + state.offsetY };
}

let wrapper: VueWrapper | null = null;
let store: ReturnType<typeof useInteractiveStore>;
let labels: string[];

function mountCanvas(): VueWrapper {
  wrapper = mount(BoardCanvas, { attachTo: document.body });
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  const shell = wrapper.get(".board-shell").element as HTMLElement;
  const rect = (left: number, top: number, width: number, height: number) =>
    ({ left, top, right: left + width, bottom: top + height, width, height, x: left, y: top, toJSON: () => ({}) }) as DOMRect;
  view.getBoundingClientRect = () => rect(VIEW_LEFT, VIEW_TOP, VIEW_W, VIEW_H);
  shell.getBoundingClientRect = () => rect(VIEW_LEFT, VIEW_TOP, VIEW_W, VIEW_H);
  Object.defineProperty(view, "clientWidth", { value: VIEW_W, configurable: true });
  Object.defineProperty(view, "clientHeight", { value: VIEW_H, configurable: true });
  return wrapper;
}

/** 用真实平移手势把视图移到 (dx, dy)。 */
async function panBy(dx: number, dy: number): Promise<void> {
  const view = wrapper!.get(".board-viewport").element as HTMLElement;
  const startX = VIEW_LEFT + 20;
  const startY = VIEW_TOP + 20;
  view.dispatchEvent(pointer("pointerdown", startX, startY));
  window.dispatchEvent(pointer("pointermove", startX + dx, startY + dy));
  await nextTick();
  window.dispatchEvent(pointer("pointerup", startX + dx, startY + dy));
  await nextTick();
}

async function flushFrame(times = 2): Promise<void> {
  for (let i = 0; i < times; i += 1) {
    await nextTick();
    await new Promise<void>((resolve) => {
      if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => resolve());
      else setTimeout(resolve, 16);
    });
    await nextTick();
  }
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  store = useInteractiveStore();
  store.board = board([]);
  labels = [];
  vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
    labels.push(label);
    store.board = { ...next, boardId: store.boardId };
  });
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("空板：四个方向连续平移", () => {
  it("没有卡片也能向四个方向各拖过两个屏幕，回到起点后点阵相位一致", async () => {
    mountCanvas();
    const gridOf = () => {
      const style = (wrapper!.get(".board-viewport").element as HTMLElement).style;
      return { size: style.backgroundSize, position: style.backgroundPosition };
    };
    const start = gridOf();
    expect(start.position).toBe("0px 0px");

    // 向左上拖 2 个屏幕：世界原点右移下移
    await panBy(VIEW_W * 2, VIEW_H * 2);
    expect(viewState(wrapper!).offsetX).toBe(VIEW_W * 2);
    expect(viewState(wrapper!).offsetY).toBe(VIEW_H * 2);

    // 向右下拖回起点：四个方向都连续，没有边界
    await panBy(-VIEW_W * 2, -VIEW_H * 2);
    expect(viewState(wrapper!).offsetX).toBeCloseTo(0, 6);
    expect(viewState(wrapper!).offsetY).toBeCloseTo(0, 6);
    // 回到起点后点阵相位一致（同一个世界原点 → 同一个相位）
    expect(gridOf()).toEqual(start);
    // 全程没有板面保存 / 撤销 / 提交记录
    expect(labels).toEqual([]);
    expect(store.undoStack).toHaveLength(0);
  });

  it("负方向平移：点阵相位用取模收口，不会出现负的 background-position", async () => {
    mountCanvas();
    await panBy(-VIEW_W, -VIEW_H);
    const style = (wrapper!.get(".board-viewport").element as HTMLElement).style;
    const [px, py] = style.backgroundPosition.split(" ").map((item) => Number.parseFloat(item));
    expect(px).toBeGreaterThanOrEqual(0);
    expect(px).toBeLessThan(24);
    expect(py).toBeGreaterThanOrEqual(0);
    expect(py).toBeLessThan(24);
  });
});

describe("负坐标：拖动只改本次应移动的对象", () => {
  it("拖动 A 跨过原点：B 的坐标 / 更新时间 / 撤销记录都不变", async () => {
    const bStamp = "2026-11-20T00:00:00.000Z";
    store.board = board([mkCard("a", 100, 100), mkCard("b", 600, 400, 260, 170, { updatedAt: bStamp })]);
    mountCanvas();
    await flushFrame();

    const state = viewState(wrapper!);
    const cardA = wrapper!.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === "a")!;
    const press = screenOf(100 + 130, 100 + 85, state);
    cardA.element.dispatchEvent(pointer("pointerdown", press.x, press.y));
    await nextTick();
    // 拖到负坐标区域：板面 (-400, -300)
    window.dispatchEvent(pointer("pointermove", screenOf(-400, -300, state).x, screenOf(-400, -300, state).y));
    await nextTick();
    window.dispatchEvent(pointer("pointerup", screenOf(-400, -300, state).x, screenOf(-400, -300, state).y));
    await nextTick();

    const after = store.board!;
    const movedA = after.cards.find((card) => card.id === "a")!;
    const untouchedB = after.cards.find((card) => card.id === "b")!;
    // A 真的到了负坐标
    expect(movedA.x).toBeLessThan(0);
    expect(movedA.y).toBeLessThan(0);
    // B 一个字段都没动（坐标 + 更新时间）
    expect({ x: untouchedB.x, y: untouchedB.y }).toEqual({ x: 600, y: 400 });
    expect(untouchedB.updatedAt).toBe(bStamp);
    // 只有「选中 + 移动 A」两次提交，没有整板重定位（旧模型会额外平移整块板面）
    expect(labels).toEqual(["选择卡片", "移动卡片"]);
  });

  it("负坐标卡片正常渲染：世界层坐标就是板面坐标（不做任何偏移补偿）", async () => {
    store.board = board([mkCard("neg", -8000, -6000)]);
    mountCanvas();
    await flushFrame();
    const card = wrapper!.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === "neg")!;
    const style = card.element.getAttribute("style") ?? "";
    expect(style).toContain("left: -8000px");
    expect(style).toContain("top: -6000px");
  });
});

describe("平移 / 缩放只改查看状态", () => {
  it("滚轮缩放：指针下的板面点留在同一屏幕位置，且不产生任何提交", async () => {
    store.board = board([mkCard("a", 300, 200)]);
    mountCanvas();
    await flushFrame();
    const before = viewState(wrapper!);
    const anchor = screenOf(300, 200, before);
    const view = wrapper!.get(".board-viewport").element as HTMLElement;
    view.dispatchEvent(new WheelEvent("wheel", { clientX: anchor.x, clientY: anchor.y, deltaY: -462, deltaMode: 0, bubbles: true, cancelable: true }));
    await nextTick();
    const after = viewState(wrapper!);
    expect(after.scale).toBeGreaterThan(1.9);
    const anchorAfter = screenOf(300, 200, after);
    expect(anchorAfter.x).toBeCloseTo(anchor.x, 2);
    expect(anchorAfter.y).toBeCloseTo(anchor.y, 2);
    expect(labels).toEqual([]);
    expect(store.undoStack).toHaveLength(0);
  });

  it("缩放后拖动卡片：位移按板面单位换算（屏幕位移 ÷ 缩放）", async () => {
    store.board = board([mkCard("a", 0, 0, 100, 60)]);
    mountCanvas();
    await flushFrame();
    const view = wrapper!.get(".board-viewport").element as HTMLElement;
    view.dispatchEvent(new WheelEvent("wheel", { clientX: VIEW_LEFT + 50, clientY: VIEW_TOP + 30, deltaY: -462, deltaMode: 0, bubbles: true, cancelable: true }));
    await nextTick();
    const state = viewState(wrapper!);
    expect(state.scale).toBeGreaterThan(1.9);

    const cardA = wrapper!.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === "a")!;
    const start = screenOf(50, 30, state);
    cardA.element.dispatchEvent(pointer("pointerdown", start.x, start.y));
    await nextTick();
    window.dispatchEvent(pointer("pointermove", start.x + 100, start.y + 60));
    await nextTick();
    window.dispatchEvent(pointer("pointerup", start.x + 100, start.y + 60));
    await nextTick();
    const moved = store.board!.cards[0];
    // 100 屏幕像素 ÷ 约 2 倍缩放 ≈ 50 板面单位
    expect(moved.x).toBeCloseTo(50, 0);
    expect(moved.y).toBeCloseTo(30, 0);
  });
});

describe("查看全部", () => {
  it("两张卡分处原点两侧：点击后两卡同时落在可用区，且比例可以低于 25%", async () => {
    // 相距 20000：可用区 1200 宽 → 需要约 0.06 的适配比例
    store.board = board([mkCard("left", -10000, 0), mkCard("right", 10000, 0)]);
    mountCanvas();
    await flushFrame();
    window.dispatchEvent(new CustomEvent("qio:interactive:view-all"));
    await flushFrame();

    const state = viewState(wrapper!);
    expect(state.scale).toBeLessThan(0.25);
    for (const card of store.board!.cards) {
      const screen = screenOf(card.x, card.y, state);
      expect(screen.x).toBeGreaterThanOrEqual(VIEW_LEFT - 1);
      expect(screen.x + card.w * state.scale).toBeLessThanOrEqual(VIEW_LEFT + VIEW_W + 1);
      expect(screen.y).toBeGreaterThanOrEqual(VIEW_TOP - 1);
      expect(screen.y + card.h * state.scale).toBeLessThanOrEqual(VIEW_TOP + VIEW_H + 1);
    }
    // 只改视图：卡片坐标 / 选中 / 连线一个字段都不改
    expect(store.board!.cards.map((card) => ({ x: card.x, y: card.y }))).toEqual([
      { x: -10000, y: 0 },
      { x: 10000, y: 0 },
    ]);
    expect(labels).toEqual([]);
  });

  it("查看全部之后再滚一级：不会跳回 25%", async () => {
    store.board = board([mkCard("left", -10000, 0), mkCard("right", 10000, 0)]);
    mountCanvas();
    await flushFrame();
    window.dispatchEvent(new CustomEvent("qio:interactive:view-all"));
    await flushFrame();
    const fitted = viewState(wrapper!).scale;
    expect(fitted).toBeLessThan(0.25);

    const view = wrapper!.get(".board-viewport").element as HTMLElement;
    view.dispatchEvent(new WheelEvent("wheel", { clientX: VIEW_LEFT + 600, clientY: VIEW_TOP + 400, deltaY: -100, deltaMode: 0, bubbles: true, cancelable: true }));
    await nextTick();
    const zoomed = viewState(wrapper!).scale;
    // 放大一级：仍然远低于 0.25，没有「跳回 0.25」的突变
    expect(zoomed).toBeGreaterThan(fitted);
    expect(zoomed).toBeLessThan(0.25);
  });

  it("空板：回到初始视野与 100%", async () => {
    store.board = board([]);
    mountCanvas();
    await flushFrame();
    await panBy(-500, -400);
    expect(viewState(wrapper!).offsetX).toBe(-500);

    window.dispatchEvent(new CustomEvent("qio:interactive:view-all"));
    await flushFrame();
    const state = viewState(wrapper!);
    expect(state.scale).toBe(1);
    expect(state.offsetX).toBe(0);
    expect(state.offsetY).toBe(0);
  });

  it("单卡：居中且不被异常放大（比例上限 1）", async () => {
    store.board = board([mkCard("only", 500, 400, 260, 170)]);
    mountCanvas();
    await flushFrame();
    window.dispatchEvent(new CustomEvent("qio:interactive:view-all"));
    await flushFrame();
    const state = viewState(wrapper!);
    expect(state.scale).toBe(1);
    // 卡片中心落在可用区中心
    const center = screenOf(500 + 130, 400 + 85, state);
    expect(center.x).toBeCloseTo(VIEW_LEFT + VIEW_W / 2, 0);
    expect(center.y).toBeCloseTo(VIEW_TOP + VIEW_H / 2, 0);
  });

  it("折叠组用折叠后真正显示的外框参与包围盒（不按成员原始位置撑大）", async () => {
    store.board = board([mkCard("m1", 0, 0), mkCard("m2", 400, 0)], {
      groups: [
        {
          id: "g1",
          name: "组",
          defaultName: true,
          ordered: false,
          x: -16,
          y: -30,
          w: 700,
          h: 260,
          members: ["m1", "m2"],
          deleted: false,
          createdAt: STAMP,
          updatedAt: STAMP,
        },
      ],
    });
    mountCanvas();
    await flushFrame();
    // 收起组：成员卡片不再单独画，组框压成紧凑框
    await wrapper!.get('[data-im="group-collapse-toggle"]').trigger("click");
    await flushFrame();
    window.dispatchEvent(new CustomEvent("qio:interactive:view-all"));
    await flushFrame();
    const state = viewState(wrapper!);
    // 收起后组框高度 ≤ 120：适配比例不会被成员原始高度撑小
    expect(state.scale).toBeGreaterThan(0.5);
    expect(labels.filter((label) => label.includes("收起"))).toHaveLength(1);
  });
});

describe("双击空白处建卡", () => {
  it("在负坐标区域双击：新卡落在指针对应的板面坐标，并进入选中", async () => {
    store.board = board([]);
    mountCanvas();
    await flushFrame();
    // 先把视野平移到负坐标区域
    await panBy(VIEW_W, VIEW_H);
    const state = viewState(wrapper!);
    const point = screenOf(-500, -400, state);
    const view = wrapper!.get(".board-viewport").element as HTMLElement;
    // 真实双击 = 两次 pointerdown/up + 一次 dblclick（浏览器就是这么派发的）
    for (let i = 0; i < 2; i += 1) {
      view.dispatchEvent(pointer("pointerdown", point.x, point.y));
      window.dispatchEvent(pointer("pointerup", point.x, point.y));
      await nextTick();
    }
    view.dispatchEvent(new MouseEvent("dblclick", { clientX: point.x, clientY: point.y, button: 0, bubbles: true, cancelable: true }));
    await flushFrame();

    expect(store.board!.cards).toHaveLength(1);
    const created = store.board!.cards[0];
    expect(created.kind).toBe("text");
    expect(created.x).toBeCloseTo(-500, 0);
    expect(created.y).toBeCloseTo(-400, 0);
    // 建卡 + 选中是一次提交（撤销一次就回到建卡前）
    expect(labels).toEqual(["双击空白处新建文字卡"]);
    expect(store.board!.selection).toEqual([created.id]);
  });

  it("双击卡片不穿透建卡", async () => {
    store.board = board([mkCard("a", 100, 100)]);
    mountCanvas();
    await flushFrame();
    const state = viewState(wrapper!);
    const card = wrapper!.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === "a")!;
    const point = screenOf(100 + 130, 100 + 85, state);
    card.element.dispatchEvent(new MouseEvent("dblclick", { clientX: point.x, clientY: point.y, button: 0, bubbles: true, cancelable: true }));
    await flushFrame();
    expect(store.board!.cards).toHaveLength(1);
    expect(labels).toEqual([]);
  });

  it("完成一次拖动后不误建卡（浏览器仍会派发 dblclick）", async () => {
    store.board = board([]);
    mountCanvas();
    await flushFrame();
    await panBy(-300, -200);
    const view = wrapper!.get(".board-viewport").element as HTMLElement;
    view.dispatchEvent(new MouseEvent("dblclick", { clientX: VIEW_LEFT + 400, clientY: VIEW_TOP + 300, button: 0, bubbles: true, cancelable: true }));
    await flushFrame();
    expect(store.board!.cards).toHaveLength(0);
    expect(labels).toEqual([]);
  });
});
