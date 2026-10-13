/**
 * [P2 卡片内预览] CardInlinePreview 组件级用例（契约 §3.5）。
 *
 * 这个文件测的是**组件这一层真的做了什么**，而不是某个函数被调用过：
 *
 *   1. 六种 `renderer`（image / pdf / docx / xlsx / pptx / code）各自渲染出对应容器
 *      （用契约 §8.4 的 `data-im` 钩子断言，不依赖 scoped 类名）；
 *   2. 不支持 / 失败时显示**真实原因**与打开/下载入口，**没有假按钮**；
 *   3. `active=false` 或 `card.id` 变化时不继续工作：换附件后旧结果**不写进新卡**；
 *   4. 卸载时真的释放：中止在途请求（AbortController.abort）、revoke blob URL、
 *      销毁 pdf 文档对象。
 *
 * 模拟边界只有两处：**渲染器模块**（动态 import 的边界）与**网络/系统动作**
 * （services/attachments）。previewPlan / previewStatusText / CardInlinePreview 本身
 * 全部用真实实现 —— 判据不是「mock 被调用了」。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";

const mocks = vi.hoisted(() => ({
  loadRenderer: vi.fn(),
  disposeRenderer: vi.fn(),
  fetchAttachmentPreview: vi.fn(),
  fetchAttachmentBytes: vi.fn(),
  openAttachment: vi.fn(),
  downloadAttachment: vi.fn(),
  destroy: vi.fn(async () => undefined),
  renderPage: vi.fn(async () => ({ width: 600, height: 800 })),
  revokeObjectURL: vi.fn(),
  createObjectURL: vi.fn(() => "blob:qio-preview-1"),
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
import type { BoardCard } from "../../../interactive/types";

// ---------------------------------------------------------------------------
// 假渲染器模块：形状与真实渲染器一致（view(data) → { ok, view } | { ok:false, reason }）
// ---------------------------------------------------------------------------

function rendererModule(name: string) {
  return {
    RENDERER: name,
    view: (data: unknown) => ({ ok: true, view: { ...(data as object), renderer: name } }),
  };
}

const PDF_MODULE = {
  RENDERER: "pdf",
  view: () => ({
    ok: true,
    view: { pages: 3, title: "季度报告", encrypted: false, textChars: 42, renderable: true, reason: "" },
  }),
  openPdf: async () => ({
    pages: 3,
    renderPage: mocks.renderPage,
    destroy: mocks.destroy,
  }),
};

const DOCX_MODULE = {
  RENDERER: "docx",
  view: () => ({
    ok: true,
    view: {
      blocks: [
        { type: "heading", level: 1, ordered: null, runs: [{ text: "标题", bold: false, italic: false }], text: "标题", marker: "H1" },
        { type: "paragraph", level: null, ordered: null, runs: [{ text: "正文", bold: false, italic: false }], text: "正文", marker: "" },
      ],
      tables: [{ rows: [["A", "B"]], columns: 2 }],
      truncated: false,
      notice: "结构化正文预览，不是原版面",
      empty: false,
    },
  }),
};

const XLSX_MODULE = {
  RENDERER: "xlsx",
  view: () => ({
    ok: true,
    view: {
      sheets: [
        {
          name: "汇总",
          rows: [{ index: 1, cells: [{ ref: "A1", column: "A", row: 1, value: "12", formula: null, cached: null, kind: "value", display: "12", formulaNote: "" }] }],
          columns: ["A"],
          rowCount: 1,
          columnCount: 1,
        },
      ],
      truncated: false,
      notice: "结构化表格预览（不是原版面）",
      empty: false,
    },
  }),
};

const PPTX_MODULE = {
  RENDERER: "pptx",
  view: () => ({
    ok: true,
    view: {
      slides: [
        { index: 1, title: "封面", texts: ["第一页正文"], notes: ["备注一"], images: [{ name: "pic1.png", bytes: 2048, size: "2.0 KB" }], tables: [] },
        { index: 2, title: "第二页", texts: ["第二页正文"], notes: [], images: [], tables: [] },
      ],
      truncated: false,
      notice: "结构化幻灯片预览（不是原版面）",
      empty: false,
    },
  }),
};

const CODE_MODULE = {
  RENDERER: "code",
  view: () => ({
    ok: true,
    view: {
      language: "python",
      languageLabel: "Python",
      lines: [
        { number: 1, text: "def f():" },
        { number: 2, text: "    return 1" },
      ],
      totalLines: 2,
      encoding: "utf-8",
      truncated: false,
    },
  }),
};

const IMAGE_MODULE = {
  RENDERER: "image",
  view: () => ({ ok: true, view: { width: 800, height: 600, bytes: 4096, shape: "800×600 像素 · 4.1 KB" } }),
};

const MODULES: Record<string, unknown> = {
  image: IMAGE_MODULE,
  pdf: PDF_MODULE,
  docx: DOCX_MODULE,
  xlsx: XLSX_MODULE,
  pptx: PPTX_MODULE,
  code: CODE_MODULE,
};

function envelope(kind: string, data: unknown, extra: Record<string, unknown> = {}) {
  return {
    status: "ready",
    kind,
    version: "v1",
    attachmentId: "att_1",
    recipe: 1,
    data,
    reason: "",
    truncated: false,
    stats: {},
    ...extra,
  };
}

// ---------------------------------------------------------------------------
// 卡片夹具
// ---------------------------------------------------------------------------

function fileCard(name: string, extra: Record<string, unknown> = {}): BoardCard {
  return {
    id: "card_1",
    kind: "file",
    x: 0,
    y: 0,
    w: 320,
    h: 200,
    content: "",
    checked: false,
    hidden: false,
    meta: { name, file: { name, attachmentId: "att_1", size: 4096, ...extra } },
  } as unknown as BoardCard;
}

/**
 * 渲染上下文是必填 props（显式声明「对话内」还是「独立材料阅读」，不靠 DOM 猜测）。
 * 默认给 material：这个文件测的是卡片内展开阅读（独立材料阅读）那一条路径。
 */
