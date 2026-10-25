/**
 * P4 接线用例：卡片内展开预览 + 临时布局（契约 2026-10-12 §3.5 / §3.6）。
 *
 * 用**真实 store**（只把 services/interactive 换成替身，用来证明零请求），覆盖四层接线：
 * 1.【状态】`expandedCards` / `toggleCardPreview` / `collapseAllPreviews`：
 *    只改前端内存的查看状态，不写板面状态、不产生表达、不排保存、不发请求；
 * 2.【卡片】BoardCard：这张卡自己出现「展开预览 / 收起预览」按钮与**卡片内**的预览挂载点；
 * 3.【画布】BoardCanvas：展开**不改变任何卡片外框**（F1：正式几何是唯一权威）；旧行为
 *    「展开卡原位放大 + 推开邻卡」的断言已删除，改写为「外框逐字段不变」；
 *    缩放（board-surface 的 transform: scale）仍然只影响查看方式；
 * 4.【预览层】BoardPreviewLayer：展开容器随 F1 删除，只剩既有意图虚线预览（语义不变）。
 *
 * 注意：jsdom 没有真实布局，画布用例给容器打**桩几何**（与 boardCanvas.test.ts 同一套做法）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createPinia, setActivePinia } from "pinia";
import { mount, type VueWrapper } from "@vue/test-utils";
import BoardCanvas from "../BoardCanvas.vue";
import BoardCard from "../BoardCard.vue";
import BoardPreviewLayer from "../BoardPreviewLayer.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as imApi from "../../../services/interactive";
import { emptyState } from "../../../interactive/board";
import type { BoardCard as BoardCardModel, BoardState, Intent } from "../../../interactive/types";

vi.mock("../../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  fetchVisibleRange: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  checkMaterialImpact: vi.fn(),
  previewMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
  approveIntent: vi.fn(),
  rejectIntent: vi.fn(),
  batchDecide: vi.fn(),
  advanceIntent: vi.fn(),
  updateIntentPreview: vi.fn(),
}));

const STAMP = "2026-10-12T00:00:00.000Z";
const VIEW_LEFT = 40;
const VIEW_TOP = 100;
const VIEW_W = 3000;
const VIEW_H = 2000;

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

/** A 在 (100,100)：展开后覆盖 100..660 × 100..520；B 在 (600,200) 与它相交（向右最短） */
function seedBoard(): BoardState {
  return { ...emptyState("board_default"), seq: 7, cards: [mkCard("exp_a", 100, 100), mkCard("exp_b", 600, 200)] };
}

