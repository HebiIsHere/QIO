/**
 * 0.2.5 两个回归的**独立**验证（W3）—— 与 W1 / W2 的用例角度不重复。
 *
 * 覆盖分工（诚实标注）：
 * - 这里能验的：连接点的**归属层与定位数学**（不在被缩放的内容层、宿主 = displayRect × scale、
 *   展开 / 让位后跟随显示矩形）、连接点与尺寸热区的**手势互斥**与恢复、
 *   hitCardAt 必须用**当前显示矩形**（旧位置不再命中）、AddMenu 提示的**计时器生命周期**。
 * - 这里验不了的：z 序造成的真实像素遮挡（jsdom 没有布局与命中测试）→ 由
 *   D:/qio-dev/_fix025/verify-025.mjs 的真实浏览器用例（elementFromPoint + page.mouse）承担。
 * - 连接点热区 ≥18×18 屏幕像素：jsdom 里只能从样式声明推断（见 declaredHitBox），
 *   推不出来时只记录说明，硬测量由真实浏览器用例承担。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import BoardCanvas from "../BoardCanvas.vue";
import BoardCardComponent from "../BoardCard.vue";
import AddMenu from "../AddMenu.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";
import { publishBoardArea } from "../../../interactive/viewport";
import type { BoardCard, BoardState } from "../../../interactive/types";

// 附件服务必须在 AddMenu 导入前替换：pickBrowserFile / uploadAttachment / waitUntilSettled
const h = vi.hoisted(() => ({ pick: vi.fn(), upload: vi.fn(), settle: vi.fn() }));
vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return { ...actual, pickBrowserFile: h.pick, uploadAttachment: h.upload, waitUntilSettled: h.settle };
});

const STAMP = "2026-10-13T00:00:00.000Z";
const VIEW_LEFT = 40;
const VIEW_TOP = 100;
// 视口比板面内容（2400×1600）还大：缩放 / 平移都不会产生滚动，
// 屏幕坐标 = 容器原点 + 板面坐标 × scale（与 BoardCanvas 的换算逐字对应）。
const VIEW_W = 3000;
const VIEW_H = 2000;

function mkCard(id: string, x: number, y: number, patch: Partial<BoardCard> = {}): BoardCard {
  return {
    id,
    kind: patch.kind ?? "text",
    content: patch.content ?? "内容",
    meta: patch.meta ?? {},
    x,
    y,
    w: patch.w ?? 200,
    h: patch.h ?? 120,
    checked: patch.checked ?? false,
    hidden: patch.hidden ?? false,
    folded: patch.folded ?? false,
    bookmarked: patch.bookmarked ?? false,
    deleted: patch.deleted ?? false,
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

function pointer(type: string, x: number, y: number): MouseEvent {
  return new MouseEvent(type, { clientX: x, clientY: y, bubbles: true, cancelable: true });
}

function wheel(x: number, y: number, deltaY: number): Event {
  return new WheelEvent("wheel", { clientX: x, clientY: y, deltaY, deltaMode: 0, bubbles: true, cancelable: true });
}

/** jsdom 无布局：给容器与外壳打桩几何，让坐标换算可被验证。 */
function stubGeometry(wrapper: VueWrapper) {
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  const shell = wrapper.get(".board-shell").element as HTMLElement;
  view.getBoundingClientRect = () =>
    ({ left: VIEW_LEFT, top: VIEW_TOP, right: VIEW_LEFT + VIEW_W, bottom: VIEW_TOP + VIEW_H, width: VIEW_W, height: VIEW_H, x: VIEW_LEFT, y: VIEW_TOP, toJSON: () => ({}) }) as DOMRect;
  shell.getBoundingClientRect = () =>
    ({ left: VIEW_LEFT, top: VIEW_TOP, right: VIEW_LEFT + VIEW_W, bottom: VIEW_TOP + VIEW_H, width: VIEW_W, height: VIEW_H, x: VIEW_LEFT, y: VIEW_TOP, toJSON: () => ({}) }) as DOMRect;
  Object.defineProperty(view, "clientWidth", { value: VIEW_W, configurable: true });
  Object.defineProperty(view, "clientHeight", { value: VIEW_H, configurable: true });
}

