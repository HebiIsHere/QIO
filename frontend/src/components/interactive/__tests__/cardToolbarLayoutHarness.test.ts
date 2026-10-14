/**
 * jsdom 布局桩（{@link installCardToolbarLayout}）的自检与**反证**。
 *
 * 为什么要这个文件：渲染后几何断言的可信度取决于「桩本身像不像浏览器」。
 * 这里做两件事：
 * 1. 正向：在真实组件（BoardCanvas + BoardCard）上量到可解释的屏幕几何（scale 1 / 缩放 / 真实滚动）；
 * 2. 反向：人为搭出**基线缺陷结构**（工具栏挂在 .board-surface 里、行内 left/top 用客户端像素）
 *    与**宿主原点漏减**结构，证明同一套桩会把这几种错误量出来 —— 桩不是「永远返回 0」的摆设。
 *
 * 真实浏览器对照：D:\\qio-dev\\_tb-evidence 下的 probe_focus.mjs / probe_accept.mjs。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount, type VueWrapper } from "@vue/test-utils";
import { nextTick } from "vue";
import BoardCanvas from "../BoardCanvas.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
import type { BoardCard as BoardCardModel, BoardState } from "../../../interactive/types";
import { installCardToolbarLayout, parseScale, type CardToolbarHarness } from "./cardToolbarLayoutHarness";

const VIEW = { left: 40, top: 100, width: 1440, height: 800 };
const SURFACE = { w: 2800, h: 1600 };
const TOOLBAR = { w: 337.59, h: 35.25 };
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

async function flushFrame(times = 3): Promise<void> {
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
    /* 用例里已经卸载过 */
  }
  wrapper = null;
  document.body.innerHTML = "";
});

describe("布局桩 · 正向：真实组件上量得到可解释的几何", () => {
  it("scale=1：卡片外框 = 内容原点 + 板面坐标；工具栏 = 卡片左缘、底边上方 10px", async () => {
    store.board = board([mkCard("c1", 300, 200)], { selection: ["c1"] });
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    harness = installCardToolbarLayout(wrapper.get(".board-shell").element as HTMLElement, {
      viewport: VIEW,
      surfaceSize: SURFACE,
      toolbarSize: TOOLBAR,
    });
    await flushFrame();
    const m = harness.measure();
    expect(harness.warnings).toEqual([]);
    expect(m.cardRect).toMatchObject({ left: VIEW.left + 300, top: VIEW.top + 200, width: 260, height: 170 });
    expect(m.dx).toBeCloseTo(0, 3);
    expect(m.gap).toBeCloseTo(10, 3);
    expect(m.toolbarInsideSurface).toBe(false);
    expect(m.toolbarParent).toContain("card-toolbar-host");
  });

  it("滚动量参与换算：内容原点 = 可视区原点 − scrollLeft/Top", async () => {
    store.board = board([mkCard("c1", 300, 200)], { selection: ["c1"] });
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    harness = installCardToolbarLayout(wrapper.get(".board-shell").element as HTMLElement, {
      viewport: VIEW,
      surfaceSize: SURFACE,
      toolbarSize: TOOLBAR,
    });
    await flushFrame(2);
    harness.setScroll(500, 300);
    (wrapper.get(".board-viewport").element as HTMLElement).dispatchEvent(new Event("scroll"));
    await flushFrame();
    const m = harness.measure();
    expect(m.scroll).toEqual({ x: 500, y: 300 });
    expect(m.cardRect!.left).toBeCloseTo(VIEW.left + 300 - 500, 3);
    expect(m.dx).toBeCloseTo(0, 3);
    expect(m.gap).toBeCloseTo(10, 3);
  });
});

describe("布局桩 · 反向：同一套桩能把这些错误量出来", () => {
  it("基线结构（工具栏在 .board-surface 里、行内 left 是客户端像素）→ 报出被缩放放大的偏差", () => {
    document.body.innerHTML =
      '<div class="board-shell"><div class="board-main"><div class="board-viewport"><div class="board-scroll-content">' +
      '<div class="board-surface" style="transform: scale(2); transform-origin: 0 0; width: 4000px; height: 2000px;">' +
      '<article class="card" data-im="card" data-card-id="c1" style="left: 300px; top: 200px; width: 260px; height: 170px;"></article>' +
      '<div class="card-toolbar" data-im="card-toolbar" data-card-id="c1" style="left: 500px; top: 330px;"></div>' +
      "</div></div></div></div></div>";
    const local = installCardToolbarLayout(document.querySelector(".board-shell") as HTMLElement, {
      viewport: VIEW,
      surfaceSize: { w: 2000, h: 1000 },
      toolbarSize: TOOLBAR,
    });
    try {
      const m = local.measure();
      expect(m.toolbarInsideSurface).toBe(true);
      // 想放在屏幕 500，祖先 scale(2) 却把它放到 40 + 500×2：偏差 540
      expect(Math.abs(m.dx as number)).toBeGreaterThan(100);
      expect(Math.abs((m.gap as number) - 10)).toBeGreaterThan(100);
    } finally {
      local.restore();
    }
  });

  it("宿主原点漏减（行内 left/top 用了屏幕坐标而不是相对宿主）→ 报出偏差", () => {
    document.body.innerHTML =
      '<div class="board-shell"><div class="board-main"><div class="board-viewport"><div class="board-scroll-content">' +
      '<div class="board-surface" style="transform: scale(1); transform-origin: 0 0; width: 2800px; height: 1600px;">' +
      '<article class="card" data-im="card" data-card-id="c1" style="left: 300px; top: 200px; width: 260px; height: 170px;"></article>' +
      "</div></div>" +
      '<div class="card-toolbar-host" data-im="card-toolbar-host">' +
      // 忘记减去宿主原点（VIEW.left=40 / VIEW.top=100）时的错误写法
      '<div class="card-toolbar" data-im="card-toolbar" data-card-id="c1" style="left: 340px; top: 254.75px;"></div>' +
      "</div></div></div></div>";
    const local = installCardToolbarLayout(document.querySelector(".board-shell") as HTMLElement, {
      viewport: VIEW,
      surfaceSize: SURFACE,
      toolbarSize: TOOLBAR,
    });
    try {
      const m = local.measure();
      expect(m.cardRect!.left).toBeCloseTo(340, 3);
      expect(Math.abs((m.dx as number) - 40)).toBeLessThan(0.5);
      expect(Math.abs(m.gap as number)).toBeGreaterThan(50);
    } finally {
      local.restore();
    }
  });

  it("parseScale 认得 matrix / scale 两种写法", () => {
    expect(parseScale("matrix(2.5, 0, 0, 2.5, 0, 0)")).toEqual({ x: 2.5, y: 2.5 });
    expect(parseScale("scale(0.25)")).toEqual({ x: 0.25, y: 0.25 });
    expect(parseScale("none")).toEqual({ x: 1, y: 1 });
  });
});
