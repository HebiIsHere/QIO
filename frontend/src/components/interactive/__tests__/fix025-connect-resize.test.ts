/**
 * 0.2.5 回归修复定向用例（W2）：连接点 × 卡片尺寸调整。
 *
 * 基线复现（Lead 的真实浏览器证据，msedge + 真实鼠标，基线 = e590bfac）：
 * 选中卡片后连接点元素在（4 个 12×12），但连接点中心 elementFromPoint 命中的是
 * class="rz-hit rz-hit-n" / data-resize-handle="n" —— 被尺寸热区挡住；在该点 page.mouse.down()
 * 后 [data-im=link-draft] 数量 = 0，继续拖动卡片高度 170 → 96（真的是尺寸调整生效）。
 *
 * 这个文件证明修复后的结构与接线：
 * 1. 连接点与尺寸把手在**同一个不随画布缩放的交互层**（BoardResizeLayer）里，连接点在更高 z-index 上；
 *    命中区是屏幕像素 18×18（视觉圆点仍是 12px），尺寸热区一个都没缩（边 10px / 角 16px）；
 * 2. 命中规则纯几何：连接点 → 拖线，其余边 / 角 → 尺寸调整，两种操作同时可用、不需要切换模式；
 * 3. 同一指针只驱动一种手势：开始任一手势前清掉其余手势；拖线中不移动 / 不拉伸 / 不成组；
 * 4. 拖线在 Esc / pointercancel / 失焦 / 卸载后都不留半条关系，并立刻恢复尺寸调整能力；
 * 5. hitCardAt 认**当前显示矩形**（displayRect）：展开、让位、resize、平移缩放之后落点仍然准；
 *    临时显示坐标只读，绝不写回板面。
 *
 * 注意：jsdom 没有真实布局，容器几何是打桩的（与 boardCanvas.test.ts / irl-c-canvas.test.ts 同一套做法）。
 * 「连接点中心 elementFromPoint 命中连接入口」的硬证据由 W3 在真实浏览器（Playwright + msedge）里出；
 * 这里只保证结构、层级、命中区尺寸与手势接线具备那个结论成立的条件。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
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
const STAMP = "2026-10-14T00:00:00.000Z";

const SRC = resolve(process.cwd(), "src");

function source(relativePath: string): string {
  return readFileSync(resolve(SRC, relativePath), "utf8");
}

function mkCard(id: string, x: number, y: number, w = 100, h = 60, patch: Partial<BoardCardModel> = {}): BoardCardModel {
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

/** jsdom 里 pointer 事件用 MouseEvent 就够（既有用例同一做法）。 */
function pointer(type: string, x: number, y: number): MouseEvent {
  return new MouseEvent(type, { clientX: x, clientY: y, bubbles: true, cancelable: true });
}

function styleOf(element: Element | null | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of (element?.getAttribute("style") ?? "").split(";")) {
    const at = part.indexOf(":");
    if (at < 0) continue;
    out[part.slice(0, at).trim()] = part.slice(at + 1).trim();
  }
  return out;
}

function px(value: string | undefined): number {
  return Number.parseFloat((value ?? "").replace("px", ""));
}

function rectOf(element: Element | null | undefined): { x: number; y: number; w: number; h: number } {
  const style = styleOf(element);
  return { x: px(style.left), y: px(style.top), w: px(style.width), h: px(style.height) };
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

/** 让容器「真的能滚动」：jsdom 里 scrollLeft 才能被写进 DOM（坐标换算的事实来源）。 */
function makeScrollable(view: HTMLElement, left = 0, top = 0): void {
  Object.defineProperty(view, "scrollWidth", { value: VIEW_W * 2, configurable: true });
  Object.defineProperty(view, "scrollHeight", { value: VIEW_H * 2, configurable: true });
  Object.defineProperty(view, "scrollLeft", { value: left, writable: true, configurable: true });
  Object.defineProperty(view, "scrollTop", { value: top, writable: true, configurable: true });
}

function viewState(): { scale: number; scrollLeft: number; scrollTop: number } {
  const surface = wrapper!.get(".board-surface").element as HTMLElement;
  const view = wrapper!.get(".board-viewport").element as HTMLElement;
  const match = /scale\(([\d.]+)\)/.exec(surface.style.transform);
  return {
    scale: match ? Number(match[1]) : Number.NaN,
    scrollLeft: view.scrollLeft,
    scrollTop: view.scrollTop,
  };
}

/** 板面坐标 → 屏幕坐标（与 BoardCanvas 的换算口径一致）。 */
function screenOf(boardX: number, boardY: number, state: { scale: number; scrollLeft: number; scrollTop: number }) {
  return {
    x: VIEW_LEFT + boardX * state.scale - state.scrollLeft,
    y: VIEW_TOP + boardY * state.scale - state.scrollTop,
  };
}

function cardEl(cardId: string): Element | null {
  const found = wrapper!.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === cardId);
  return found ? found.element : null;
}