function scaleOf(wrapper: VueWrapper): number {
  const surface = wrapper.get(".board-surface").element as HTMLElement;
  const m = /scale\(([\d.]+)\)/.exec(surface.style.transform);
  return m ? Number(m[1]) : Number.NaN;
}

function screenOf(boardX: number, boardY: number, scale: number) {
  return { x: VIEW_LEFT + boardX * scale, y: VIEW_TOP + boardY * scale };
}

/** 这张卡这一帧的显示矩形（直接读 BoardCard 收到的渲染坐标，不猜内部实现） */
function displayRectOf(wrapper: VueWrapper, id: string): { x: number; y: number; w: number; h: number } | null {
  const card = wrapper.findAllComponents(BoardCardComponent).find((c) => (c.props("card") as BoardCard).id === id);
  if (!card) return null;
  const model = card.props("card") as BoardCard;
  return { x: Number(card.props("x")), y: Number(card.props("y")), w: model.w, h: model.h };
}

/** 连接点元素（文档级查询：修复后它可能不在卡片内部） */
function connectPoints(wrapper: VueWrapper, id: string) {
  return wrapper.findAll('[data-im="connect-point"][data-card-id="' + id + '"]');
}

/** 连接点的定位宿主：最近一个带行内 left / top 的祖先 */
function positionedHost(el: Element): HTMLElement | null {
  let node = el.parentElement as HTMLElement | null;
  while (node) {
    if (node.style && node.style.left && node.style.top) return node;
    node = node.parentElement as HTMLElement | null;
  }
  return null;
}

function px(value: string): number {
  const n = Number.parseFloat(value);
  return Number.isFinite(n) ? n : Number.NaN;
}

// 样式来源（只在推不出行内尺寸时用）
const layerSource = readFileSync(resolve(process.cwd(), "src/components/interactive/BoardResizeLayer.vue"), "utf8");
const cardSource = readFileSync(resolve(process.cwd(), "src/components/interactive/BoardCard.vue"), "utf8");

/**
 * 从样式声明推断连接点元素自身的可点击盒。推不出来返回 null（不假装知道）。
 * 只接受「选择器里的所有 .class 都是这个元素自己的类」的规则，避免把父级规则算进来。
 */
function declaredHitBox(el: Element): { w: number; h: number } | null {
  const classes = [...el.classList];
  if (!classes.length) return null;
  let best: { w: number; h: number } | null = null;
  for (const src of [layerSource, cardSource]) {
    const start = src.indexOf("<style");
    if (start < 0) continue;
    const style = src.slice(start);
    const ruleRe = /([^{}]+)\{([^{}]*)\}/g;
    let m: RegExpExecArray | null;
    while ((m = ruleRe.exec(style))) {
      const selector = m[1].trim();
      const body = m[2];
      const selClasses = [...selector.matchAll(/\.([a-zA-Z0-9_-]+)/g)].map((x) => x[1]);
      if (!selClasses.length || !selClasses.every((c) => classes.includes(c))) continue;
      if (!selClasses.some((c) => classes.includes(c))) continue;
      const pseudo = /::?(before|after)/.test(selector);
      let w = /(?:^|[;\s])width:\s*([\d.]+)px/.exec(body);
      let hgt = /(?:^|[;\s])height:\s*([\d.]+)px/.exec(body);
      const minW = /min-width:\s*([\d.]+)px/.exec(body);
      const minH = /min-height:\s*([\d.]+)px/.exec(body);
      const padAll = /(?:^|[;\s])padding:\s*([\d.]+)px/.exec(body);
      const pad = /(?:^|[;\s])padding:\s*([\d.]+)px(?:\s+([\d.]+)px)?(?:\s+([\d.]+)px)?(?:\s+([\d.]+)px)?/.exec(body);
      const inset = /(?:^|[;\s])inset:\s*(-?[\d.]+)px/.exec(body);
      let wv = w ? Number.parseFloat(w[1]) : null;
      let hv = hgt ? Number.parseFloat(hgt[1]) : null;
      if (minW) wv = Math.max(wv ?? 0, Number.parseFloat(minW[1]));
      if (minH) hv = Math.max(hv ?? 0, Number.parseFloat(minH[1]));
      if (padAll && wv !== null && hv !== null) {
        wv += 2 * Number.parseFloat(padAll[1]);
        hv += 2 * Number.parseFloat(padAll[1]);
      } else if (pad && wv !== null && hv !== null) {
        const vertical = Number.parseFloat(pad[1]);
        const horizontal = pad[2] !== undefined ? Number.parseFloat(pad[2]) : vertical;
        wv += 2 * horizontal;
        hv += 2 * vertical;
      }
      if (pseudo && inset && wv !== null && hv !== null) {
        const delta = -Number.parseFloat(inset[1]);
        if (delta > 0) { wv += 2 * delta; hv += 2 * delta; }
      }
      if (wv !== null && hv !== null) {
        if (!best || wv * hv > best.w * best.h) best = { w: wv, h: hv };
      }
    }
  }
  return best;
}

