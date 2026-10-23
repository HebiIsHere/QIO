/**
 * Phase 2 集成（task-14）：BoardCanvas 接线测试。
 *
 * 证明以下能力**真的接进生产路径**，不是只写了纯函数：
 * 1. 图片卡的所有尺寸写回路径（拖动落点 / 键盘 / 指针 resize / 比例恢复）都满足原图比例，
 *    非图片卡一个字段都不改；
 * 2. 组收起：成员卡片不再单独渲染，组框 / 连线层收到 collapsed / foldedGroupIds；
 * 3. 组标题拖动整组：屏幕位移换算成板面位移，成员一次提交，拖动中不改板面；
 * 4. 组框缩放：走 A 的 planGroupResize，成员按比例适配（图片成员保持原图比例），取消不落库；
 * 5. 组拖动避让：走 A 的 avoidOverlaps，组框与其它组框留出 gap；
 * 6. 拖线落点支持组框（group:<id>），当前板面模型拒绝组端点时如实说明、不伪造链接；
 * 7. 参与范围 includedCardIds 传给连线层；
 * 8. 拖动中的显示矩形来自 A 的 display frames（连线层同步读到临时位置）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import BoardCanvas from "../../components/interactive/BoardCanvas.vue";
import BoardCard from "../../components/interactive/BoardCard.vue";
import BoardGroupFrame from "../../components/interactive/BoardGroupFrame.vue";
import BoardLinkLayer from "../../components/interactive/BoardLinkLayer.vue";
import { useInteractiveStore } from "../../stores/interactive";
import * as boardLib from "../board";
import type { BoardCard as BoardCardModel, BoardGroup, BoardState } from "../types";

const VIEW_LEFT = 40;
const VIEW_TOP = 100;
const VIEW_W = 3000;
const VIEW_H = 2000;
const STAMP = "2026-10-06T00:00:00.000Z";

function pointer(type: string, x: number, y: number): MouseEvent {
  return new MouseEvent(type, { clientX: x, clientY: y, bubbles: true, cancelable: true });
}

/** 屏幕坐标 = 容器原点 + 板面坐标 × scale（jsdom 缩放为 1、无滚动）。 */
function clientOf(boardX: number, boardY: number): { x: number; y: number } {
  return { x: VIEW_LEFT + boardX, y: VIEW_TOP + boardY };
}

function mkCard(id: string, x: number, y: number, patch: Partial<BoardCardModel> = {}): BoardCardModel {
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
    createdAt: STAMP,
    updatedAt: STAMP,
    ...(patch.participation ? { participation: patch.participation } : {}),
  };
}