function connectHostStyle(cardId: string): Record<string, string> {
  return styleOf(wrapper!.find('[data-connect-card-id="' + cardId + '"]').element);
}

function resizeHostStyle(cardId: string): Record<string, string> {
  return styleOf(wrapper!.find('[data-resize-card-id="' + cardId + '"]').element);
}

function connectPoints(): ReturnType<VueWrapper["findAll"]> {
  return wrapper!.findAll('[data-im="connect-point"]');
}

function resizeHandles(): ReturnType<VueWrapper["findAll"]> {
  return wrapper!.findAll("[data-resize-handle]");
}

/** 只在连接点上按下（进入拖线），坐标与命中判定无关，取一个板面里的合理点即可。 */
async function pressConnectPoint(index = 0, board: { x: number; y: number } = { x: 50, y: 9 }): Promise<void> {
  const state = viewState();
  const screen = screenOf(board.x, board.y, state);
  connectPoints()[index].element.dispatchEvent(pointer("pointerdown", screen.x, screen.y));
  await nextTick();
}

function draftTarget(): string {
  return wrapper!.get('[data-im="link-draft"]').attributes("data-target") ?? "";
}

function moveTo(boardX: number, boardY: number): void {
  const screen = screenOf(boardX, boardY, viewState());
  window.dispatchEvent(pointer("pointermove", screen.x, screen.y));
}