describe("A 连接点归属与定位（jsdom 可判的部分）", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  let labels: string[];
  const mounted: VueWrapper[] = [];

  afterEach(() => {
    while (mounted.length) { try { mounted.pop()?.unmount(); } catch (err) { /* 已卸载 */ } }
    document.body.innerHTML = "";
    vi.restoreAllMocks();
  });

  beforeEach(() => {
    setActivePinia(createPinia());
    store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    labels = [];
    vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
      labels.push(label);
      store.board = { ...next, boardId: store.boardId };
    });
  });

  function setBoard(next: BoardState) { store.board = board.normalizeState(next); }

  function mountCanvas(): VueWrapper {
    const wrapper = mount(BoardCanvas, { attachTo: document.body });
    mounted.push(wrapper);
    stubGeometry(wrapper);
    return wrapper;
  }

  async function select(wrapper: VueWrapper, id: string) {
    const card = wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === id);
    expect(card, "卡片 " + id + " 应存在").toBeTruthy();
    card!.element.dispatchEvent(pointer("pointerdown", 100, 100));
    await wrapper.vm.$nextTick();
  }

  it("四向连接点齐全，且带 data-card-id / data-side（冻结属性）", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0), mkCard("b", 800, 0)] });
    const wrapper = mountCanvas();
    await select(wrapper, "a");
    const points = connectPoints(wrapper, "a");
    expect(points).toHaveLength(4);
    expect(points.map((p) => p.attributes("data-side")).sort()).toEqual(["bottom", "left", "right", "top"]);
    for (const p of points) expect(p.attributes("data-card-id")).toBe("a");
  });

  it("连接点不在被缩放的内容层里，而在不随画布缩放的交互层里", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0)] });
    const wrapper = mountCanvas();
    await select(wrapper, "a");
    const point = connectPoints(wrapper, "a")[0];
    expect(point, "选中后应有连接点").toBeTruthy();
    expect(point.element.closest(".board-surface"), "连接点不能画在带 transform: scale() 的内容层里").toBeNull();
    expect(point.element.closest('[data-im="resize-layer"]'), "连接点应与尺寸把手同处一个不缩放的交互层").not.toBeNull();
  });

  it("连接点宿主位置 = displayRect × scale（缩放后仍与卡片边缘对齐）", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0)] });
    const wrapper = mountCanvas();
    await select(wrapper, "a");
    const view = wrapper.get(".board-viewport").element;
    view.dispatchEvent(wheel(400, 300, 462)); // exp(-462*0.0015) ≈ 0.5
    await wrapper.vm.$nextTick();
    const scale = scaleOf(wrapper);
    expect(scale).toBeCloseTo(0.5, 2);
    const display = displayRectOf(wrapper, "a")!;
    expect(display).toBeTruthy();
    for (const point of connectPoints(wrapper, "a")) {
      const host = positionedHost(point.element);
      expect(host, "连接点应有一个定位宿主").toBeTruthy();
      expect(px(host!.style.left)).toBeCloseTo(display.x * scale, 1);
      expect(px(host!.style.top)).toBeCloseTo(display.y * scale, 1);
      expect(parseFloat(host!.style.width)).toBeCloseTo(display.w * scale, 1);
      expect(parseFloat(host!.style.height)).toBeCloseTo(display.h * scale, 1);
    }
  });

  it("连接点热区声明尺寸 ≥18×18（推不出时只记录说明，不假装通过）", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0)] });
    const wrapper = mountCanvas();
    await select(wrapper, "a");
    const point = connectPoints(wrapper, "a")[0];
    // DOMWrapper.element 的类型是 Element（没有 style），这里按行内样式读尺寸
    const pointEl = point.element as unknown as HTMLElement;
    const inlineW = px(pointEl.style.width);
    const inlineH = px(pointEl.style.height);
    const declared = Number.isFinite(inlineW) && Number.isFinite(inlineH) && inlineW > 0 ? { w: inlineW, h: inlineH } : declaredHitBox(point.element);
    if (declared) {
      expect(Math.min(declared.w, declared.h), "连接点热区应 ≥18×18 屏幕像素").toBeGreaterThanOrEqual(18);
    } else {
      console.log("[note] jsdom 推不出连接点热区尺寸，该项由真实浏览器用例（verify-025.mjs 的 A/B 段）承担实测");
    }
  });

  it("折叠或多选时不显示连接点（显示条件与现状一致）", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0), mkCard("b", 800, 0), mkCard("folded", 200, 400, { folded: true })] });
    const wrapper = mountCanvas();
    await select(wrapper, "folded");
    expect(connectPoints(wrapper, "folded")).toHaveLength(0);
    await select(wrapper, "a");
    expect(connectPoints(wrapper, "a")).toHaveLength(4);
    // 多选：a + b
    const cardB = wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === "b")!;
    cardB.element.dispatchEvent(new MouseEvent("pointerdown", { clientX: 100, clientY: 100, bubbles: true, cancelable: true, shiftKey: true }));
    await wrapper.vm.$nextTick();
    expect(store.board!.selection.length).toBe(2);
    expect(wrapper.findAll('[data-im="connect-point"]')).toHaveLength(0);
  });

  it("展开 / 让位后连接点跟随显示矩形（不是正式位置）", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0), mkCard("b", 700, 0)] });
    const wrapper = mountCanvas();
    await select(wrapper, "b");
    const before = displayRectOf(wrapper, "b")!;
    store.toggleCardPreview("b");
    await wrapper.vm.$nextTick();
    const after = displayRectOf(wrapper, "b")!;
    expect(after.w > before.w + 20 || after.h > before.h + 20, "展开后显示矩形应放大（前置条件）").toBe(true);
    // 缩到 50%：scale ≠ 1 时「宿主 = displayRect × scale」才有区分度
    (wrapper.get(".board-viewport").element as HTMLElement).dispatchEvent(wheel(400, 300, 462));
    await wrapper.vm.$nextTick();
    const scale = scaleOf(wrapper);
    expect(scale).toBeCloseTo(0.5, 2);
    const point = connectPoints(wrapper, "b")[0];
    const host = positionedHost(point.element)!;
    expect(px(host.style.left)).toBeCloseTo(after.x * scale, 1);
    expect(px(host.style.top)).toBeCloseTo(after.y * scale, 1);
    expect(parseFloat(host.style.width)).toBeCloseTo(after.w * scale, 1);
    expect(parseFloat(host.style.height)).toBeCloseTo(after.h * scale, 1);
  });

  it("拖线开始后尺寸把手让路；Esc 取消后尺寸能力恢复（同一指针只驱动一种手势）", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0), mkCard("b", 800, 0)] });
    const wrapper = mountCanvas();
    await select(wrapper, "a");
    expect(wrapper.findAll("[data-resize-handle]").length).toBeGreaterThan(0);
    const point = connectPoints(wrapper, "a")[0];
    point.element.dispatchEvent(pointer("pointerdown", 120, 120));
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-im="link-draft"]').exists(), "按下连接点后应进入拖线").toBe(true);
    expect(wrapper.findAll("[data-resize-handle]"), "拖线期间尺寸热区必须让路").toHaveLength(0);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-im="link-draft"]').exists()).toBe(false);
    expect(wrapper.findAll("[data-resize-handle]").length, "取消后尺寸能力必须恢复").toBeGreaterThan(0);
    expect(store.board!.links).toHaveLength(0);
  });

  it("从尺寸热区按下不会开始拖线，也不会建立关系", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0), mkCard("b", 800, 0)] });
    const wrapper = mountCanvas();
    await select(wrapper, "a");
    const hit = wrapper.findAll("[data-resize-handle]").find((x) => x.attributes("data-resize-handle") === "se");
    expect(hit).toBeTruthy();
    hit!.element.dispatchEvent(pointer("pointerdown", 400, 300));
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-im="link-draft"]').exists(), "尺寸拖动不应出现拖线草稿").toBe(false);
    window.dispatchEvent(pointer("pointermove", 480, 340));
    window.dispatchEvent(pointer("pointerup", 480, 340));
    await wrapper.vm.$nextTick();
    expect(store.board!.links).toHaveLength(0);
    expect(labels.filter((label) => label.includes("新建关系"))).toHaveLength(0);
  });
});

