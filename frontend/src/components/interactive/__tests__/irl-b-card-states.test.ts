/**
 * [IRL-B] 卡片内部结构：常态 / 阅读展开 / 编辑 / 折叠 四态，最小尺寸自适应，许可可见性，更多菜单。
 *
 * 标注：【组件/DOM】挂载真实 BoardCard.vue + 真实 store（不给组件注入替身）。
 * 唯一被替换的是**网络与系统动作**（services/attachments：预览取字节、外部打开）——
 * 这几条链路在单测里不该真的发请求。
 *
 * jsdom 没有真实排版，"内容区高度 > 0 / 按钮不被裁掉" 这类结论不能靠 getBoundingClientRect 假装，
 * 所以这里分两层取证：
 *   1. DOM 层：四态互斥、入口文案、许可徽章、更多菜单可达性、勾选只改 checked；
 *   2. 规则层：读 BoardCard.vue 的样式规则，断言「头部与页脚 flex: none、正文 flex: 1 且
 *      min-height: 0、正文是唯一的主要纵向视口（代码块自己不再滚动）、卡片自身 overflow: hidden」——
 *      这几条共同保证 180×96 时内容区仍拿到正高度、长内容不撑破卡片、也不被无故裁掉。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  fetchAttachmentPreview: vi.fn(),
  fetchAttachmentBytes: vi.fn(),
  openAttachment: vi.fn(),
  downloadAttachment: vi.fn(),
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

import BoardCard from "../BoardCard.vue";
import CardAttachmentPreview from "../CardAttachmentPreview.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
import { FENCE, parseDocText } from "../../../interactive/textCard";
import { isVisibleCard, localVisibleRange } from "../../../interactive/submission";
import type { BoardCard as BoardCardModel } from "../../../interactive/types";

const STAMP = "2026-10-13T00:00:00.000Z";
const SRC = resolve(process.cwd(), "src");

const DOC_TEXT = ["# 采购清单", "", "第一段正文。", "", FENCE + "python", "x = 1", FENCE, ""].join("\n");

function mkCard(id: string, patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return {
    id,
    kind: "text",
    content: "",
    meta: {},
    x: 0,
    y: 0,
    w: 260,
    h: 170,
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

/** 一张真实的统一文字卡：标题块 + 段落 + 代码块（meta.doc 与 meta.docText 成对，与落地形状一致） */
function textCard(patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return mkCard("c1", {
    content: "采购清单",
    meta: { doc: parseDocText(DOC_TEXT), docText: DOC_TEXT },
    ...patch,
  });
}

let store: ReturnType<typeof useInteractiveStore>;
let wrapper: VueWrapper | null = null;

function mountBoard(card: BoardCardModel, overrides: Record<string, unknown> = {}): VueWrapper {
  store.board = { ...emptyState("board_t"), cards: [card], selection: [card.id] };
  wrapper = mount(BoardCard, {
    props: {
      card,
      selected: true,
      highlight: false,
      dragging: false,
      x: card.x,
      y: card.y,
      groupName: null,
      groups: [],
      toolbarLeft: 0,
      toolbarTop: card.y - 40,
      multi: false,
      connecting: false,
      ...overrides,
    },
    attachTo: document.body,
  });
  return wrapper;
}

/** 按可见文案找一个按钮（不用下标猜，避免「换了顺序就点错」） */
function buttonByText(w: VueWrapper, text: string) {
  return w.findAll("button").find((item) => item.text() === text);
}

function source(relative: string): string {
  return readFileSync(resolve(SRC, relative), "utf8");
}