function intentFixture(): Intent {
  const previewCard = mkCard("pv_a", 120, 120, 100, 60);
  return {
    id: "intent_1",
    boardId: "board_default",
    submissionId: null,
    title: "待审批结果",
    summary: "",
    status: "pending",
    preview: { cards: [previewCard], groups: [], links: [], note: "演示预览" },
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 0, text: "" },
    reason: "",
    demo: true,
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

/** jsdom 没有真实布局：给容器与外壳打桩几何，让「板面可视范围」是确定的 */
function stubGeometry(wrapper: VueWrapper): void {
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  const shell = wrapper.get(".board-shell").element as HTMLElement;
  view.getBoundingClientRect = () =>
    ({ left: VIEW_LEFT, top: VIEW_TOP, right: VIEW_LEFT + VIEW_W, bottom: VIEW_TOP + VIEW_H, width: VIEW_W, height: VIEW_H, x: VIEW_LEFT, y: VIEW_TOP, toJSON: () => ({}) }) as DOMRect;
  shell.getBoundingClientRect = () =>
    ({ left: VIEW_LEFT, top: VIEW_TOP, right: VIEW_LEFT + VIEW_W, bottom: VIEW_TOP + VIEW_H + 200, width: VIEW_W, height: VIEW_H + 200, x: VIEW_LEFT, y: VIEW_TOP, toJSON: () => ({}) }) as DOMRect;
  Object.defineProperty(view, "clientWidth", { value: VIEW_W, configurable: true });
  Object.defineProperty(view, "clientHeight", { value: VIEW_H, configurable: true });
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

function cardStyle(wrapper: VueWrapper, cardId: string): Record<string, string> {
  const found = wrapper.findAll('[data-im="card"]').find((item) => item.attributes("data-card-id") === cardId);
  return styleOf(found?.element);
}

function wheel(x: number, y: number, deltaY: number, target: Element): void {
  target.dispatchEvent(new WheelEvent("wheel", { clientX: x, clientY: y, deltaY, deltaMode: 0, bubbles: true, cancelable: true }));
}

function snapshot(state: BoardState | null): string {
  return JSON.stringify(state);
}

const SRC = resolve(process.cwd(), "src");
function source(relativePath: string): string {
  return readFileSync(resolve(SRC, relativePath), "utf8");
}

describe("store：展开状态只在前端内存", () => {
  let store: ReturnType<typeof useInteractiveStore>;

  beforeEach(() => {
    localStorage.clear();
    vi.resetAllMocks();
    setActivePinia(createPinia());
    store = useInteractiveStore();
    store.board = seedBoard();
  });

  it("toggleCardPreview 是唯一的展开开关：展开 → 收起 → 再展开", () => {
    expect(store.previewExpandedCount).toBe(0);
    expect(store.expandedCardIds).toEqual([]);

    expect(store.toggleCardPreview("exp_a")).toBe(true);
    expect(store.isCardPreviewExpanded("exp_a")).toBe(true);
    expect(store.expandedCardIds).toEqual(["exp_a"]);
    expect(store.previewExpandedCount).toBe(1);

    expect(store.toggleCardPreview("exp_a")).toBe(false);
    expect(store.isCardPreviewExpanded("exp_a")).toBe(false);
    expect(store.expandedCardIds).toEqual([]);

    expect(store.toggleCardPreview("exp_b")).toBe(true);
    expect(store.expandedCardIds).toEqual(["exp_b"]);
  });

  it("展开 / 收起伏笔不写板面状态、不产生表达、不排保存、不发任何请求", () => {
    const before = snapshot(store.board);
    const dirtyBefore = store.dirty;
    const saveStatusBefore = store.saveStatus;

    store.toggleCardPreview("exp_a");
    store.toggleCardPreview("exp_b");
    store.collapseAllPreviews();
    store.toggleCardPreview("exp_a");
    store.toggleCardPreview("exp_a");

    // 板面状态逐字节不变：cards 的 x/y/w/h、seq、selection、updatedAt 全部没动
    expect(snapshot(store.board)).toBe(before);
    expect(store.board?.seq).toBe(7);
    for (const card of store.board?.cards ?? []) {
      expect(card.w).toBe(120);
      expect(card.h).toBe(80);
    }
    // 没有新表达、没有新提交、没有保存动作、没有请求
    expect(store.dirty).toBe(dirtyBefore);
    expect(store.saveStatus).toBe(saveStatusBefore);
    expect(store.submissions).toEqual([]);
    expect(store.lastSubmission).toBeNull();
    expect(store.intents).toEqual([]);
    for (const fn of Object.values(imApi)) {
      expect(vi.mocked(fn as () => unknown)).not.toHaveBeenCalled();
    }
  });

  it("collapseAllPreviews 收起全部并返回数量；没有展开时返回 0", () => {
    expect(store.collapseAllPreviews()).toBe(0);
    store.toggleCardPreview("exp_a");
    store.toggleCardPreview("exp_b");
    expect(store.collapseAllPreviews()).toBe(2);
    expect(store.expandedCardIds).toEqual([]);
    expect(store.collapseAllPreviews()).toBe(0);
  });

  it("空 id 不产生展开项（不会留下看不见的展开状态）", () => {
    expect(store.toggleCardPreview("")).toBe(false);
    expect(store.previewExpandedCount).toBe(0);
  });

  it("图片卡不能进入阅读态：返回 false，也不出现在任何展开出口", () => {
    store.board = {
      ...seedBoard(),
      cards: [
        {
          ...mkCard("img_1", 100, 100),
          kind: "file",
          meta: { name: "照片.png", file: { name: "照片.png", attachmentId: "att_img", size: 2048 } },
        },
      ],
    };
    expect(store.toggleCardPreview("img_1")).toBe(false);
    expect(store.isCardPreviewExpanded("img_1")).toBe(false);
    expect(store.expandedCardIds).toEqual([]);
    expect(store.previewExpandedCount).toBe(0);
    // 图片不算数：收起全部预览返回 0（没有可收起的阅读）
    expect(store.collapseAllPreviews()).toBe(0);
  });
});

describe("BoardCard：卡片自己的「展开预览 / 收起预览」", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  let wrapper: VueWrapper | null = null;

  beforeEach(() => {
    localStorage.clear();
    vi.resetAllMocks();
    setActivePinia(createPinia());
    store = useInteractiveStore();
    store.board = seedBoard();
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
    document.body.innerHTML = "";
  });

  function mountCard(card: BoardCardModel): VueWrapper {
    wrapper = mount(BoardCard, {
      props: {
        card,
        selected: false,
        highlight: false,
        dragging: false,
        x: card.x,
        y: card.y,
        groupName: null,
        groups: [],
        toolbarLeft: 0,
        toolbarTop: 0,
        multi: false,
        connecting: false,
      },
      attachTo: document.body,
    });
    return wrapper;
  }

  it("小卡片默认简洁，旧「展开预览」按钮不再出现", async () => {
    const card = store.board!.cards[0];
    const view = mountCard(card);
    expect(view.get('[data-im="card"]').attributes("data-card-state")).toBe("normal");
    expect(view.find('[data-im="card-preview-toggle"]').exists()).toBe(false);
    expect(view.find('[data-im="card-preview-host"]').exists()).toBe(false);
    await view.vm.$nextTick();
  });

  it("尺寸达到阈值时自动出现卡片内阅读主体，不写板面状态", async () => {
    const card = mkCard("auto_detail", 100, 100, 420, 280);
    store.board = { ...store.board!, cards: [card] };
    const view = mountCard(card);
    const before = snapshot(store.board);
    await view.vm.$nextTick();
    expect(view.get('[data-im="card"]').attributes("data-card-state")).toBe("reading");
    const host = view.get('[data-im="card-preview-host"]');
    expect(host.attributes("data-card-id")).toBe(card.id);
    expect(host.element.closest('[data-im="card"]')).not.toBeNull();
    expect(snapshot(store.board)).toBe(before);
    expect(store.lastSubmission).toBeNull();
  });

  it("折叠的卡片不渲染阅读主体", async () => {
    const card = { ...store.board!.cards[0], folded: true };
    const view = mountCard(card);
    expect(view.find('[data-im="card-preview-toggle"]').exists()).toBe(false);
    expect(view.find('[data-im="card-preview-host"]').exists()).toBe(false);
  });
});

describe("BoardCanvas：渲染坐标改由临时槽位决定", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  let wrapper: VueWrapper | null = null;

  beforeEach(() => {
    localStorage.clear();
    vi.resetAllMocks();
    setActivePinia(createPinia());
    store = useInteractiveStore();
    store.board = seedBoard();
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
    document.body.innerHTML = "";
  });

  function mountCanvas(): VueWrapper {
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    stubGeometry(wrapper);
    return wrapper;
  }

  it("没有展开时：卡片位置逐字段等于正式位置（改动前的行为保持不变）", () => {
    const view = mountCanvas();
    expect(cardStyle(view, "exp_a")).toMatchObject({ left: "100px", top: "100px", width: "120px", height: "80px" });
    expect(cardStyle(view, "exp_b")).toMatchObject({ left: "600px", top: "200px", width: "120px", height: "80px" });
  });

  it("展开：任何卡片的外框逐字段不变（不再原位放大，也不推开邻卡）", async () => {
    const view = mountCanvas();
    store.toggleCardPreview("exp_a");
    await view.vm.$nextTick();

    // F1：正式矩形是唯一权威。展开只切卡内内容，外框一个像素都不动。
    expect(store.isCardPreviewExpanded("exp_a")).toBe(true);
    expect(cardStyle(view, "exp_a")).toMatchObject({ left: "100px", top: "100px", width: "120px", height: "80px" });
    // 与它相交的邻卡同样分毫不动（旧行为是向右让到 676px）
    expect(cardStyle(view, "exp_b")).toMatchObject({ left: "600px", top: "200px", width: "120px", height: "80px" });

    // 板面状态一个字没改
    const cards = store.board!.cards;
    expect(cards.map((card) => [card.id, card.x, card.y, card.w, card.h])).toEqual([
      ["exp_a", 100, 100, 120, 80],
      ["exp_b", 600, 200, 120, 80],
    ]);
    expect(store.board!.seq).toBe(7);
    expect(store.dirty).toBe(false);
    for (const fn of Object.values(imApi)) {
      expect(vi.mocked(fn as () => unknown)).not.toHaveBeenCalled();
    }
  });

  it("收起（含「收起全部预览」）前后外框完全一致：本来就没有临时位置可回", async () => {
    const view = mountCanvas();
    store.toggleCardPreview("exp_a");
    await view.vm.$nextTick();
    expect(cardStyle(view, "exp_b")).toMatchObject({ left: "600px", top: "200px", width: "120px", height: "80px" });

    store.collapseAllPreviews();
    await view.vm.$nextTick();
    expect(cardStyle(view, "exp_a")).toMatchObject({ left: "100px", top: "100px", width: "120px", height: "80px" });
    expect(cardStyle(view, "exp_b")).toMatchObject({ left: "600px", top: "200px", width: "120px", height: "80px" });
  });

  it("展开期间用户手动移动过这张卡：收起后停在用户的新位置（不是旧位置）", async () => {
    const view = mountCanvas();
    store.toggleCardPreview("exp_a");
    await view.vm.$nextTick();

    // 用户把展开中的卡片拖到新位置并放下（正式位置被用户改过；这里直接改正式状态模拟 commit 之后的事实）
    store.board = {
      ...store.board!,
      cards: store.board!.cards.map((card) => (card.id === "exp_a" ? { ...card, x: 400, y: 500 } : card)),
    };
    await view.vm.$nextTick();
    expect(cardStyle(view, "exp_a")).toMatchObject({ left: "400px", top: "500px" });

    store.collapseAllPreviews();
    await view.vm.$nextTick();
    expect(cardStyle(view, "exp_a")).toMatchObject({ left: "400px", top: "500px", width: "120px", height: "80px" });
  });

  it("板面缩放（transform: scale）下展开仍按板面坐标算：外框逐字段不变", async () => {
    const view = mountCanvas();
    store.toggleCardPreview("exp_a");
    await view.vm.$nextTick();
    const before = cardStyle(view, "exp_b");

    // 滚轮以指针附近为缩放中心（约 ×2）：缩放只是查看方式
    wheel(500, 400, -462, view.get(".board-viewport").element);
    await view.vm.$nextTick();
    const scale = Number(/scale\(([\d.]+)\)/.exec(styleOf(view.get(".board-surface").element).transform ?? "")?.[1] ?? "1");
    expect(scale).toBeGreaterThan(1.9);
    // 板面坐标（行内 left/top/width/height）不随缩放改变 —— 缩放由 .board-surface 的 transform 承担
    expect(cardStyle(view, "exp_b")).toMatchObject(before);
    // 展开也没有改变外框：始终是正式矩形
    expect(cardStyle(view, "exp_a")).toMatchObject({ left: "100px", top: "100px", width: "120px", height: "80px" });
  });

  it("画布不再产生第二套位置：预览层里没有任何展开容器（展开与否都不产生）", async () => {
    const view = mountCanvas();
    expect(view.findAll('[data-im="preview-expanded"]')).toHaveLength(0);
    store.toggleCardPreview("exp_a");
    await view.vm.$nextTick();
    expect(view.findAll('[data-im="preview-expanded"]')).toHaveLength(0);
  });
});