function mountPreview(card: BoardCard, active = true, context: "material" | "chat" = "material") {
  return mount(CardInlinePreview, {
    props: { card, active, onClose: () => undefined, context },
  });
}

/** 假 canvas 2D 上下文（jsdom 没有真实实现，这里只提供代码路径需要的成员）。 */
function stubCanvasContext(): void {
  Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
    configurable: true,
    value: () => ({ clearRect: () => undefined, save: () => undefined, restore: () => undefined }),
  });
}

beforeEach(() => {
  mocks.loadRenderer.mockReset();
  mocks.disposeRenderer.mockReset();
  mocks.fetchAttachmentPreview.mockReset();
  mocks.fetchAttachmentBytes.mockReset();
  mocks.openAttachment.mockReset();
  mocks.downloadAttachment.mockReset();
  mocks.destroy.mockClear();
  mocks.renderPage.mockClear();
  mocks.revokeObjectURL.mockClear();
  mocks.createObjectURL.mockClear();
  mocks.loadRenderer.mockImplementation(async (name: string) => MODULES[name]);
  mocks.fetchAttachmentPreview.mockImplementation(async () => envelope("code", { kind: "code", language: "text", lines: ["x"], totalLines: 1, truncated: false }));
  mocks.fetchAttachmentBytes.mockImplementation(async () => new ArrayBuffer(8));
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: mocks.createObjectURL });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: mocks.revokeObjectURL });
  stubCanvasContext();
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// 1. 六种渲染器各自渲染出对应容器
// ---------------------------------------------------------------------------