function releaseAt(boardX: number, boardY: number): void {
  const screen = screenOf(boardX, boardY, viewState());
  window.dispatchEvent(pointer("pointerup", screen.x, screen.y));
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
    /* 用例里已经卸载过 */
  }
  wrapper = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("结构：连接点与尺寸热区在同一个不随画布缩放的交互层，连接点在上面", () => {
  it("恰好选中一张未折叠卡片：交互层里有 4 个连接点，属性齐全，且不在卡片 / 尺寸宿主里面", async () => {
    store.board = board([mkCard("a", 0, 0), mkCard("b", 400, 0)], { selection: ["a"] });
    const view = mountCanvas();

    const points = connectPoints();
    expect(points).toHaveLength(4);
    expect(points.map((point) => point.attributes("data-side"))).toEqual(["top", "right", "bottom", "left"]);
    for (const point of points) {
      expect(point.attributes("data-card-id")).toBe("a");
      expect(point.attributes("title")).toContain("建立关系");
    }

    const layer = view.get('[data-im="resize-layer"]').element;
    for (const point of points) {
      // 与尺寸把手同一层
      expect(point.element.closest('[data-im="resize-layer"]')).toBe(layer);
      // 但不在尺寸宿主里（所以在同层可以排到热区之上，不靠 DOM 顺序）
      expect(point.element.closest("[data-resize-card-id]")).toBeNull();
      // 也不在卡片里：卡片处在 .board-surface 的缩放层叠上下文里，画在那里就会被热区盖住
      expect(point.element.closest('[data-im="card"]')).toBeNull();
    }
    // 这一层不在带 transform: scale() 的板面里 —— 18px / 10px / 16px 都是真实屏幕像素
    expect(layer.closest(".board-surface")).toBeNull();

    // 两种操作同时可用：连接点 4 个、八向热区 8 个，同屏存在，不需要切换模式
    expect(view.find('[data-resize-card-id="a"]').exists()).toBe(true);
    expect(resizeHandles()).toHaveLength(8);
    await nextTick();
  });

  it("多选 / 折叠 / 未选中：都不出现连接点（与改动前的显示条件一致）", async () => {
    store.board = board([mkCard("a", 0, 0), mkCard("b", 400, 0)], { selection: ["a", "b"] });
    const view = mountCanvas();
    expect(connectPoints()).toHaveLength(0);

    store.board = board([mkCard("a", 0, 0, 100, 60, { folded: true })], { selection: ["a"] });
    await nextTick();
    expect(connectPoints()).toHaveLength(0);

    store.board = board([mkCard("a", 0, 0)], { selection: [] });
    await nextTick();
    expect(connectPoints()).toHaveLength(0);
    expect(view.find('[data-im="resize-layer"]').exists()).toBe(true);
  });

  it("位置统一：连接点宿主 = 尺寸把手宿主 = 卡片外框（displayRect × scale）", async () => {
    store.board = board([mkCard("a", 100, 100, 120, 80), mkCard("b", 600, 400)], { selection: ["a"] });
    mountCanvas();
    const official = { left: "100px", top: "100px", width: "120px", height: "80px" };
    expect(styleOf(cardEl("a"))).toMatchObject(official);
    expect(connectHostStyle("a")).toMatchObject(official);
    expect(resizeHostStyle("a")).toMatchObject(official);

    // 缩放后三者仍然一致（把手层与连接点层都在未缩放的内容层里，位置 = 显示矩形 × scale）
    const view = wrapper!.get(".board-viewport").element as HTMLElement;
    view.dispatchEvent(new WheelEvent("wheel", { clientX: 500, clientY: 400, deltaY: -462, deltaMode: 0, bubbles: true, cancelable: true }));
    await nextTick();
    const state = viewState();
    expect(state.scale).toBeGreaterThan(1.9);
    expect(connectHostStyle("a")).toMatchObject({
      left: 100 * state.scale + "px",
      top: 100 * state.scale + "px",
      width: 120 * state.scale + "px",
      height: 80 * state.scale + "px",
    });
    expect(connectHostStyle("a")).toEqual(resizeHostStyle("a"));

    // 展开让位（显示槽位 ≠ 正式位置）时也跟着显示矩形走，不掉回正式位置
    store.toggleCardPreview("a");
    await nextTick();
    const expanded = rectOf(cardEl("a"));
    expect(expanded.w).toBeGreaterThan(120);
    expect(connectHostStyle("a")).toMatchObject({
      left: expanded.x * state.scale + "px",
      top: expanded.y * state.scale + "px",
      width: expanded.w * state.scale + "px",
      height: expanded.h * state.scale + "px",
    });
    expect(connectHostStyle("a")).toEqual(resizeHostStyle("a"));
  });

  it("样式守卫：命中区 ≥18px、圆点仍 12px、连接点在尺寸热区之上、尺寸热区没被缩小", () => {
    const layer = source("components/interactive/BoardResizeLayer.vue");
    const layerStyle = layer.slice(layer.indexOf("<style"));
    const handles = source("components/interactive/ResizeHandles.vue");
    const handleStyle = handles.slice(handles.indexOf("<style"));
    const tokens = readFileSync(resolve(SRC, "styles/tokens.css"), "utf8");

    const pointBlock = /\.connect-point\s*\{([^}]*)\}/.exec(layerStyle)?.[1] ?? "";
    expect(pointBlock).toMatch(/width:\s*18px/);
    expect(pointBlock).toMatch(/height:\s*18px/);
    expect(pointBlock).toMatch(/pointer-events:\s*auto/);
    expect(pointBlock).toMatch(/cursor:\s*crosshair/);
    // 视觉圆点仍是 12px（命中区变大，看起来没变重）
    const dotBlock = /\.connect-point::after\s*\{([^}]*)\}/.exec(layerStyle)?.[1] ?? "";
    expect(dotBlock).toMatch(/width:\s*12px/);
    expect(dotBlock).toMatch(/height:\s*12px/);

    // 同层 z-index：连接点宿主 > 尺寸宿主；整层不吃指针，只有命中区吃
    const resizeHost = /\.resize-host\s*\{([^}]*)\}/.exec(layerStyle)?.[1] ?? "";
    const connectHost = /\.connect-host\s*\{([^}]*)\}/.exec(layerStyle)?.[1] ?? "";
    const layerRoot = /\.resize-layer\s*\{([^}]*)\}/.exec(layerStyle)?.[1] ?? "";
    expect(Number(/z-index:\s*(\d+)/.exec(connectHost)?.[1])).toBeGreaterThan(Number(/z-index:\s*(\d+)/.exec(resizeHost)?.[1]));
    expect(layerRoot).toMatch(/pointer-events:\s*none/);
    expect(connectHost).toMatch(/pointer-events:\s*none/);

    // 尺寸热区一个都没缩：边 10px、角 16px（与改动前逐字段相同）
    expect(/\.rz-hit-n,\s*\.rz-hit-s\s*\{([^}]*)\}/.exec(handleStyle)?.[1] ?? "").toMatch(/height:\s*10px/);
    expect(/\.rz-hit-e,\s*\.rz-hit-w\s*\{([^}]*)\}/.exec(handleStyle)?.[1] ?? "").toMatch(/width:\s*10px/);
    const corner = /\.rz-hit-ne,\s*\.rz-hit-nw,\s*\.rz-hit-se,\s*\.rz-hit-sw\s*\{([^}]*)\}/.exec(handleStyle)?.[1] ?? "";
    expect(corner).toMatch(/width:\s*16px/);
    expect(corner).toMatch(/height:\s*16px/);

    // 颜色只走令牌，且用到的令牌都在 tokens.css 里定义
    expect(layerStyle).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(layerStyle).not.toMatch(/\b(?:rgb|rgba|hsl|hsla)\(/);
    const used = Array.from(layerStyle.matchAll(/var\((--[a-z0-9-]+)/g)).map((match) => match[1]);
    const missing = Array.from(new Set(used)).filter((name) => !new RegExp(name + "\\s*:").test(tokens));
    expect(missing).toEqual([]);
  });
});

