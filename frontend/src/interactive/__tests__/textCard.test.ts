/**
 * 统一文字卡内容模型的契约测试（Lead 维护）。
 *
 * 契约：docs/superpowers/plans/2026-10-12-unified-cards-media-preview-contract.md 第 1 节。
 * 三条性质必须成立：代码逐字保真、serialize(parse(t)) === t、块序稳定。
 */
import { describe, expect, it } from "vitest";
import {
  FENCE,
  blocksToText,
  cardDocFields,
  detectLanguage,
  docFromCard,
  emptyDoc,
  insertBlock,
  moveBlock,
  newBlock,
  normalizeDoc,
  parseDocText,
  removeBlock,
  serializeDocText,
  wrapSelection,
} from "../textCard";

const SAMPLE = [
  "普通文字",
  "",
  "## 标题",
  "",
  "- 一",
  "- 二",
  "",
  FENCE + "python",
  "  def f():",
  "      return 1",
  FENCE,
  "",
  "[QIO](https://example.com)",
  "",
].join("\n");

describe("统一文字卡内容模型", () => {
  it("混合内容解析成块序列", () => {
    const doc = parseDocText(SAMPLE);
    expect(doc.blocks.map((b) => b.kind)).toEqual(["paragraph", "heading", "list", "code", "link"]);
  });

  it("serialize(parse(t)) === t（规范写法）", () => {
    expect(serializeDocText(parseDocText(SAMPLE))).toBe(SAMPLE);
  });

  it("代码块逐字保真：缩进、换行、语言信息", () => {
    const before = parseDocText(SAMPLE);
    const code = before.blocks.find((b) => b.kind === "code")!;
    expect(code.text).toBe("  def f():\n      return 1\n");
    expect(code.attrs?.language).toBe("python");
    const again = parseDocText(serializeDocText(before));
    expect(again.blocks.map((b) => b.kind)).toEqual(before.blocks.map((b) => b.kind));
    expect(again.blocks.find((b) => b.kind === "code")!.text).toBe(code.text);
  });

  it("围栏内的 # / - / > 不会被当成块标记", () => {
    const text = FENCE + "md\n# 不是标题\n- 不是列表\n> 不是引用\n" + FENCE + "\n";
    const doc = parseDocText(text);
    expect(doc.blocks).toHaveLength(1);
    expect(doc.blocks[0].kind).toBe("code");
    expect(doc.blocks[0].text).toBe("# 不是标题\n- 不是列表\n> 不是引用\n");
    expect(serializeDocText(doc)).toBe(text);
  });

  it("未闭合围栏：读到末尾且如实记录问题", () => {
    const doc = parseDocText(FENCE + "js\nlet a = 1");
    expect(doc.blocks[0].kind).toBe("code");
    expect(doc.blocks[0].text).toBe("let a = 1\n");
    expect((doc.issues ?? []).join("")).toContain("未闭合");
  });

  it("行内的 [x](y) 不算链接块", () => {
    const doc = parseDocText("见 [QIO](https://example.com) 这一行");
    expect(doc.blocks).toHaveLength(1);
    expect(doc.blocks[0].kind).toBe("paragraph");
  });

  it("纯文本派生：标题 / 列表 / 代码 / 链接各按口径", () => {
    const plain = blocksToText(parseDocText(SAMPLE));
    expect(plain).toContain("## 标题");
    expect(plain).toContain("- 一");
    expect(plain).toContain("  def f():");
    expect(plain).toContain("QIO <https://example.com>");
  });

  it("卡片字段：content 是纯文本，meta.docText 是编辑区文本", () => {
    const fields = cardDocFields(parseDocText(SAMPLE));
    expect(fields.content).toContain("- 二");
    expect(fields.meta.docText).toBe(SAMPLE);
    expect(fields.meta.doc.blocks).toHaveLength(5);
  });

  it("docFromCard 的优先级：meta.doc > meta.docText > content", () => {
    const fromDoc = docFromCard({
      content: "旧的",
      meta: { doc: { schema: "qio.textcard/1", blocks: [newBlock("paragraph", "来自 doc")] } },
    });
    expect(fromDoc.blocks[0].text).toBe("来自 doc");
    const fromText = docFromCard({ content: "旧的", meta: { docText: "来自 docText\n" } });
    expect(fromText.blocks[0].text).toBe("来自 docText");
    const fromContent = docFromCard({ content: "来自 content" });
    expect(fromContent.blocks[0].text).toBe("来自 content");
  });

  it("损坏内容不丢：未知块保留为段落并记 issue", () => {
    const doc = normalizeDoc({ blocks: [{ kind: "weird", text: "x" }, "oops"] });
    // 损坏的数据结构本身也是原文：能读出来的字符串照样保留，绝不静默丢内容
    expect(doc.blocks.map((b) => b.text)).toEqual(["x", "oops"]);
    expect((doc.issues ?? []).length).toBeGreaterThanOrEqual(2);
    expect(normalizeDoc(null).blocks).toEqual([]);
  });

  it("块级操作：插入 / 删除 / 移动不改其他块", () => {
    const doc = parseDocText("一\n\n二\n\n三\n");
    const inserted = insertBlock(doc, 1, newBlock("paragraph", "插"));
    expect(inserted.blocks.map((b) => b.text)).toEqual(["一", "插", "二", "三"]);
    const removed = removeBlock(inserted, 1);
    expect(removed.blocks.map((b) => b.text)).toEqual(["一", "二", "三"]);
    const moved = moveBlock(doc, 0, 2);
    expect(moved.blocks.map((b) => b.text)).toEqual(["二", "三", "一"]);
  });

  it("工具栏格式：加粗 / 行内代码 / 链接 / 标题 / 列表 / 引用", () => {
    const bold = wrapSelection("abcdef", 1, 3, "bold");
    expect(bold.text).toBe("a**bc**def");
    expect(bold.selection).toEqual({ start: 3, end: 5 });
    const unbold = wrapSelection(bold.text, 3, 5, "bold");
    expect(unbold.text).toBe("abcdef");
    const code = wrapSelection("abc", 0, 3, "code");
    expect(code.text).toBe("`abc`");
    const link = wrapSelection("abc", 0, 3, "link", "https://x.dev");
    expect(link.text).toBe("[abc](https://x.dev)");
    expect(link.selection).toEqual({ start: 1, end: 4 });
    const heading = wrapSelection("标题\n正文", 0, 2, "heading2");
    expect(heading.text).toBe("## 标题\n正文");
    const list = wrapSelection("一\n二", 0, 3, "list");
    expect(list.text).toBe("- 一\n- 二");
    const quote = wrapSelection("引用", 0, 2, "quote");
    expect(quote.text).toBe("> 引用");
  });

  it("选中文字加格式后仍然是一个连续编辑区（可继续输入、可撤销）", () => {
    const text =
      "第一段\n\n" + FENCE + "py\nx = 1\n" + FENCE + "\n\n尾巴";
    const bolded = wrapSelection(text, 0, 3, "bold");
    const doc = parseDocText(bolded.text);
    expect(doc.blocks.map((b) => b.kind)).toEqual(["paragraph", "code", "paragraph"]);
    expect(doc.blocks[1].text).toBe("x = 1\n");
    expect(doc.blocks[2].text).toBe("尾巴");
  });

  it("扩展名 → 语言", () => {
    expect(detectLanguage("a/b/main.py")).toBe("python");
    expect(detectLanguage("App.VUE")).toBe("xml");
    expect(detectLanguage("Dockerfile")).toBe("dockerfile");
    expect(detectLanguage("noext")).toBe("");
  });

  it("emptyDoc 是合法空文档", () => {
    expect(emptyDoc().blocks).toEqual([]);
    expect(serializeDocText(emptyDoc())).toBe("");
  });
});
