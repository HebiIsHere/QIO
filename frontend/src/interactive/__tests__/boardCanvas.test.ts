/**
 * 板面组件接线测试（子智能体 B 负责）：证明板面操作真的按契约工作，而不只是能编译。
 *
 * 契约：docs/interactive-mode-contract.md §8.2 / §8.4。
 * 关注点：
 * - 所有状态变化都经过 store.commit(next, label)，组件不直接写 store.board；
 * - 拖动：按下 → 移动（只预演）→ 放下才提交；Esc 中断不提交；没产生变化的放下不提交；
 * - 平移 / 空格框选 / 滚轮缩放：只改查看状态，不动板面状态、不触发保存；
 * - 缩放后卡片拖动坐标仍然准确（统一走 viewport.ts）；
 * - 连接点拖线：拖动中显示待建连线，无效位置或取消不建链；
 * - 两卡明确重叠：提示「松开后合并成组」，松手才成组；
 * - 卡片局部工具栏：选中才出现，未选中即隐藏，勾选框只在文字注释上（材料没有）。
 *
 * 注意：jsdom 没有真实布局，所以这里给容器打的是**桩几何**（见 stubGeometry）；
 * 坐标数学的精确性由 viewport.test.ts 断言，这里只验证「组件确实按同一套换算在工作」。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BoardCanvas from "../../components/interactive/BoardCanvas.vue";
import { useInteractiveStore } from "../../stores/interactive";
import * as board from "../board";
import type { BoardCard, BoardGroup, BoardLink, BoardState, Intent } from "../types";

const SHELL_LEFT = 40;
const SHELL_TOP = 60;
const VIEW_LEFT = 40;
const VIEW_TOP = 100;
// 容器比板面内容（2400×1600）还大：这样视口不会产生滚动，
// 缩放 / 平移的坐标换算在测试里是确定的（屏幕 = 容器原点 + 板面坐标 × scale）。
const VIEW_W = 3000;
const VIEW_H = 2000;

function previewFixture(): { cardA: BoardCard; cardB: BoardCard; group: BoardGroup; link: BoardLink } {
  const seed = board.addCard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "材料一" }), {
    kind: "text",
    content: "材料二",
  });
  const cardA: BoardCard = { ...seed.cards[0], id: "pv_a", x: 120, y: 200, w: 120, h: 70 };
  const cardB: BoardCard = { ...seed.cards[1], id: "pv_b", x: 320, y: 200, w: 120, h: 70 };
  const group: BoardGroup = {
    id: "pv_g",
    name: "预览组",
    defaultName: true,
    ordered: true,
    x: 104,
    y: 140,
    w: 360,
    h: 160,
    members: ["pv_a", "pv_b"],
    deleted: false,
    createdAt: "2026-10-06T00:00:00.000Z",
    updatedAt: "2026-10-06T00:00:00.000Z",
  };
  const link: BoardLink = {
    id: "pv_l",
    src: "pv_a",
    dst: "pv_b",
    direction: true,
    meaning: "用户写的关系",
    deleted: false,
    createdAt: "2026-10-06T00:00:00.000Z",
    updatedAt: "2026-10-06T00:00:00.000Z",
  };
  return { cardA, cardB, group, link };
}

function intentFixture(status: Intent["status"]): Intent {
  const { cardA, cardB, group, link } = previewFixture();
  return {
    id: "intent_1",
    boardId: "board_t",
    submissionId: null,
    title: "整理两份材料",
    summary: "",
    status,
    preview: { cards: [cardA, cardB], groups: [group], links: [link], note: "演示预览" },
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 0, text: "" },
    reason: "",
    demo: true,
    createdAt: "2026-10-06T00:00:00.000Z",
    updatedAt: "2026-10-06T00:00:00.000Z",
  };
}

function pointer(type: string, x: number, y: number, extra: PointerEventInit = {}): MouseEvent {
  return new MouseEvent(type, { clientX: x, clientY: y, bubbles: true, cancelable: true, ...extra });
}

function wheel(x: number, y: number, deltaY: number, target?: Element): Event {
  const event = new WheelEvent("wheel", {
    clientX: x,
    clientY: y,
    deltaY,
    deltaMode: 0,
    bubbles: true,
    cancelable: true,
  });
  (target ?? document.body).dispatchEvent(event);
  return event;
}

/** jsdom 没有真实布局：给容器与外壳打桩几何，让坐标换算可被验证。 */
function stubGeometry(wrapper: VueWrapper) {
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  const shell = wrapper.get(".board-shell").element as HTMLElement;
  view.getBoundingClientRect = () =>
    ({ left: VIEW_LEFT, top: VIEW_TOP, right: VIEW_LEFT + VIEW_W, bottom: VIEW_TOP + VIEW_H, width: VIEW_W, height: VIEW_H, x: VIEW_LEFT, y: VIEW_TOP, toJSON: () => ({}) }) as DOMRect;
  shell.getBoundingClientRect = () =>
    ({ left: SHELL_LEFT, top: SHELL_TOP, right: SHELL_LEFT + VIEW_W, bottom: SHELL_TOP + VIEW_H + 200, width: VIEW_W, height: VIEW_H + 200, x: SHELL_LEFT, y: SHELL_TOP, toJSON: () => ({}) }) as DOMRect;
  Object.defineProperty(view, "clientWidth", { value: VIEW_W, configurable: true });
  Object.defineProperty(view, "clientHeight", { value: VIEW_H, configurable: true });
  return { view, shell };
}