describe("命中与手势：连接点拖线、尺寸调整、正文拖动各归各", () => {
  it("连接点按下即进入拖线：拖动 / 平移 / 框选被清掉，拖线过程中卡片不移动也不变形", async () => {
    store.board = board([mkCard("a", 0, 0), mkCard("b", 400, 0)], { selection: ["a"] });
    mountCanvas();

    // 先在卡片正文上按住并拖出去：确认拖动真的在进行（同一指针只驱动一种手势的前置）
    cardEl("a")!.dispatchEvent(pointer("pointerdown", VIEW_LEFT + 10, VIEW_TOP + 30));
    await nextTick();
    moveTo(410, 330);
    await nextTick();
    expect(rectOf(cardEl("a")).x).toBeGreaterThan(0);

    // 再按连接点：拖动必须被清掉，卡片回到操作前的位置
    await pressConnectPoint();
    expect(wrapper!.find('[data-im="link-draft"]').exists()).toBe(true);
    expect(wrapper!.find('[data-im="link-draft"]').attributes("data-from")).toBe("a");
    expect(rectOf(cardEl("a"))).toEqual({ x: 0, y: 0, w: 100, h: 60 });
    expect(store.board?.cards.find((card) => card.id === "a")).toMatchObject({ x: 0, y: 0, w: 100, h: 60 });
    expect(wrapper!.get(".board-viewport").classes()).not.toContain("panning");
    expect(wrapper!.find(".select-rect").exists()).toBe(false);

    // 向右下拖 60 屏幕像素（≈ 60 板面单位）：如果被尺寸热区接走（n 把手），高度会变成 120
    moveTo(50, 60);
    await nextTick();
    expect(rectOf(cardEl("a"))).toEqual({ x: 0, y: 0, w: 100, h: 60 });
    expect(draftTarget()).toBe("");
    expect(labels.filter((label) => label.includes("调整卡片尺寸"))).toHaveLength(0);
    expect(labels.filter((label) => label.includes("移动卡片"))).toHaveLength(0);
  });

  it("松手到另一张卡片建立 1 条关系并打开方向 / 含义编辑；空白或自身不建立", async () => {
    store.board = board([mkCard("a", 0, 0), mkCard("b", 400, 0)], { selection: ["a"] });
    mountCanvas();

    await pressConnectPoint();
    moveTo(450, 30); // b 的显示中心
    await nextTick();
    expect(draftTarget()).toBe("b");
    releaseAt(450, 30);
    await nextTick();
    expect(store.board?.links).toHaveLength(1);
    expect(labels.filter((label) => label.includes("新建关系"))).toHaveLength(1);
    expect(wrapper!.find('[data-im="link-meaning"]').exists()).toBe(true);
    expect(store.board?.links[0].meaning).toBe("");

    // 拖到空白：目标失效，松手不建链
    await pressConnectPoint();
    moveTo(2000, 1200);
    await nextTick();
    expect(draftTarget()).toBe("");
    releaseAt(2000, 1200);
    await nextTick();
    expect(store.board?.links).toHaveLength(1);
    expect(wrapper!.text()).toContain("没有建立关系");

    // 拖回自身：不算有效目标
    await pressConnectPoint();
    moveTo(50, 30);
    await nextTick();
    expect(draftTarget()).toBe("");
    releaseAt(50, 30);
    await nextTick();
    expect(store.board?.links).toHaveLength(1);
  });

  it("拖线进行中按住卡片正文：不移动卡片，拖线照常完成（同一指针只驱动一种手势）", async () => {
    store.board = board([mkCard("a", 0, 0), mkCard("b", 400, 0)], { selection: ["a"] });
    mountCanvas();
    await pressConnectPoint();

    // 第二个按下来自卡片正文（多指针场景）：不允许开始拖动，更不允许把拖线挤掉
    cardEl("a")!.dispatchEvent(pointer("pointerdown", VIEW_LEFT + 10, VIEW_TOP + 30));
    await nextTick();
    moveTo(410, 330);
    await nextTick();
    expect(rectOf(cardEl("a"))).toEqual({ x: 0, y: 0, w: 100, h: 60 });

    moveTo(450, 30);
    await nextTick();
    expect(draftTarget()).toBe("b");
    releaseAt(450, 30);
    await nextTick();
    expect(store.board?.links).toHaveLength(1);
  });

  it("拉伸进行中按连接点：尺寸预览被清掉且不提交，改由拖线接管", async () => {
    store.board = board([mkCard("a", 0, 0), mkCard("b", 400, 0)], { selection: ["a"] });
    const view = mountCanvas();
    const handle = view.get('[data-resize-card-id="a"] [data-resize-handle="se"]');
    handle.element.dispatchEvent(pointer("pointerdown", VIEW_LEFT + 100, VIEW_TOP + 60));
    document.dispatchEvent(pointer("pointermove", VIEW_LEFT + 180, VIEW_TOP + 140));
    await nextTick();
    expect(rectOf(cardEl("a")).w).toBeGreaterThan(100);
    expect(view.get(".board-surface").classes()).toContain("resizing");

    await pressConnectPoint();
    expect(wrapper!.find('[data-im="link-draft"]').exists()).toBe(true);
    expect(view.get(".board-surface").classes()).not.toContain("resizing");
    expect(rectOf(cardEl("a"))).toEqual({ x: 0, y: 0, w: 100, h: 60 });

    moveTo(2000, 1200);
    releaseAt(2000, 1200);
    await nextTick();
    expect(labels.filter((label) => label.includes("调整卡片尺寸"))).toHaveLength(0);
    expect(store.board?.cards.find((card) => card.id === "a")).toMatchObject({ x: 0, y: 0, w: 100, h: 60 });
    expect(store.board?.links).toHaveLength(0);
  });

  it("正文仍是选择 / 移动：按卡片中心不会进入拖线，拖动照常提交", async () => {
    store.board = board([mkCard("a", 0, 0), mkCard("b", 400, 0)]);
    mountCanvas();
    cardEl("a")!.dispatchEvent(pointer("pointerdown", VIEW_LEFT + 50, VIEW_TOP + 30));
    await nextTick();
    expect(wrapper!.find('[data-im="link-draft"]').exists()).toBe(false);
    moveTo(150, 80);
    await nextTick();
    expect(rectOf(cardEl("a")).x).toBeGreaterThan(0);
    releaseAt(150, 80);
    await nextTick();
    expect(store.board?.cards.find((card) => card.id === "a")?.x).toBeCloseTo(100, 0);
  });

  it("拖线中尺寸把手让路，取消后立刻恢复：两种操作不需要切换模式", async () => {
    store.board = board([mkCard("a", 0, 0), mkCard("b", 400, 0)], { selection: ["a"] });
    const view = mountCanvas();
    expect(resizeHandles()).toHaveLength(8);

    await pressConnectPoint();
    // 拖线进行中：尺寸热区整体让路（同一个指针不驱动两件事），连接点照常在
    expect(view.findAll("[data-resize-handle]")).toHaveLength(0);
    expect(connectPoints()).toHaveLength(4);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await nextTick();
    expect(wrapper!.find('[data-im="link-draft"]').exists()).toBe(false);
    expect(view.findAll('[data-resize-card-id="a"] [data-resize-handle]')).toHaveLength(8);

    // 恢复后尺寸拖动照常提交一次
    view.get('[data-resize-card-id="a"] [data-resize-handle="se"]').element.dispatchEvent(pointer("pointerdown", VIEW_LEFT + 100, VIEW_TOP + 60));
    document.dispatchEvent(pointer("pointermove", VIEW_LEFT + 160, VIEW_TOP + 120));
    document.dispatchEvent(pointer("pointerup", VIEW_LEFT + 160, VIEW_TOP + 120));
    await nextTick();
    expect(labels.filter((label) => label === "调整卡片尺寸")).toHaveLength(1);
    expect(store.board?.cards.find((card) => card.id === "a")).toMatchObject({ x: 0, y: 0, w: 160, h: 120 });
    // 尺寸提交之后连接点仍在，且跟着新的显示矩形走
    expect(connectPoints()).toHaveLength(4);
    expect(rectOf(wrapper!.find('[data-connect-card-id="a"]').element)).toEqual({ x: 0, y: 0, w: 160, h: 120 });
  });
});

