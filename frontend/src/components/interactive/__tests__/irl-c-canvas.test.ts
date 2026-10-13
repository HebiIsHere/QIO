/**
 * IRL-C 画布接线测试（任务书 §3.1-3.10 / §7.1-§7.8）。
 *
 * 这里证明的是**生产路径真的接上了**，而不是「纯函数写好了但没人用」：
 * 1. 尺寸把手层：位置 = 显示槽位 × 缩放（缩放后仍是真 10px / 16px 热区），只给选中的卡；
 * 2. 拖动中只预览（跟手、不提交），松手 / 键盘 −＋ 才**一次** commit，标签固定「调整卡片尺寸」；
 * 3. 尺寸调整不改许可 / 选择 / 展开集合，也不会触发重叠成组；
 * 4. 展开卡调整尺寸只提交 { w, h }：临时槽位坐标绝不写回板面（位置只由拖动改变）；
 * 5. BoardCanvas 传了 expandedAvoid:true 与**真实**的 stageOrigin（平移后的可视起点）：
 *    多张展开卡互不遮挡；平移之后展开卡被拉进可视区；
 * 6. 可用区域扣掉底部工具栏（Y 轴）与对话窗口等浮层；
 * 7. 组框与关系线继续走同一份显示坐标：调整尺寸 / 让位后仍然框得住、接得上。
 *
 * 注意：jsdom 没有真实布局，容器几何是打桩的（与 boardCanvas.test.ts 同一套做法）。
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
import { publishChatFrame, resetChatFrameState } from "../../../interactive/chatFrameState";
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

function px(value: string | undefined): number {
  return Number.parseFloat((value ?? "").replace("px", ""));
}

/** 显示矩形（从 DOM 行内样式读）：left / top / width / height 都是**板面坐标** */
function rectOfCard(wrapper: VueWrapper, cardId: string): { x: number; y: number; w: number; h: number } {
  const found = wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === cardId);
  const style = styleOf(found?.element);
  return { x: px(style.left), y: px(style.top), w: px(style.width), h: px(style.height) };
}

function overlaps(a: { x: number; y: number; w: number; h: number }, b: { x: number; y: number; w: number; h: number }): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

const SRC = resolve(process.cwd(), "src");
function source(relativePath: string): string {
  return readFileSync(resolve(SRC, relativePath), "utf8");
}

/** jsdom 没有真实布局：给容器打桩几何，让可用区域是可预期的 */
function stubGeometry(wrapper: VueWrapper): HTMLElement {
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  const shell = wrapper.get(".board-shell").element as HTMLElement;
  view.getBoundingClientRect = () =>
    ({ left: VIEW_LEFT, top: VIEW_TOP, right: VIEW_LEFT + VIEW_W, bottom: VIEW_TOP + VIEW_H, width: VIEW_W, height: VIEW_H, x: VIEW_LEFT, y: VIEW_TOP, toJSON: () => ({}) }) as DOMRect;
  shell.getBoundingClientRect = () =>
    ({ left: VIEW_LEFT, top: VIEW_TOP, right: VIEW_LEFT + VIEW_W, bottom: VIEW_TOP + VIEW_H, width: VIEW_W, height: VIEW_H, x: VIEW_LEFT, y: VIEW_TOP, toJSON: () => ({}) }) as DOMRect;
  Object.defineProperty(view, "clientWidth", { value: VIEW_W, configurable: true });
  Object.defineProperty(view, "clientHeight", { value: VIEW_H, configurable: true });
  return view;
}

let wrapper: VueWrapper | null = null;
let store: ReturnType<typeof useInteractiveStore>;
let labels: string[];

function mountCanvas(): VueWrapper {
  wrapper = mount(BoardCanvas, { attachTo: document.body });
  stubGeometry(wrapper);
  return wrapper;
}

