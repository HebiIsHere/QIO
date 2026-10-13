/**
 * 统一文字卡编辑区的行为用例（契约 2026-10-12 §1.3 / §1.6）。
 *
 * 核心立场：**一个连续编辑区**。用户输入的 markdown 子集文本是编辑区的唯一格式，
 * 工具栏动作只改文本并把**选区还给用户**（原生 textarea 的 setSelectionRange），
 * 不重建 DOM、不自建撤销栈、不在「文字 / 链接 / 代码」之间切换控件。
 *
 * 标注：
 * - 【组件/DOM】挂载真实 TextCardEditor.vue 与真实 <textarea>；
 * - 【真实选区】用 textarea 自己的 setSelectionRange + input 事件驱动，不断言内部实现细节。
 */
import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { mount, type VueWrapper } from "@vue/test-utils";
import TextCardEditor from "../TextCardEditor.vue";
import { FENCE, parseDocText, serializeDocText, type TextDoc } from "../../interactive/textCard";
import type { BoardCard } from "../../interactive/types";

const STAMP = "2026-10-12T00:00:00.000Z";

function mkCard(patch: Partial<BoardCard> = {}): BoardCard {
  return {
    id: "c_text",
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

function mountEditor(patch: Partial<BoardCard> = {}, props: Record<string, unknown> = {}): VueWrapper {
  return mount(TextCardEditor, {
    props: { card: mkCard(patch), ...props },
    attachTo: document.body,
  });
}

function editor(vm: VueWrapper): HTMLTextAreaElement {
  return vm.get('[data-im="card-editor"]').element as HTMLTextAreaElement;
}

/** 真实 textarea 的输入：设选区 → 派发 input 事件（与用户按键同一条路径）。 */
async function typeAt(vm: VueWrapper, value: string, start: number, end = start): Promise<void> {
  const el = editor(vm);
  el.value = value;
  el.setSelectionRange(start, end);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  await vm.vm.$nextTick();
}

function lastChange(vm: VueWrapper): { docText: string; doc: TextDoc } {
  const events = vm.emitted("change");
  expect(events, "必须 emit change").toBeTruthy();
  return events![events!.length - 1][0] as { docText: string; doc: TextDoc };
}

/** 工具栏按钮：pointerdown 一律 preventDefault（不抢焦点），click 执行格式动作。 */
async function toolbar(vm: VueWrapper, action: string): Promise<void> {
  const btn = vm.get('[data-im="card-fmt-' + action + '"]');
  await btn.trigger("pointerdown");
  await btn.trigger("click");
}

describe("TextCardEditor：一个连续编辑区", () => {
  it("只有一个编辑面，承载 meta.docText；输入即 emit change { docText, doc }", async () => {
    const vm = mountEditor({ meta: { docText: "第一段\n" } });
    expect(vm.findAll("textarea")).toHaveLength(1);
    expect(editor(vm).value).toBe("第一段\n");

    await typeAt(vm, "第一段\n\n## 标题\n", 13);
    const payload = lastChange(vm);
    expect(payload.docText).toBe("第一段\n\n## 标题\n");
    expect(payload.doc.blocks.map((b) => b.kind)).toEqual(["paragraph", "heading"]);
    expect(serializeDocText(payload.doc)).toBe(payload.docText);
    vm.unmount();
  });

  it("没有 meta.docText 时用 meta.doc / content 兜底，绝不显示空编辑器把正文藏掉", () => {
    const fromDoc = mountEditor({
      meta: { doc: { schema: "qio.textcard/1", blocks: [{ id: "b1", kind: "paragraph", text: "来自块序列" }] } },
    });
    expect(editor(fromDoc).value).toContain("来自块序列");
    fromDoc.unmount();
    const fromContent = mountEditor({ content: "来自 content 的正文" });
    expect(editor(fromContent).value).toContain("来自 content 的正文");
    fromContent.unmount();
  });

  it("工具栏齐全：加粗 / 斜体 / 行内代码 / 删除线 / 标题2 / 列表 / 引用 / 链接 / 代码块", () => {
    const vm = mountEditor();
    const toolbarEl = vm.get('[data-im="card-editor-toolbar"]');
    for (const action of ["bold", "italic", "code", "strike", "heading2", "list", "quote", "link", "codeblock"]) {
      expect(toolbarEl.find('[data-im="card-fmt-' + action + '"]').exists(), action).toBe(true);
    }
    vm.unmount();
  });

  it("格式动作把选区还给用户：加粗后选区就是原来的字，并且可以继续输入", async () => {
    // 编辑区文本是规范写法（块后有空行），与其他用例同一口径
    const vm = mountEditor({ content: "abcdef" });
    expect(editor(vm).value).toBe("abcdef\n");
    editor(vm).setSelectionRange(1, 3);
    await toolbar(vm, "bold");

    const payload = lastChange(vm);
    expect(payload.docText).toBe("a**bc**def\n");
    expect(editor(vm).value).toBe("a**bc**def\n");
    // 选区就是加粗后的 "bc"：用户不必自己重新选
    expect([editor(vm).selectionStart, editor(vm).selectionEnd]).toEqual([3, 5]);

    // 还能接着在末尾输入（说明编辑面没有被重建、光标可用）
    await typeAt(vm, "a**bc**def！\n", 12);
    expect(lastChange(vm).docText).toBe("a**bc**def！\n");
    vm.unmount();
  });

  it("格式动作不改动选中的代码块正文（逐字保真）", async () => {
    const vm = mountEditor({ meta: { docText: "开头\n\n" + FENCE + "py\n  x = 1\n" + FENCE + "\n" } });
    const el = editor(vm);
    el.setSelectionRange(0, 2);
    await toolbar(vm, "bold");
    const doc = parseDocText(lastChange(vm).docText);
    const code = doc.blocks.find((b) => b.kind === "code")!;
    expect(code.text).toBe("  x = 1\n");
    expect(code.attrs?.language).toBe("py");
    vm.unmount();
  });

  it("点「行内代码」把选中文字包成一对反引号，仍然是一个连续文本", async () => {
    const vm = mountEditor({ content: "https://example.com 打开" });
    editor(vm).setSelectionRange(0, 19);
    await toolbar(vm, "code");
    const text = lastChange(vm).docText;
    const MARK = String.fromCharCode(96);
    expect(text).toBe(MARK + "https://example.com" + MARK + " 打开\n");
    // 反引号里的内容不会被当成链接块
    expect(parseDocText(text).blocks[0].kind).toBe("paragraph");
    vm.unmount();
  });

  it("插入链接：给出地址后 [标题](地址) 成为链接块，选区落在标题上", async () => {
    const vm = mountEditor({ content: "QIO 仓库" });
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue("https://github.com/HebiIsHere/QIO");
    // 选区是标题本体（不含块后空行）：编辑器得到的是规范文本 "QIO 仓库\n"
    editor(vm).setSelectionRange(0, "QIO 仓库".length);
    await toolbar(vm, "link");
    promptSpy.mockRestore();

    const payload = lastChange(vm);
    const doc = parseDocText(payload.docText);
    expect(doc.blocks[0].kind).toBe("link");
    expect(doc.blocks[0].text).toBe("QIO 仓库");
    expect(doc.blocks[0].attrs?.href).toBe("https://github.com/HebiIsHere/QIO");
    expect([editor(vm).selectionStart, editor(vm).selectionEnd]).toEqual([1, 1 + "QIO 仓库".length]);
    vm.unmount();
  });

  it("插入链接：用户取消输入地址时不静默失败，如实说明地址为空、链接打不开", async () => {
    const vm = mountEditor({ content: "标题" });
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue(null);
    editor(vm).setSelectionRange(0, 2);
    await toolbar(vm, "link");
    promptSpy.mockRestore();

    const doc = parseDocText(lastChange(vm).docText);
    expect(doc.blocks[0].kind).toBe("link");
    expect(doc.blocks[0].attrs?.href).toBe("");
    expect(vm.get('[data-im="card-fmt-note"]').text()).toContain("地址为空");
    vm.unmount();
  });

  it("插入代码块：一对围栏 + 语言，正文逐字保留", async () => {
    const vm = mountEditor({ meta: { docText: "" } });
    editor(vm).setSelectionRange(0, 0);
    await toolbar(vm, "codeblock");
    expect(lastChange(vm).docText).toBe(FENCE + "\n\n" + FENCE + "\n");

    // 在围栏之间写入，缩进与换行逐字保留
    const typed = FENCE + "python\n  def f():\n      return 1\n" + FENCE + "\n";
    await typeAt(vm, typed, typed.length);
    const doc = parseDocText(lastChange(vm).docText);
    expect(doc.blocks[0].kind).toBe("code");
    expect(doc.blocks[0].text).toBe("  def f():\n      return 1\n");
    expect(doc.blocks[0].attrs?.language).toBe("python");
    vm.unmount();
  });

  it("在代码块内继续输入、再把光标移到链接行、又回到正文：都是同一个 textarea", async () => {
    const text = "正文\n\n" + FENCE + "js\nlet a = 1;\n" + FENCE + "\n\n[示例](https://example.com)\n";
    const vm = mountEditor({ meta: { docText: text } });
    const el = editor(vm);

    const codeLine = text.indexOf("let a = 1;") + "let a = 1;".length;
    el.setSelectionRange(codeLine, codeLine);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    await vm.vm.$nextTick();
    expect(el.selectionStart).toBe(codeLine);

    const linkLine = text.indexOf("[示例]");
    el.setSelectionRange(linkLine, linkLine + 4);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    await vm.vm.$nextTick();
    expect([el.selectionStart, el.selectionEnd]).toEqual([linkLine, linkLine + 4]);

    await typeAt(vm, text + "尾巴", text.length + 2);
    const doc = lastChange(vm).doc;
    expect(doc.blocks.map((b) => b.kind)).toEqual(["paragraph", "code", "link", "paragraph"]);
    expect(doc.blocks[1].text).toBe("let a = 1;\n");
    vm.unmount();
  });

  it("原生撤销可用：组件不自建撤销栈、不拦截输入（撤销交给真实 textarea）", () => {
    const source = readFileSync(resolve(__dirname, "../TextCardEditor.vue"), "utf8");
    expect(source).not.toMatch(/undoStack|redoStack|execCommand\(\s*["']undo/);
    // 输入路径不得 preventDefault：拦掉输入事件就等于自己实现编辑历史
    expect(source).not.toMatch(/@input[\s\S]{0,80}(preventDefault|stopPropagation)/);
    // 选区还给用户用的是真实 textarea 的 setSelectionRange
    expect(source).toContain("setSelectionRange");
  });

  it("readonly 时不接受输入：编辑面只读、工具栏禁用", async () => {
    const vm = mountEditor({ content: "只读正文" }, { readonly: true });
    expect(editor(vm).readOnly).toBe(true);
    expect(vm.get('[data-im="card-fmt-bold"]').attributes("disabled")).toBeDefined();
    vm.unmount();
  });

  it("minHeight 只影响编辑区高度，不改变格式（显示参数）", () => {
    const vm = mountEditor({ content: "x" }, { minHeight: 220 });
    expect(editor(vm).style.minHeight || "").toContain("220");
    vm.unmount();
  });

  /*
    连续编辑区（本轮滚动规则）：编辑框**自己从不滚动**，高度跟随内容长 ——
    这样纵向只剩卡片正文这一层，不会出现「外层正文滚一段、内层编辑框再滚一段」。
    只改显示高度；文本、选区、撤销重做与提交内容都不受影响。
  */
  it("编辑框自己不滚动：overflow hidden + 输入后按内容自动增高（jsdom 无布局时不写高度）", async () => {
    const source = readFileSync(resolve(__dirname, "../TextCardEditor.vue"), "utf8");
    const rule = source.slice(source.indexOf(".editor {"), source.indexOf("}", source.indexOf(".editor {")) + 1);
    expect(rule).toContain("overflow: hidden");
    expect(rule).toContain("resize: vertical");

    const vm = mountEditor({ content: "第一行" });
    const area = editor(vm);
    // jsdom 里 scrollHeight 恒为 0：这时**不许**把高度写成 0（否则编辑区会被压没）
    Object.defineProperty(area, "scrollHeight", { value: 0, configurable: true });
    await area.dispatchEvent(new Event("input"));
    expect(area.style.height === "" || area.style.height === "auto").toBe(true);
    // 真实布局里 scrollHeight > 0：高度跟着内容走
    Object.defineProperty(area, "scrollHeight", { value: 240, configurable: true });
    await area.dispatchEvent(new Event("input"));
    expect(area.style.height).toBe("240px");
    vm.unmount();
  });

  it("编辑区容器带 data-qio-fit：横向边界由共享规则兜住，不靠裁切", () => {
    const vm = mountEditor({ content: "x" });
    expect(vm.get('[data-im="text-card-editor"]').attributes("data-qio-fit")).toBeDefined();
    vm.unmount();
  });
});

describe("TextCardEditor：外部更新跟随", () => {
  it("props.card 的正文被外部改掉时编辑面跟随（不留下旧文字）", async () => {
    const vm = mountEditor({ meta: { docText: "旧正文\n" } });
    expect(editor(vm).value).toBe("旧正文\n");
    await vm.setProps({ card: mkCard({ meta: { docText: "新正文\n" } }) });
    expect(editor(vm).value).toBe("新正文\n");
    vm.unmount();
  });
});
