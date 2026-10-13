/**
 * [滚动统一规则 · 横向不溢出] 用**真实测量口径**取证（Lead 收尾条件 2）。
 *
 * 口径（两项缺一不可，只断言源码里写了 hidden/clip 不算证据）：
 *   1. 内容溢出：scrollWidth - clientWidth > 1px；
 *   2. scrollLeft 真的能移动（给一个增量后读回，值变了才算）。
 *
 * 两两组合的含义：
 *   - 溢出 + 能移动 = 横向浏览区（**只允许出现在 context="material" 的独立材料阅读里**）；
 *   - 溢出 + 不能移动 = 内容被裁掉 / 不可达 → 不通过；
 *   - 不溢出 = 无论能不能移动都不构成横向滚动。
 *
 * jsdom 没有真实排版，所以这里**打桩尺寸**来驱动判定逻辑（口径本身用真实实现
 * components/interactive/horizontalFit.ts，不 mock 判定）；真实像素由复核在浏览器里测，
 * 出口是根节点上的 data-qio-h-fit 与组件暴露的 verifyHorizontalFit()。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { measureHorizontalFit, H_OVERFLOW_EPS } from "../horizontalFit";

const mocks = vi.hoisted(() => ({
  loadRenderer: vi.fn(),
  disposeRenderer: vi.fn(),
  fetchAttachmentPreview: vi.fn(),
  fetchAttachmentBytes: vi.fn(),
  openAttachment: vi.fn(),
  downloadAttachment: vi.fn(),
  createObjectURL: vi.fn(() => "blob:qio-fit-1"),
  revokeObjectURL: vi.fn(),
}));

vi.mock("../../../interactive/renderers", () => ({
  loadRenderer: mocks.loadRenderer,
  disposeRenderer: mocks.disposeRenderer,
  isRendererKey: () => true,
  RENDERER_LOADERS: {},
}));

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
  return {
    ...actual,
    fetchAttachmentPreview: mocks.fetchAttachmentPreview,
    fetchAttachmentBytes: mocks.fetchAttachmentBytes,
    openAttachment: mocks.openAttachment,
    downloadAttachment: mocks.downloadAttachment,
  };
});

import CardInlinePreview from "../CardInlinePreview.vue";
import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
import type { BoardCard as BoardCardModel } from "../../../interactive/types";

const STAMP = "2026-10-13T00:00:00.000Z";

const CODE_MODULE = {
  RENDERER: "code",
  view: () => ({
    ok: true,
    view: { language: "python", languageLabel: "Python", totalLines: 2, encoding: "utf-8", truncated: false, lines: [{ number: 1, text: "def f():" }, { number: 2, text: "    return 1" }] },
  }),
};

const XLSX_MODULE = {
  RENDERER: "xlsx",
  view: () => ({
    ok: true,
    view: {
      sheets: [{ name: "汇总", rows: [{ index: 1, cells: [{ ref: "A1", column: "A", row: 1, value: "12", formula: null, cached: null, kind: "value", display: "12", formulaNote: "" }] }], columns: ["A"], rowCount: 1, columnCount: 1 }],
      truncated: false,
      notice: "结构化表格预览（不是原版面）",
      empty: false,
    },
  }),
};

const IMAGE_MODULE = {
  RENDERER: "image",
  view: () => ({ ok: true, view: { width: 800, height: 600, bytes: 4096, shape: "800×600" } }),
};

const MODULES: Record<string, unknown> = { code: CODE_MODULE, xlsx: XLSX_MODULE, image: IMAGE_MODULE };

function envelope(renderer: string, data: Record<string, unknown>) {
  return { status: "ready", kind: renderer, version: "v1", attachmentId: "att_1", recipe: 1, data, reason: "", truncated: false, stats: {} };
}

function fileCard(name: string): BoardCardModel {
  return {
    id: "card_1",
    kind: "file",
    x: 0,
    y: 0,
    w: 420,
    h: 380,
    content: "",
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
    meta: { name, file: { name, attachmentId: "att_1", size: 4096 } },
  } as unknown as BoardCardModel;
}

/**
 * 打桩一个元素的横向几何：
 * @param movable scrollLeft 是否真的能移动（模拟「真的能左右滚」与「只是藏起来 / 被裁掉」两种实现）
 */