describe("B hitCardAt 必须用当前显示矩形（展开后旧位置不命中）", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  const mounted: VueWrapper[] = [];

  afterEach(() => {
    while (mounted.length) { try { mounted.pop()?.unmount(); } catch (err) { /* 已卸载 */ } }
    document.body.innerHTML = "";
    vi.restoreAllMocks();
  });

  beforeEach(() => {
    setActivePinia(createPinia());
    store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    vi.spyOn(store, "commit").mockImplementation((next: BoardState) => { store.board = { ...next, boardId: store.boardId }; });
  });

  function setBoard(next: BoardState) { store.board = board.normalizeState(next); }

  function mountCanvas(): VueWrapper {
    const wrapper = mount(BoardCanvas, { attachTo: document.body });
    mounted.push(wrapper);
    stubGeometry(wrapper);
    return wrapper;
  }

  async function selectCard(wrapper: VueWrapper, id: string) {
    const card = wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === id);
    expect(card, "卡片 " + id + " 应存在").toBeTruthy();
    card!.element.dispatchEvent(pointer("pointerdown", 200, 200));
    window.dispatchEvent(pointer("pointerup", 200, 200));
    await wrapper.vm.$nextTick();
  }

  function insideOutside(inside: { x: number; y: number; w: number; h: number }, outside: { x: number; y: number; w: number; h: number }) {
    for (let gy = 0.1; gy <= 0.91; gy += 0.2) {
      for (let gx = 0.1; gx <= 0.91; gx += 0.2) {
        const p = { x: inside.x + inside.w * gx, y: inside.y + inside.h * gy };
        const inOutside = p.x > outside.x - 4 && p.x < outside.x + outside.w + 4 && p.y > outside.y - 4 && p.y < outside.y + outside.h + 4;
        const inInside = p.x > inside.x + 3 && p.x < inside.x + inside.w - 3 && p.y > inside.y + 3 && p.y < inside.y + inside.h - 3;
        if (inInside && !inOutside) return p;
      }
    }
    return null;
  }

  async function expectTargetFor(wrapper: VueWrapper, fromId: string, source: { x: number; y: number }, probeBoard: { x: number; y: number }, scale: number) {
    const start = screenOf(source.x, source.y, scale);
    const point = wrapper.findAll('[data-im="connect-point"][data-card-id="' + fromId + '"]')[0];
    expect(point, "源卡片应有连接点").toBeTruthy();
    point.element.dispatchEvent(pointer("pointerdown", start.x, start.y));
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-im="link-draft"]').exists()).toBe(true);
    const target = screenOf(probeBoard.x, probeBoard.y, scale);
    window.dispatchEvent(pointer("pointermove", target.x, target.y));
    await wrapper.vm.$nextTick();
    const draft = wrapper.get('[data-im="link-draft"]');
    const raw = draft.attributes("data-target") || "";
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await wrapper.vm.$nextTick();
    return raw;
  }

  it("未展开时（显示矩形 = 正式矩形）：拖到目标卡中心命中 b —— 顺带自检屏幕↔板面换算", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0, { w: 200, h: 120 }), mkCard("b", 800, 0, { w: 200, h: 120 })] });
    const wrapper = mountCanvas();
    await selectCard(wrapper, "a");
    const hit = await expectTargetFor(wrapper, "a", { x: 100, y: 60 }, { x: 900, y: 60 }, 1);
    expect(hit, "未展开时目标卡中心必须命中 b（这条同时证明坐标换算没跑偏）").toBe("b");
  });

  it("展开后：拖到「显示矩形内、正式矩形外」的点命中目标卡；旧位置不再命中", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0, { w: 200, h: 120 }), mkCard("b", 800, 0, { w: 200, h: 120 })] });
    const wrapper = mountCanvas();
    await selectCard(wrapper, "a");
    const official = { x: 800, y: 0, w: 200, h: 120 };
    store.toggleCardPreview("b");
    await wrapper.vm.$nextTick();
    const display = displayRectOf(wrapper, "b")!;
    expect(display.w > official.w || display.h > official.h || display.x !== official.x || display.y !== official.y).toBe(true);

    const probeNew = insideOutside(display, official);
    expect(probeNew, "展开矩形必须伸出正式矩形之外，否则该用例没有区分度").not.toBeNull();
    const hitNew = await expectTargetFor(wrapper, "a", { x: 100, y: 60 }, probeNew!, 1);
    expect(hitNew, "拖到显示矩形内（正式矩形外）必须命中 b").toBe("b");

    const probeOld = insideOutside(official, display);
    if (probeOld) {
      const hitOld = await expectTargetFor(wrapper, "a", { x: 100, y: 60 }, probeOld, 1);
      expect(hitOld, "旧位置已经不在显示矩形里，就不能再命中 b").not.toBe("b");
    } else {
      console.log("[note] 展开矩形完全包含正式矩形，没有「旧位置」可用；命中用显示矩形已由上一条证明");
    }
  });

  it("缩小画布（50%）后命中仍按同一套换算（不重复乘除 scale）", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0, { w: 200, h: 120 }), mkCard("b", 900, 0, { w: 200, h: 120 })] });
    const wrapper = mountCanvas();
    await selectCard(wrapper, "a");
    const view = wrapper.get(".board-viewport").element;
    view.dispatchEvent(wheel(400, 300, 462));
    await wrapper.vm.$nextTick();
    const scale = scaleOf(wrapper);
    expect(scale).toBeCloseTo(0.5, 2);
    store.toggleCardPreview("b");
    await wrapper.vm.$nextTick();
    const display = displayRectOf(wrapper, "b")!;
    const probe = { x: display.x + display.w / 2, y: display.y + display.h / 2 };
    const hit = await expectTargetFor(wrapper, "a", { x: 100, y: 60 }, probe, scale);
    expect(hit, "50% 缩放下拖到目标卡显示中心必须命中 b").toBe("b");
  });
});