describe("取消路径：拖线在 Esc / 指针中断 / 失焦 / 卸载后都不留半条关系", () => {
  async function startDraftWithTarget(): Promise<void> {
    await pressConnectPoint();
    moveTo(450, 30);
    await nextTick();
    expect(draftTarget()).toBe("b");
  }

  it("Esc：清掉草稿、不建链，尺寸把手立刻恢复", async () => {
    store.board = board([mkCard("a", 0, 0), mkCard("b", 400, 0)], { selection: ["a"] });
    mountCanvas();
    await startDraftWithTarget();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await nextTick();
    expect(wrapper!.find('[data-im="link-draft"]').exists()).toBe(false);
    expect(store.board?.links).toHaveLength(0);
    expect(wrapper!.findAll('[data-resize-card-id="a"] [data-resize-handle]')).toHaveLength(8);
    // 松手也不会补建关系（监听已经摘掉）
    releaseAt(450, 30);
    await nextTick();
    expect(store.board?.links).toHaveLength(0);
  });

  it("pointercancel：不建链，光标与尺寸能力都复原", async () => {
    store.board = board([mkCard("a", 0, 0), mkCard("b", 400, 0)], { selection: ["a"] });
    mountCanvas();
    await startDraftWithTarget();
    window.dispatchEvent(pointer("pointercancel", VIEW_LEFT + 490, VIEW_TOP + 130));
    await nextTick();
    expect(wrapper!.find('[data-im="link-draft"]').exists()).toBe(false);
    expect(store.board?.links).toHaveLength(0);
    expect(wrapper!.findAll('[data-resize-card-id="a"] [data-resize-handle]')).toHaveLength(8);
  });

  it("窗口失焦：半条拖线不再残留（原来这一条出口只判拖动 / 框选 / 平移）", async () => {
    store.board = board([mkCard("a", 0, 0), mkCard("b", 400, 0)], { selection: ["a"] });
    mountCanvas();
    await startDraftWithTarget();
    window.dispatchEvent(new Event("blur"));
    await nextTick();
    expect(wrapper!.find('[data-im="link-draft"]').exists()).toBe(false);
    expect(store.board?.links).toHaveLength(0);
    expect(wrapper!.findAll('[data-resize-card-id="a"] [data-resize-handle]')).toHaveLength(8);
  });

  it("卸载：草稿与 window 监听一起清掉，卸载后的松手不会补建关系", async () => {
    store.board = board([mkCard("a", 0, 0), mkCard("b", 400, 0)], { selection: ["a"] });
    mountCanvas();
    await startDraftWithTarget();
    wrapper!.unmount();
    const unmounted = wrapper;
    wrapper = null;
    expect(unmounted).not.toBeNull();
    // 卸载后的事件不再属于这个组件：既不该建链，也不该改板面
    window.dispatchEvent(pointer("pointermove", VIEW_LEFT + 490, VIEW_TOP + 130));
    window.dispatchEvent(pointer("pointerup", VIEW_LEFT + 490, VIEW_TOP + 130));
    await nextTick();
    expect(store.board?.links).toHaveLength(0);
    expect(labels.filter((label) => label.includes("新建关系"))).toHaveLength(0);
  });
});