/** 取出某个选择器的规则块（第一个匹配），与 closure-c-visual-guards 同一套读法 */
function cssBlock(text: string, selector: string): string {
  const start = text.indexOf(selector);
  expect(start, "找不到选择器 " + selector).toBeGreaterThan(-1);
  const open = text.indexOf("{", start);
  const close = text.indexOf("}", open);
  return text.slice(open + 1, close);
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  mocks.fetchAttachmentPreview.mockResolvedValue({
    status: "unsupported",
    kind: "pdf",
    reason: "单测不取真实预览",
    data: null,
    version: "v1",
    truncated: false,
  });
  mocks.openAttachment.mockResolvedValue({ ok: true, note: "单测：已交给系统打开" });
  mocks.downloadAttachment.mockResolvedValue({ ok: true, note: "单测：已下载" });
  setActivePinia(createPinia());
  store = useInteractiveStore();
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
});

describe("四态互斥：常态 / 阅读展开 / 编辑 / 折叠", () => {
  it("常态：标题 + 简短内容 + 必要状态，正文只渲染一份，入口叫「展开预览」", () => {
    const w = mountBoard(textCard());

    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("normal");
    expect(w.get('[data-im="card-title"]').text()).toBe("采购清单");
    expect(w.find('[data-im="card-subject"]').exists()).toBe(true);
    expect(w.get('[data-im="card-body"]').text()).toContain("第一段正文。");
    expect(w.find('[data-im="card-block-code"]').exists()).toBe(true);
    expect(w.get('[data-im="card-preview-toggle"]').text()).toBe("展开预览");
    expect(w.get('[data-im="card-meta"]').text()).toContain("未许可");

    // 既不渲染编辑器，也不渲染预览挂载点
    expect(w.find('[data-im="card-editor"]').exists()).toBe(false);
    expect(w.find('[data-im="card-preview-host"]').exists()).toBe(false);

    // 正文只出现一次（曾经块序列与派生纯文本各渲染一遍，同一句话在卡片里出现两遍）
    const shown = w.get('[data-im="card-subject"]').text();
    expect(shown.split("第一段正文。").length - 1).toBe(1);
  });

  it("阅读展开：正文成为主体；没有真实附件的文字卡不造假预览", async () => {
    const w = mountBoard(textCard());
    await w.get('[data-im="card-preview-toggle"]').trigger("click");

    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("reading");
    const host = w.get('[data-im="card-preview-host"]');
    expect(host.attributes("data-card-id")).toBe("c1");
    // 挂载点在卡片内部（不是浮层）
    expect(host.element.closest('[data-im="card"]')).not.toBeNull();
    expect(w.get('[data-im="card-preview-toggle"]').text()).toBe("收起预览");
    // 文字卡的阅读主体是它自己的正文；这里不出现「没有附件」的假预览面板
    expect(host.find('[data-testid="cip-root"]').exists()).toBe(false);
    expect(host.text()).toBe("");
    expect(w.get('[data-im="card-subject"]').text()).toContain("第一段正文。");

    await w.get('[data-im="card-preview-toggle"]').trigger("click");
    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("normal");
    expect(w.find('[data-im="card-preview-host"]').exists()).toBe(false);
  });

  it("编辑：只有一个连续编辑区；阅读挂载点与常态主体都让位", async () => {
    const w = mountBoard(textCard());
    await w.get('[data-im="card-edit"]').trigger("click");

    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("editing");
    expect(w.findAll('[data-im="card-editor"]')).toHaveLength(1);
    expect(w.find('[data-im="card-editor-toolbar"]').exists()).toBe(true);
    expect(w.find('[data-im="card-subject"]').exists()).toBe(false);
    expect(w.find('[data-im="card-preview-host"]').exists()).toBe(false);
    // 编辑态不出现点了也不会变的阅读入口
    expect(w.find('[data-im="card-preview-toggle"]').exists()).toBe(false);

    const cancel = buttonByText(w, "取消");
    expect(cancel).toBeTruthy();
    await cancel!.trigger("click");
    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("normal");
    expect(w.find('[data-im="card-editor"]').exists()).toBe(false);
  });

  it("编辑与阅读互斥但州状态不互改：正在阅读时进编辑，退出后仍回到阅读展开", async () => {
    const w = mountBoard(textCard());
    await w.get('[data-im="card-preview-toggle"]').trigger("click");
    expect(store.isCardPreviewExpanded("c1")).toBe(true);

    await w.get('[data-im="card-edit"]').trigger("click");
    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("editing");
    // 阅读展开是查看状态：进编辑器不该把它偷偷清掉（也没有写板面状态、没有产生表达）
    expect(store.isCardPreviewExpanded("c1")).toBe(true);
    expect(store.dirty).toBe(false);
    expect(store.submissions).toEqual([]);

    await buttonByText(w, "取消")!.trigger("click");
    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("reading");
  });

  it("折叠：只剩标题与必要状态，恢复入口叫「恢复卡片」", async () => {
    const w = mountBoard(textCard({ folded: true }));

    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("folded");
    expect(w.find('[data-im="card-body"]').exists()).toBe(false);
    expect(w.find('[data-im="card-editor"]').exists()).toBe(false);
    expect(w.find('[data-im="card-preview-toggle"]').exists()).toBe(false);
    expect(w.find('[data-im="card-preview-host"]').exists()).toBe(false);
    // 折叠仍认得出是哪一条，并且状态里写明「已折叠：只改变显示」
    expect(w.get('[data-im="card-title"]').text()).toBe("采购清单");
    expect(w.get('[data-im="card-meta"]').text()).toContain("已折叠");

    await w.get('[data-im="card-unfold"]').trigger("click");
    expect(w.emitted("toggle")).toEqual([["c1", "folded"]]);
  });

  it("卡片上只有一个「展开」按钮：折成标题与展开预览不会各叫一个「展开」", () => {
    const w = mountBoard(textCard());
    const expandButtons = w.findAll("button").filter((item) => item.text().startsWith("展开"));
    expect(expandButtons.map((item) => item.attributes("data-im"))).toEqual(["card-preview-toggle"]);
    // 折成标题的动作说明的是「折」，恢复卡片说的是「恢复」
    expect(w.get('[data-im="card-fold"]').text()).toBe("折成标题");

    const folded = mountBoard(textCard({ folded: true }));
    expect(folded.findAll("button").filter((item) => item.text().startsWith("展开"))).toHaveLength(0);
    expect(folded.get('[data-im="card-unfold"]').text()).toBe("恢复卡片");
    expect(folded.get('[data-im="card-fold"]').text()).toBe("恢复卡片");
  });
});