function stubWidth(el: HTMLElement, clientWidth: number, scrollWidth: number, movable: boolean): void {
  Object.defineProperty(el, "clientWidth", { value: clientWidth, configurable: true });
  Object.defineProperty(el, "scrollWidth", { value: scrollWidth, configurable: true });
  let left = 0;
  Object.defineProperty(el, "scrollLeft", {
    configurable: true,
    get: () => left,
    set: (next: number) => {
      if (!movable) return;
      const max = Math.max(0, scrollWidth - clientWidth);
      left = Math.min(Math.max(0, Number(next) || 0), max);
    },
  });
}

async function mountPreview(context: "material" | "chat", renderer = "code") {
  const w = mount(CardInlinePreview, {
    props: { card: fileCard("材料.txt"), active: true, onClose: () => undefined, context },
    attachTo: document.body,
  });
  await flushPromises();
  expect(w.get("[data-testid=\"cip-root\"]").attributes("data-qio-context")).toBe(context);
  return w;
}

type Exposed = { verifyHorizontalFit: () => { ok: boolean; outerOverflow: string[]; clipped: string[]; pannable: string[]; outer: Array<{ key: string }> } | null };

beforeEach(() => {
  mocks.loadRenderer.mockReset();
  mocks.fetchAttachmentPreview.mockReset();
  mocks.fetchAttachmentBytes.mockReset();
  mocks.loadRenderer.mockImplementation(async (name: string) => MODULES[name]);
  mocks.fetchAttachmentBytes.mockImplementation(async () => new ArrayBuffer(8));
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: mocks.createObjectURL });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: mocks.revokeObjectURL });
});

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// 0. 口径本身：溢出 + 能移动，两项缺一不可
// ---------------------------------------------------------------------------