describe("hitCardAt 认显示矩形：展开 / 让位 / resize / 平移缩放之后落点仍然准", () => {
  it("展开让位：按显示位置命中，正式保存位置不再命中，且临时槽位不写回板面", async () => {
    store.board = board([mkCard("a", 0, 0, 300, 200), mkCard("b", 200, 0, 300, 200)], { selection: ["a"] });
    mountCanvas();
    store.toggleCardPreview("a");
    store.toggleCardPreview("b");
    await nextTick();
    const slotA = rectOf(wrapper!.find('[data-im="card"]').element);
    const slotB = rectOf(wrapper!.findAll('[data-im="card"]')[1].element);
    expect(slotB.x).toBeGreaterThan(500); // 让位后 b 的显示槽位远离正式位置（x=200）
    const before = store.board!.cards.map((card) => ({ id: card.id, x: card.x, y: card.y, w: card.w, h: card.h }));

    await pressConnectPoint(0, { x: slotA.x + slotA.w / 2, y: slotA.y + 9 });
    moveTo(slotB.x + slotB.w / 2, slotB.y + slotB.h / 2);
    await nextTick();
    expect(draftTarget()).toBe("b");

    // 拖到 b 的**正式保存位置**（已被 a 的展开槽位覆盖）：a 被排除，b 不在那儿 → 无效
    moveTo(350, 100);
    await nextTick();
    expect(draftTarget()).toBe("");

    releaseAt(350, 100);
    await nextTick();
    expect(store.board?.links).toHaveLength(0);
    // 显示槽位只是渲染：板面里的坐标一个字段都没被动过
    expect(store.board!.cards.map((card) => ({ id: card.id, x: card.x, y: card.y, w: card.w, h: card.h }))).toEqual(before);
  });

  it("resize 之后：命中新的显示矩形，旧位置不再命中", async () => {
    store.board = board([mkCard("a", 0, 0), mkCard("b", 400, 0, 300, 60)], { selection: ["b"] });
    const view = mountCanvas();
    // 把 b 从 300 宽缩到 200 宽（最小 180）：右边界 700 → 600
    view.get('[data-resize-card-id="b"] [data-resize-handle="e"]').element.dispatchEvent(pointer("pointerdown", VIEW_LEFT + 700, VIEW_TOP + 30));
    document.dispatchEvent(pointer("pointermove", VIEW_LEFT + 600, VIEW_TOP + 30));
    document.dispatchEvent(pointer("pointerup", VIEW_LEFT + 600, VIEW_TOP + 30));
    await nextTick();
    expect(store.board?.cards.find((card) => card.id === "b")?.w).toBe(200);

    // 选中 a（纯单击：只改选择，不移动卡片）
    cardEl("a")!.dispatchEvent(pointer("pointerdown", VIEW_LEFT + 50, VIEW_TOP + 30));
    releaseAt(50, 30);
    await nextTick();
    expect(connectPoints()).toHaveLength(4);

    await pressConnectPoint();
    // 旧位置（b 原来到 700）：缩小之后在 b 之外
    moveTo(650, 30);
    await nextTick();
    expect(draftTarget()).toBe("");
    // 新位置（b 现在到 600）
    moveTo(550, 30);
    await nextTick();
    expect(draftTarget()).toBe("b");
    releaseAt(550, 30);
    await nextTick();
    expect(store.board?.links).toHaveLength(1);
  });

  it("只平移（滚动量 ≠ 0）之后：落点仍是板面坐标，滚动量不被重复计算", async () => {
    // 这一条钉住的是 0.2.5 §四「平移之后仍能命中」的坐标前提：
    // 屏幕 → 板面必须与「板面 → 屏幕」严格互逆（原来 surfaceRect 已减过一次滚动量，
    // view.x 又减一次；拖动用位移差看不出来，拖线用绝对坐标就直接判错卡片）。
    store.board = board([mkCard("a", 0, 0), mkCard("b", 900, 700)], { selection: ["a"] });
    const view = mountCanvas();
    const element = view.get(".board-viewport").element as HTMLElement;
    makeScrollable(element, 800, 600);
    element.dispatchEvent(new Event("scroll"));
    await nextTick();
    expect(element.scrollLeft).toBe(800);
    expect(element.scrollTop).toBe(600);
    expect(viewState().scale).toBe(1);

    // 按下连接点（坐标不参与判定，给一个视口里的点即可）
    connectPoints()[0].element.dispatchEvent(pointer("pointerdown", 500, 400));
    await nextTick();
    expect(wrapper!.find('[data-im="link-draft"]').exists()).toBe(true);

    // b 的可见中心：屏幕 = 容器左上 + 板面 × 缩放 − 滚动量
    moveTo(950, 730);
    await nextTick();
    expect(draftTarget()).toBe("b");

    // 同一行再往右 450 板面单位：那里没有卡片
    moveTo(1400, 730);
    await nextTick();
    expect(draftTarget()).toBe("");

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await nextTick();
    expect(store.board?.links).toHaveLength(0);
  });

  it("平移 + 缩放（×2）之后：板面坐标系里比较，不重复乘除 scale", async () => {
    store.board = board([mkCard("a", 0, 0), mkCard("b", 400, 0)], { selection: ["a"] });
    const view = mountCanvas();
    const element = view.get(".board-viewport").element as HTMLElement;
    makeScrollable(element, 0, 0);
    element.dispatchEvent(new WheelEvent("wheel", { clientX: VIEW_LEFT + 200, clientY: VIEW_TOP + 100, deltaY: -462, deltaMode: 0, bubbles: true, cancelable: true }));
    await nextTick();
    const state = viewState();
    expect(state.scale).toBeGreaterThan(1.9);

    await pressConnectPoint(0, { x: 50, y: 9 });
    const scrollBefore = { x: element.scrollLeft, y: element.scrollTop };
    moveTo(450, 30); // b 的显示中心
    await nextTick();
    expect(draftTarget()).toBe("b");
    // 拖线不会顺手平移画布
    expect({ x: element.scrollLeft, y: element.scrollTop }).toEqual(scrollBefore);
    expect(view.get(".board-viewport").classes()).not.toContain("panning");

    // 板面 x=510 已在 b（400..500）之外：屏幕位移要按 scale 除回去，不能被放大成板面 40 单位
    moveTo(510, 30);
    await nextTick();
    expect(draftTarget()).toBe("");

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await nextTick();
    expect(store.board?.links).toHaveLength(0);
  });
});

