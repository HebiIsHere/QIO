/**
 * F 独立验证（阶段一 · F13）：列表内的代码块 / 嵌套列表 / 表格结构被压平。
 *
 * 契约：docs/plans/2026-10-09-process-attachment-audit-consolidation.md §C1 相关（统一过程区 /
 * Markdown 渲染）。产品规则：列表项里的块级结构（嵌套列表 / 代码块 / 表格）必须保留结构，
 * 不得压成一段纯文本，更不得把内容整段丢掉。
 *
 * 真实组件：mount(MarkdownContent)。基线的 listItem 用 renderInline 渲染子节点，块级节点落到
 * 默认分支 → 嵌套 ul 丢失、代码块内容消失、表格结构消失。
 */
import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import MarkdownContent from "../MarkdownContent.vue";

describe("F13 列表内的块级结构", () => {
  it("列表项里的嵌套列表必须保留嵌套 ul", () => {
    const w = mount(MarkdownContent, {
      props: { source: "- 顶层\n  - 子项 A\n  - 子项 B" },
    });
    expect(w.find("ul ul").exists(), w.html()).toBe(true);
    expect(w.text()).toContain("子项 A");
    expect(w.text()).toContain("子项 B");
    w.unmount();
  });

  it("列表项里的代码块必须保留代码内容与结构", () => {
    const w = mount(MarkdownContent, {
      props: { source: "- 步骤：\n\n  \x60\x60\x60js\n  const a = 1;\n  \x60\x60\x60" },
    });
    expect(w.find(".code-block").exists(), w.html()).toBe(true);
    expect(w.find("pre code").exists(), w.html()).toBe(true);
    expect(w.text()).toContain("const a = 1;");
    w.unmount();
  });

  it("列表项里的表格必须保留 table 结构", () => {
    const w = mount(MarkdownContent, {
      props: { source: "- 表格：\n\n  | a | b |\n  | --- | --- |\n  | 1 | 2 |" },
    });
    expect(w.find(".table-wrap table").exists(), w.html()).toBe(true);
    expect(w.find("th").exists(), w.html()).toBe(true);
    w.unmount();
  });

  it("对照组：列表外的代码块与表格本来就能正常渲染", () => {
    const w = mount(MarkdownContent, {
      props: { source: "\x60\x60\x60js\nconst b = 2;\n\x60\x60\x60\n\n| x | y |\n| --- | --- |\n| 1 | 2 |" },
    });
    expect(w.find(".code-block").exists()).toBe(true);
    expect(w.find(".table-wrap table").exists()).toBe(true);
    w.unmount();
  });
});