describe("CardInlinePreview：六种 renderer 各自渲染出对应容器", () => {
  it("图片：等比容器 + 缩放工具 + 真实像素事实", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(
      envelope("image", { kind: "image", width: 800, height: 600, bytes: 4096 }),
    );
    const w = mountPreview(fileCard("示意图.png"));
    await flushPromises();
    const box = w.find('[data-im="cip-image"]');
    expect(box.exists(), "图片容器没有渲染出来").toBe(true);
    expect(w.find('[data-testid="cip-zoom-in"]').exists()).toBe(true);
    expect(w.find('[data-testid="cip-image-fact"]').text()).toContain("800×600");
    expect(mocks.createObjectURL).toHaveBeenCalledTimes(1);
  });

  it("pdf：canvas 容器 + 真实页数（逐页渲染）", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(
      envelope("pdf", { kind: "pdf", pages: 3, title: "季度报告", encrypted: false, textChars: 42 }),
    );
    const w = mountPreview(fileCard("季度报告.pdf"));
    await flushPromises();
    expect(w.find('[data-im="cip-pdf"]').exists(), "pdf 容器没有渲染出来").toBe(true);
    expect(w.find('[data-testid="cip-canvas"]').exists()).toBe(true);
    expect(w.find('[data-testid="cip-page"]').text()).toContain("3");
  });

  it("docx：结构化正文容器 + 「不是原版面」的说明", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(envelope("docx", { kind: "docx", blocks: [], tables: [] }));
    const w = mountPreview(fileCard("报告.docx"));
    await flushPromises();
    expect(w.find('[data-im="cip-docx"]').exists(), "docx 容器没有渲染出来").toBe(true);
    expect(w.find('[data-testid="cip-notice"]').text()).toContain("不是原版面");
  });

  it("xlsx：工作表 + 单元格容器", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(envelope("xlsx", { kind: "xlsx", sheets: [] }));
    const w = mountPreview(fileCard("台账.xlsx"));
    await flushPromises();
    expect(w.find('[data-im="cip-xlsx"]').exists(), "xlsx 容器没有渲染出来").toBe(true);
    expect(w.find('[data-testid="cip-grid"]').exists()).toBe(true);
  });

  it("pptx：幻灯片容器 + 图片只列清单", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(envelope("pptx", { kind: "pptx", slides: [] }));
    const w = mountPreview(fileCard("汇报.pptx"));
    await flushPromises();
    expect(w.find('[data-im="cip-pptx"]').exists(), "pptx 容器没有渲染出来").toBe(true);
    expect(w.find('[data-testid="cip-slide-images"]').text()).toContain("只列清单");
  });

  it("code：行号 + 等宽 + 语言 + 只读", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(
      envelope("code", { kind: "code", language: "python", lines: ["def f():", "    return 1"], totalLines: 2 }),
    );
    const w = mountPreview(fileCard("示例.py"));
    await flushPromises();
    expect(w.find('[data-im="cip-code"]').exists(), "code 容器没有渲染出来").toBe(true);
    expect(w.find('[data-testid="cip-code-facts"]').text()).toContain("Python");
    expect(w.find('[data-testid="cip-code-facts"]').text()).toContain("只读");
    expect(w.findAll('[data-testid="cip-line"]')).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// 1b. 渲染上下文是显式的：独立材料阅读 vs 对话内（本轮滚动规则）
// ---------------------------------------------------------------------------

describe("CardInlinePreview：显式渲染上下文决定滚动与横向规则", () => {
  async function mountWith(renderer: string, data: Record<string, unknown>, context: "material" | "chat") {
    mocks.fetchAttachmentPreview.mockResolvedValue(envelope(renderer, data));
    const w = mountPreview(fileCard("材料.bin"), true, context);
    await flushPromises();
    return w;
  }

  it("material：图片 / PDF / 表格 / 代码各自声明二维浏览区（QIO 指示条 + data-qio-pan）", async () => {
    const image = await mountWith("image", { kind: "image", width: 800, height: 600, bytes: 4096 }, "material");
    expect(image.get('[data-testid="cip-root"]').attributes("data-qio-context")).toBe("material");
    expect(image.get('[data-testid="cip-root"]').attributes("data-qio-fit")).toBeDefined();
    // 图片：默认适应区域，放大后由图片框承担二维浏览
    expect(image.get(".cip-image-box").attributes("data-qio-pan")).toBe("both");
    expect(image.get(".cip-image-box").classes()).toContain("qio-scroll-host");
    // 缩放工具属于独立材料阅读
    expect(image.find('[data-testid="cip-zoom-in"]').exists()).toBe(true);
    image.unmount();

    const pdf = await mountWith("pdf", { kind: "pdf", pages: 3, title: "报告", encrypted: false, textChars: 10 }, "material");
    expect(pdf.get(".cip-canvas-box").attributes("data-qio-pan")).toBe("both");
    expect(pdf.find('[data-testid="cip-pdf-zoom-in"]').exists()).toBe(true);
    pdf.unmount();

    const code = await mountWith("code", { kind: "code", language: "text", lines: ["x"], totalLines: 1, truncated: false }, "material");
    // 代码预览的纵向滚动责任写死在 .cip-pre 自己身上
    expect(code.get('[data-testid="cip-pre"]').attributes("data-qio-pan")).toBe("both");
    code.unmount();

    const xlsx = await mountWith("xlsx", {
      kind: "xlsx",
      sheets: [{ name: "汇总", rows: [{ index: 1, cells: [{ ref: "A1", column: "A", row: 1, value: "12", formula: null, cached: null, kind: "value", display: "12", formulaNote: "" }] }], columns: ["A"], rowCount: 1, columnCount: 1 }],
      truncated: false,
    }, "material");
    // 电子表格：一个明确的网格区域承担二维查看
    expect(xlsx.get(".cip-grid-box").attributes("data-qio-pan")).toBe("both");
    // 网格保持真实表格结构（行号列 + 列标表头），不展开成「记录」列表
    expect(xlsx.get('[data-testid="cip-grid"]').element.tagName).toBe("TABLE");
    expect(xlsx.get('[data-testid="cip-grid"]').attributes("data-mode")).toBeUndefined();
    xlsx.unmount();
  });

  it("chat：无横向规则 —— 不建内层滚动容器、不给缩放、正文软换行", async () => {
    const code = await mountWith("code", { kind: "code", language: "text", lines: ["x"], totalLines: 1, truncated: false }, "chat");
    expect(code.get('[data-testid="cip-root"]').attributes("data-qio-context")).toBe("chat");
    // 三档视口里，对话内是 content：一个滚动容器都不建
    const body = code.get(".cip-body");
    expect(body.attributes("data-qio-viewport")).toBe("content");
    expect(body.attributes("data-qio-context")).toBe("chat");
    expect(code.get('[data-testid="cip-pre"]').attributes("data-qio-pan")).toBe("none");
    code.unmount();

    const image = await mountWith("image", { kind: "image", width: 800, height: 600, bytes: 4096 }, "chat");
    // 放大必然产生横向浏览：对话内不提供缩放入口
    expect(image.find('[data-testid="cip-zoom-in"]').exists()).toBe(false);
    expect(image.find('[data-testid="cip-zoom-out"]').exists()).toBe(false);
    expect(image.get(".cip-image-box").attributes("data-qio-pan")).toBe("none");
    image.unmount();

    const xlsx = await mountWith("xlsx", {
      kind: "xlsx",
      sheets: [{ name: "汇总", rows: [{ index: 1, cells: [] }], columns: ["A"], rowCount: 1, columnCount: 1 }],
      truncated: false,
    }, "chat");
    expect(xlsx.get(".cip-grid-box").attributes("data-qio-pan")).toBe("none");
    xlsx.unmount();
  });

  /*
    本轮验收硬条件：展开阅读时**只保留一个主要纵向视口**。
    这里逐个渲染器数一遍「声明的纵向视口」：二维浏览区（data-qio-pan="both"）与
    预览正文层（data-qio-viewport="self"）加起来必须恰好是 1 —— 不能既有卡片正文滚、
    又有 cip-body 滚、还有内层预览滚。
  */
  it("每个渲染器都只有一个主要纵向视口（不出现三层各滚一段）", async () => {
    const cases: Array<[string, Record<string, unknown>]> = [
      ["image", { kind: "image", width: 800, height: 600, bytes: 4096 }],
      ["pdf", { kind: "pdf", pages: 3, title: "报告", encrypted: false, textChars: 10 }],
      ["code", { kind: "code", language: "text", lines: ["x"], totalLines: 1, truncated: false }],
      ["xlsx", {
        kind: "xlsx",
        sheets: [{ name: "汇总", rows: [{ index: 1, cells: [] }], columns: ["A"], rowCount: 1, columnCount: 1 }],
        truncated: false,
      }],
      ["docx", {
        kind: "docx",
        blocks: [{ type: "paragraph", level: null, ordered: null, runs: [{ text: "正文", bold: false, italic: false }], text: "正文", marker: "" }],
        tables: [],
        truncated: false,
      }],
      ["pptx", {
        kind: "pptx",
        slides: [{ index: 1, title: "封面", texts: ["正文"], notes: [], images: [], tables: [] }],
        truncated: false,
      }],
    ];
    for (const [renderer, data] of cases) {
      const w = await mountWith(renderer, data, "material");
      const cip = w.get('[data-testid="cip-root"]');
      const body = cip.get(".cip-body");
      const innerViewports = cip.findAll('[data-qio-pan="both"]').length;
      const selfViewport = body.attributes("data-qio-viewport") === "self" ? 1 : 0;
      expect(innerViewports + selfViewport, renderer + " 的纵向视口数").toBe(1);
      // 预览正文层自己绝不再加一层无条件滚动
      expect(body.attributes("data-qio-viewport")).not.toBeUndefined();
      w.unmount();
    }
  });

  it("docx / pptx：材料阅读里纵向视口落在预览正文这一层（self），不是每个渲染器各建一层", async () => {
    const docx = await mountWith("docx", {
      kind: "docx",
      blocks: [{ type: "paragraph", level: null, ordered: null, runs: [{ text: "正文", bold: false, italic: false }], text: "正文", marker: "" }],
      tables: [],
      truncated: false,
    }, "material");
    expect(docx.get(".cip-body").attributes("data-qio-viewport")).toBe("self");
    docx.unmount();
    const pptx = await mountWith("pptx", {
      kind: "pptx",
      slides: [{ index: 1, title: "封面", texts: ["正文"], notes: [], images: [], tables: [] }],
      truncated: false,
    }, "material");
    expect(pptx.get(".cip-body").attributes("data-qio-viewport")).toBe("self");
    pptx.unmount();
  });
});

// ---------------------------------------------------------------------------
// 2. 不支持 / 失败：真实原因 + 打开/下载入口，没有假按钮
// ---------------------------------------------------------------------------

describe("CardInlinePreview：看不了的时候说真话", () => {
  it("后端 failed：显示真实原因 + 打开/下载入口，不渲染任何渲染器容器", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue({
      status: "failed",
      kind: "docx",
      version: "v1",
      attachmentId: "att_1",
      recipe: 1,
      data: null,
      reason: "这份 docx 的压缩结构损坏，读不出正文",
      truncated: false,
      stats: {},
    });
    const w = mountPreview(fileCard("坏掉的.docx"));
    await flushPromises();
    expect(w.find('[data-im="cip-external"]').exists()).toBe(true);
    expect(w.find('[data-testid="cip-reason"]').text()).toContain("压缩结构损坏");
    expect(w.find('[data-im="cip-docx"]').exists(), "失败时还渲染了正文容器").toBe(false);
    expect(mocks.loadRenderer).not.toHaveBeenCalled();
    // 入口是**真的**：按钮数量与 previewPlan 给出的动作一一对应（浏览器里 docx 走「新标签页查看」+ 下载）
    expect(w.findAll('[data-testid^="cip-action-"]')).toHaveLength(2);
    expect(w.find('[data-testid="cip-action-view"]').exists()).toBe(true);
    expect(w.find('[data-testid="cip-action-download"]').exists()).toBe(true);
    await w.find('[data-testid="cip-action-download"]').trigger("click");
    await flushPromises();
    expect(mocks.downloadAttachment).toHaveBeenCalledTimes(1);
  });

  it("旧版 Office：如实说明不能内嵌预览，保留打开/下载入口", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue({
      status: "unsupported",
      kind: "legacy_office",
      version: "v1",
      attachmentId: "att_1",
      recipe: 1,
      data: null,
      reason: "",
      truncated: false,
      stats: {},
    });
    const w = mountPreview(fileCard("旧表格.xls"));
    await flushPromises();
    expect(w.find('[data-testid="cip-reason"]').text()).toContain("旧版 Office");
    // 浏览器里没有桌面打开方式：只能下载副本 —— 入口真实存在，且只有真实存在的那些
    expect(w.find('[data-testid="cip-action-download"]').exists()).toBe(true);
    expect(mocks.loadRenderer).not.toHaveBeenCalled();
  });

  it("没有关联附件：没有预览、也没有假按钮（一个请求都不发）", async () => {
    const card = fileCard("只记着名字.png", {});
    (card.meta as Record<string, unknown>).file = { name: "只记着名字.png", size: 10 };
    const w = mountPreview(card);
    await flushPromises();
    expect(w.find('[data-testid="cip-reason"]').text()).toContain("还没有关联已保存的附件");
    expect(w.findAll(".cip-action")).toHaveLength(0);
    expect(mocks.fetchAttachmentPreview).not.toHaveBeenCalled();
    expect(mocks.fetchAttachmentBytes).not.toHaveBeenCalled();
  });

  it("打开失败：如实给出失败原因，不谎报「已打开」", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue({
      status: "failed",
      kind: "docx",
      version: "v1",
      attachmentId: "att_1",
      recipe: 1,
      data: null,
      reason: "读不出正文",
      truncated: false,
      stats: {},
    });
    mocks.openAttachment.mockRejectedValue(new Error("系统没有关联的打开方式"));
    const w = mountPreview(fileCard("报告.docx"));
    await flushPromises();
    await w.find('[data-testid="cip-action-view"]').trigger("click");
    await flushPromises();
    expect(mocks.openAttachment).toHaveBeenCalledTimes(1);
    expect(w.find('[data-testid="cip-action-note"]').text()).toContain("系统没有关联的打开方式");
  });
});

