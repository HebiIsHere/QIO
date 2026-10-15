/**
 * iml-b：结果卡 Markdown 渲染（契约 F3 / 任务 B1-B7）。
 *
 * 覆盖三件事（每一件都是本轮**新**要求，不是既有行为的复述）：
 * 1. MarkdownContent 的元素齐全性与宽度/滚动约束：标题 / 加粗 / 斜体 / 删除线 / 列表 /
 *    **任务列表** / 引用 / 链接 / 行内代码 / 代码块 / 表格；宽表格与超长正文完整渲染；
 *    渲染器自身不建滚动容器、不含 overflow-x: auto|scroll；模型原文不被当可信 HTML。
 * 2. BoardCard 的结果卡（kind="reply"）正文走 MarkdownContent，**不**走普通文字 p.content；
 *    未选中的常态与 reading 态都要渲染；正文仍是唯一纵向阅读区（只有一个 QIO 滚动宿主）。
 * 3. 「复制原文」给出完整 Markdown 原文（代码块复制仍是代码原文）；允许 QIO 查看的文案
 *    对文字卡与结果卡都成立；正文更新 / 阅读切换不改变卡片外框、不触发板面变更。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import BoardCard from "../BoardCard.vue";
import MarkdownContent from "../../MarkdownContent.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
import { cardDraftKey } from "../../../interactive/drafts";
import type { BoardCard as BoardCardModel } from "../../../interactive/types";

const STAMP = "2026-10-15T00:00:00.000Z";
/** 反引号用常量拼：测试源码里不出现三连反引号，避免被 Markdown 工具链误读 */
const B = String.fromCharCode(96);
const FENCE = B + B + B;
/** 围栏正文：结尾换行属于闭合围栏那一行，解析后不再留在代码原文里（既有用例同口径） */
const PY_CODE = "  def f():\n      return 1";
const PY_BLOCK = FENCE + "python\n" + PY_CODE + "\n" + FENCE;

const REPLY_MD = [
  "# 结论",
  "",
  "**要点**：链路可用。",
  "",
  "- 第一步",
  "- 第二步",
  "",
  "- [x] 已验证",
  "- [ ] 待复核",
  "",
  "> 依据：本机日志",
  "",
  "行内 " + B + "qio status" + B + " 可用。",
  "",
  PY_BLOCK,
  "",
  "[文档](https://example.com/doc)",
  "",
  "| 项 | 值 |",
  "| --- | --- |",
  "| 状态 | 正常 |",
].join("\n");

function source(relative: string): string {
  return readFileSync(resolve(process.cwd(), "src", relative), "utf8");
}