/** 让容器「真的能滚动」，并把滚动量设成 panned：jsdom 里默认滚动量会被钳到 0 */
function makeScrollable(view: HTMLElement, left: number, top: number): void {
  Object.defineProperty(view, "scrollWidth", { value: VIEW_W * 2, configurable: true });
  Object.defineProperty(view, "scrollHeight", { value: VIEW_H * 2, configurable: true });
  Object.defineProperty(view, "scrollLeft", { value: left, writable: true, configurable: true });
  Object.defineProperty(view, "scrollTop", { value: top, writable: true, configurable: true });
}

beforeEach(() => {
  localStorage.clear();
  resetChatFrameState();
  setActivePinia(createPinia());
  store = useInteractiveStore();
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
  resetChatFrameState();
});

describe("尺寸把手层：位置对齐显示槽位，缩放后仍是真像素热区", () => {
  it("只有选中的卡片有把手，位置 / 尺寸 = 显示槽位 × 缩放", async () => {
    store.board = board([mkCard("c1", 100, 100), mkCard("c2", 600, 400)], { selection: ["c1"] });
    const view = mountCanvas();
    expect(view.find('[data-resize-card-id="c1"]').exists()).toBe(true);
    expect(view.find('[data-resize-card-id="c2"]').exists()).toBe(false);
    const host = view.get('[data-resize-card-id="c1"]');
    expect(styleOf(host.element)).toMatchObject({ left: "100px", top: "100px", width: "120px", height: "80px" });
    // 八向热区 + 右下角标记 + 键盘 −/＋（由 ResizeHandles 自带）
    expect(host.findAll("[data-resize-handle]")).toHaveLength(8);
    expect(host.find("[data-resize-keys]").exists()).toBe(true);

    // 缩放后：把手层在**未缩放**的内容层里，位置随缩放走，热区仍是 10px / 16px
    view.get(".board-viewport").element.dispatchEvent(
      new WheelEvent("wheel", { clientX: 500, clientY: 400, deltaY: -462, deltaMode: 0, bubbles: true, cancelable: true }),
    );
    await nextTick();
    const scale = Number(/scale\(([\d.]+)\)/.exec(styleOf(view.get(".board-surface").element).transform ?? "")?.[1] ?? "1");
    expect(scale).toBeGreaterThan(1.9);
    expect(styleOf(view.get('[data-resize-card-id="c1"]').element)).toMatchObject({
      left: 100 * scale + "px",
      top: 100 * scale + "px",
      width: 120 * scale + "px",
      height: 80 * scale + "px",
    });
  });

  it("拖动中只预览、不提交；松手才一次 commit，标签「调整卡片尺寸」，只改几何", async () => {
    store.board = board([mkCard("c1", 100, 100, 120, 80, { checked: true })], { selection: ["c1"] });
    const view = mountCanvas();
    const handle = view.get('[data-resize-card-id="c1"] [data-resize-handle="se"]');
    // 卡片右下角在屏幕 (160, 280)：SE 热区就在那里
    handle.element.dispatchEvent(pointer("pointerdown", VIEW_LEFT + 220, VIEW_TOP + 180));

    document.dispatchEvent(pointer("pointermove", VIEW_LEFT + 280, VIEW_TOP + 220));
    await nextTick();
    // 跟手：显示变大了，但板面状态一个字没改，也没有提交
    expect(labels).toEqual([]);
    expect(rectOfCard(view, "c1").w).toBe(180);
    expect(rectOfCard(view, "c1").h).toBe(120);
    expect(store.board?.cards[0].w).toBe(120);
    expect(store.board?.cards[0].h).toBe(80);

    document.dispatchEvent(pointer("pointerup", VIEW_LEFT + 280, VIEW_TOP + 220));
    await nextTick();
    expect(labels).toEqual(["调整卡片尺寸"]);
    expect(store.board?.cards[0]).toMatchObject({ x: 100, y: 100, w: 180, h: 120 });
    // 许可 / 选择 / 展开集合一个都没动
    expect(store.board?.cards[0].checked).toBe(true);
    expect(store.board?.cards[0].hidden).toBe(false);
    expect(store.board?.selection).toEqual(["c1"]);
    expect(store.expandedCardIds).toEqual([]);
    // 没有成组、也没有任何表达
    expect(store.board?.groups).toEqual([]);
    expect(store.board?.links).toEqual([]);
  });

  it("指针取消：不提交，卡片回到拖动前的尺寸", async () => {
    store.board = board([mkCard("c1", 100, 100)], { selection: ["c1"] });
    const view = mountCanvas();
    view.get('[data-resize-card-id="c1"] [data-resize-handle="se"]').element.dispatchEvent(
      pointer("pointerdown", VIEW_LEFT + 220, VIEW_TOP + 180),
    );
    document.dispatchEvent(pointer("pointermove", VIEW_LEFT + 300, VIEW_TOP + 240));
    await nextTick();
    expect(rectOfCard(view, "c1").w).toBe(200);

    document.dispatchEvent(pointer("pointercancel", VIEW_LEFT + 300, VIEW_TOP + 240));
    await nextTick();
    expect(labels).toEqual([]);
    expect(rectOfCard(view, "c1")).toEqual({ x: 100, y: 100, w: 120, h: 80 });
    expect(store.board?.cards[0].w).toBe(120);
  });

  it("键盘 ＋ 一步：直接提交一次，宽度 +24", async () => {
    store.board = board([mkCard("c1", 100, 100)], { selection: ["c1"] });
    const view = mountCanvas();
    await view.get('[data-resize-card-id="c1"] [data-resize-key="w+"]').trigger("click");
    expect(labels).toEqual(["调整卡片尺寸"]);
    expect(store.board?.cards[0].w).toBe(144);
    expect(store.board?.cards[0].h).toBe(80);
    expect(store.board?.groups).toEqual([]);
  });

  it("尺寸调整不会触发重叠成组：两张重叠的卡只改尺寸，不建组", async () => {
    store.board = board([mkCard("c1", 100, 100), mkCard("c2", 150, 140)], { selection: ["c1"] });
    const view = mountCanvas();
    view.get('[data-resize-card-id="c1"] [data-resize-handle="se"]').element.dispatchEvent(
      pointer("pointerdown", VIEW_LEFT + 220, VIEW_TOP + 180),
    );
    document.dispatchEvent(pointer("pointermove", VIEW_LEFT + 320, VIEW_TOP + 300));
    document.dispatchEvent(pointer("pointerup", VIEW_LEFT + 320, VIEW_TOP + 300));
    await nextTick();
    expect(labels).toEqual(["调整卡片尺寸"]);
    expect(store.board?.groups).toEqual([]);
    expect(labels.some((label) => label.includes("成组") || label.includes("合并"))).toBe(false);
  });
});

