/**
 * F08（B 组）· 定位收起组内的卡片：先展开组、布局可用后再定位 + 高亮，且按实际可用视野定位。
 *
 * 基线缺陷（9b23634）：BoardCanvas.locate 只 revealDelta + highlight；收起的组用
 * collapsedGroupIds 控制成员卡片不渲染 —— 目标卡片根本不在画面上，定位等于什么都没发生；
 * 同时可用区按整个视口算，不考虑浮动对话窗口的遮挡。
 *
 * 断言的是真实行为：卡片是否真的重新渲染、是否高亮、滚动位置是否让目标进入**实际可用区**，
 * 以及定位是否只改查看位置（不动卡片 / 成员 / 参与 / 正文 / 提交）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import BoardCanvas from "../../components/interactive/BoardCanvas.vue";
import BoardLinkLayer from "../../components/interactive/BoardLinkLayer.vue";
import { useInteractiveStore } from "../../stores/interactive";
import { publishChatFrame, resetChatFrameState } from "../../interactive/chatFrameState";
import * as board from "../board";
import type { BoardCard, BoardGroup, BoardState } from "../types";

vi.mock("../../services/interactive", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/interactive")>();
  return {
    ...actual,
    fetchBoardState: vi.fn(async () => null),
    saveBoardState: vi.fn(async (_boardId: string, putState: unknown) => ({
      ok: true,
      seq: 1,
      savedAt: "2026-10-25T00:00:00.000Z",
      state: putState,
    })),
    saveDrafts: vi.fn(async () => ({ drafts: {}, updatedAt: "2026-10-25T00:00:00.000Z" })),
    fetchVisibleRange: vi.fn(async () => ({ visibleRange: null })),
    fetchIntents: vi.fn(async () => ({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } })),
  };
});

const STAMP = "2026-10-25T00:00:00.000Z";
const VIEW_LEFT = 40;
const VIEW_TOP = 100;
const VIEW_W = 800;
const VIEW_H = 600;
/** 定位留白（与 BoardCanvas.locate 的 margin 一致）。 */
const LOCATE_MARGIN = 80;

