/**
 * [滚动统一规则 · 板面 / 卡片] 本轮验收的硬条件在**真实组件路径**上的取证。
 *
 * 覆盖三件事（都用真实 BoardCanvas / BoardCard / TextCardEditor + 真实 store，
 * 只把网络与系统动作换成替身）：
 *
 * 1.【板面视口】策略 hidden：原生滚动条与 QIO 指示条都不出现；
 *    无限画布下视口自己裁切画面（overflow: hidden），平移由 view 驱动世界层 transform ——
 *    空板也能向四个方向连续平移，且不产生任何板面保存 / 撤销记录；
 * 2.【一个主要纵向视口】卡片正文是那张卡唯一的主要纵向视口；代码块不再自己纵向滚动；
 *    展开阅读时材料自带的二维浏览区接管视口（卡片正文因此没有可滚范围）；
 * 3.【编辑态】连续编辑区：编辑框跟随内容增高、自己不滚动；最小高度按卡片高度收敛，
 *    不因为一个固定的最小高度把较小的卡片撑出第二层滚动。
 *
 * jsdom 没有真实排版：几何结论一律来自**样式规则 + DOM 归属**，不假装 getBoundingClientRect 有布局。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createPinia, setActivePinia } from "pinia";
import { mount, type VueWrapper } from "@vue/test-utils";
import BoardCanvas from "../BoardCanvas.vue";
import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
import type { BoardCard as BoardCardModel, BoardState } from "../../../interactive/types";

const STAMP = "2026-10-13T00:00:00.000Z";
const SRC = resolve(process.cwd(), "src");

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
  return {
    ...actual,
    fetchAttachmentPreview: vi.fn(async () => ({ status: "failed", kind: "code", reason: "单测里不取字节", version: "v1", data: null, truncated: false, stats: {}, recipe: 1 })),
    fetchAttachmentBytes: vi.fn(async () => new ArrayBuffer(8)),
    openAttachment: vi.fn(async () => ({ ok: true, note: "已打开" })),
    downloadAttachment: vi.fn(async () => ({ ok: true, note: "已下载" })),
  };
});

function source(relative: string): string {
  return readFileSync(resolve(SRC, relative), "utf8");
}

function cssBlock(text: string, selector: string): string {
  const at = text.indexOf(selector);
  if (at < 0) return "";
  const start = text.indexOf("{", at);
  const end = text.indexOf("}", start);
  return text.slice(start, end + 1);
}

function mkCard(id: string, patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return {
    id,
    kind: "text",
    content: "卡片 " + id,
    meta: {},
    x: 0,
    y: 0,
    w: 260,
    h: 220,
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

function mountCard(card: BoardCardModel, selected = false): VueWrapper {
  return mount(BoardCard, {
    props: {
      card,
      selected,
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
}

describe("板面滚动区：策略 hidden（指示条与原生滚动条都不出现，滚动能力照旧）", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  let wrapper: VueWrapper | null = null;

  beforeEach(() => {
    localStorage.clear();
    setActivePinia(createPinia());
    store = useInteractiveStore();
    store.board = { ...emptyState("board_t"), cards: [mkCard("c1", { x: 100, y: 100, w: 120, h: 80 })] } as BoardState;
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
    document.body.innerHTML = "";
    vi.restoreAllMocks();
  });

  it("挂载后：宿主标记 hidden，一个指示条节点都没有，range 固定 none", () => {
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    const view = wrapper.get(".board-viewport").element as HTMLElement;
    expect(view.getAttribute("data-qio-scroll")).toBe("hidden");
    expect(view.getAttribute("data-qio-range")).toBe("none");
    expect(view.classList.contains("qio-scroll-host")).toBe(true);
    // 指示条是引擎在 auto 策略下才会建的节点：hidden 策略下必须一个都不存在
    expect(view.querySelectorAll(".qio-scroll-rail").length).toBe(0);
    expect(view.querySelectorAll(".qio-scroll-thumb").length).toBe(0);
  });

  it("视口自己裁切画面：overflow 是 hidden，不再靠容器滚动查看", () => {
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    const view = wrapper.get(".board-viewport").element as HTMLElement;
    // 规则层：无限画布由 view 决定画面，视口只负责裁切
    const rule = cssBlock(source("components/interactive/BoardCanvas.vue"), ".board-viewport {");
    expect(rule).toContain("overflow: hidden");
    // 旧模型的滚动内容层已经不存在（它撑出有限内容区，空板向左上平移会碰边界）
    expect(wrapper.find(".board-scroll-content").exists()).toBe(false);
    // 世界层只做一次 translate + scale，尺寸由内容自己决定
    const surfaceRule = cssBlock(source("components/interactive/BoardCanvas.vue"), ".board-surface {");
    expect(surfaceRule).toContain("position: absolute");
    expect(surfaceRule).not.toContain("min-width: 100%");
    expect(view.style.backgroundSize).toBe("24px 24px");
  });

  it("空板也能向四个方向连续平移：世界层 transform 跟着走，板面状态一个字不变", async () => {
    // 空板：没有任何卡片撑大内容区 —— 这正是旧模型会立刻碰到边界的情形
    store.board = emptyState("board_t");
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    const view = wrapper.get(".board-viewport").element as HTMLElement;
    Object.defineProperty(view, "clientWidth", { value: 800, configurable: true });
    Object.defineProperty(view, "clientHeight", { value: 600, configurable: true });
    view.getBoundingClientRect = () =>
      ({ left: 40, top: 100, right: 840, bottom: 700, width: 800, height: 600, x: 40, y: 100, toJSON: () => ({}) }) as DOMRect;
    const surface = wrapper.get(".board-surface").element as HTMLElement;
    const transformOf = () => surface.style.transform;

    // 向左上拖（视野往右下走）：世界原点右移下移
    view.dispatchEvent(new MouseEvent("pointerdown", { clientX: 400, clientY: 400, button: 0, bubbles: true, cancelable: true }));
    document.dispatchEvent(new MouseEvent("pointermove", { clientX: 700, clientY: 600, bubbles: true, cancelable: true }));
    await Promise.resolve();
    expect(transformOf()).toContain("translate(300px, 200px)");
    // 继续向同一方向拖过两个屏幕：没有边界
    document.dispatchEvent(new MouseEvent("pointermove", { clientX: 400 + 1600, clientY: 400 + 1200, bubbles: true, cancelable: true }));
    await Promise.resolve();
    expect(transformOf()).toContain("translate(1600px, 1200px)");
    // 反向拖回原点：世界层回到 0（四个方向都连续）
    document.dispatchEvent(new MouseEvent("pointermove", { clientX: 400, clientY: 400, bubbles: true, cancelable: true }));
    await Promise.resolve();
    expect(transformOf()).toContain("translate(0px, 0px)");
    document.dispatchEvent(new MouseEvent("pointerup", { clientX: 400, clientY: 400, bubbles: true, cancelable: true }));
    // 平移只改查看方式：板面状态一个字没变，也没有任何提交
    expect(store.board?.cards).toEqual([]);
  });

  it("平移不产生板面保存 / 撤销 / 提交记录", async () => {
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    const view = wrapper.get(".board-viewport").element as HTMLElement;
    Object.defineProperty(view, "clientWidth", { value: 800, configurable: true });
    Object.defineProperty(view, "clientHeight", { value: 600, configurable: true });
    view.getBoundingClientRect = () =>
      ({ left: 40, top: 100, right: 840, bottom: 700, width: 800, height: 600, x: 40, y: 100, toJSON: () => ({}) }) as DOMRect;
    const labels: string[] = [];
    vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
      labels.push(label);
      store.board = { ...next, boardId: store.boardId };
    });
    const before = JSON.stringify(store.board);
    view.dispatchEvent(new MouseEvent("pointerdown", { clientX: 400, clientY: 400, button: 0, bubbles: true, cancelable: true }));
    document.dispatchEvent(new MouseEvent("pointermove", { clientX: 900, clientY: 800, bubbles: true, cancelable: true }));
    await Promise.resolve();
    document.dispatchEvent(new MouseEvent("pointerup", { clientX: 900, clientY: 800, bubbles: true, cancelable: true }));
    await Promise.resolve();
    expect(labels).toEqual([]);
    expect(JSON.stringify(store.board)).toBe(before);
    expect(store.undoStack).toHaveLength(0);
  });

  it("尺寸调整取消（pointercancel）：不提交、不留预览，指针状态清干净", async () => {
    store.board = { ...emptyState("board_t"), cards: [mkCard("c1", { x: 100, y: 100, w: 120, h: 80 })], selection: ["c1"] } as BoardState;
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    const view = wrapper;
    const labels: string[] = [];
    vi.spyOn(store, "commit").mockImplementation((next: BoardState, label: string) => {
      labels.push(label);
      store.board = { ...next, boardId: store.boardId };
    });
    await Promise.resolve();
    const handle = view.get('[data-resize-card-id="c1"] [data-resize-handle="se"]');
    handle.element.dispatchEvent(new MouseEvent("pointerdown", { clientX: 220, clientY: 180, button: 0, bubbles: true, cancelable: true }));
    document.dispatchEvent(new MouseEvent("pointermove", { clientX: 320, clientY: 280, bubbles: true, cancelable: true }));
    await Promise.resolve();
    // 拖动中：只预览，不提交
    expect(labels).toEqual([]);
    document.dispatchEvent(new MouseEvent("pointercancel", { clientX: 320, clientY: 280, bubbles: true, cancelable: true }));
    await Promise.resolve();
    // 取消后：一次提交都没有，也没有残留在拖动态（板面几何字段保持原值）
    expect(labels).toEqual([]);
    expect(store.board?.cards[0].w).toBe(120);
    expect(store.board?.cards[0].h).toBe(80);
    expect(view.get(".board-surface").classes()).not.toContain("resizing");
    // 再松手也不许补一次提交（监听已释放）
    document.dispatchEvent(new MouseEvent("pointerup", { clientX: 320, clientY: 280, bubbles: true, cancelable: true }));
    await Promise.resolve();
    expect(labels).toEqual([]);
  });

  it("源码守卫：指令与 overflow 声明成对出现（缺一个就等于回到原生滚动条）", () => {
    const canvas = source("components/interactive/BoardCanvas.vue");
    expect(canvas).toContain("import { vQioScrollbar } from \"../../composables/useQioScrollbars\"");
    expect(canvas).toMatch(/class="board-viewport"\s+v-qio-scrollbar="'hidden'"/);
  });
});

describe("卡片：一个主要纵向视口 + 代码块不自己滚动", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  let wrapper: VueWrapper | null = null;

  beforeEach(() => {
    localStorage.clear();
    setActivePinia(createPinia());
    store = useInteractiveStore();
    store.board = { ...emptyState("board_t"), cards: [mkCard("c1")] } as BoardState;
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
    document.body.innerHTML = "";
  });

  it("卡片正文带 data-qio-fit 与统一的纵向指示条（policy auto / axis y）", () => {
    const card = source("components/interactive/BoardCard.vue");
    expect(card).toMatch(/class="card-body"[\s\S]{0,200}data-qio-fit/);
    expect(card).toContain("v-qio-scrollbar=\"{ policy: 'auto', axis: 'y' }\"");
    const body = cssBlock(card, ".card-body {");
    expect(body).toContain("overflow-y: auto");
    expect(body).toContain("min-height: 0");
  });

  it("代码块：软换行、自己不滚动（横纵都不），原文逐字保留", async () => {
    const card = store.board!.cards[0];
    // 直接把带代码围栏的正文放进卡片（meta.docText 是请求级事实来源之一，权威顺序见 textCard.ts）
    store.board = {
      ...store.board!,
      cards: [{ ...card, meta: { docText: "# 标题\n\n```python\nx = 1\n```" } }],
    } as BoardState;
    const view = mountCard(store.board!.cards[0]);
    wrapper = view;
    const block = view.find('[data-im="card-block-code"]');
    expect(block.exists(), "带围栏的正文必须渲染出代码块").toBe(true);
    const pre = block.get(".code-body");
    // textContent 逐字保留（软换行只改呈现，复制结果不变）
    expect(pre.text()).toContain("x = 1");
    expect(pre.text()).not.toContain("\\n");
    const rule = cssBlock(source("components/interactive/BoardCard.vue"), ".code-body {");
    expect(rule).toContain("white-space: pre-wrap");
    expect(rule).toContain("overflow-wrap: anywhere");
    expect(rule).not.toContain("overflow-x: auto");
    expect(rule).not.toContain("overflow-y: auto");
  });

  it("展开阅读（材料卡）：卡片内预览收到显式 material 上下文，并按材料类型声明视口", async () => {
    const card = mkCard("f1", {
      kind: "file",
      meta: { name: "台账.xlsx", file: { name: "台账.xlsx", attachmentId: "att_1", size: 4096 } },
      w: 420,
      h: 380,
    });
    store.board = { ...emptyState("board_t"), cards: [card] } as BoardState;
    const view = mountCard(card);
    wrapper = view;
    expect(view.find('[data-im="card-preview-toggle"]').exists()).toBe(false);
    const host = view.get('[data-im="card-preview-host"]');
    const cip = host.get('[data-testid="cip-root"]');
    expect(cip.attributes("data-qio-context")).toBe("material");
    // 显式上下文以属性形式落在 DOM 上：material 才允许按材料类型做二维浏览
    expect(cip.attributes("data-qio-fit")).toBeDefined();
  });

  it("预览层源码守卫：纵向视口只有一个（不允许 cip-body 与内层各建一层）", () => {
    const cip = source("components/interactive/CardInlinePreview.vue");
    // .cip-body 的基线规则里不允许再出现无条件的 overflow: auto
    const body = cssBlock(cip, ".cip-body {");
    expect(body).not.toContain("overflow: auto");
    expect(body).not.toContain("overflow-y: auto");
    // 纵向责任按 data-qio-viewport 明确；二维浏览区按 data-qio-pan 明确
    expect(cip).toContain('.cip-body[data-qio-viewport="self"]');
    expect(cip).toContain('.cip-body[data-qio-viewport="viewer"]');
    // 图片不再内嵌阅读：只保留 PDF / 表格 / 代码的二维浏览区
    expect(cip).not.toContain("cip-image");
    expect(cip).toContain('.cip-canvas-box[data-qio-pan="both"]');
    expect(cip).toContain('.cip-pre[data-qio-pan="both"]');
    expect(cip).toContain('.cip-grid-box[data-qio-pan="both"]');
    // 旧的三层纵向滚动源头（max-height + overflow: auto 的 60vh 盒子）不再存在
    expect(cip).not.toContain("max-height: 60vh");
    expect(cip).not.toContain("max-height: 50vh");
  });
});

describe("编辑态：连续编辑区（自己不滚，最小高度按卡片收敛）", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  let wrapper: VueWrapper | null = null;

  beforeEach(() => {
    localStorage.clear();
    setActivePinia(createPinia());
    store = useInteractiveStore();
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
    document.body.innerHTML = "";
  });

  async function openEditor(card: BoardCardModel): Promise<VueWrapper> {
    // 局部工具栏只在「这张卡是选择里的最后一张」时渲染：先选中再点「编辑」
    store.board = { ...emptyState("board_t"), cards: [card], selection: [card.id] } as BoardState;
    const view = mountCard(card, true);
    wrapper = view;
    await view.get('[data-im="card-edit"]').trigger("click");
    return view;
  }

  it("很小的卡片：编辑区吃满卡片，不再用固定最小高度撑开（2026-10-14 输入框占满卡片）", async () => {
    const view = await openEditor(mkCard("c1", { w: 200, h: 96 }));
    const editor = view.get('[data-im="card-editor"]');
    expect(editor.attributes("style") ?? "").toContain("min-height: 0px");
    // 编辑区仍是同一个连续编辑面（只有一个 textarea，格式工具栏在它上方）
    expect(view.findAll('[data-im="card-editor"]').length).toBe(1);
    // 正文区是 flex 列，编辑区吃满它（fill 档）
    expect(view.get(".card-body").classes()).toContain("card-body-editing-text");
    expect(view.get('[data-im="text-card-editor"]').classes()).toContain("is-fill");
  });

  it("高卡片同样吃满：高度交给 flex，不写死最小高度", async () => {
    const view = await openEditor(mkCard("c1", { w: 420, h: 600 }));
    expect(view.get('[data-im="card-editor"]').attributes("style") ?? "").toContain("min-height: 0px");
  });

  it("吃满档输入框自己滚动；基础档（非 fill）仍不滚动，纵向只剩卡片正文这一层", async () => {
    const view = await openEditor(mkCard("c1", { w: 320, h: 420 }));
    // fill 档（板面里的文字卡）：输入框吃满剩余高度并自己滚动
    const fillRule = cssBlock(source("interactive/TextCardEditor.vue"), ".text-card-editor.is-fill .editor {");
    expect(fillRule).toContain("overflow: auto");
    expect(fillRule).toContain("flex: 1 1 0%");
    // 基础档（独立挂载等）：编辑框自己不滚动，高度跟随内容长
    const baseRule = cssBlock(source("interactive/TextCardEditor.vue"), "\n.editor {");
    expect(baseRule).toContain("overflow: hidden");
    // 卡片正文仍是主要纵向视口
    const body = cssBlock(source("components/interactive/BoardCard.vue"), ".card-body {");
    expect(body).toContain("overflow-y: auto");
    // 编辑区容器带 data-qio-fit：横向边界保护
    expect(source("interactive/TextCardEditor.vue")).toContain("data-qio-fit");
    expect(view.find('[data-im="text-card-editor"]').exists()).toBe(true);
  });
});