describe("展开期间调整尺寸：只提交 w / h，临时槽位绝不写回板面", () => {
  it("原位展开的卡片调整尺寸：card.x / card.y 不变，展开状态保留", async () => {
    store.board = board([mkCard("c1", 100, 100)], { selection: ["c1"] });
    const view = mountCanvas();
    store.toggleCardPreview("c1");
    await nextTick();
    // 展开后槽位是 560×420（阅读尺寸）：右下角在屏幕 (700, 620)
    expect(rectOfCard(view, "c1")).toEqual({ x: 100, y: 100, w: 560, h: 420 });
    view.get('[data-resize-card-id="c1"] [data-resize-handle="se"]').element.dispatchEvent(
      pointer("pointerdown", VIEW_LEFT + 660, VIEW_TOP + 520),
    );
    document.dispatchEvent(pointer("pointermove", VIEW_LEFT + 700, VIEW_TOP + 560));
    document.dispatchEvent(pointer("pointerup", VIEW_LEFT + 700, VIEW_TOP + 560));
    await nextTick();
    expect(labels).toEqual(["调整卡片尺寸"]);
    // 尺寸落到 card.w/h（用户展开期间的调整生效），位置仍是正式位置
    expect(store.board?.cards[0]).toMatchObject({ x: 100, y: 100, w: 600, h: 460 });
    expect(store.expandedCardIds).toEqual(["c1"]);
    // 收起后停在用户调过的尺寸，不再被旧快照覆盖
    store.collapseAllPreviews();
    await nextTick();
    expect(rectOfCard(view, "c1")).toEqual({ x: 100, y: 100, w: 600, h: 460 });
  });
});