describe("最小尺寸 180×96：标题 / 主要操作 / 必要状态 / 内容区都还在", () => {
  it("窄矮卡片收一档，但四样东西一个不少，主要操作仍然可点", async () => {
    const card = textCard({ w: 180, h: 96 });
    const w = mountBoard(card);
    const root = w.get('[data-im="card"]');

    // 容器尺寸来自卡片对象（板面状态），不是 transform 缩放
    const style = root.attributes("style") ?? "";
    expect(style).toContain("width: 180px");
    expect(style).toContain("height: 96px");
    expect(root.classes()).toContain("is-compact");
    expect(root.attributes("data-card-state")).toBe("normal");

    // 标题 + 必要状态
    expect(w.find('[data-im="card-title"]').exists()).toBe(true);
    expect(w.get('[data-im="card-license"]').text()).toBe("未允许查看");
    expect(w.get('[data-im="card-meta"]').text()).toContain("未许可");
    // 让位的是次级信息：材料类型标签与更新时间
    expect(w.find('[data-im="card-head"] .kind').exists()).toBe(false);
    expect(w.find('[data-im="card-meta"] .time').exists()).toBe(false);

    // 内容区
    expect(w.get('[data-im="card-body"]').text()).toContain("第一段正文。");
    // 正对照：卡片刻意放大后材料类型与更新时间都在（证明上面两条不是选择器写错导致空过）
    const wide = mountBoard(textCard({ w: 260, h: 170 }));
    expect(wide.find('[data-im="card-head"] .kind').exists()).toBe(true);
    expect(wide.find('[data-im="card-meta"] .time').exists()).toBe(true);
    expect(wide.get('[data-im="card"]').classes()).not.toContain("is-compact");

    // 主要操作都能点：编辑 → 有编辑器；更多 → 面板打开；展开预览 → 进入阅读态
    await w.get('[data-im="card-edit"]').trigger("click");
    expect(w.find('[data-im="card-editor"]').exists()).toBe(true);
    await buttonByText(w, "取消")!.trigger("click");

    await w.get('[data-im="card-more"]').trigger("click");
    expect(w.get('[data-im="card-more"]').attributes("aria-expanded")).toBe("true");

    await w.get('[data-im="card-preview-toggle"]').trigger("click");
    expect(root.attributes("data-card-state")).toBe("reading");
  });
});

