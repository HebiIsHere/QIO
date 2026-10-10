/**
 * F13 反例（acc-c）：列表里的代码块 / 表格 / 嵌套列表 / 引用必须按块语义渲染。
 *
 * 复现：独立代码块正常，但列表项内的 fenced code 消失、嵌套列表被拼成文本、
 * 列表中的表格只剩文字没有 table 结构。
 */
import { describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import MarkdownContent from "../MarkdownContent.vue";

function render(source: string) {
  return mount(MarkdownContent, { props: { source } });
}

describe("F13：列表内的块级结构", () => {
  it("有序列表项里的 fenced code 必须渲染成代码块（不是消失）", () => {
    const src = ["1. 第一步：", "", "   ```js", "   const a = 1;", "   ```", "", "2. 第二步"].join("\n");
    const w = render(src);
    const ol = w.find("ol");
    expect(ol.exists()).toBe(true);
    const firstLi = ol.findAll("li")[0]!;
    expect(firstLi.find(".code-block").exists(), "列表项里的代码块不能消失").toBe(true);
    expect(firstLi.find("pre code").text()).toContain("const a = 1;");
    // 第二步也还在
    expect(ol.text()).toContain("第二步");
    w.unmount();
  });

  it("无序嵌套列表保持嵌套结构（不是拼接文本）", () => {
    const src = ["- 外层", "  - 内层一", "  - 内层二", "- 另一个外层"].join("\n");
    const w = render(src);
    const outer = w.find("ul");
    expect(outer.exists()).toBe(true);
    const inner = outer.find("li ul");
    expect(inner.exists(), "嵌套列表必须是真的 ul").toBe(true);
    expect(inner.findAll("li").map((li) => li.text())).toEqual(["内层一", "内层二"]);
    w.unmount();
  });

  it("列表项里的表格保留 table 结构（文字存在不等于结构正确）", () => {
    const src = [
      "- 说明：",
      "",
      "  | 列 A | 列 B |",
      "  | --- | --- |",
      "  | 1 | 2 |",
    ].join("\n");
    const w = render(src);
    const li = w.find("li");
    expect(li.exists()).toBe(true);
    expect(li.find(".table-wrap table").exists(), "列表里的表格必须有 table 结构").toBe(true);
    expect(li.findAll("th").map((th) => th.text())).toEqual(["列 A", "列 B"]);
    expect(li.findAll("td").map((td) => td.text())).toEqual(["1", "2"]);
    w.unmount();
  });

  it("列表项里的引用保持 blockquote 结构", () => {
    const src = ["- 说明：", "", "  > 引用一句"].join("\n");
    const w = render(src);
    expect(w.find("li blockquote").exists()).toBe(true);
    expect(w.find("li blockquote").text()).toContain("引用一句");
    w.unmount();
  });

  it("列表项里的多段正文仍然是段落（不压平成一行）", () => {
    const src = ["- 第一段", "", "  第二段"].join("\n");
    const w = render(src);
    const li = w.find("li");
    expect(li.findAll("p").length).toBeGreaterThanOrEqual(2);
    w.unmount();
  });
});

describe("F13：既有行为不回归", () => {
  it("独立的代码块、表格、列表仍然正常", () => {
    const w = render("\n```js\nconst a = 1;\n```\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n");
    expect(w.find(".code-block pre code").text()).toContain("const a = 1;");
    expect(w.find(".table-wrap table").exists()).toBe(true);
    w.unmount();
  });

  it("流式未闭合的围栏 / 列表 / 表格不把内容藏起来", () => {
    const cases = [
      "说明文字\n```python\nprint(1)\n",
      "步骤：\n- 第一步\n- 第二步\n- 第三",
      "| 列 A | 列 B |\n| --- | --- |\n| 1 | 2 ",
      "1. 第一步\n   ```js\n   const a = 1;\n",
    ];
    for (const src of cases) {
      const w = render(src);
      expect(w.text().trim().length).toBeGreaterThan(0);
      expect(w.text()).not.toContain("```");
      w.unmount();
    }
  });

  it("列表内代码块的复制按钮复制的是原始代码", async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const src = ["1. 第一步", "", "   ```js", "   const a = 1;", "   ```"].join("\n");
    const w = render(src);
    const btn = w.find("li .code-copy");
    expect(btn.exists()).toBe(true);
    await btn.trigger("click");
    expect(writeText).toHaveBeenCalledWith("const a = 1;");
    w.unmount();
  });
});