function mkCard(id: string, x: number, y: number, patch: Partial<BoardCard> = {}): BoardCard {
  return {
    id,
    kind: patch.kind ?? "text",
    content: patch.content ?? "卡片 " + id,
    meta: patch.meta ?? {},
    x,
    y,
    w: patch.w ?? 100,
    h: patch.h ?? 60,
    checked: patch.checked ?? false,
    hidden: patch.hidden ?? false,
    folded: patch.folded ?? false,
    bookmarked: patch.bookmarked ?? false,
    deleted: patch.deleted ?? false,
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

function mkGroup(id: string, members: string[], patch: Partial<BoardGroup> = {}): BoardGroup {
  return {
    id,
    name: patch.name ?? "组 " + id,
    defaultName: patch.defaultName ?? true,
    ordered: patch.ordered ?? false,
    x: patch.x ?? 0,
    y: patch.y ?? 0,
    w: patch.w ?? 1,
    h: patch.h ?? 1,
    members,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

function mkState(cards: BoardCard[], groups: BoardGroup[] = []): BoardState {
  return board.normalizeState({ ...board.emptyState("board_t"), cards, groups });
}

function stubGeometry(wrapper: VueWrapper) {
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  const shell = wrapper.get(".board-shell").element as HTMLElement;
  const rect = (left: number, top: number, width: number, height: number) =>
    ({ left, top, right: left + width, bottom: top + height, width, height, x: left, y: top, toJSON: () => ({}) }) as DOMRect;
  view.getBoundingClientRect = () => rect(VIEW_LEFT, VIEW_TOP, VIEW_W, VIEW_H);
  shell.getBoundingClientRect = () => rect(VIEW_LEFT, VIEW_TOP, VIEW_W, VIEW_H);
  Object.defineProperty(view, "clientWidth", { value: VIEW_W, configurable: true });
  Object.defineProperty(view, "clientHeight", { value: VIEW_H, configurable: true });
  // 让容器「真的能滚动」：jsdom 里 scrollLeft / scrollTop 才会被写进 DOM（坐标换算的事实来源）。
  Object.defineProperty(view, "scrollWidth", { value: 4000, configurable: true });
  Object.defineProperty(view, "scrollHeight", { value: 4000, configurable: true });
  Object.defineProperty(view, "scrollLeft", { value: 0, writable: true, configurable: true });
  Object.defineProperty(view, "scrollTop", { value: 0, writable: true, configurable: true });
}

interface ViewState {
  scale: number;
  scrollLeft: number;
  scrollTop: number;
}

function viewState(wrapper: VueWrapper): ViewState {
  const surface = wrapper.get(".board-surface").element as HTMLElement;
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  const match = /scale\(([\d.]+)\)/.exec(surface.style.transform);
  return {
    scale: match ? Number(match[1]) : Number.NaN,
    scrollLeft: view.scrollLeft,
    scrollTop: view.scrollTop,
  };
}

/** 板面坐标 → 屏幕坐标（与 BoardCanvas.screenPointOfBoard 同一口径：容器左上 − 滚动量）。 */
function screenOf(boardX: number, boardY: number, state: ViewState) {
  return {
    x: VIEW_LEFT + boardX * state.scale - state.scrollLeft,
    y: VIEW_TOP + boardY * state.scale - state.scrollTop,
  };
}

/** 定位是异步的（展开 → nextTick → 一帧）：等两帧 + 宏任务，让布局与定位都落地。 */
async function flushLocate(): Promise<void> {
  for (let i = 0; i < 2; i += 1) {
    await nextTick();
    await new Promise<void>((resolve) => {
      if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => resolve());
      else setTimeout(resolve, 16);
    });
    await new Promise<void>((resolve) => setTimeout(resolve, 20));
    await nextTick();
  }
}

function locateCard(cardId: string): void {
  window.dispatchEvent(new CustomEvent("qio:interactive:locate-card", { detail: { cardId } }));
}

describe("F08 定位收起组内卡片：先展开、再按实际可用视野定位", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  let labels: string[];
  const mounted: VueWrapper[] = [];

  afterEach(() => {
    while (mounted.length) mounted.pop()?.unmount();
    document.body.innerHTML = "";
    resetChatFrameState();
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

  function setBoard(next: BoardState) {
    store.board = board.normalizeState(next);
  }

  function mountCanvas(): VueWrapper {
    const wrapper = mount(BoardCanvas, { attachTo: document.body });
    mounted.push(wrapper);
    stubGeometry(wrapper);
    return wrapper;
  }

  function cardEls(wrapper: VueWrapper, cardId: string) {
    return wrapper.findAll('[data-im="card"]').filter((item) => item.attributes("data-card-id") === cardId);
  }

  function expectCardInsideUsableArea(wrapper: VueWrapper, card: BoardCard, area: { left: number; top: number; right: number; bottom: number }) {
    const state = viewState(wrapper);
    const screen = screenOf(card.x, card.y, state);
    expect(screen.x, "定位后卡片左边缘在可用区内").toBeGreaterThanOrEqual(area.left + LOCATE_MARGIN - 1);
    expect(screen.y, "定位后卡片上边缘在可用区内").toBeGreaterThanOrEqual(area.top + LOCATE_MARGIN - 1);
    expect(screen.x + card.w * state.scale, "定位后卡片右边缘在可用区内").toBeLessThanOrEqual(area.right - LOCATE_MARGIN + 1);
    expect(screen.y + card.h * state.scale, "定位后卡片下边缘在可用区内").toBeLessThanOrEqual(area.bottom - LOCATE_MARGIN + 1);
  }

  it("目标在收起的组里：先展开该组，等布局可用后再定位并高亮；不改卡片 / 成员 / 参与 / 正文", async () => {
    const state = mkState(
      [mkCard("a", 100, 1200, { w: 120, h: 80 }), mkCard("b", 100, 1300, { w: 120, h: 80, content: "要定位的卡片" })],
      [mkGroup("g1", ["a", "b"], { name: "深处组" })],
    );
    setBoard(board.setParticipation(state, "b", true));
    const wrapper = mountCanvas();
    // 组默认展开；先收起（纯显示状态）：成员卡不在画面上
    await wrapper.get('[data-im="group-collapse-toggle"]').trigger("click");
    await nextTick();
    expect(cardEls(wrapper, "b"), "收起后成员卡不应渲染").toHaveLength(0);

    locateCard("b");
    await flushLocate();

    // 目标真的回到画面上，并被高亮
    expect(cardEls(wrapper, "b"), "定位收起组内卡片必须先展开组，让目标可见").toHaveLength(1);
    expect(cardEls(wrapper, "b")[0].classes()).toContain("highlight");
    expect(wrapper.text()).toContain("已展开");
    // 定位只改查看位置：板面状态一个字段都不改，也不产生提交
    expect(labels, "定位不是板面改动，不该 commit").toHaveLength(0);
    const after = store.board!;
    expect(board.groupById(after, "g1")!.members).toEqual(["a", "b"]);
    expect(board.cardById(after, "b")).toMatchObject({ x: 100, y: 1300, content: "要定位的卡片" });
    expect(board.participationOf(board.cardById(after, "b")!)).toBe("included");
    // 深处卡片被滚进实际可用区（视口 800×600）
    expect(viewState(wrapper).scrollTop).toBeGreaterThan(0);
    expectCardInsideUsableArea(wrapper, board.cardById(after, "b")!, {
      left: VIEW_LEFT,
      top: VIEW_TOP,
      right: VIEW_LEFT + VIEW_W,
      bottom: VIEW_TOP + VIEW_H,
    });
    wrapper.unmount();
    mounted.splice(mounted.indexOf(wrapper), 1);
  });

  it("按实际可用视野定位：浮动对话窗口盖住的那块屏幕不算可用区", async () => {
    // 视口 1200×800；浮动对话窗口盖住右侧 900..1600 × 250..950
    const viewW = 1200;
    const viewH = 800;
    setBoard(mkState([mkCard("wide", 1000, 300, { w: 200, h: 100 })]));
    const wrapper = mountCanvas();
    // 换成大视口桩几何 + 可滚动
    const view = wrapper.get(".board-viewport").element as HTMLElement;
    const shell = wrapper.get(".board-shell").element as HTMLElement;
    const rect = (left: number, top: number, width: number, height: number) =>
      ({ left, top, right: left + width, bottom: top + height, width, height, x: left, y: top, toJSON: () => ({}) }) as DOMRect;
    view.getBoundingClientRect = () => rect(VIEW_LEFT, VIEW_TOP, viewW, viewH);
    shell.getBoundingClientRect = () => rect(VIEW_LEFT, VIEW_TOP, viewW, viewH);
    Object.defineProperty(view, "clientWidth", { value: viewW, configurable: true });
    Object.defineProperty(view, "clientHeight", { value: viewH, configurable: true });
    await nextTick();
    publishChatFrame({ x: 900, y: 250, w: 700, h: 700 }, false);
    await nextTick();

    const chat = { left: 900, top: 250, right: 1600, bottom: 950 };
    locateCard("wide");
    await flushLocate();

    const state = viewState(wrapper);
    const card = board.cardById(store.board!, "wide")!;
    const screen = screenOf(card.x, card.y, state);
    // 卡片必须落在「视口扣掉浮动对话窗口」后剩下的可用区里（右侧到对话窗口左边缘为止）
    expect(screen.x + card.w * state.scale).toBeLessThanOrEqual(chat.left - LOCATE_MARGIN + 1);
    expect(screen.x).toBeGreaterThanOrEqual(VIEW_LEFT + LOCATE_MARGIN - 1);
    expect(screen.y).toBeGreaterThanOrEqual(VIEW_TOP + LOCATE_MARGIN - 1);
    expect(screen.y + card.h * state.scale).toBeLessThanOrEqual(VIEW_TOP + viewH - LOCATE_MARGIN + 1);
    // 不压住对话窗口
    expect(board.rectsOverlap(
      { x: screen.x, y: screen.y, w: card.w * state.scale, h: card.h * state.scale },
      { x: chat.left, y: chat.top, w: chat.right - chat.left, h: chat.bottom - chat.top },
    )).toBe(false);
    // 高亮仍然生效（定位流程一致）
    expect(cardEls(wrapper, "wide")[0].classes()).toContain("highlight");
    wrapper.unmount();
    mounted.splice(mounted.indexOf(wrapper), 1);
  });

  it("卡片比可用视野还大：稳定对齐左上角（可预期的查看策略，不来回跳）", async () => {
    setBoard(mkState([mkCard("huge", 500, 500, { w: 2000, h: 1200 })]));
    const wrapper = mountCanvas();
    locateCard("huge");
    await flushLocate();

    const state = viewState(wrapper);
    const card = board.cardById(store.board!, "huge")!;
    const screen = screenOf(card.x, card.y, state);
    // 只对齐左上角：不试图把右下边也拉进来（否则两次定位会左右 / 上下轮流跳）
    expect(screen.x).toBeCloseTo(VIEW_LEFT + LOCATE_MARGIN, 0);
    expect(screen.y).toBeCloseTo(VIEW_TOP + LOCATE_MARGIN, 0);
    wrapper.unmount();
    mounted.splice(mounted.indexOf(wrapper), 1);
  });

  it("定位只执行明确请求的那一次：之后用户手动滚动不会被抢回", async () => {
    setBoard(mkState([mkCard("far", 2000, 1200)]));
    const wrapper = mountCanvas();
    locateCard("far");
    await flushLocate();
    const located = viewState(wrapper);
    expect(located.scrollLeft).toBeGreaterThan(0);
    expect(located.scrollTop).toBeGreaterThan(0);

    // 用户自己滚回左上：定位不得持续追踪 / 自动抢视野
    const view = wrapper.get(".board-viewport").element as HTMLElement;
    view.scrollLeft = 0;
    view.scrollTop = 0;
    view.dispatchEvent(new Event("scroll"));
    await flushLocate();
    expect(viewState(wrapper).scrollLeft).toBe(0);
    expect(viewState(wrapper).scrollTop).toBe(0);
    wrapper.unmount();
    mounted.splice(mounted.indexOf(wrapper), 1);
  });
});

describe("C 接口接线：BoardLinkLayer 收到画布缩放（F09）", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  const mounted: VueWrapper[] = [];

  afterEach(() => {
    while (mounted.length) mounted.pop()?.unmount();
    document.body.innerHTML = "";
  });

  beforeEach(() => {
    setActivePinia(createPinia());
    store = useInteractiveStore();
    store.board = board.emptyState("board_t");
  });

  it("BoardCanvas 把 scaleSafe 传给 BoardLinkLayer（缩放时的拖动换算依赖它）", () => {
    store.board = board.normalizeState({
      ...board.emptyState("board_t"),
      cards: [mkCard("a", 0, 0)],
    });
    const wrapper = mount(BoardCanvas, { attachTo: document.body });
    mounted.push(wrapper);
    stubGeometry(wrapper);
    const layer = wrapper.findComponent(BoardLinkLayer);
    // C 的 BoardLinkLayer 会把它声明成 prop；本分支尚未合并 C 的 commit 时它作为 attribute 透传。
    // 两种形态都接受，只断言「画布确实把当前缩放传给了连线层」。
    const layerProps = layer.props() as unknown as Record<string, unknown>;
    const declared = layerProps.scale;
    const attr = layer.attributes("scale");
    expect(declared ?? Number(attr)).toBe(1);
  });
});