function mkCard(id: string, patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return {
    id,
    kind: "text",
    content: "",
    meta: {},
    x: 40,
    y: 60,
    w: 320,
    h: 240,
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

function mountCard(card: BoardCardModel, selected = true): VueWrapper {
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

afterEach(() => {
  Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

/* =========================================================================
 * 1. MarkdownContent：结果卡需要的元素与宽度约束（B2 / B3 / B5）
 * ======================================================================= */

describe("MarkdownContent：结果卡正文需要的 Markdown 元素齐全（F3 / B2）", () => {
  it("标题 / 加粗 / 斜体 / 删除线 / 无序与有序列表 / 引用 / 行内代码", () => {
    const src = [
      "# 一级标题",
      "## 二级标题",
      "### 三级标题",
      "",
      "**粗体** *斜体* ~~删除线~~ " + B + "行内代码" + B,
      "",
      "- 无序一",
      "- 无序二",
      "",
      "1. 有序一",
      "2. 有序二",
      "",
      "> 引用一句",
    ].join("\n");
    const w = mount(MarkdownContent, { props: { source: src } });
    expect(w.find("h1").text()).toBe("一级标题");
    expect(w.find("h2").text()).toBe("二级标题");
    expect(w.find("h3").text()).toBe("三级标题");
    expect(w.find("strong").text()).toBe("粗体");
    expect(w.find("em").text()).toBe("斜体");
    expect(w.find("del").text()).toBe("删除线");
    expect(w.find("code.inline").text()).toBe("行内代码");
    expect(w.findAll("ul li")).toHaveLength(2);
    expect(w.findAll("ol li")).toHaveLength(2);
    expect(w.find("blockquote").text()).toContain("引用一句");
    w.unmount();
  });

  it("GFM 任务列表：复选框只读、状态与无障碍标签如实、文字可读", () => {
    const w = mount(MarkdownContent, { props: { source: "- [x] 已完成的事\n- [ ] 还没做的事" } });
    const boxes = w.findAll("input.task-checkbox");
    expect(boxes).toHaveLength(2);
    expect(w.find("ul").classes()).toContain("contains-task-list");
    expect(w.findAll("li.task-list-item")).toHaveLength(2);
    // 只读：这是模型产出的呈现，用户不能借复选框改写原文
    expect((boxes[0].element as HTMLInputElement).disabled).toBe(true);
    expect((boxes[1].element as HTMLInputElement).disabled).toBe(true);
    expect((boxes[0].element as HTMLInputElement).checked).toBe(true);
    expect((boxes[1].element as HTMLInputElement).checked).toBe(false);
    // 可访问：状态有两通道（勾选图形 + 文字标签），不只靠颜色
    expect(boxes[0].attributes("aria-label")).toBe("已完成");
    expect(boxes[1].attributes("aria-label")).toBe("未完成");
    expect(w.text()).toContain("已完成的事");
    expect(w.text()).toContain("还没做的事");
    w.unmount();
  });

  it("代码块：语言、复制按钮都在，复制出来的是代码原文", async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const w = mount(MarkdownContent, { props: { source: PY_BLOCK } });
    expect(w.find(".code-block").attributes("data-lang")).toBe("python");
    // 逐字比较用 textContent：VTU 的 .text() 会 trim，缩进恰好会被吃掉
    expect((w.find("pre code").element as HTMLElement).textContent).toBe(PY_CODE);
    await w.find(".code-copy").trigger("click");
    expect(writeText).toHaveBeenCalledWith(PY_CODE);
    w.unmount();
  });

  it("链接：白名单 scheme 可点；危险 scheme 只给纯文本、不带 href", () => {
    const w = mount(MarkdownContent, {
      props: { source: "[站](https://example.com) [坏](javascript:alert(1))" },
    });
    expect(w.find("a.ext-link").attributes("href")).toBe("https://example.com");
    expect(w.findAll("a")).toHaveLength(1);
    expect(w.find(".link-blocked").text()).toBe("坏");
    expect(w.html()).not.toContain("javascript:alert");
    w.unmount();
  });

  it("宽表格：结构完整、受正文宽度约束、没有横向滚动容器", () => {
    const src = [
      "| 一 | 二 | 三 | 四 | 五 | 六 |",
      "| --- | --- | --- | --- | --- | --- |",
      "| 甲乙丙丁 | " + "a".repeat(40) + " | c | d | e | f |",
    ].join("\n");
    const w = mount(MarkdownContent, { props: { source: src } });
    const wrap = w.find(".qio-table-wrap");
    expect(wrap.exists()).toBe(true);
    expect(wrap.attributes("data-cols")).toBe("6");
    expect(wrap.attributes("data-qio-fit")).toBeDefined();
    expect(w.findAll("th")).toHaveLength(6);
    expect(w.findAll("td")).toHaveLength(6);
    expect(w.text()).toContain("甲乙丙丁");
    // 不引入横向滚动：既没有 QIO 滚动宿主，也没有 overflow-x: auto|scroll
    expect(w.find(".qio-scroll-host").exists()).toBe(false);
    expect(w.html()).not.toMatch(/overflow-x\s*:\s*(auto|scroll)/);
    w.unmount();
  });

  it("超长正文（>600 字，含围栏 / 列表 / 表格）逐字完整渲染，不截半段也不假称完整", () => {
    const filler = "长".repeat(620);
    const src = [
      "# 结论",
      "",
      filler,
      "",
      "- 要点一",
      "- 要点二",
      "",
      FENCE + "js",
      "const value = 1;",
      FENCE,
      "",
      "| 列 A | 列 B |",
      "| --- | --- |",
      "| 甲 | 乙 |",
    ].join("\n");
    const w = mount(MarkdownContent, { props: { source: src } });
    expect(w.find("h1").text()).toBe("结论");
    expect(w.text()).toContain(filler);
    expect(w.findAll("ul li")).toHaveLength(2);
    expect(w.find("pre code").text()).toBe("const value = 1;");
    expect(w.find("table td").text()).toBe("甲");
    // 围栏被解析成结构（不是把半段围栏原文留在正文里）
    expect(w.text()).not.toContain(FENCE);
    w.unmount();
  });

  it("模型原文不当可信 HTML：script / img onerror 都不产生元素", () => {
    const w = mount(MarkdownContent, {
      props: { source: "<script>alert(1)</" + "script>\n\n<img src=x onerror=\"alert(2)\">" },
    });
    expect(w.find("script").exists()).toBe(false);
    expect(w.find("img").exists()).toBe(false);
    w.unmount();
  });

  it("渲染器自身不建滚动容器（B3）：源码里没有 overflow-y/x 的 auto|scroll", () => {
    const src = source("components/MarkdownContent.vue");
    expect(src).toMatch(/overflow-x:\s*clip/);
    expect(src).not.toMatch(/overflow-x\s*:\s*(auto|scroll)/);
    expect(src).not.toMatch(/overflow-y\s*:\s*(auto|scroll)/);
    // 根节点只带 data-qio-fit（边界保护），滚动由容器决定
    expect(src).toMatch(/class="markdown-body" data-qio-fit/);
  });
});

/* =========================================================================
 * 2. BoardCard：结果卡正文（B1 / B3 / B5 / B7）
 * ======================================================================= */

describe("BoardCard：结果卡正文用 MarkdownContent 渲染（F3 / B1 / B3）", () => {
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
  });

  function seed(card: BoardCardModel) {
    store.board = { ...emptyState("board_t"), cards: [card], selection: [card.id] } as never;
  }

  it("未选中的常态：结果卡正文走 MarkdownContent，不再走普通文字分支", () => {
    const card = mkCard("r1", { kind: "reply", content: REPLY_MD });
    seed(card);
    wrapper = mountCard(card, false);
    expect(wrapper.get('[data-im="card"]').attributes("data-card-state")).toBe("normal");
    const body = wrapper.find('[data-im="card-reply-markdown"]');
    expect(body.exists()).toBe(true);
    // 普通文字分支（p.content + .desc）不该再出现
    expect(wrapper.find(".card-subject .content").exists()).toBe(false);
  });

  it("Markdown 结构真的渲染出来：标题 / 列表 / 任务列表 / 引用 / 行内代码 / 代码 / 链接 / 表格", () => {
    const card = mkCard("r2", { kind: "reply", content: REPLY_MD });
    seed(card);
    wrapper = mountCard(card, false);
    const body = wrapper.get('[data-im="card-reply-markdown"]');
    expect(body.find("h1").text()).toBe("结论");
    expect(body.find("strong").text()).toBe("要点");
    expect(body.findAll("ul li").length).toBeGreaterThanOrEqual(4);
    expect(body.findAll("input.task-checkbox")).toHaveLength(2);
    expect(body.find("blockquote").text()).toContain("依据");
    expect(body.find("code.inline").text()).toBe("qio status");
    expect((body.find("pre code").element as HTMLElement).textContent).toBe(PY_CODE);
    expect(body.find("a.ext-link").attributes("href")).toBe("https://example.com/doc");
    expect(body.find("table td").text()).toBe("状态");
    // 没有 reveal：挂载即可见完整正文（不是逐字点亮的空壳）
    expect(body.text()).toContain("正常");
  });

  it("reading 态仍渲染同一份 Markdown，且只有卡片正文一个纵向滚动宿主", async () => {
    const card = mkCard("r3", { kind: "reply", content: REPLY_MD });
    seed(card);
    wrapper = mountCard(card);
    await wrapper.get('[data-im="card-preview-toggle"]').trigger("click");
    await flushPromises();
    expect(wrapper.get('[data-im="card"]').attributes("data-card-state")).toBe("reading");
    const body = wrapper.get('[data-im="card-reply-markdown"]');
    expect(body.find("h1").text()).toBe("结论");
    expect(body.find("table").exists()).toBe(true);
    // 唯一纵向阅读区：整个卡片里只有一个 QIO 滚动宿主，且它是 .card-body
    const hosts = wrapper.findAll(".qio-scroll-host");
    expect(hosts).toHaveLength(1);
    expect(hosts[0].attributes("data-im")).toBe("card-body");
  });

  it("正文更新与阅读切换不改变卡片外框、不产生板面变更（B7 / F1）", async () => {
    const card = mkCard("r4", { kind: "reply", content: "# 第一版" });
    seed(card);
    wrapper = mountCard(card);
    const cardEl = wrapper.get('[data-im="card"]');
    const before = cardEl.attributes("style");
    expect(before).toContain("width: 320px");
    expect(before).toContain("height: 240px");

    // 异步到达 / 正文更新（模型后续写入、板面刷新）
    await wrapper.setProps({ card: { ...card, content: REPLY_MD } });
    await flushPromises();
    expect(wrapper.get('[data-im="card"]').attributes("style")).toBe(before);
    expect(wrapper.get('[data-im="card-reply-markdown"]').find("table").exists()).toBe(true);

    // 阅读展开只换查看方式，不改外框
    await wrapper.get('[data-im="card-preview-toggle"]').trigger("click");
    await flushPromises();
    expect(wrapper.get('[data-im="card"]').attributes("style")).toBe(before);
    // 渲染本身不发出任何板面变更（不触发整板重排）
    expect(wrapper.emitted("patch")).toBeUndefined();
    expect(wrapper.emitted("move")).toBeUndefined();
  });

  it("没有正文时如实说还没有正文，不渲染空 Markdown", () => {
    const card = mkCard("r5", { kind: "reply", content: "   " });
    seed(card);
    wrapper = mountCard(card);
    expect(wrapper.find('[data-im="card-reply-markdown"]').exists()).toBe(false);
    expect(wrapper.get('[data-im="card-reply-empty"]').text()).toContain("还没有正文");
  });
});

/* =========================================================================
 * 3. 复制原文 / 许可文案（B4 / B6）
 * ======================================================================= */

describe("结果卡：复制原文与许可文案（B4 / B6）", () => {
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
  });

  function seed(card: BoardCardModel) {
    store.board = { ...emptyState("board_t"), cards: [card], selection: [card.id] } as never;
  }

  it("「复制原文」把完整 Markdown 原文交给剪贴板（不是卡片编号、不是截断片段）", async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const card = mkCard("c1", { kind: "reply", content: REPLY_MD });
    seed(card);
    wrapper = mountCard(card);
    await wrapper.get('[data-im="card-copy-source"]').trigger("click");
    await flushPromises();
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith(REPLY_MD);
    expect(wrapper.get('[data-im="card-copy-source"]').text()).toContain("已复制原文");
  });

  it("剪贴板不可用时如实说「复制失败」，并给出手动复制退路", async () => {
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    const card = mkCard("c2", { kind: "reply", content: REPLY_MD });
    seed(card);
    wrapper = mountCard(card);
    const btn = wrapper.get('[data-im="card-copy-source"]');
    await btn.trigger("click");
    await flushPromises();
    expect(btn.text()).toContain("复制失败");
    expect(btn.attributes("title") ?? "").toContain("手动复制");
  });

  it("没有正文时「复制原文」不可用（不假装复制出内容）", () => {
    const card = mkCard("c3", { kind: "reply", content: "" });
    seed(card);
    wrapper = mountCard(card);
    expect(wrapper.get('[data-im="card-copy-source"]').attributes("disabled")).toBeDefined();
  });

  it("卡片正文里的代码块复制仍然是代码原文（与整卡「复制原文」两个层级）", async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const card = mkCard("c4", { kind: "reply", content: REPLY_MD });
    seed(card);
    wrapper = mountCard(card);
    await wrapper.get('[data-im="card-reply-markdown"] .code-copy').trigger("click");
    await flushPromises();
    expect(writeText).toHaveBeenCalledWith(PY_CODE);
  });

  it("许可文案对文字卡与结果卡都成立（工具栏说明与整卡徽章）", () => {
    const card = mkCard("t1", {
      kind: "text",
      content: "注释",
      checked: true,
      meta: { doc: { schema: "qio.textcard/1", blocks: [] }, docText: "" },
    });
    store.board = { ...emptyState("board_t"), cards: [card], selection: [card.id] } as never;
    store.setDraft(cardDraftKey("t1"), "");
    wrapper = mountCard(card);
    const check = wrapper.get('[data-im="card-toolbar"] .check');
    expect(check.text()).toContain("许可：允许 QIO 查看这张卡");
    expect(check.text()).toContain("文字卡与结果卡");
    expect(check.text()).toContain("只有你手动取消才失效");
    const license = wrapper.get('[data-im="card-license"]');
    expect(license.attributes("title") ?? "").toContain("文字卡与结果卡");
  });
});