describe("展开让位：多卡互相避让 + 平移后的可视起点（生产路径接线）", () => {
  it("两张卡同时展开：画布上互不遮挡（expandedAvoid 在生产路径打开）", async () => {
    store.board = board([mkCard("a", 0, 0, 300, 200), mkCard("b", 200, 0, 300, 200)]);
    const view = mountCanvas();
    store.toggleCardPreview("a");
    store.toggleCardPreview("b");
    await nextTick();
    const a = rectOfCard(view, "a");
    const b = rectOfCard(view, "b");
    expect(a.w).toBe(560);
    expect(overlaps(a, b)).toBe(false);
    expect(b.x).toBe(560 + 16);
  });

  it("平移之后展开：展开卡被拉进可视区（stageOrigin 来自真实滚动量）", async () => {
    store.board = board([mkCard("c1", 100, 100), mkCard("c2", 100, 900)]);
    const view = mountCanvas();
    const element = view.get(".board-viewport").element as HTMLElement;
    makeScrollable(element, 800, 0);
    element.dispatchEvent(new Event("scroll"));
    await nextTick();
    store.toggleCardPreview("c1");
    await nextTick();
    // 可视起点 = 800：展开卡被夹进可视区（而不是留在板面 x=100）
    expect(rectOfCard(view, "c1").x).toBe(800);
    // 没有展开的卡片不受影响，仍在自己的正式位置
    expect(rectOfCard(view, "c2").x).toBe(100);
  });

  it("底部工具栏吃掉的高度必须扣掉（Y 轴）：展开尺寸向可用高度收敛", async () => {
    const toolbar = document.createElement("div");
    toolbar.setAttribute("data-im", "board-toolbar");
    toolbar.getBoundingClientRect = () =>
      ({ left: 0, top: VIEW_TOP + 300, right: 4000, bottom: VIEW_TOP + VIEW_H, width: 4000, height: VIEW_H - 300, x: 0, y: VIEW_TOP + 300, toJSON: () => ({}) }) as DOMRect;
    document.body.appendChild(toolbar);
    store.board = board([mkCard("c1", 100, 100)], { selection: ["c1"] });
    const view = mountCanvas();
    store.toggleCardPreview("c1");
    await nextTick();
    // 可用高度只剩 300：展开高度收敛到 300，并且贴着可用区顶边（不钻到工具栏底下）
    const slot = rectOfCard(view, "c1");
    expect(slot).toEqual({ x: 100, y: 0, w: 560, h: 300 });
    // 尺寸上限同样来自这个可用区域：拖到再大也不会越过工具栏
    view.get('[data-resize-card-id="c1"] [data-resize-handle="se"]').element.dispatchEvent(
      pointer("pointerdown", VIEW_LEFT + 660, VIEW_TOP + 400),
    );
    document.dispatchEvent(pointer("pointermove", VIEW_LEFT + 1500, VIEW_TOP + 1500));
    document.dispatchEvent(pointer("pointerup", VIEW_LEFT + 1500, VIEW_TOP + 1500));
    await nextTick();
    expect(store.board?.cards[0].h).toBeLessThanOrEqual(300);
    toolbar.remove();
  });

  it("对话窗口（chatObstacleFrame 发布的真实显示框）也会被扣掉", async () => {
    // 对话窗口占住左下（屏幕 140..940 × 200..1700）→ 可用区退到它右边（板面 x ≥ 900）
    publishChatFrame({ x: VIEW_LEFT + 100, y: VIEW_TOP + 100, w: 800, h: 1500 }, false);
    store.board = board([mkCard("c1", 200, 300)]);
    const view = mountCanvas();
    store.toggleCardPreview("c1");
    await nextTick();
    const slot = rectOfCard(view, "c1");
    // 展开卡被拉进可用区，绝不压在对话窗口上
    expect(slot.x).toBe(900);
    expect(overlaps(slot, { x: 100, y: 100, w: 800, h: 1500 })).toBe(false);
    resetChatFrameState();
  });
});

