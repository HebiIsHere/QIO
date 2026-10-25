/**
 * 卡片局部工具栏「固定锚点」的**渲染后几何**测试（2026-10-14，用户报告「工具栏与卡片有偏移、松手后还会漂」）。
 *
 * 验收口径（Lead 裁定，与真实浏览器探针同一口径）：
 *   1. |工具栏屏幕 left − 卡片屏幕 left| ≤ 1px
 *   2. |(卡片屏幕 top − 工具栏屏幕 bottom) − 10px| ≤ 1px
 *   3. 工具栏必须是独立浮层：不在 .board-surface（缩放层）内部；
 *   4. 裁切场景（卡片顶边在板面可视区之上）：**完整几何**仍满足 1 / 2；
 *   5. 完全离屏（卡片与板面可视区完全不相交）：隐藏（data-toolbar-visible=0 且 display:none），返回后恢复。
 *
 * 所有断言都建立在 {@link installCardToolbarLayout} 的 jsdom 布局桩上：
 * 它按真实浏览器语义算外框（祖先 transform 缩放、真实 scroll、containing block），
 * 不是「源码里出现某个关键词」的文本断言，也不依赖任何经验补偿常数。
 *
 * 真实浏览器对照证据（同一批场景 + 动画期间采样）在 D:\\qio-dev\\_tb-evidence 下，
 * 由 probe_focus.mjs / probe_switch.mjs / probe_accept.mjs 产出。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount, type VueWrapper } from "@vue/test-utils";
import { nextTick } from "vue";
import BoardCanvas from "../BoardCanvas.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
import type { BoardCard as BoardCardModel, BoardState } from "../../../interactive/types";
import {
  installCardToolbarLayout,
  type CardToolbarHarness,
  type ToolbarMeasurement,
} from "./cardToolbarLayoutHarness";

const VIEW = { left: 0, top: 84, width: 1440, height: 816 };
const SURFACE = { w: 2800, h: 1600 };
const TOOLBAR = { w: 337.59, h: 35.25 };
const GAP = 10;
const STAMP = "2026-10-14T00:00:00.000Z";

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
  return new MouseEvent(type, { clientX: x, clientY: y, bubbles: true, cancelable: true });
}

let wrapper: VueWrapper | null = null;
let harness: CardToolbarHarness | null = null;
let store: ReturnType<typeof useInteractiveStore>;

function mountCanvas(): void {
  wrapper = mount(BoardCanvas, { attachTo: document.body });
  harness = installCardToolbarLayout(wrapper.get(".board-shell").element as HTMLElement, {
    viewport: VIEW,
    surfaceSize: SURFACE,
    toolbarSize: TOOLBAR,
  });
}

/** 工具栏定位写在组件合并的 requestAnimationFrame 里：等一帧（含一次宏任务）再断言。 */
async function flushFrame(times = 1): Promise<void> {
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

/** 固定锚点的两条不等式（外加「必须是独立浮层」）。 */
function expectAnchored(m: ToolbarMeasurement): void {
  expect(m.toolbar).not.toBeNull();
  expect(m.cardRect).not.toBeNull();
  expect(m.toolbarVisible).toBe(true);
  expect(m.toolbarInsideSurface).toBe(false);
  expect(Math.abs(m.dx as number)).toBeLessThanOrEqual(1);
  // 2026-10-25：工具栏容器改为卡片左上角坐标系，按钮再相对它上下/左右展开。
  expect(Math.abs((m.gap as number) + m.toolbarRect!.height)).toBeLessThanOrEqual(1);
}

function viewState(): { scale: number; scrollLeft: number; scrollTop: number } {
  const scroll = harness!.scroll();
  return { scale: harness!.scale(), scrollLeft: scroll.x, scrollTop: scroll.y };
}

/** 板面坐标 → 屏幕坐标（与画布口径一致）。 */
function screenOf(boardX: number, boardY: number): { x: number; y: number } {
  const state = viewState();
  return {
    x: VIEW.left + boardX * state.scale - state.scrollLeft,
    y: VIEW.top + boardY * state.scale - state.scrollTop,
  };
}

function viewportEl(): HTMLElement {
  return wrapper!.get(".board-viewport").element as HTMLElement;
}

function cardEl(cardId: string): HTMLElement {
  const found = wrapper!.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === cardId);
  if (!found) throw new Error("找不到卡片 " + cardId);
  return found.element as HTMLElement;
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

describe("固定锚点：基本与缩放", () => {
  it("选中后：左缘对齐卡片、底边在卡片顶边上方 10px，且不在缩放层里", async () => {
    store.board = board([mkCard("c1", 200, 200)], { selection: ["c1"] });
    mountCanvas();
    await flushFrame();
    const m = harness!.measure();
    expect(m.cardId).toBe("c1");
    expect(m.hostOwnerId).toBe("c1");
    expect(m.hostVisible).toBe("1");
    expectAnchored(m);
    expect(harness!.warnings).toEqual([]);
  });

  it("缩放 25% / 50% / 100% / 150% / 250%：两条不等式都成立，工具栏自身尺寸不随缩放变化", async () => {
    store.board = board([mkCard("c1", 900, 600)], { selection: ["c1"] });
    mountCanvas();
    await flushFrame();
    const viewport = viewportEl();
    // 以卡片中心为缩放中心：缩放到两端它都还在可视区里
    const center = screenOf(900 + 130, 600 + 85);
    await viewport.dispatchEvent(new WheelEvent("wheel", { clientX: center.x, clientY: center.y, deltaY: -462, deltaMode: 0, bubbles: true, cancelable: true }));
    await flushFrame(2);
    expect(harness!.scale()).toBeGreaterThan(1.9);
    expectAnchored(harness!.measure());
    expect(harness!.measure().toolbarRect!.width).toBeCloseTo(TOOLBAR.w, 1);

    for (let i = 0; i < 20; i += 1) {
      const scale = harness!.scale();
      if (scale <= 0.2501) break;
      await viewport.dispatchEvent(new WheelEvent("wheel", { clientX: center.x, clientY: center.y, deltaY: 120, deltaMode: 0, bubbles: true, cancelable: true }));
      await flushFrame();
      const m = harness!.measure();
      if (m.toolbarVisible) {
        expectAnchored(m);
        expect(m.toolbarRect!.width).toBeCloseTo(TOOLBAR.w, 1);
      }
    }
    expect(harness!.scale()).toBeLessThanOrEqual(0.26);

    for (let i = 0; i < 24; i += 1) {
      const scale = harness!.scale();
      if (scale >= 2.499) break;
      await viewport.dispatchEvent(new WheelEvent("wheel", { clientX: center.x, clientY: center.y, deltaY: -100, deltaMode: 0, bubbles: true, cancelable: true }));
      await flushFrame();
      const m = harness!.measure();
      if (m.toolbarVisible) {
        expectAnchored(m);
        expect(m.toolbarRect!.width).toBeCloseTo(TOOLBAR.w, 1);
      }
    }
    expect(harness!.scale()).toBeGreaterThanOrEqual(2.49);
  });

  it("平移（真实 scrollLeft/Top 非零）：工具栏跟着卡片走，相对位置不变", async () => {
    store.board = board([mkCard("c1", 900, 700)], { selection: ["c1"] });
    mountCanvas();
    await flushFrame();
    expectAnchored(harness!.measure());

    const from = screenOf(2400, 1400);
    viewportEl().dispatchEvent(pointer("pointerdown", from.x, from.y));
    await nextTick();
    window.dispatchEvent(pointer("pointermove", from.x - 120, from.y - 90));
    await flushFrame();
    expect(harness!.scroll().x).toBeGreaterThan(0);
    expect(harness!.scroll().y).toBeGreaterThan(0);
    expectAnchored(harness!.measure());
    window.dispatchEvent(pointer("pointerup", from.x - 120, from.y - 90));
    await flushFrame();
    expectAnchored(harness!.measure());
  });
});

describe("固定锚点：手势（拖动 / 拉伸 / 展开）", () => {
  it("拖动中与松手后都是同一相对位置（不再有松手漂移）", async () => {
    store.board = board([mkCard("c1", 300, 300)], { selection: ["c1"] });
    mountCanvas();
    await flushFrame();
    const start = screenOf(340, 320);
    cardEl("c1").dispatchEvent(pointer("pointerdown", start.x, start.y));
    await nextTick();
    window.dispatchEvent(pointer("pointermove", start.x + 60, start.y + 40));
    await flushFrame();
    const during = harness!.measure();
    expectAnchored(during);
    window.dispatchEvent(pointer("pointermove", start.x + 150, start.y + 110));
    await flushFrame();
    expectAnchored(harness!.measure());
    window.dispatchEvent(pointer("pointerup", start.x + 150, start.y + 110));
    await flushFrame();
    const after = harness!.measure();
    expectAnchored(after);
    // 卡片真的移动了，且相对位置没有在松手后发生变化
    expect(after.cardRect!.left).toBeCloseTo(during.cardRect!.left + 90, 0);
    expect(after.dx).toBeCloseTo(during.dx as number, 3);
    expect(after.gap).toBeCloseTo(during.gap as number, 3);
  });

  it("拉伸中与松手后都是同一相对位置", async () => {
    store.board = board([mkCard("c1", 300, 300)], { selection: ["c1"] });
    mountCanvas();
    await flushFrame();
    const handle = wrapper!.get('[data-resize-card-id="c1"] [data-resize-handle="se"]');
    const se = screenOf(300 + 260, 300 + 170);
    handle.element.dispatchEvent(pointer("pointerdown", se.x, se.y));
    await nextTick();
    document.dispatchEvent(pointer("pointermove", se.x + 40, se.y + 20));
    await flushFrame();
    expectAnchored(harness!.measure());
    document.dispatchEvent(pointer("pointerup", se.x + 40, se.y + 20));
    await flushFrame(2);
    const after = harness!.measure();
    expectAnchored(after);
    expect(after.cardRect!.width).toBeCloseTo(300 * harness!.scale(), 0);
  });

  it("展开 / 收起预览：卡片外框不变，工具栏每一帧都稳定贴住卡片", async () => {
    store.board = board([mkCard("c1", 300, 300)], { selection: ["c1"] });
    mountCanvas();
    await flushFrame();
    const before = harness!.measure();
    expectAnchored(before);

    store.toggleCardPreview("c1");
    for (let i = 0; i < 3; i += 1) {
      await flushFrame();
      expectAnchored(harness!.measure());
    }
    const expanded = harness!.measure();
    // F1：展开只切卡内内容，外框逐字段不变（旧行为是放大到阅读尺寸）
    expect(expanded.cardRect!.width).toBeCloseTo(before.cardRect!.width, 1);
    expect(expanded.cardRect!.left).toBeCloseTo(before.cardRect!.left, 1);
    expect(expanded.cardRect!.top).toBeCloseTo(before.cardRect!.top, 1);

    store.toggleCardPreview("c1");
    for (let i = 0; i < 3; i += 1) {
      await flushFrame();
      expectAnchored(harness!.measure());
    }
  });
});

describe("固定锚点：结构变化（撤销重做 / 容器尺寸 / 换行 / 切换）", () => {
  it("另一张卡展开不再推开任何卡片：工具栏锚点逐帧保持不变（F1）", async () => {
    store.board = board([mkCard("c1", 300, 300), mkCard("c2", 380, 360)], { selection: ["c1"] });
    mountCanvas();
    await flushFrame();
    const before = harness!.measure();
    expectAnchored(before);

    store.toggleCardPreview("c2");
    await flushFrame(3);
    const after = harness!.measure();
    expectAnchored(after);
    // 展开不让位：c1 的外框一个像素都不动（旧行为是被推开），工具栏也不动
    expect(after.cardRect!.left).toBeCloseTo(before.cardRect!.left, 1);
    expect(after.cardRect!.top).toBeCloseTo(before.cardRect!.top, 1);
  });

  it("撤销 / 重做后都回到固定锚点", async () => {
    store.board = board([mkCard("c1", 300, 300)], { selection: ["c1"] });
    mountCanvas();
    await flushFrame();
    const origin = harness!.measure();
    expectAnchored(origin);

    const current = store.board as BoardState;
    store.commit({ ...current, cards: current.cards.map((card) => ({ ...card, x: 700, y: 620 })) }, "移动卡片");
    await flushFrame(2);
    const moved = harness!.measure();
    expectAnchored(moved);
    expect(moved.cardRect!.left).toBeGreaterThan(origin.cardRect!.left + 100);

    store.undo();
    await flushFrame(2);
    const undone = harness!.measure();
    expectAnchored(undone);
    expect(undone.cardRect!.left).toBeCloseTo(origin.cardRect!.left, 0);

    store.redo();
    await flushFrame(2);
    const redone = harness!.measure();
    expectAnchored(redone);
    expect(redone.cardRect!.left).toBeCloseTo(moved.cardRect!.left, 0);
  });

  it("容器 / 窗口尺寸变化后仍固定锚点", async () => {
    store.board = board([mkCard("c1", 300, 300)], { selection: ["c1"] });
    mountCanvas();
    await flushFrame();
    expectAnchored(harness!.measure());

    harness!.setViewport({ left: 0, top: 84, width: 900, height: 520 });
    window.dispatchEvent(new Event("resize"));
    await flushFrame(2);
    expectAnchored(harness!.measure());

    harness!.setViewport({ left: 40, top: 120, width: 1200, height: 700 });
    window.dispatchEvent(new Event("resize"));
    await flushFrame(2);
    expectAnchored(harness!.measure());
  });

  it("工具栏换行变高后，底边仍在卡片顶边上方 10px（用新高度）", async () => {
    store.board = board([mkCard("c1", 300, 300)], { selection: ["c1"] });
    mountCanvas();
    await flushFrame();
    expectAnchored(harness!.measure());

    harness!.setToolbarSize({ w: 337.59, h: 60 });
    window.dispatchEvent(new Event("resize"));
    await flushFrame(2);
    const m = harness!.measure();
    expectAnchored(m);
    expect(m.toolbarRect!.height).toBeCloseTo(60, 1);
    expect(m.cardRect!.top - m.toolbarRect!.top).toBeCloseTo(0, 1);
  });

  it("切换所有者：工具栏跟到最后点中的卡片，旧的工具栏不残留", async () => {
    store.board = board([mkCard("c1", 200, 200), mkCard("c2", 900, 500)], { selection: ["c1"] });
    mountCanvas();
    await flushFrame();
    expect(harness!.measure().cardId).toBe("c1");

    store.board = { ...(store.board as BoardState), selection: ["c1", "c2"] };
    await flushFrame(2);
    const multi = harness!.measure();
    expect(multi.cardId).toBe("c2");
    expect(wrapper!.findAll('[data-im="card-toolbar"]')).toHaveLength(1);
    expect(multi.hostOwnerId).toBe("c2");
    expectAnchored(multi);

    store.board = { ...(store.board as BoardState), selection: ["c2", "c1"] };
    await flushFrame(2);
    const back = harness!.measure();
    expect(back.cardId).toBe("c1");
    expect(wrapper!.findAll('[data-im="card-toolbar"]')).toHaveLength(1);
    expectAnchored(back);

    store.board = { ...(store.board as BoardState), selection: [] };
    await flushFrame(2);
    const gone = harness!.measure();
    expect(gone.toolbarVisible).toBe(false);
    expect(gone.hostVisible).toBe("0");
  });
});

describe("固定锚点：离屏与裁切", () => {
  it("部分离屏（左缘在板面可视区外）：完整几何仍满足固定锚点", async () => {
    store.board = board([mkCard("c1", -160, 300)], { selection: ["c1"] });
    mountCanvas();
    await flushFrame(2);
    const m = harness!.measure();
    expect(m.cardIntersectsViewport).toBe(true);
    expectAnchored(m);
    // 工具栏（在卡片上方）此时会被宿主裁掉一部分：完整几何仍然对齐
    expect(m.cardRect!.left).toBeLessThan(VIEW.left);
  });

  it("完全离屏：隐藏（data-toolbar-visible=0 + display:none），返回可视区后恢复", async () => {
    store.board = board([mkCard("c1", 400, 400)], { selection: ["c1"] });
    mountCanvas();
    await flushFrame();
    expectAnchored(harness!.measure());

    const current = store.board as BoardState;
    store.board = { ...current, cards: [{ ...current.cards[0], x: -3000, y: -3000 }] };
    await flushFrame(3);
    const hidden = harness!.measure();
    expect(hidden.cardIntersectsViewport).toBe(false);
    expect(hidden.toolbarVisible).toBe(false);
    expect(hidden.hostVisible).toBe("0");

    store.board = { ...current, cards: [{ ...current.cards[0], x: 500, y: 340 }] };
    await flushFrame(3);
    const restored = harness!.measure();
    expect(restored.cardIntersectsViewport).toBe(true);
    expectAnchored(restored);
  });
});