describe("平移（滚动量 ≠ 0）之后的统一换算：框选 / 工具栏 / 合并提示 / 缩放锚点", () => {
  /** 让 .board-surface 的矩形与真实浏览器一致：内容原点 = 容器左上 − 滚动量。 */
  function stubSurfaceRect(view: VueWrapper): void {
    const viewport = view.get(".board-viewport").element as HTMLElement;
    const surface = view.get(".board-surface").element as HTMLElement;
    surface.getBoundingClientRect = () =>
      ({ left: VIEW_LEFT - viewport.scrollLeft, top: VIEW_TOP - viewport.scrollTop, right: VIEW_LEFT - viewport.scrollLeft, bottom: VIEW_TOP - viewport.scrollTop, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
  }

  it("平移之后框选：选中的是指针真正框住的卡片（滚动量不再被多减一次）", async () => {
    // c 正好落在「多减一次滚动量」时会算出来的错误矩形里：修复前会误选 c、漏掉 a
    store.board = board([mkCard("a", 900, 700), mkCard("c", 1700, 1300), mkCard("far", 200, 200)], { selection: [] });
    const view = mountCanvas();
    const element = view.get(".board-viewport").element as HTMLElement;
    makeScrollable(element, 800, 600);
    element.dispatchEvent(new Event("scroll"));
    await nextTick();
    expect(viewState()).toMatchObject({ scale: 1, scrollLeft: 800, scrollTop: 600 });

    // 空格 + 拖动空白处 = 框选（手势固定，不需要切换模式）
    window.dispatchEvent(new KeyboardEvent("keydown", { key: " ", code: "Space", bubbles: true, cancelable: true }));
    const state = viewState();
    const from = screenOf(880, 680, state);
    const to = screenOf(1020, 780, state);
    element.dispatchEvent(pointer("pointerdown", from.x, from.y));
    window.dispatchEvent(pointer("pointermove", to.x, to.y));
    await nextTick();
    window.dispatchEvent(pointer("pointerup", to.x, to.y));
    await nextTick();

    // 板面 (880,680)-(1020,780) 只框住 a：框选矩形不会因为平移而整体偏移
    expect(store.board?.selection).toEqual(["a"]);
    expect(labels.filter((label) => label.includes("框选"))).toHaveLength(1);
    expect(wrapper!.text()).toContain("140×100");
    window.dispatchEvent(new KeyboardEvent("keyup", { key: " ", code: "Space", bubbles: true }));
  });

  it("平移之后：局部工具栏仍贴着选中卡片（相对卡片的屏幕位置正确）", async () => {
    store.board = board([mkCard("a", 900, 700)], { selection: ["a"] });
    const view = mountCanvas();
    const element = view.get(".board-viewport").element as HTMLElement;
    makeScrollable(element, 800, 600);
    stubSurfaceRect(view);
    element.dispatchEvent(new Event("scroll"));
    await nextTick();

    // 在平移后的画面上单击这张卡：工具栏按当下的视口重新定位（与真实操作同一条刷新路径）
    const state = viewState();
    const press = screenOf(950, 730, state);
    cardEl("a")!.dispatchEvent(pointer("pointerdown", press.x, press.y));
    window.dispatchEvent(pointer("pointerup", press.x, press.y));
    await nextTick();

    const card = rectOf(cardEl("a"));
    const toolbar = styleOf(view.get('[data-im="card-toolbar"]').element);
    const surfaceLeft = VIEW_LEFT - state.scrollLeft;
    const surfaceTop = VIEW_TOP - state.scrollTop;
    // 缩放为 1：工具栏行内坐标与板面单位一一对应，且它的屏幕位置要贴着卡片的左上角
    expect(px(toolbar.left)).toBeCloseTo(card.x, 0);
    expect(surfaceLeft + px(toolbar.left)).toBeCloseTo(screenOf(card.x, card.y, state).x, 0);
    // 竖直方向：工具栏在卡片上方（不压卡片顶边），间距 ≈ 量到的工具栏高度 + 10
    const gap = card.y - px(toolbar.top);
    expect(gap).toBeGreaterThanOrEqual(2);
    expect(gap).toBeLessThanOrEqual(14);
    expect(surfaceTop + px(toolbar.top)).toBeLessThan(screenOf(card.x, card.y, state).y);
  });

  it("平移之后：合并提示仍贴在目标卡片上方（相对卡片的屏幕位置正确）", async () => {
    store.board = board([mkCard("a", 830, 700), mkCard("b", 1000, 700)]);
    const view = mountCanvas();
    const element = view.get(".board-viewport").element as HTMLElement;
    makeScrollable(element, 800, 600);
    element.dispatchEvent(new Event("scroll"));
    await nextTick();
    const state = viewState();

    // 把 a 拖到完全盖住 b：出现「松开后合并成组」提示
    const a = rectOf(cardEl("a"));
    const press = screenOf(a.x + a.w / 2, a.y + a.h / 2, state);
    cardEl("a")!.dispatchEvent(pointer("pointerdown", press.x, press.y));
    await nextTick();
    moveTo(1000 + a.w / 2, a.y + a.h / 2);
    await nextTick();
    const hint = view.get('[data-im="group-merge-hint"]');
    expect(hint.attributes("data-target-card-id")).toBe("b");

    const b = rectOf(cardEl("b"));
    const bClient = screenOf(b.x, b.y, viewState());
    const hintStyle = styleOf(hint.element);
    // 提示左边缘对齐目标卡片左边缘；底边在卡片上方 34px（都是屏幕坐标，缩放为 1）
    expect(VIEW_LEFT + px(hintStyle.left)).toBeCloseTo(bClient.x, 0);
    expect(bClient.y - (VIEW_TOP + px(hintStyle.top))).toBeCloseTo(34, 0);

    const drop = screenOf(1000 + a.w / 2, a.y + a.h / 2, viewState());
    window.dispatchEvent(pointer("pointerup", drop.x, drop.y));
    await nextTick();
  });

  it("平移之后滚轮缩放：指针下的板面点留在同一屏幕位置（锚点不漂）", async () => {
    store.board = board([mkCard("a", 500, 400)]);
    const view = mountCanvas();
    const element = view.get(".board-viewport").element as HTMLElement;
    makeScrollable(element, 400, 300);
    element.dispatchEvent(new Event("scroll"));
    await nextTick();
    const before = viewState();
    expect(before.scale).toBe(1);
    const anchor = screenOf(500, 400, before);

    element.dispatchEvent(new WheelEvent("wheel", { clientX: anchor.x, clientY: anchor.y, deltaY: -462, deltaMode: 0, bubbles: true, cancelable: true }));
    await nextTick();
    const after = viewState();
    expect(after.scale).toBeGreaterThan(1.9);
    // 不变量：指针下的板面点 (500,400) 缩放后仍在同一屏幕位置（与 viewport.test.ts 的同一条口径）
    expect(VIEW_LEFT + 500 * after.scale - after.scrollLeft).toBeCloseTo(anchor.x, 0);
    expect(VIEW_TOP + 400 * after.scale - after.scrollTop).toBeCloseTo(anchor.y, 0);
  });
});