describe("规则层守卫：高度预算与横向滚动（jsdom 没有真实排版）", () => {
  const card = source("components/interactive/BoardCard.vue");

  it("内容区拿得到剩余高度：头部与页脚固定，正文 flex: 1 且 min-height: 0", () => {
    const body = cssBlock(card, ".card-body {");
    expect(body).toContain("flex: 1 1 auto");
    expect(body).toContain("min-height: 0");
    expect(cssBlock(card, ".card-head {")).toContain("flex: none");
    expect(cssBlock(card, ".card-meta {")).toContain("flex: none");
    // 头部一行到底：标题省略号收尾，不会换成三行
    expect(cssBlock(card, ".card-head {")).toContain("flex-wrap: nowrap");
  });

  it("180×96 时收一档：内边距与间隙各收一档，把高度让给内容区", () => {
    expect(card).toContain(".card.is-compact { padding: var(--sp-2); gap: var(--sp-1); }");
  });

  /*
    规则变更（2026-10-14 统一滚动规则）：
    旧断言是「横向滚动只发生在代码块内部（.code-body 的 overflow-x: auto）+ 卡片正文 overflow-x: hidden」。

    为什么必须改：
    1. 「代码块自己横向滚动」= 藏起滚动条、内容还在左右滚，正是本轮要消灭的形态；
       而且代码块同时带 overflow-y: auto，于是阅读一段长代码要在
       .card-body → .cip-body → .code-body 三个小窗口里分别滚。
    2. 新规则：代码正文**不再自己滚动**（横纵都不），长行按视觉软换行
       （white-space: pre-wrap + overflow-wrap: anywhere，textContent 与复制逐字不变），
       纵向滚动只由卡片正文这一层承担；横向边界用共享的 data-qio-fit（overflow-x: clip，
       它不建立滚动容器）兜底，而不是靠 .card-body 的 overflow-x: hidden 把内容裁掉。
  */
  it("代码块不再自己滚动：软换行 + 纵向交给卡片正文这一层", () => {
    const code = cssBlock(card, ".code-body {");
    expect(code).toContain("white-space: pre-wrap");
    expect(code).toContain("overflow-wrap: anywhere");
    // 关键反转：横向、纵向都不再是它自己的滚动方向
    expect(code).not.toContain("overflow-x: auto");
    expect(code).not.toContain("overflow-y: auto");
    // 卡片正文是这张卡唯一的主要纵向视口，并且用统一 QIO 指示条
    const body = cssBlock(card, ".card-body {");
    expect(body).toContain("overflow-y: auto");
    expect(cssBlock(card, ".blk-code {")).toContain("max-width: 100%");
  });

  it("卡片本身不产生横向滚动；横向边界由 data-qio-fit 兜底", () => {
    const root = cssBlock(card, ".card {");
    expect(root).toContain("overflow: hidden");
    expect(root).not.toContain("overflow-x: auto");
    // data-qio-fit 挂在正文层：min-width/max-width + overflow-x: clip
    expect(card).toMatch(/data-qio-fit/);
    // 纵向视口只有一个：走共享指示条，且策略是 auto（实际溢出才出现）
    expect(card).toContain('v-qio-scrollbar="{ policy: \'auto\', axis: \'y\' }"');
  });

  it("颜色只用设计令牌（没有裸色值）", () => {
    expect(card).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(card).not.toMatch(/rgba?\(/);
  });
});

describe("整卡许可：未选中可见、勾选只改 checked、完整说明在详情里", () => {
  it("未选中也能辨认「未允许查看 / 允许 QIO 查看」", () => {
    const off = mountBoard(textCard(), { selected: false });
    expect(off.find('[data-im="card-toolbar"]').exists()).toBe(false);
    expect(off.get('[data-im="card-license"]').attributes("data-checked")).toBe("0");
    expect(off.get('[data-im="card-license"]').text()).toBe("未允许查看");
    expect(off.get('[data-im="card-meta"]').text()).toContain("未许可");
    off.unmount();

    const on = mountBoard(textCard({ checked: true }), { selected: false });
    expect(on.get('[data-im="card-license"]').attributes("data-checked")).toBe("1");
    expect(on.get('[data-im="card-license"]').text()).toBe("允许 QIO 查看");
    expect(on.get('[data-im="card-meta"]').text()).toContain("已许可");
  });

  it("勾选 / 取消只改 checked：不碰 hidden / deleted，也不改内容", async () => {
    const w = mountBoard(textCard());
    await w.get('[data-im="check"]').trigger("change");

    expect(w.emitted("toggle")).toEqual([["c1", "checked"]]);
    expect(w.emitted("patch")).toBeUndefined();
    expect(w.emitted("remove")).toBeUndefined();
    expect(w.emitted("duplicate")).toBeUndefined();
    expect(w.emitted("drag-start")).toBeUndefined();
  });

  it("查看 / 展开 / 编辑都不改许可：未许可的卡片不会因为「被看过」就变成可提交", async () => {
    const card = textCard();
    const w = mountBoard(card);
    await w.get('[data-im="card-preview-toggle"]').trigger("click");
    await w.get('[data-im="card-edit"]').trigger("click");
    await buttonByText(w, "取消")!.trigger("click");

    expect(w.emitted("toggle")).toBeUndefined();
    expect(w.emitted("patch")).toBeUndefined();
    expect(isVisibleCard(card), "未许可的内容不该进入提交范围").toBe(false);
  });

  it("勾选框只留短文案；完整规则放在可达的「说明」详情里（默认收起）", () => {
    const w = mountBoard(textCard());
    const check = w.get('[data-im="card-toolbar"] .check');
    expect(check.get(".check-label").text()).toBe("允许 QIO 查看");
    // 完整说明仍在同一处，点开「说明」就能读到（收起 ≠ 删掉）
    expect(check.text()).toContain("许可：允许 QIO 查看这张卡");
    expect(check.text()).toContain("只有你手动取消才失效");
    expect(check.text()).toContain("覆盖卡里的全部文字、代码与链接");
    const details = check.get("details.check-detail");
    expect(details.attributes("open"), "完整说明默认收起").toBeUndefined();
    expect(details.find("summary").text()).toBe("说明");
  });

  it("隐藏与删除都不进提交载荷相关视图（卡片仍然如实说自己被隐藏）", () => {
    const state = {
      ...emptyState("board_t"),
      cards: [
        mkCard("c_on", { content: "许可过的内容", checked: true }),
        mkCard("c_off", { content: "未许可的内容" }),
        mkCard("c_hidden", { content: "隐藏的内容", checked: true, hidden: true }),
        mkCard("c_deleted", { content: "删掉的内容", checked: true, deleted: true }),
      ],
    };
    expect(state.cards.filter(isVisibleCard).map((item) => item.id)).toEqual(["c_on"]);
    expect(localVisibleRange(state).cards.map((item) => item.id)).toEqual(["c_on"]);

    // 隐藏卡片在界面上仍然可读，但状态如实写明它已退出讨论范围
    const w = mountBoard(mkCard("c_hidden", { content: "隐藏的内容", checked: true, hidden: true }), { selected: false });
    expect(w.get('[data-im="card-subject"]').text()).toContain("隐藏的内容");
    expect(w.get('[data-im="card-meta"]').text()).toContain("已隐藏");
  });
});

describe("更多：低频操作收在一处，能力一个不少", () => {
  it("默认不铺开：面板存在但不占画面，点开才出现", async () => {
    const w = mountBoard(textCard());
    const more = w.get('[data-im="card-more"]');
    expect(more.attributes("aria-expanded")).toBe("false");
    expect(w.get('[data-im="card-more-panel"]').isVisible()).toBe(false);

    for (const hook of ["card-duplicate", "card-hide", "card-bookmark", "selection-menu", "delete-card"]) {
      expect(w.get('[data-im="card-more-panel"]').find('[data-im="' + hook + '"]').exists(), hook).toBe(true);
    }
    // 常用的编辑与折成标题留在近处，没有被收进面板
    expect(w.get('[data-im="card-toolbar"]').find('[data-im="card-edit"]').exists()).toBe(true);
    expect(w.get('[data-im="card-toolbar"]').find('[data-im="card-fold"]').exists()).toBe(true);

    await more.trigger("click");
    expect(w.get('[data-im="card-more"]').attributes("aria-expanded")).toBe("true");
    expect(w.get('[data-im="card-more-panel"]').isVisible()).toBe(true);
    // 工具栏在卡片上方（toolbarTop < y）时面板向上开 —— 朝卡片外侧，不盖标题与正文
    expect(w.get('[data-im="card-more-panel"]').classes()).toContain("up");

    const below = mountBoard(textCard(), { toolbarTop: 200 });
    await below.get('[data-im="card-more"]').trigger("click");
    expect(below.get('[data-im="card-more-panel"]').classes()).toContain("down");
  });

  it("面板里的每个动作都真的发出去，并且用完就收起", async () => {
    const w = mountBoard(textCard());

    await w.get('[data-im="card-more"]').trigger("click");
    await w.get('[data-im="card-duplicate"]').trigger("click");
    expect(w.emitted("duplicate")).toEqual([["c1"]]);
    expect(w.get('[data-im="card-more-panel"]').isVisible()).toBe(false);

    for (const [hook, flag] of [["card-hide", "hidden"], ["card-bookmark", "bookmarked"]] as const) {
      await w.get('[data-im="card-more"]').trigger("click");
      await w.get('[data-im="' + hook + '"]').trigger("click");
      expect(w.emitted("toggle")![w.emitted("toggle")!.length - 1]).toEqual(["c1", flag]);
      expect(w.get('[data-im="card-more-panel"]').isVisible()).toBe(false);
    }

    await w.get('[data-im="card-more"]').trigger("click");
    await w.get('[data-im="delete-card"]').trigger("click");
    expect(w.emitted("remove")).toEqual([["c1"]]);
  });

  it("Esc 只关这个面板，不让板面把它当成取消拖动", async () => {
    const w = mountBoard(textCard());
    await w.get('[data-im="card-more"]').trigger("click");
    expect(w.get('[data-im="card-more-panel"]').isVisible()).toBe(true);

    const seen: string[] = [];
    const onWindowKeydown = (event: KeyboardEvent) => seen.push(event.key);
    window.addEventListener("keydown", onWindowKeydown);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await w.vm.$nextTick();
    window.removeEventListener("keydown", onWindowKeydown);

    expect(w.get('[data-im="card-more-panel"]').isVisible()).toBe(false);
    expect(seen).toEqual([]);
  });
});

describe("附件预览的紧凑档（阅读展开时用）", () => {
  const fileCard = (patch: Record<string, unknown> = {}) =>
    mkCard("f9", {
      kind: "file",
      content: "说明文字",
      meta: { name: "预算表.pdf", file: { name: "预算表.pdf", attachmentId: "att_1", size: 4096 } },
      ...patch,
    });

  function mountAttach(card: BoardCardModel, compact: boolean) {
    wrapper = mount(CardAttachmentPreview, {
      props: { kind: card.kind, meta: card.meta, cardId: card.id, compact },
      attachTo: document.body,
    });
    return wrapper;
  }

  it("compact：只留「外部打开」入口，不再铺关联状态、缩略图与大段说明", () => {
    const w = mountAttach(fileCard(), true);
    expect(w.find('[data-im="card-attachment-open"]').exists()).toBe(true);
    expect(w.find('[data-im="card-attachment"]').exists()).toBe(false);
    expect(w.find('[data-im="card-attachment-preview-state"]').exists()).toBe(false);
    expect(w.find('[data-im="card-attachment-open-note"]').exists()).toBe(false);
    expect(w.get('[data-im="card-attachment-preview"]').classes()).toContain("compact");
  });

  it("默认档（不传 compact）：既有行为一字不变", () => {
    const w = mountAttach(fileCard(), false);
    expect(w.find('[data-im="card-attachment"]').exists()).toBe(true);
    expect(w.find('[data-im="card-attachment-preview-state"]').exists()).toBe(true);
    expect(w.find('[data-im="card-attachment-open-note"]').exists()).toBe(true);
  });

  it("图片缩略图保留比例：限宽不限高、等比缩放（不拉伸、不撑破卡片）", () => {
    const text = readFileSync(resolve(SRC, "components/interactive/CardAttachmentPreview.vue"), "utf8");
    const thumb = cssBlock(text, ".thumb {");
    expect(thumb).toContain("max-width: 100%");
    expect(thumb).toContain("height: auto");
    expect(thumb).toContain("object-fit: contain");
    expect(thumb).not.toContain("object-fit: fill");
  });
});

describe("阅读展开与可读材料：没有材料就不给假入口", () => {
  it("文件卡没有真实附件：不出现阅读展开入口，也不出现假的打开按钮", () => {
    const card = mkCard("f1", { kind: "file", content: "待补充说明", meta: { name: "预算表.pdf" } });
    const w = mountBoard(card, { selected: false });

    expect(w.find('[data-im="card-preview-toggle"]').exists()).toBe(false);
    expect(w.get('[data-im="card-subject"]').text()).toContain("还没有关联已保存的附件");
    expect(w.find('[data-im="card-attachment-open"]').exists()).toBe(false);
  });

  it("文件卡有真实附件：入口存在；阅读展开把主体让给预览，说明压成紧凑档", async () => {
    const card = mkCard("f2", {
      kind: "file",
      content: "这是一段应该被压缩的说明文字",
      meta: { name: "预算表.pdf", file: { name: "预算表.pdf", attachmentId: "att_1", size: 4096 } },
    });
    const w = mountBoard(card, { selected: false });
    expect(w.get('[data-im="card-preview-toggle"]').text()).toBe("展开预览");

    await w.get('[data-im="card-preview-toggle"]').trigger("click");
    expect(w.get('[data-im="card"]').attributes("data-card-state")).toBe("reading");

    // 说明与缩略图不再铺在预览上方：附件只留一行外部打开入口
    expect(w.get('[data-im="card-subject"]').classes()).toContain("is-compact");
    expect(w.get('[data-im="card-attachment-preview"]').classes()).toContain("compact");
    expect(w.find('[data-im="card-attachment-thumb"]').exists()).toBe(false);
    expect(w.find('[data-im="card-attachment-open"]').exists()).toBe(true);
    // 真实预览组件挂在卡片内部
    const host = w.get('[data-im="card-preview-host"]');
    expect(host.find('[data-testid="cip-root"]').exists()).toBe(true);
  });
});