describe("C AddMenu 提示生命周期（计时器 / 竞态 / 卸载）", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  let wrapper: VueWrapper | null = null;

  const readyRef = (id: string, name: string) => ({ id, name, sizeBytes: 12, kind: "copy" as const, display: "已保存副本", state: "ready" as const, error: null });

  function noticeEl() {
    if (!wrapper) return null;
    const byIm = wrapper.find('[data-im="add-material-status"]');
    if (byIm.exists()) return byIm;
    const statuses = wrapper.findAll('[role="status"]');
    return statuses.length ? statuses[0] : null;
  }
  function noticeText(): string | null {
    const el = noticeEl();
    if (!el) return null;
    // 优先读纯文本节点：提示里还可能有「重新选择文件 / 关闭」按钮
    const body = el.find('[data-im="add-material-status-text"]');
    return ((body.exists() ? body.text() : el.text()) || "").trim();
  }
  function controls(re: RegExp) {
    if (!wrapper) return [];
    return wrapper.findAll("button, [role='button']").filter((b) => {
      const text = (b.text() || "").trim();
      const aria = b.attributes("aria-label") || "";
      const im = b.attributes("data-im") || "";
      return re.test(text) || re.test(aria) || re.test(im);
    });
  }
  async function mountMenu() {
    wrapper = mount(AddMenu, { attachTo: document.body });
    await wrapper.get('[data-im="add-menu"]').trigger("click");
    return wrapper;
  }
  async function addImage() {
    const menu = await mountMenu();
    await menu.get('[data-im="add-file"]').trigger("click");
    return menu;
  }
  function addedCardId(before: Set<string>): string | null {
    const now = store.board?.cards ?? [];
    const found = now.find((c) => !before.has(c.id));
    return found ? found.id : null;
  }

  beforeEach(() => {
    vi.useFakeTimers();
    setActivePinia(createPinia());
    store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    publishBoardArea(null);
    vi.spyOn(store, "commit").mockImplementation((next: BoardState) => { store.board = { ...next, boardId: store.boardId }; });
    h.pick.mockReset();
    h.upload.mockReset();
    h.settle.mockReset();
  });

  afterEach(() => {
    if (wrapper) { wrapper.unmount(); wrapper = null; }
    document.body.innerHTML = "";
    publishBoardArea(null);
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("成功提示约 3 秒后自动移除；卡片与 meta 关联不受影响", async () => {
    const before = new Set((store.board?.cards ?? []).map((c) => c.id));
    h.pick.mockResolvedValue(new File(["x"], "w3-image.png"));
    h.upload.mockResolvedValue(readyRef("att_w3_1", "w3-image.png"));
    h.settle.mockImplementation(async (ref: unknown) => ref);
    await addImage();
    await flushPromises();
    const cardId = addedCardId(before)!;
    expect(cardId).toBeTruthy();
    expect(noticeText(), "成功提示应出现").toContain("已关联");
    const card = store.board!.cards.find((c) => c.id === cardId)!;
    // 文件与图片合并后统一写 meta.file（第九个问题）
    expect((card.meta as Record<string, { attachmentId?: string }>).file?.attachmentId, "meta 必须绑定真实附件 id").toBe("att_w3_1");

    await vi.advanceTimersByTimeAsync(2400);
    expect(noticeText(), "2.4 秒时还不该消失").not.toBeNull();
    await vi.advanceTimersByTimeAsync(1200);
    expect(noticeText(), "3.6 秒内应自动移除").toBeNull();
    const after = store.board!.cards.find((c) => c.id === cardId)!;
    expect((after.meta as Record<string, { attachmentId?: string }>).file?.attachmentId, "提示消失不影响附件关联").toBe("att_w3_1");
    expect((store.board?.cards ?? []).length, "提示消失不影响卡片").toBe(before.size + 1);
  });

  it("上传未结束时进行中提示一直显示，不会被成功计时器提前隐藏", async () => {
    let release: ((value: unknown) => void) | null = null;
    h.pick.mockResolvedValue(new File(["x"], "slow.png"));
    h.upload.mockResolvedValue(readyRef("att_slow", "slow.png"));
    h.settle.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    await addImage();
    await flushPromises();
    const busy = noticeText();
    expect(busy, "上传中应有进行中提示").not.toBeNull();
    expect(busy).not.toContain("已关联");
    await vi.advanceTimersByTimeAsync(6000);
    const stillBusy = noticeText();
    expect(stillBusy, "6 秒后仍在上传，提示不能被提前清掉").not.toBeNull();
    expect(stillBusy).not.toContain("已关联");
    release!(readyRef("att_slow", "slow.png"));
    await flushPromises();
    expect(noticeText()).toContain("已关联");
    await vi.advanceTimersByTimeAsync(3600);
    expect(noticeText(), "成功之后仍按 3 秒生命周期移除").toBeNull();
  });

  it("失败提示带原因、不自动消失，并且重试会重跑同一次上传", async () => {
    const before = new Set((store.board?.cards ?? []).map((c) => c.id));
    h.pick.mockResolvedValue(new File(["x"], "fail.png"));
    h.settle.mockImplementation(async (ref: unknown) => ref);
    h.upload.mockRejectedValueOnce(new Error("W3 注入的失败"));
    await addImage();
    await flushPromises();
    const text = noticeText();
    expect(text, "失败必须有可见提示").not.toBeNull();
    expect(text).toMatch(/失败|不可用|没有响应|拒绝/);
    expect(text).toContain("W3 注入的失败");
    await vi.advanceTimersByTimeAsync(9000);
    expect(noticeText(), "失败提示不能自动消失（失败更持久）").not.toBeNull();
    const retry = controls(/重试|重新|再试/);
    expect(retry.length, "失败提示必须有可发现的重新操作入口").toBeGreaterThan(0);
    const dismiss = controls(/关闭|关掉|知道了|收起|✕|×/);
    expect(dismiss.length, "失败提示必须有可发现的关闭入口").toBeGreaterThan(0);

    // 点关闭：提示消失
    await dismiss[0].trigger("click");
    await flushPromises();
    expect(noticeText(), "关闭入口必须真的关掉提示").toBeNull();

    // 再失败一次，点重试：应重跑上传并成功
    h.upload.mockRejectedValueOnce(new Error("W3 注入的失败 2"));
    const menu = await addImage();
    await flushPromises();
    expect(noticeText()).toMatch(/失败|不可用/);
    const callsBefore = h.upload.mock.calls.length;
    h.upload.mockResolvedValue(readyRef("att_retry", "fail.png"));
    const retry2 = controls(/重试|重新|再试/);
    expect(retry2.length).toBeGreaterThan(0);
    await retry2[0].trigger("click");
    await flushPromises();
    await vi.advanceTimersByTimeAsync(50);
    await flushPromises();
    expect(h.upload.mock.calls.length, "重试必须真的重跑这一次上传").toBeGreaterThan(callsBefore);
    expect(noticeText(), "重试成功后应显示成功提示").toContain("已关联");
    const cardId = addedCardId(before)!;
    expect(cardId).toBeTruthy();
    await vi.advanceTimersByTimeAsync(3600);
    expect(noticeText(), "重试成功提示仍会按 3 秒移除").toBeNull();
  });

  it("连续两次添加：第二次提示不会被第一次的旧计时器提前清掉", async () => {
    h.pick.mockResolvedValue(new File(["x"], "two.png"));
    h.upload.mockResolvedValue(readyRef("att_two", "two.png"));
    h.settle.mockImplementation(async (ref: unknown) => ref);
    await addImage();
    await flushPromises();
    expect(noticeText()).toContain("已关联");
    // 距第一次成功 2.5 秒时开始第二次（第一次的 3 秒计时器还没到）
    await vi.advanceTimersByTimeAsync(2500);
    const menu2 = await mountMenu();
    await menu2.get('[data-im="add-file"]').trigger("click");
    await flushPromises();
    expect(noticeText(), "第二次成功提示应出现").toContain("已关联");
    // 再走 1.6 秒：此时第一次的计时器已经到点，但它绝不能清掉第二次的提示
    await vi.advanceTimersByTimeAsync(1600);
    expect(noticeText(), "旧计时器不得提前清掉新提示").not.toBeNull();
    await vi.advanceTimersByTimeAsync(2000);
    expect(noticeText(), "第二次提示仍应按自己的 3 秒移除").toBeNull();
  });

  it("用户取消选择时如实说明，不假装成功、不写附件 meta", async () => {
    const before = new Set((store.board?.cards ?? []).map((c) => c.id));
    h.pick.mockResolvedValue(null);
    await addImage();
    await flushPromises();
    const text = noticeText();
    expect(text).not.toBeNull();
    expect(text).toMatch(/没有选择|取消/);
    expect(text).not.toMatch(/已关联|已保存副本/);
    const cardId = addedCardId(before)!;
    const card = store.board!.cards.find((c) => c.id === cardId)!;
    expect((card.meta as Record<string, unknown>).file, "取消选择不能写附件关联").toBeUndefined();
    await vi.advanceTimersByTimeAsync(6000);
    expect(noticeText(), "如实说明属于失败 / 未完成一类，不自动消失").not.toBeNull();
  });

  it("卸载后没有残留计时器（成功计时器被清掉，迟到结果写不回来）", async () => {
    h.pick.mockResolvedValue(new File(["x"], "unmount.png"));
    h.upload.mockResolvedValue(readyRef("att_unmount", "unmount.png"));
    h.settle.mockImplementation(async (ref: unknown) => ref);
    const menu = await mountMenu();
    // 组件挂载本身可能带来环境计时器：以「挂载后、操作前」为基线，只看这次操作新增的计时器
    const base = vi.getTimerCount();
    await menu.get('[data-im="add-file"]').trigger("click");
    await flushPromises();
    expect(noticeText()).toContain("已关联");
    const withTimer = vi.getTimerCount();
    expect(withTimer, "成功提示应该在跑一个自动移除计时器").toBeGreaterThan(base);
    wrapper!.unmount();
    wrapper = null;
    document.body.innerHTML = "";
    const after = vi.getTimerCount();
    // 注意：store 自己的保存防抖等计时器不属于组件；这里只能断言「组件这次新增的
    // 成功自动移除计时器确实被清掉」（withTimer → after 至少少一个）。
    expect(after, "卸载必须清掉成功提示的自动移除计时器（base=" + base + " success=" + withTimer + " after=" + after + "）").toBeLessThan(withTimer);
  });
});