// ---------------------------------------------------------------------------
// 3. active=false / 换卡片：不在途工作，旧结果不写进新卡
// ---------------------------------------------------------------------------

describe("CardInlinePreview：收起与换附件", () => {
  it("active=false 时一个请求都不发", async () => {
    const w = mountPreview(fileCard("报告.docx"), false);
    await flushPromises();
    expect(mocks.fetchAttachmentPreview).not.toHaveBeenCalled();
    expect(w.find('[data-im="cip-code"]').exists()).toBe(false);
  });

  it("从 active 变 false：中止在途请求，并丢弃迟到的结果", async () => {
    let release: (value: unknown) => void = () => undefined;
    mocks.fetchAttachmentPreview.mockImplementation(
      () => new Promise((resolve) => {
        release = resolve;
      }),
    );
    const w = mountPreview(fileCard("示例.py"));
    await flushPromises();
    expect(mocks.fetchAttachmentPreview).toHaveBeenCalledTimes(1);
    await w.setProps({ active: false });
    await flushPromises();
    // 迟到的响应到了：不许写进界面
    release(envelope("code", { kind: "code", language: "python", lines: ["def f():"], totalLines: 1 }));
    await flushPromises();
    expect(w.find('[data-im="cip-code"]').exists(), "收起之后迟到结果被写进了界面").toBe(false);
    expect(w.find('[data-testid="cip-status"]').text()).toContain("附件已保存");
  });

  it("换附件（card.id 变化）：旧结果不写进新卡，新卡按自己的附件重新拉取", async () => {
    const first = fileCard("第一份.py");
    const second = { ...fileCard("第二份.docx"), id: "card_2" } as BoardCard;
    (second.meta as Record<string, unknown>).file = { name: "第二份.docx", attachmentId: "att_2", size: 2048 };
    mocks.fetchAttachmentPreview.mockImplementation(async (id: string) => {
      if (id === "att_1") return envelope("code", { kind: "code", language: "python", lines: ["第一份"], totalLines: 1 });
      return envelope("docx", { kind: "docx", blocks: [], tables: [] });
    });
    const w = mountPreview(first);
    await flushPromises();
    expect(w.find('[data-im="cip-code"]').exists()).toBe(true);

    await w.setProps({ card: second });
    await flushPromises();
    expect(mocks.fetchAttachmentPreview).toHaveBeenLastCalledWith("att_2", expect.anything());
    expect(w.find('[data-im="cip-docx"]').exists()).toBe(true);
    expect(w.find('[data-im="cip-code"]').exists(), "旧卡片的预览结果留在了新卡片上").toBe(false);
    expect(w.find('[data-testid="cip-name"]').text()).toBe("第二份.docx");
  });

  it("换卡片后旧请求的迟到结果不许覆盖新卡", async () => {
    const first = fileCard("第一份.py");
    const second = { ...fileCard("第二份.docx"), id: "card_2" } as BoardCard;
    (second.meta as Record<string, unknown>).file = { name: "第二份.docx", attachmentId: "att_2", size: 2048 };
    const pending: Record<string, (value: unknown) => void> = {};
    mocks.fetchAttachmentPreview.mockImplementation(
      (id: string) => new Promise((resolve) => {
        pending[id] = resolve;
      }),
    );
    const w = mountPreview(first);
    await flushPromises();
    await w.setProps({ card: second });
    await flushPromises();
    // 新卡的响应先到
    pending["att_2"](envelope("docx", { kind: "docx", blocks: [], tables: [] }));
    await flushPromises();
    expect(w.find('[data-im="cip-docx"]').exists()).toBe(true);
    // 旧卡的响应后到：必须被丢弃
    pending["att_1"](envelope("code", { kind: "code", language: "python", lines: ["第一份"], totalLines: 1 }));
    await flushPromises();
    expect(w.find('[data-im="cip-code"]').exists(), "旧卡的迟到结果覆盖了新卡").toBe(false);
    expect(w.find('[data-im="cip-docx"]').exists()).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 4. 卸载：真的释放
// ---------------------------------------------------------------------------

describe("CardInlinePreview：卸载与换附件时释放资源", () => {
  it("图片：卸载时 revoke blob URL", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(
      envelope("image", { kind: "image", width: 800, height: 600, bytes: 4096 }),
    );
    const w = mountPreview(fileCard("示意图.png"));
    await flushPromises();
    expect(mocks.createObjectURL).toHaveBeenCalledTimes(1);
    w.unmount();
    await flushPromises();
    expect(mocks.revokeObjectURL).toHaveBeenCalledWith("blob:qio-preview-1");
  });

  it("pdf：卸载时销毁文档对象（destroy 被调用）", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(
      envelope("pdf", { kind: "pdf", pages: 3, title: "季度报告", encrypted: false, textChars: 0 }),
    );
    const w = mountPreview(fileCard("季度报告.pdf"));
    await flushPromises();
    w.unmount();
    await flushPromises();
    expect(mocks.destroy).toHaveBeenCalledTimes(1);
  });

  it("结构化渲染器：卸载时要求释放渲染器资源（disposeRenderer 被调用）", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(envelope("docx", { kind: "docx", blocks: [], tables: [] }));
    const w = mountPreview(fileCard("报告.docx"));
    await flushPromises();
    expect(w.find('[data-im="cip-docx"]').exists()).toBe(true);
    mocks.disposeRenderer.mockClear();
    w.unmount();
    await flushPromises();
    expect(mocks.disposeRenderer, "卸载时没有释放渲染器资源").toHaveBeenCalledWith("docx");
  });

  it("卸载时中止在途请求（AbortController.abort 真的被调用）", async () => {
    const aborted: boolean[] = [];
    const realAbort = AbortController.prototype.abort;
    const spy = vi.spyOn(AbortController.prototype, "abort").mockImplementation(function (this: AbortController, ...args) {
      aborted.push(true);
      return realAbort.apply(this, args as []);
    });
    mocks.fetchAttachmentPreview.mockImplementation(
      () => new Promise(() => {
        /* 永远不 resolve：模拟在途 */
      }),
    );
    const w = mountPreview(fileCard("示例.py"));
    await flushPromises();
    expect(aborted).toHaveLength(0);
    w.unmount();
    await flushPromises();
    expect(aborted.length, "卸载时没有中止在途请求").toBeGreaterThan(0);
    spy.mockRestore();
  });

  it("换附件时也释放上一份：pdf 文档被销毁", async () => {
    mocks.fetchAttachmentPreview.mockResolvedValue(
      envelope("pdf", { kind: "pdf", pages: 2, title: "旧报告", encrypted: false, textChars: 0 }),
    );
    const first = fileCard("旧报告.pdf");
    const second = { ...fileCard("新报告.pdf"), id: "card_2" } as BoardCard;
    (second.meta as Record<string, unknown>).file = { name: "新报告.pdf", attachmentId: "att_2", size: 1024 };
    const w = mountPreview(first);
    await flushPromises();
    await w.setProps({ card: second });
    await flushPromises();
    expect(mocks.destroy, "换附件时上一份 pdf 文档没有被销毁").toHaveBeenCalled();
    w.unmount();
  });
});