interface ViewState {
  scale: number;
  scrollLeft: number;
  scrollTop: number;
}

/** 查看状态：内容用 transform 缩放，平移查看位置 = 容器滚动。 */
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

/** 板面坐标 → 屏幕坐标（按上面确定的换算关系）。 */
function screenOf(boardX: number, boardY: number, state: ViewState) {
  return {
    x: VIEW_LEFT + boardX * state.scale - state.scrollLeft,
    y: VIEW_TOP + boardY * state.scale - state.scrollTop,
  };
}

/** 卡片中心在屏幕上的位置（用于在缩放 / 平移后按下正确的像素点）。 */
function cardCenterOnScreen(card: BoardCard, state: ViewState) {
  return screenOf(card.x + card.w / 2, card.y + card.h / 2, state);
}

function mkCard(id: string, x: number, y: number, patch: Partial<BoardCard> = {}): BoardCard {
  const stamp = "2026-10-06T00:00:00.000Z";
  return {
    id,
    kind: patch.kind ?? "text",
    content: patch.content ?? "",
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
    createdAt: stamp,
    updatedAt: stamp,
  };
}

describe("BoardCanvas 接线", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  let labels: string[];
  const mounted: VueWrapper[] = [];

  // 用例之间必须隔离：挂载过的组件全部卸载并清空 body，
  // 否则 wrapper.get(...) 可能命中上一个用例残留的节点（查询是整份 document）。
  afterEach(() => {
    while (mounted.length) {
      const wrapper = mounted.pop();
      try {
        wrapper?.unmount();
      } catch {
        /* 已经卸载过 */
      }
    }
    document.body.innerHTML = "";
  });

  beforeEach(() => {
    setActivePinia(createPinia());
    store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    labels = [];
    // 模拟 store.commit 的效果（推撤销栈 + 保存），但跳过自动保存的定时器与网络。
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

  it("改版后画布不再渲染工具栏与搜索面板（由页面壳与 A 的组件负责）", () => {
    const wrapper = mountCanvas();
    expect(wrapper.find('[data-im="board-toolbar"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="add-text"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="search"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="board"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("未选中卡片时没有局部工具栏，也没有连接点", () => {
    setBoard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "一条注释" }));
    const wrapper = mountCanvas();
    expect(wrapper.find('[data-im="card"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="card-toolbar"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="connect-point"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it("选中卡片后浮出局部工具栏：勾选框只在文字注释上（材料没有），连接点出现", async () => {
    setBoard(
      board.addCard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "一条注释" }), {
        kind: "file",
        content: "材料说明",
        meta: { name: "a.pdf" },
      }),
    );
    const textId = store.board!.cards[0].id;
    const fileId = store.board!.cards[1].id;
    const wrapper = mountCanvas();
    const cards = wrapper.findAll('[data-im="card"]');
    expect(cards).toHaveLength(2);

    const textCard = cards.find((item) => item.attributes("data-card-id") === textId)!;
    textCard.element.dispatchEvent(pointer("pointerdown", 60, 30));
    await wrapper.vm.$nextTick();
    // 选中才出现：工具栏与连接点
    const toolbar = wrapper.get('[data-im="card-toolbar"]');
    expect(toolbar.attributes("data-card-id")).toBe(textId);
    expect(toolbar.find('[data-im="check"]').exists()).toBe(true);
    expect(wrapper.findAll('[data-im="connect-point"]').length).toBeGreaterThanOrEqual(4);
    expect(wrapper.get('[data-im="connect-point"]').attributes("data-card-id")).toBe(textId);
    // 工具栏里有编辑 / 复制 / 删除 / 折叠
    for (const hook of ["card-edit", "card-duplicate", "delete-card", "card-fold"]) {
      expect(toolbar.find('[data-im="' + hook + '"]').exists()).toBe(true);
    }
    expect(textCard.text()).toContain("未勾选");

    // 选中材料卡片：工具栏里没有勾选框
    wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === fileId)!.element.dispatchEvent(pointer("pointerdown", 60, 30));
    await wrapper.vm.$nextTick();
    const fileToolbar = wrapper.get('[data-im="card-toolbar"]');
    expect(fileToolbar.attributes("data-card-id")).toBe(fileId);
    expect(fileToolbar.find('[data-im="check"]').exists()).toBe(false);
    // 材料的状态说明在卡片本体上（工具栏只放操作）
    expect(wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === fileId)!.text()).toContain(
      "材料：默认在本次允许查看范围内",
    );
    wrapper.unmount();
  });

  it("多选时工具栏只显示共同适用的操作，不把单卡片编辑应用到整组", async () => {
    setBoard({
      ...board.emptyState("board_t"),
      cards: [mkCard("a", 0, 0), mkCard("b", 200, 0)],
      selection: ["a", "b"],
    });
    const wrapper = mountCanvas();
    const toolbar = wrapper.get('[data-im="card-toolbar"]');
    expect(toolbar.attributes("data-multi")).toBe("1");
    expect(toolbar.find('[data-im="card-edit"]').exists()).toBe(false);
    expect(toolbar.find('[data-im="card-duplicate"]').exists()).toBe(false);
    expect(toolbar.find('[data-im="card-fold"]').exists()).toBe(true);
    expect(toolbar.find('[data-im="delete-card"]').exists()).toBe(true);
    expect(toolbar.text()).toContain("多选：只显示共同适用的操作");
    wrapper.unmount();
  });

  it("勾选文字注释走 commit，并且勾选后不再是「未勾选」", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0, { content: "一条注释" })], selection: ["a"] });
    const wrapper = mountCanvas();
    await wrapper.get('[data-im="check"]').trigger("change");
    expect(labels[0]).toContain("勾选");
    expect(store.board?.cards[0].checked).toBe(true);
    expect(wrapper.get('[data-im="card"]').text()).toContain("已勾选");
    wrapper.unmount();
  });

  it("拖动：移动期间只预演（状态不变），放下才提交；明确重叠才自动成组", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0), mkCard("b", 240, 0)] });
    const wrapper = mountCanvas();
    const cardA = wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === "a");
    expect(cardA).toBeTruthy();

    // a 的矩形 0..100 x 0..60，按下中心 (50,30)
    cardA?.element.dispatchEvent(pointer("pointerdown", 90, 130));
    await wrapper.vm.$nextTick();
    // 拖到屏幕 (140,140) → 板面 (100,40)：与 b（240..340）不重叠
    window.dispatchEvent(pointer("pointermove", 140, 140));
    await wrapper.vm.$nextTick();
    expect(store.board?.cards.find((card) => card.id === "a")?.x).toBe(0);
    expect(wrapper.find('[data-im="group-merge-hint"]').exists()).toBe(false);

    // 再拖到屏幕 (290,140) → 指针在板面 (250,40)，卡片左上角落在 (200,10)：
    // 卡片矩形 200..300 x 10..70 与 b（240..340 x 0..60）明显重叠 → 提示「松开后合并成组」
    window.dispatchEvent(pointer("pointermove", 290, 140));
    await wrapper.vm.$nextTick();
    const hint = wrapper.get('[data-im="group-merge-hint"]');
    expect(hint.text()).toContain("松开后合并成组");
    expect(hint.attributes("data-target-card-id")).toBe("b");
    expect(store.board?.groups).toHaveLength(0); // 还没松手

    window.dispatchEvent(pointer("pointerup", 290, 140));
    await wrapper.vm.$nextTick();
    expect(store.board?.cards.find((card) => card.id === "a")?.x).toBe(200);
    expect(store.board?.groups).toHaveLength(1);
    expect(store.board?.groups[0].members.sort()).toEqual(["a", "b"]);
    // 单次松手只产生一次成组改动（这里走的是「加入新组」这条路径，不是两个已有组合并）
    expect(labels.filter((label) => label.includes("组"))).toHaveLength(1);
    expect(labels.filter((label) => label.includes("合并组"))).toHaveLength(0);
    wrapper.unmount();
  });

  it("靠近但没重叠时不提示成组，松手也不成组", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0), mkCard("b", 240, 0)] });
    const wrapper = mountCanvas();
    wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === "a")?.element.dispatchEvent(pointer("pointerdown", 90, 130));
    // 落点板面 (110,40) → a 的矩形 110..210，与 b（240..340）差 30px
    window.dispatchEvent(pointer("pointermove", 150, 140));
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-im="group-merge-hint"]').exists()).toBe(false);
    window.dispatchEvent(pointer("pointerup", 150, 140));
    await wrapper.vm.$nextTick();
    expect(store.board?.groups).toHaveLength(0);
    wrapper.unmount();
  });

  it("Esc 中断拖动：不提交，卡片回到操作前位置", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0)] });
    const wrapper = mountCanvas();
    wrapper.get('[data-im="card"]').element.dispatchEvent(pointer("pointerdown", 50, 30));
    window.dispatchEvent(pointer("pointermove", 550, 530));
    await wrapper.vm.$nextTick();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await wrapper.vm.$nextTick();
    expect(labels.filter((label) => label !== "选择卡片")).toHaveLength(0);
    expect(store.board?.cards[0].x).toBe(0);
    expect(wrapper.text()).toContain("已取消拖动");
    wrapper.unmount();
  });

  it("拖动后没有产生任何变化时不提交（不留下无意义改动）", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0), mkCard("b", 240, 0)] });
    const wrapper = mountCanvas();
    const cardA = wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === "a")!;
    cardA.element.dispatchEvent(pointer("pointerdown", 90, 130));
    await wrapper.vm.$nextTick();
    labels.length = 0;
    // 指针没动就松手：位置没变、也没有重叠
    window.dispatchEvent(pointer("pointerup", 90, 130));
    await wrapper.vm.$nextTick();
    expect(labels).toEqual([]);
    expect(store.board?.cards[0].x).toBe(0);
    wrapper.unmount();
  });

  it("拖动空白处 = 平移查看位置：只改视口，不动板面状态、不触发保存", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0)] });
    const wrapper = mountCanvas();
    const before = JSON.stringify(store.board);
    const view = wrapper.get(".board-viewport").element as HTMLElement;
    // 容器比内容大：先把缩放调大，让内容比容器大、可以滚动查看
    view.dispatchEvent(wheel(500, 400, -462));
    await wrapper.vm.$nextTick();
    const zoomed = viewState(wrapper);
    expect(zoomed.scale).toBeGreaterThan(1.9);

    view.dispatchEvent(pointer("pointerdown", 500, 400));
    window.dispatchEvent(pointer("pointermove", 560, 450));
    await wrapper.vm.$nextTick();
    const panned = viewState(wrapper);
    // 向右下拖 = 查看位置向左上移动 = 滚动量减少 60 / 50
    expect(panned.scrollLeft).toBe(zoomed.scrollLeft - 60);
    expect(panned.scrollTop).toBe(zoomed.scrollTop - 50);
    expect(panned.scale).toBe(zoomed.scale);
    window.dispatchEvent(pointer("pointerup", 560, 450));
    await wrapper.vm.$nextTick();
    expect(JSON.stringify(store.board)).toBe(before);
    expect(labels).toEqual([]);
    expect(wrapper.text()).toContain("平移");
    wrapper.unmount();
  });

  it("空白处单击（没有拖动）＝ 取消选择", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0)], selection: ["a"] });
    const wrapper = mountCanvas();
    wrapper.get(".board-viewport").element.dispatchEvent(pointer("pointerdown", 600, 500));
    window.dispatchEvent(pointer("pointerup", 600, 500));
    await wrapper.vm.$nextTick();
    expect(store.board?.selection).toEqual([]);
    expect(labels[0]).toContain("清空选择");
    wrapper.unmount();
  });

  it("空格 + 拖动空白处 = 框选卡片（缩放后范围仍然准确）", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0), mkCard("b", 400, 0)] });
    const wrapper = mountCanvas();
    const view = wrapper.get(".board-viewport").element;
    // 以卡片 a 的中心为缩放中心：缩放后它仍然在可视区，验证框选走的是同一套换算
    const cardCenter = { x: 50, y: 30 };
    view.dispatchEvent(wheel(VIEW_LEFT + cardCenter.x, VIEW_TOP + cardCenter.y, -462)); // 约 ×2
    await wrapper.vm.$nextTick();
    const state = viewState(wrapper);
    expect(state.scale).toBeGreaterThan(1.9);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: " ", code: "Space", bubbles: true, cancelable: true }));
    await wrapper.vm.$nextTick();
    // 在缩放后的坐标系里框住卡片 a：板面 (0,0)-(100,60) → 屏幕
    const from = screenOf(0, 0, state);
    const to = screenOf(100, 60, state);
    view.dispatchEvent(pointer("pointerdown", from.x, from.y));
    window.dispatchEvent(pointer("pointermove", to.x + 2, to.y + 2));
    await wrapper.vm.$nextTick();
    window.dispatchEvent(pointer("pointerup", to.x + 2, to.y + 2));
    await wrapper.vm.$nextTick();
    expect(store.board?.selection).toEqual(["a"]);
    expect(labels.filter((label) => label.includes("框选"))).toHaveLength(1);
    // 框选只改选择，不改卡片位置
    expect(store.board?.cards.find((card) => card.id === "a")?.x).toBe(0);
    window.dispatchEvent(new KeyboardEvent("keyup", { key: " ", code: "Space", bubbles: true }));
    wrapper.unmount();
  });

  it("输入框 / 可编辑内容里按空格不进入框选模式，空格正常输入", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0)] });
    const wrapper = mountCanvas();
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    // 事件打在输入框上（真实用户输入时空格就是这样冒泡的）
    input.dispatchEvent(new KeyboardEvent("keydown", { key: " ", code: "Space", bubbles: true, cancelable: true }));
    await wrapper.vm.$nextTick();
    // 空格没有启动框选：拖动空白处只会平移，不会框选
    wrapper.get(".board-viewport").element.dispatchEvent(pointer("pointerdown", 500, 400));
    window.dispatchEvent(pointer("pointermove", 540, 430));
    await wrapper.vm.$nextTick();
    expect(wrapper.find(".select-rect").exists()).toBe(false);
    window.dispatchEvent(pointer("pointerup", 540, 430));
    await wrapper.vm.$nextTick();
    expect(labels.filter((label) => label.includes("框选"))).toHaveLength(0);
    input.remove();
    wrapper.unmount();
  });

  it("滚轮以指针附近为缩放中心，且聊天 / 列表 / 代码区优先滚动自身", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0)] });
    const wrapper = mountCanvas();
    const view = wrapper.get(".board-viewport").element;
    const anchorScreen = screenOf(200, 100, { scale: 1, scrollLeft: 0, scrollTop: 0 });
    // 真实用户就是在板面上滚轮（事件目标是板面容器）
    const event = wheel(anchorScreen.x, anchorScreen.y, -462, view);
    expect(event.defaultPrevented).toBe(true);
    await wrapper.vm.$nextTick();
    const state = viewState(wrapper);
    expect(state.scale).toBeGreaterThan(1.9);
    // 指针下的板面点在缩放前后落在同一屏幕位置
    const screenAfter = screenOf(200, 100, state);
    expect(screenAfter.x).toBeCloseTo(anchorScreen.x, 2);
    expect(screenAfter.y).toBeCloseTo(anchorScreen.y, 2);
    expect(JSON.stringify(store.board)).toBe(JSON.stringify(board.normalizeState(store.board!)));

    // 可滚动子元素里的滚轮不缩放板面
    const scroller = document.createElement("div");
    scroller.style.overflowY = "auto";
    Object.defineProperty(scroller, "scrollHeight", { value: 400, configurable: true });
    Object.defineProperty(scroller, "clientHeight", { value: 100, configurable: true });
    view.appendChild(scroller);
    const before = viewState(wrapper).scale;
    const blocked = wheel(500, 400, -462, scroller);
    await wrapper.vm.$nextTick();
    expect(viewState(wrapper).scale).toBe(before);
    expect(blocked.defaultPrevented).toBe(false);
    scroller.remove();
    wrapper.unmount();
  });

  it("缩放后拖动卡片：坐标按板面坐标换算（不是屏幕像素）", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0), mkCard("b", 800, 400)] });
    const wrapper = mountCanvas();
    const view = wrapper.get(".board-viewport").element;
    view.dispatchEvent(wheel(500, 400, -462)); // 约 ×2
    await wrapper.vm.$nextTick();
    const state = viewState(wrapper);
    expect(state.scale).toBeGreaterThan(1.9);

    // 在缩放后的屏幕上按下卡片 a 的中心，再移动 100 屏幕像素 ≈ 50 板面单位
    const start = cardCenterOnScreen(mkCard("a", 0, 0), state);
    const cardA = wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === "a")!;
    cardA.element.dispatchEvent(pointer("pointerdown", start.x, start.y));
    await wrapper.vm.$nextTick();
    window.dispatchEvent(pointer("pointermove", start.x + 100, start.y + 60));
    await wrapper.vm.$nextTick();
    window.dispatchEvent(pointer("pointerup", start.x + 100, start.y + 60));
    await wrapper.vm.$nextTick();
    // 落点板面坐标 = (0,0) + (50,30) = (50,30)
    const moved = store.board?.cards.find((card) => card.id === "a")!;
    expect(moved.x).toBeCloseTo(50, 1);
    expect(moved.y).toBeCloseTo(30, 1);
    wrapper.unmount();
  });

  it("连接点拖线：拖动中显示待建连线与有效目标，松手建立关系；无效位置不建链", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0), mkCard("b", 400, 0)] });
    const wrapper = mountCanvas();
    const cardA = wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === "a")!;
    cardA.element.dispatchEvent(pointer("pointerdown", 50, 30));
    await wrapper.vm.$nextTick();
    const point = wrapper.findAll('[data-im="connect-point"]')[0];
    expect(point.attributes("data-card-id")).toBe("a");
    point.element.dispatchEvent(pointer("pointerdown", 100, 30));
    await wrapper.vm.$nextTick();

    // 拖到卡片 b 上：显示待建连线 + 有效目标
    window.dispatchEvent(pointer("pointermove", 480, 130));
    await wrapper.vm.$nextTick();
    const draft = wrapper.get('[data-im="link-draft"]');
    expect(draft.attributes("data-from")).toBe("a");
    expect(draft.attributes("data-target")).toBe("b");
    expect(draft.text()).toContain("松开后建立关系");
    expect(store.board?.links).toHaveLength(0); // 还没松手

    // 拖到空白处：待建连线仍在，但目标失效
    window.dispatchEvent(pointer("pointermove", 900, 600));
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-im="link-draft"]').attributes("data-target")).toBe("");
    window.dispatchEvent(pointer("pointerup", 900, 600));
    await wrapper.vm.$nextTick();
    expect(store.board?.links).toHaveLength(0);
    expect(labels.filter((label) => label.includes("新建关系"))).toHaveLength(0);
    expect(wrapper.text()).toContain("没有建立关系");

    // 重新拖一次，落在 b 上 → 建立关系并打开方向 / 含义编辑
    point.element.dispatchEvent(pointer("pointerdown", 100, 30));
    window.dispatchEvent(pointer("pointermove", 480, 130));
    await wrapper.vm.$nextTick();
    window.dispatchEvent(pointer("pointerup", 480, 130));
    await wrapper.vm.$nextTick();
    expect(store.board?.links).toHaveLength(1);
    expect([store.board?.links[0].src, store.board?.links[0].dst].sort()).toEqual(["a", "b"]);
    expect(labels.filter((label) => label.includes("新建关系"))).toHaveLength(1);
    // 方向与含义由用户写明：编辑器打开，系统不补写解释
    expect(wrapper.find('[data-im="link-meaning"]').exists()).toBe(true);
    expect(store.board?.links[0].meaning).toBe("");
    wrapper.unmount();
  });

  it("连接点拖线被 Esc 取消：不建链、不保存半条链接", async () => {
    setBoard({ ...board.emptyState("board_t"), cards: [mkCard("a", 0, 0), mkCard("b", 400, 0)] });
    const wrapper = mountCanvas();
    wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === "a")!.element.dispatchEvent(pointer("pointerdown", 50, 30));
    await wrapper.vm.$nextTick();
    wrapper.findAll('[data-im="connect-point"]')[0].element.dispatchEvent(pointer("pointerdown", 100, 30));
    window.dispatchEvent(pointer("pointermove", 480, 130));
    await wrapper.vm.$nextTick();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-im="link-draft"]').exists()).toBe(false);
    expect(store.board?.links).toHaveLength(0);
    window.dispatchEvent(pointer("pointerup", 480, 130));
    await wrapper.vm.$nextTick();
    expect(store.board?.links).toHaveLength(0);
    wrapper.unmount();
  });

  it("拖到已有组上：拖动期间组框显示插入位置，放下后加入该组并保留组名", async () => {
    const base = board.addCard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "A" }), {
      kind: "text",
      content: "C",
    });
    const grouped = board.createGroup(base, [base.cards[0].id], "发布计划");
    const groupId = grouped.groups[0].id;
    setBoard({
      ...grouped,
      cards: [
        { ...grouped.cards[0], x: 0, y: 0, w: 100, h: 60 },
        { ...grouped.cards[1], id: "c", x: 900, y: 0, w: 100, h: 60 },
      ],
    });
    const wrapper = mountCanvas();
    const free = wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === "c")!;
    // 按在 c 的左上角：这样落点就是卡片左上角的位置
    free.element.dispatchEvent(pointer("pointerdown", 940, 100));
    // 落点板面 (40,40)：卡片矩形 40..140 x 40..100，进入组框范围（-16..116 x -58..76）
    window.dispatchEvent(pointer("pointermove", 80, 140));
    await wrapper.vm.$nextTick();
    const frame = wrapper.get('[data-im="group"]');
    expect(frame.text()).toContain("普通组：摆放顺序不代表先后");
    expect(frame.get('[data-im="group-drop-hint"]').text()).toContain("将插入第");
    expect(store.board?.cards.find((card) => card.id === "c")?.x).toBe(900);

    window.dispatchEvent(pointer("pointerup", 80, 140));
    await wrapper.vm.$nextTick();
    expect(store.board?.groups[0].members).toContain("c");
    expect(store.board?.groups[0].name).toBe("发布计划");
    expect(board.groupById(store.board!, groupId)?.name).toBe("发布计划");
    wrapper.unmount();
  });

  it("组名编辑：输入即用；留空保留默认名且组仍然成立", async () => {
    const base = board.addCard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "A" }), {
      kind: "text",
      content: "B",
    });
    setBoard(board.createGroup(base, [base.cards[0].id, base.cards[1].id]));
    const wrapper = mountCanvas();
    const input = wrapper.get('[data-im="group-name"]');
    expect((input.element as HTMLInputElement).value).toBe("组 1");
    expect(wrapper.get('[data-im="group"]').text()).toContain("系统默认名，可改");

    await input.setValue("发布计划");
    expect(store.board?.groups[0].name).toBe("发布计划");
    expect(labels.filter((label) => label.includes("组名"))).toHaveLength(1);

    await input.setValue("   ");
    // 留空不生效：组名保持「发布计划」，组仍然成立
    expect(store.board?.groups[0].name).toBe("发布计划");
    expect(store.board?.groups[0].members).toHaveLength(2);
    wrapper.unmount();
  });

  it("有序组显示明确序号，拖动时显示插入位置", async () => {
    const base = board.addCard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "A" }), {
      kind: "text",
      content: "B",
    });
    const created = board.createGroup(base, [base.cards[0].id, base.cards[1].id]);
    setBoard(board.setGroupOrdered(created, created.groups[0].id, true));
    const wrapper = mountCanvas();
    const frame = wrapper.get('[data-im="group"]');
    expect(frame.text()).toContain("有序：序号 1..n 表示顺序");
    const chips = frame.findAll(".chip");
    expect(chips.length).toBeGreaterThanOrEqual(2);
    expect(chips[0].text()).toContain("1");
    expect(chips[1].text()).toContain("2");
    wrapper.unmount();
  });

  it("板内搜索定位（页面壳 dispatch 事件）能高亮对应卡片", async () => {
    setBoard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "发布节奏需要确认", checked: false }));
    const cardId = store.board!.cards[0].id;
    const wrapper = mountCanvas();
    expect(wrapper.get('[data-im="card"]').classes()).not.toContain("highlight");
    window.dispatchEvent(new CustomEvent("qio:interactive:locate-card", { detail: { cardId } }));
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-im="card"]').classes()).toContain("highlight");
    // 查不到 / 不存在的卡片不报错也不高亮
    window.dispatchEvent(new CustomEvent("qio:interactive:locate-card", { detail: { cardId: "ghost" } }));
    await wrapper.vm.$nextTick();
    wrapper.unmount();
  });

  it("待审批预览画在真实板面上：虚线卡片 / 组 / 关系，且不参与命中", async () => {
    store.intents = [intentFixture("pending")];
    const wrapper = mountCanvas();
    const layer = wrapper.get('[data-im="preview"][data-intent-id="intent_1"]');
    expect(layer.text()).toContain("预览（未确定）");
    expect(layer.text()).toContain("预览组");
    expect(layer.text()).toContain("用户写的关系");
    const marked = wrapper.findAll('[data-im="preview"][data-intent-id="intent_1"]');
    expect(marked.length).toBeGreaterThanOrEqual(4);
    expect(wrapper.find(".preview-layer").exists()).toBe(true);
    wrapper.unmount();
  });

  it("定位事件：对应意图的预览被高亮；已拒绝的意图不画预览", async () => {
    store.intents = [intentFixture("pending"), { ...intentFixture("rejected"), id: "intent_2" }];
    const wrapper = mountCanvas();
    expect(wrapper.find('[data-intent-id="intent_2"]').exists()).toBe(false);
    window.dispatchEvent(
      new CustomEvent("qio:interactive:locate-preview", {
        detail: { intentId: "intent_1", bounds: { x: 100, y: 140, w: 360, h: 160 } },
      }),
    );
    await wrapper.vm.$nextTick();
    expect(wrapper.get('[data-im="preview"][data-intent-id="intent_1"]').classes()).toContain("located");
    wrapper.unmount();
  });

  it("组框显示组名与有序状态，切有序走 commit", async () => {
    const base = board.addCard(board.addCard(board.emptyState("board_t"), { kind: "text", content: "A" }), {
      kind: "text",
      content: "B",
    });
    setBoard(board.createGroup(base, [base.cards[0].id, base.cards[1].id], "发布计划"));
    const wrapper = mountCanvas();
    const frame = wrapper.get('[data-im="group"]');
    expect(frame.text()).toContain("普通组：摆放顺序不代表先后");
    expect((frame.get('[data-im="group-name"]').element as HTMLInputElement).value).toBe("发布计划");
    await frame.get('[data-im="toggle-ordered"]').trigger("click");
    expect(labels[0]).toContain("有序");
    expect(store.board?.groups[0].ordered).toBe(true);
    expect(wrapper.get('[data-im="group"]').text()).toContain("有序：序号 1..n 表示顺序");
    wrapper.unmount();
  });
});