describe("BoardPreviewLayer：只剩意图虚线预览（展开容器随 F1 删除）", () => {
  let wrapper: VueWrapper | null = null;

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
    document.body.innerHTML = "";
  });

  it("既有意图虚线预览照旧，且不再有任何展开容器", () => {
    wrapper = mount(BoardPreviewLayer, {
      props: { intents: [intentFixture()], locatedIntentId: null, width: 2400, height: 1600 },
    });
    expect(wrapper.find('[data-im="preview"]').exists()).toBe(true);
    const previewCard = wrapper.find(".preview-card");
    expect(previewCard.exists()).toBe(true);
    // 虚线预览仍然用 preview 自带的坐标：展开功能删除后语义一个字没改
    expect(styleOf(previewCard.element)).toMatchObject({ left: "120px", top: "120px", width: "100px", height: "60px" });
    expect(wrapper.findAll('[data-im="preview-expanded"]')).toHaveLength(0);
  });

  it("源码守卫：预览层不再接受任何「展开槽位」输入（第二套位置没有入口）", () => {
    const text = source("components/interactive/BoardPreviewLayer.vue");
    expect(text).not.toContain("expandedSlots");
    expect(text).not.toContain("preview-expanded");
  });
});

describe("源码守卫：临时布局的边界与设计令牌", () => {
  it("cardLayout.ts 是纯函数：不读 DOM、不碰 store、不发请求、不改板面状态", () => {
    const text = source("interactive/cardLayout.ts");
    expect(text).not.toMatch(/\bdocument\.|\bwindow\.|useInteractiveStore|localStorage|fetch\(|XMLHttpRequest/);
    expect(text).toContain("export function planExpandedLayout");
  });

  it("BoardCard 的几何过渡用设计令牌的既有曲线，且拖动中的卡片不参与过渡", () => {
    const text = source("components/interactive/BoardCard.vue");
    expect(text).toContain(".card:not(.dragging)");
    expect(text).toMatch(/transition:[\s\S]{0,120}var\(--mo-2-move\)/);
    expect(text).toMatch(/var\(--ease-2\)/);
    // 减少动画由 styles/base.css 的 :root[data-motion="reduced"] 统一关掉（偏好来自 utils/motion.ts）
    const base = readFileSync(resolve(SRC, "styles/base.css"), "utf8");
    expect(base).toContain(':root[data-motion="reduced"]');
  });

  it("本轮涉及的界面文件颜色只用 var(--*)：没有裸色值", () => {
    for (const file of [
      "components/interactive/BoardCard.vue",
      "components/interactive/BoardPreviewLayer.vue",
      "components/interactive/BoardCanvas.vue",
      "views/InteractiveView.vue",
    ]) {
      const text = source(file);
      expect(text, file + " 出现裸色值").not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
      expect(text, file + " 出现裸色值").not.toMatch(/rgba?\(/);
    }
  });

  it("卡片内的挂载点与页面壳的收口入口都已接线，并标注了 P2 的替换点", () => {
    const card = source("components/interactive/BoardCard.vue");
    expect(card).toContain('data-im="card-preview-host"');
    expect(card).toContain("CardInlinePreview");
    const page = source("views/InteractiveView.vue");
    expect(page).toContain('data-im="collapse-previews"');
    expect(page).toContain("store.collapseAllPreviews()");
  });
});