function mkGroup(id: string, members: string[], patch: Partial<BoardGroup> = {}): BoardGroup {
  return {
    id,
    name: patch.name ?? "组" + id,
    defaultName: true,
    ordered: false,
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

function boardState(
  cards: BoardCardModel[],
  options: { groups?: BoardGroup[]; selection?: string[] } = {},
): BoardState {
  return {
    ...boardLib.emptyState("board_t"),
    cards,
    groups: options.groups ?? [],
    selection: options.selection ?? [],
  };
}

/** 16:9 的真实解码结果（meta.display 是 Phase 2-C 冻结字段）。 */
function imageMeta(width = 1600, height = 900): Record<string, unknown> {
  return { display: { width, height, aspectRatio: width / height } };
}

function ratioOf(card: BoardCardModel): number {
  return card.w / card.h;
}

function stubGeometry(wrapper: VueWrapper): void {
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  const shell = wrapper.get(".board-shell").element as HTMLElement;
  const rect = (left: number, top: number, width: number, height: number) =>
    ({ left, top, right: left + width, bottom: top + height, width, height, x: left, y: top, toJSON: () => ({}) }) as DOMRect;
  view.getBoundingClientRect = () => rect(VIEW_LEFT, VIEW_TOP, VIEW_W, VIEW_H);
  shell.getBoundingClientRect = () => rect(VIEW_LEFT, VIEW_TOP, VIEW_W, VIEW_H);
  Object.defineProperty(view, "clientWidth", { value: VIEW_W, configurable: true });
  Object.defineProperty(view, "clientHeight", { value: VIEW_H, configurable: true });
}

let wrapper: VueWrapper | null = null;
let store: ReturnType<typeof useInteractiveStore>;
let labels: string[];

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  store = useInteractiveStore();
  store.board = boardLib.emptyState("board_t");
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

function setBoard(next: BoardState): void {
  store.board = boardLib.normalizeState(next);
}

function mountCanvas(): VueWrapper {
  wrapper = mount(BoardCanvas, { attachTo: document.body });
  stubGeometry(wrapper);
  return wrapper;
}

function cardElement(view: VueWrapper, cardId: string) {
  return view.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === cardId);
}

function dragCard(view: VueWrapper, cardId: string, from: { x: number; y: number }, delta: { x: number; y: number }): void {
  const start = clientOf(from.x, from.y);
  const card = cardElement(view, cardId)!;
  card.element.dispatchEvent(pointer("pointerdown", start.x, start.y));
  window.dispatchEvent(pointer("pointermove", start.x + delta.x, start.y + delta.y));
  window.dispatchEvent(pointer("pointerup", start.x + delta.x, start.y + delta.y));
}

describe("图片卡尺寸：所有写回路径都满足原图比例", () => {
  it("拖动落点：图片卡按原图比例收敛，非图片卡宽高一个字段都不改", async () => {
    setBoard(
      boardState([
        mkCard("img", 0, 0, { kind: "file", w: 400, h: 200, meta: imageMeta() }),
        mkCard("txt", 900, 0, { w: 120, h: 70 }),
      ]),
    );
    const view = mountCanvas();
    dragCard(view, "img", { x: 200, y: 100 }, { x: 100, y: 50 });
    dragCard(view, "txt", { x: 960, y: 35 }, { x: 40, y: 20 });
    await nextTick();

    const img = store.board!.cards.find((card) => card.id === "img")!;
    const txt = store.board!.cards.find((card) => card.id === "txt")!;
    // 拖动本身不改尺寸；图片卡一旦有真实比例，尺寸就必须满足它（16:9，2% 容差）
    expect(Math.abs(ratioOf(img) - 16 / 9)).toBeLessThan(16 / 9 * 0.02);
    expect(img.w).toBeGreaterThanOrEqual(180);
    expect(img.h).toBeGreaterThanOrEqual(96);
    // 非图片卡：拖动只改位置，宽高不动（旧口径原样保留）
    expect({ w: txt.w, h: txt.h }).toEqual({ w: 120, h: 70 });
    expect(labels).toContain("移动卡片");
  });

  it("键盘 ＋ 一步：图片卡按比例同时变宽变高，非图片卡仍只变一维", async () => {
    setBoard(
      boardState(
        [
          mkCard("img", 0, 0, { kind: "file", w: 400, h: 225, meta: imageMeta() }),
          mkCard("txt", 600, 0, { w: 120, h: 70 }),
        ],
        { selection: ["img"] },
      ),
    );
    const view = mountCanvas();
    await view.get('[data-resize-card-id="img"] [data-resize-key="w+"]').trigger("click");
    const img = store.board!.cards.find((card) => card.id === "img")!;
    expect(img.w).toBeGreaterThan(400);
    expect(Math.abs(ratioOf(img) - 16 / 9)).toBeLessThan(16 / 9 * 0.02);

    // 换成非图片卡：＋ 只改宽度
    setBoard(boardState([mkCard("txt2", 600, 0, { w: 120, h: 70 })], { selection: ["txt2"] }));
    await nextTick();
    await view.get('[data-resize-card-id="txt2"] [data-resize-key="w+"]').trigger("click");
    const txt = store.board!.cards.find((card) => card.id === "txt2")!;
    expect(txt.w).toBe(144);
    expect(txt.h).toBe(70);
  });

  it("指针 resize：图片卡拖动右下角后仍保持原图比例", async () => {
    setBoard(
      boardState([mkCard("img", 0, 0, { kind: "file", w: 320, h: 180, meta: imageMeta() })], {
        selection: ["img"],
      }),
    );
    const view = mountCanvas();
    const start = clientOf(320, 180);
    view.get('[data-resize-card-id="img"] [data-resize-handle="se"]').element.dispatchEvent(
      pointer("pointerdown", start.x, start.y),
    );
    document.dispatchEvent(pointer("pointermove", start.x + 200, start.y + 120));
    document.dispatchEvent(pointer("pointerup", start.x + 200, start.y + 120));
    await nextTick();

    const img = store.board!.cards.find((card) => card.id === "img")!;
    expect(labels).toContain("调整卡片尺寸");
    expect(Math.abs(ratioOf(img) - 16 / 9)).toBeLessThan(16 / 9 * 0.02);
  });

  it("比例恢复：BoardCard 写入 meta.display 的那次提交同时把尺寸收敛到真实比例", async () => {
    setBoard(boardState([mkCard("img", 0, 0, { kind: "file", w: 400, h: 200 })]));
    const view = mountCanvas();
    const card = view.findAllComponents(BoardCard)[0];
    card.vm.$emit("patch", "img", { meta: imageMeta() }, "记录图片显示比例");
    await nextTick();

    const img = store.board!.cards.find((item) => item.id === "img")!;
    expect(labels).toEqual(["记录图片显示比例"]);
    expect(img.meta.display).toBeTruthy();
    expect(Math.abs(ratioOf(img) - 16 / 9)).toBeLessThan(16 / 9 * 0.02);
    expect(img.w).toBe(400);
    expect(img.h).toBe(225);
  });
});

describe("组收起：成员卡片隐藏、组框概览与连线层同步", () => {
  it("收起后成员卡不再渲染，GroupFrame / LinkLayer 收到 collapsed 与 foldedGroupIds；展开后恢复", async () => {
    setBoard(
      boardState(
        [
          mkCard("a", 0, 0, { w: 120, h: 80 }),
          mkCard("b", 200, 0, { w: 120, h: 80 }),
        ],
        { groups: [mkGroup("g1", ["a", "b"])] },
      ),
    );
    const view = mountCanvas();
    expect(view.findAll('[data-im="card"]')).toHaveLength(2);

    await view.get('[data-im="group-collapse-toggle"]').trigger("click");
    await nextTick();
    // 收起的组只画组框概览：成员卡片一个都不在画布上
    expect(view.findAll('[data-im="card"]')).toHaveLength(0);
    expect(view.findComponent(BoardGroupFrame).props("collapsed")).toBe(true);
    expect(view.findComponent(BoardLinkLayer).props("foldedGroupIds")).toEqual(["g1"]);
    // 收起是纯显示状态：不提交、不改成员
    expect(labels).toEqual([]);
    expect(store.board!.groups[0].members).toEqual(["a", "b"]);

    await view.get('[data-im="group-collapse-toggle"]').trigger("click");
    await nextTick();
    expect(view.findAll('[data-im="card"]')).toHaveLength(2);
    expect(view.findComponent(BoardGroupFrame).props("collapsed")).toBe(false);
  });
});

describe("组标题拖动整组：屏幕位移换算板面位移，一次提交", () => {
  it("拖动组标题：组框与成员一起移动，拖动中不改板面，松手只提交一次", async () => {
    setBoard(
      boardState(
        [
          mkCard("a", 0, 0, { w: 120, h: 80 }),
          mkCard("b", 200, 0, { w: 120, h: 80 }),
        ],
        { groups: [mkGroup("g1", ["a", "b"])] },
      ),
    );
    const view = mountCanvas();
    const before = store.board!.cards.map((card) => ({ id: card.id, x: card.x, y: card.y }));

    const grip = view.get('[data-im="group-drag-handle"]');
    grip.element.dispatchEvent(pointer("pointerdown", VIEW_LEFT + 100, VIEW_TOP + 100));
    await nextTick();
    document.dispatchEvent(pointer("pointermove", VIEW_LEFT + 140, VIEW_TOP + 130));
    await nextTick();

    // 拖动中：板面一个字段都不改（只预演显示坐标）
    expect(store.board!.cards.map((card) => ({ id: card.id, x: card.x, y: card.y }))).toEqual(before);
    const preview = view.findComponent(BoardLinkLayer).props("cards") as BoardCardModel[];
    expect(preview.find((card) => card.id === "a")!.x).toBe(before[0].x + 40);
    expect(preview.find((card) => card.id === "a")!.y).toBe(before[0].y + 30);

    document.dispatchEvent(pointer("pointerup", VIEW_LEFT + 140, VIEW_TOP + 130));
    await nextTick();
    expect(labels.filter((label) => label.includes("移动组"))).toHaveLength(1);
    const moved = store.board!.cards;
    expect(moved.find((card) => card.id === "a")).toMatchObject({ x: 40, y: 30 });
    expect(moved.find((card) => card.id === "b")).toMatchObject({ x: 240, y: 30 });
    // 组框由 board.ts 从成员派生：仍框得住成员
    const group = store.board!.groups[0];
    expect(group.x).toBeLessThanOrEqual(40 - 16);
    expect(group.y).toBeLessThanOrEqual(30 - 58);
  });

  it("组拖动遇到另一个组：避让后两个组框不重叠（不会移动别人的成员）", async () => {
    setBoard(
      boardState(
        [
          mkCard("a", 0, 0, { w: 120, h: 80 }),
          mkCard("b", 400, 0, { w: 120, h: 80 }),
        ],
        {
          groups: [mkGroup("g1", ["a"]), mkGroup("g2", ["b"])],
        },
      ),
    );
    const view = mountCanvas();
    const otherBefore = { ...store.board!.cards.find((card) => card.id === "b")! };
    const grip = view.get('[data-im="group-drag-handle"]');
    grip.element.dispatchEvent(pointer("pointerdown", VIEW_LEFT + 10, VIEW_TOP + 10));
    // 往右拖到与 g2 重叠的位置
    document.dispatchEvent(pointer("pointermove", VIEW_LEFT + 410, VIEW_TOP + 10));
    document.dispatchEvent(pointer("pointerup", VIEW_LEFT + 410, VIEW_TOP + 10));
    await nextTick();

    expect(labels.filter((label) => label.includes("移动组"))).toHaveLength(1);
    const g1 = store.board!.groups.find((group) => group.id === "g1")!;
    const g2 = store.board!.groups.find((group) => group.id === "g2")!;
    const overlapsWithGap =
      g1.x < g2.x + g2.w + 15.9 && g2.x < g1.x + g1.w + 15.9 && g1.y < g2.y + g2.h + 15.9 && g2.y < g1.y + g1.h + 15.9;
    expect(overlapsWithGap).toBe(false);
    // 另一个组的成员分毫不动
    const otherAfter = store.board!.cards.find((card) => card.id === "b")!;
    expect({ x: otherAfter.x, y: otherAfter.y }).toEqual({ x: otherBefore.x, y: otherBefore.y });
  });
});

describe("组框缩放：成员按比例适配，取消不落库", () => {
  function groupWithImageMember(): void {
    setBoard(
      boardState(
        [
          mkCard("txt", 0, 0, { w: 200, h: 150 }),
          mkCard("img", 220, 0, { kind: "file", w: 400, h: 225, meta: imageMeta() }),
        ],
        { groups: [mkGroup("g1", ["txt", "img"])] },
      ),
    );
  }

  it("拖动右下把手放大：成员尺寸变化、图片成员保持原图比例、整组只提交一次", async () => {
    groupWithImageMember();
    const view = mountCanvas();
    const handle = view.get('[data-im="group-resize-handle"][data-edge="se"]');
    const start = clientOf(500, 250);
    handle.element.dispatchEvent(pointer("pointerdown", start.x, start.y));
    await nextTick();
    document.dispatchEvent(pointer("pointermove", start.x + 300, start.y + 100));
    await nextTick();
    document.dispatchEvent(pointer("pointerup", start.x + 300, start.y + 100));
    await nextTick();

    expect(labels.filter((label) => label.includes("调整组框尺寸"))).toHaveLength(1);
    const img = store.board!.cards.find((card) => card.id === "img")!;
    const txt = store.board!.cards.find((card) => card.id === "txt")!;
    expect(img.w).toBeGreaterThan(400);
    expect(Math.abs(ratioOf(img) - 16 / 9)).toBeLessThan(16 / 9 * 0.02);
    expect(txt.w).toBeGreaterThan(200);
    // 成员不重叠
    const overlap = txt.x < img.x + img.w && img.x < txt.x + txt.w && txt.y < img.y + img.h && img.y < txt.y + txt.h;
    expect(overlap).toBe(false);
  });

  it("拖动中按 Esc 取消：板面没有改动、没有任何提交", async () => {
    groupWithImageMember();
    const view = mountCanvas();
    const before = JSON.stringify(store.board);
    const handle = view.get('[data-im="group-resize-handle"][data-edge="se"]');
    const start = clientOf(500, 250);
    handle.element.dispatchEvent(pointer("pointerdown", start.x, start.y));
    document.dispatchEvent(pointer("pointermove", start.x + 260, start.y + 120));
    await nextTick();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await nextTick();
    document.dispatchEvent(pointer("pointerup", start.x + 260, start.y + 120));
    await nextTick();

    expect(labels).toEqual([]);
    expect(JSON.stringify(store.board)).toBe(before);
  });
});

describe("拖线落点支持组框（group:<id>）与参与范围", () => {
  it("拖到组框空白处：待建连线目标是 group:<id>，松手后真实建立组端点关系", async () => {
    setBoard(
      boardState(
        [
          mkCard("a", 0, 0, { w: 100, h: 60 }),
          mkCard("b", 400, 100, { w: 120, h: 80 }),
        ],
        { groups: [mkGroup("g1", ["b"])], selection: ["a"] },
      ),
    );
    const view = mountCanvas();
    const group = store.board!.groups[0];
    // 组框头部空白处（不在成员卡片上）
    const target = clientOf(group.x + 8, group.y + 8);
    cardElement(view, "a")!.element.dispatchEvent(pointer("pointerdown", VIEW_LEFT + 50, VIEW_TOP + 30));
    await nextTick();
    view.findAll('[data-im="connect-point"]')[0].element.dispatchEvent(
      pointer("pointerdown", VIEW_LEFT + 100, VIEW_TOP + 30),
    );
    window.dispatchEvent(pointer("pointermove", target.x, target.y));
    await nextTick();
    const draft = view.get('[data-im="link-draft"]');
    expect(draft.attributes("data-target")).toBe("group:" + group.id);
    expect(draft.attributes("data-target-kind")).toBe("group");

    window.dispatchEvent(pointer("pointerup", target.x, target.y));
    await nextTick();
    expect(store.board!.links).toHaveLength(1);
    expect(store.board!.links[0]).toMatchObject({ src: "a", dst: "group:" + group.id });
    expect(labels.filter((label) => label.includes("新建关系"))).toHaveLength(1);
  });

  it("参与的卡片 id 传给连线层（includedCardIds）", async () => {
    setBoard(
      boardState([
        mkCard("in", 0, 0, { kind: "text", participation: "included" }),
        mkCard("out", 300, 0, { kind: "reply", participation: "excluded" }),
      ]),
    );
    const view = mountCanvas();
    const linkLayer = view.findComponent(BoardLinkLayer);
    expect(linkLayer.props("includedCardIds")).toEqual(["in"]);
  });
});

describe("拖动中的显示矩形来自统一 display frames", () => {
  it("拖动一张卡：连线层读到的卡片位置与画布同一份临时坐标；松手才写板面", async () => {
    // 预置选中：按下时不再产生一次「选择卡片」提交，断言只关注拖动本身
    setBoard(
      boardState([mkCard("a", 0, 0, { w: 100, h: 60 }), mkCard("b", 500, 0, { w: 100, h: 60 })], {
        selection: ["a"],
      }),
    );
    const view = mountCanvas();
    const start = clientOf(50, 30);
    cardElement(view, "a")!.element.dispatchEvent(pointer("pointerdown", start.x, start.y));
    window.dispatchEvent(pointer("pointermove", start.x + 120, start.y + 40));
    await nextTick();

    const cards = view.findComponent(BoardLinkLayer).props("cards") as BoardCardModel[];
    expect(cards.find((card) => card.id === "a")!.x).toBe(120);
    expect(cards.find((card) => card.id === "a")!.y).toBe(40);
    // 板面状态仍是旧值（只预演）：这次拖动本身还没有提交（选中是按下时的既有行为）
    expect(store.board!.cards.find((card) => card.id === "a")!.x).toBe(0);
    expect(labels.filter((label) => label.includes("移动卡片"))).toEqual([]);

    window.dispatchEvent(pointer("pointerup", start.x + 120, start.y + 40));
    await nextTick();
    expect(store.board!.cards.find((card) => card.id === "a")!.x).toBe(120);
    expect(labels.filter((label) => label.includes("移动卡片"))).toHaveLength(1);
  });
});