describe("测量口径：内容溢出 + scrollLeft 真能移动（缺一不可）", () => {
  it("溢出且能移动 = 可以横向浏览；不溢出 / 动不了都不算", () => {
    const el = document.createElement("div");
    stubWidth(el, 300, 600, true);
    const fit = measureHorizontalFit(el)!;
    expect(fit.overflowPx).toBe(300);
    expect(fit.overflowing).toBe(true);
    expect(fit.canScroll).toBe(true);
    expect(fit.scrollable).toBe(true);
    // 探测不改变用户看到的滚动位置
    expect(el.scrollLeft).toBe(0);

    stubWidth(el, 300, 600, false);
    const hiddenOnly = measureHorizontalFit(el)!;
    expect(hiddenOnly.overflowing).toBe(true);
    expect(hiddenOnly.canScroll).toBe(false);
    expect(hiddenOnly.scrollable).toBe(false);

    stubWidth(el, 300, 300 + H_OVERFLOW_EPS, true);
    const withinTolerance = measureHorizontalFit(el)!;
    expect(withinTolerance.overflowing).toBe(false);
    expect(withinTolerance.scrollable).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 1. chat 上下文：预览里的代码 / 表格 / 图片框都不横向溢出
// ---------------------------------------------------------------------------

describe("chat 上下文：预览里的查看区不许横向溢出（藏滚动条不算通过）", () => {
  it("代码预览：clientWidth == scrollWidth → ok，根节点 data-qio-h-fit=ok", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(envelope("code", { kind: "code" }));
    const w = await mountPreview("chat");
    const pre = w.get('[data-testid="cip-pre"]').element as HTMLElement;
    stubWidth(pre, 320, 320, false);
    const report = (w.vm as unknown as Exposed).verifyHorizontalFit()!;
    expect(report.ok).toBe(true);
    expect(report.outerOverflow).toEqual([]);
    expect(report.pannable).toEqual([]);
    expect(report.clipped).toEqual([]);
    await w.vm.$nextTick();
    w.unmount();
  });

  it("代码预览：内容真的溢出（哪怕 scrollLeft 动不了）→ 不通过、标记为 clipped", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(envelope("code", { kind: "code" }));
    const w = await mountPreview("chat");
    const pre = w.get('[data-testid="cip-pre"]').element as HTMLElement;
    // 「藏起滚动条但内容还在左右滚 / 被裁掉」的形态：溢出 + scrollLeft 不动
    stubWidth(pre, 320, 520, false);
    const report = (w.vm as unknown as Exposed).verifyHorizontalFit()!;
    expect(report.ok).toBe(false);
    expect(report.clipped.length).toBe(1);
    expect(report.pannable).toEqual([]);
    await w.vm.$nextTick();
    expect(w.get('[data-testid="cip-root"]').attributes("data-qio-h-fit")).toBe("clipped");
    w.unmount();
  });

  it("表格 / 图片查看区在 chat 里同样按同一口径判定", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(envelope("xlsx", { kind: "xlsx" }));
    const grid = await mountPreview("chat", "xlsx");
    const box = grid.get(".cip-grid-box").element as HTMLElement;
    stubWidth(box, 360, 360, false);
    expect((grid.vm as unknown as Exposed).verifyHorizontalFit()!.ok).toBe(true);
    grid.unmount();

    mocks.fetchAttachmentPreview.mockResolvedValue(envelope("image", { kind: "image" }));
    const image = await mountPreview("chat", "image");
    const imageBox = image.get(".cip-image-box").element as HTMLElement;
    stubWidth(imageBox, 360, 900, true);
    const report = (image.vm as unknown as Exposed).verifyHorizontalFit()!;
    // chat 里不允许出现任何横向浏览：即使 scrollLeft 能移动也不通过
    expect(report.ok).toBe(false);
    expect(report.pannable.length).toBe(1);
    image.unmount();
  });
});

// ---------------------------------------------------------------------------
// 2. material 上下文：查看区可以二维浏览，但外层（cip-body / card-body / 卡片）不许溢出
// ---------------------------------------------------------------------------

describe("material 上下文：二维浏览只发生在查看区，外层一律不横向溢出", () => {
  it("查看区溢出且真的能滚 → 允许（pannable），外层不溢出 → ok", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(envelope("code", { kind: "code" }));
    const w = await mountPreview("material");
    const root = w.get('[data-testid="cip-root"]').element as HTMLElement;
    const body = w.get(".cip-body").element as HTMLElement;
    const pre = w.get('[data-testid="cip-pre"]').element as HTMLElement;
    stubWidth(root, 400, 400, false);
    stubWidth(body, 380, 380, false);
    stubWidth(pre, 300, 900, true);
    const report = (w.vm as unknown as Exposed).verifyHorizontalFit()!;
    expect(report.ok).toBe(true);
    expect(report.outerOverflow).toEqual([]);
    expect(report.pannable.length).toBe(1);
    expect(report.clipped).toEqual([]);
    await w.vm.$nextTick();
    expect(w.get('[data-testid="cip-root"]').attributes("data-qio-h-fit")).toBe("ok");
    w.unmount();
  });

  it("查看区溢出但 scrollLeft 动不了 = 内容被裁掉 → 不通过", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(envelope("code", { kind: "code" }));
    const w = await mountPreview("material");
    stubWidth(w.get('[data-testid="cip-pre"]').element as HTMLElement, 300, 900, false);
    const report = (w.vm as unknown as Exposed).verifyHorizontalFit()!;
    expect(report.ok).toBe(false);
    expect(report.clipped.length).toBe(1);
    w.unmount();
  });

  it("外层（.cip-body）溢出 → 不通过（二维浏览只许留给查看区自己）", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(envelope("code", { kind: "code" }));
    const w = await mountPreview("material");
    stubWidth(w.get(".cip-body").element as HTMLElement, 380, 460, true);
    const report = (w.vm as unknown as Exposed).verifyHorizontalFit()!;
    expect(report.ok).toBe(false);
    expect(report.outerOverflow).toContain("cip-body");
    w.unmount();
  });

  it("在卡片里展开：外层样本包含 card-body 与卡片本身（口径覆盖到卡片边界）", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(envelope("code", { kind: "code" }));
    localStorage.clear();
    setActivePinia(createPinia());
    const store = useInteractiveStore();
    const card = fileCard("材料.txt");
    store.board = { ...emptyState("board_t"), cards: [card] } as never;
    const view = mount(BoardCard, {
      props: { card, selected: false, highlight: false, dragging: false, x: card.x, y: card.y, groupName: null, groups: [], toolbarLeft: 0, toolbarTop: 0, multi: false, connecting: false },
      attachTo: document.body,
    });
    await view.get('[data-im="card-preview-toggle"]').trigger("click");
    await flushPromises();
    const child = view.findComponent(CardInlinePreview);
    const body = view.get('[data-im="card-body"]').element as HTMLElement;
    const article = view.get('[data-im="card"]').element as HTMLElement;
    stubWidth(body, 380, 380, false);
    stubWidth(article, 420, 420, false);
    const report = (child.vm as unknown as Exposed).verifyHorizontalFit()!;
    const keys = report.outer.map((item) => item.key);
    expect(keys).toContain("card-body");
    expect(keys).toContain("card");
    expect(report.ok).toBe(true);
    // 卡片正文一旦横向溢出，报告必须抓出来（不是只看源码属性）
    stubWidth(body, 380, 500, true);
    const bad = (child.vm as unknown as Exposed).verifyHorizontalFit()!;
    expect(bad.ok).toBe(false);
    expect(bad.outerOverflow).toContain("card-body");
    view.unmount();
  });
});