describe("组框与关系线继续走同一份显示坐标", () => {
  it("展开 / 让位之后组框仍准确包围成员（真实分组不被改写）", async () => {
    store.board = board(
      [mkCard("a", 100, 100), mkCard("b", 300, 100)],
      { groups: [{ id: "g1", name: "发布计划", defaultName: false, ordered: false, x: 84, y: 42, w: 352, h: 154, members: ["a", "b"], deleted: false, createdAt: STAMP, updatedAt: STAMP }] },
    );
    const view = mountCanvas();
    store.toggleCardPreview("a");
    await nextTick();
    const frame = styleOf(view.get('[data-im="group"]').element);
    const a = rectOfCard(view, "a");
    const b = rectOfCard(view, "b");
    expect(px(frame.left)).toBeLessThanOrEqual(Math.min(a.x, b.x));
    expect(px(frame.top)).toBeLessThanOrEqual(Math.min(a.y, b.y));
    expect(px(frame.left) + px(frame.width)).toBeGreaterThanOrEqual(Math.max(a.x + a.w, b.x + b.w));
    expect(px(frame.top) + px(frame.height)).toBeGreaterThanOrEqual(Math.max(a.y + a.h, b.y + b.h));
    // 显示让位不改真实分组：成员顺序与组名一个字没变
    expect(store.board?.groups[0].members).toEqual(["a", "b"]);
    expect(store.board?.groups[0].name).toBe("发布计划");
  });

  it("展开让位之后关系线端点仍落在对应卡片上", async () => {
    store.board = board(
      [mkCard("a", 100, 100), mkCard("b", 400, 100)],
      { links: [{ id: "l1", src: "a", dst: "b", direction: true, meaning: "用户写的关系", deleted: false, createdAt: STAMP, updatedAt: STAMP }] },
    );
    const view = mountCanvas();
    store.toggleCardPreview("a");
    await nextTick();
    const a = rectOfCard(view, "a");
    const b = rectOfCard(view, "b");
    const line = view.get("line.hit");
    expect(Number(line.attributes("x1"))).toBe(a.x + a.w / 2);
    expect(Number(line.attributes("y1"))).toBe(a.y + a.h / 2);
    expect(Number(line.attributes("x2"))).toBe(b.x + b.w / 2);
    expect(Number(line.attributes("y2"))).toBe(b.y + b.h / 2);
  });
});

describe("源码守卫：新行为在生产路径上真的被启用", () => {
  it("BoardCanvas 给 planExpandedLayout 传了 expandedAvoid 与真实 stageOrigin", () => {
    const text = source("components/interactive/BoardCanvas.vue");
    expect(text).toContain("expandedAvoid: true");
    expect(text).toContain("stageOrigin: boardRegion.value.origin");
    expect(text).toContain("chatObstacleFrame()");
    expect(text).toContain("board-toolbar");
    expect(text).toContain("BoardResizeLayer");
  });

  it("尺寸提交只走 updateCard + 「调整卡片尺寸」，不碰 previewDrop / dropCard / bestMergeTarget", () => {
    const text = source("components/interactive/BoardCanvas.vue");
    const section = text.slice(text.indexOf("function commitCardSize"), text.indexOf("function commitCardSize") + 2000);
    expect(section).toContain('"调整卡片尺寸"');
    expect(section).toContain("updateCard(");
    expect(section).not.toContain("previewDrop");
    expect(section).not.toContain("dropCard(");
    expect(section).not.toContain("bestMergeTarget");
  });
});
