import { describe, expect, it, vi, afterEach } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import MarkdownContent from "../MarkdownContent.vue";

afterEach(() => {
  vi.useRealTimers();
});

describe("MarkdownContent 流式增量（问题11）", () => {
  it("非 reveal 模式始终渲染完整 source", () => {
    const w = mount(MarkdownContent, { props: { source: "Hello world" } });
    expect(w.text()).toContain("Hello world");
    w.unmount();
  });

  it("reveal：source 增长时不清空重播，只继续点亮新增部分", async () => {
    vi.useFakeTimers();
    const w = mount(MarkdownContent, { props: { source: "Hello", reveal: true, cps: 50 } });
    vi.advanceTimersByTime(500);
    await nextTick();
    expect(w.html()).toContain("Hello");

    await w.setProps({ source: "Hello world" });
    await nextTick();
    // 旧实现：shown=0 → 内容瞬间清空重播；新实现：已点亮部分保持
    expect(w.html()).toContain("Hello");

    vi.advanceTimersByTime(500);
    await nextTick();
    expect(w.html()).toContain("world");
    w.unmount();
  });

  it("reveal：source 被替换（不是追加）时从头点亮", async () => {
    vi.useFakeTimers();
    const w = mount(MarkdownContent, { props: { source: "Hello", reveal: true, cps: 50 } });
    vi.advanceTimersByTime(500);
    await nextTick();
    await w.setProps({ source: "完全不同" });
    await nextTick();
    // 替换语义：允许从 0 重新点亮（此时还没有内容）
    expect(w.text()).not.toContain("Hello");
    vi.advanceTimersByTime(500);
    await nextTick();
    expect(w.text()).toContain("完全不同");
    w.unmount();
  });

  it("reveal 关闭后显示完整内容", async () => {
    vi.useFakeTimers();
    const w = mount(MarkdownContent, { props: { source: "Hello", reveal: true, cps: 50 } });
    expect(w.text()).not.toContain("Hello");
    await w.setProps({ reveal: false });
    await nextTick();
    expect(w.text()).toContain("Hello");
    w.unmount();
  });
});

describe("MarkdownContent 长内容与代码块（任务02 §8/§9）", () => {
  it("代码块带复制按钮，点击复制原始代码并给出反馈", async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const src = "```js\nconst a = 1;\n```";
    const w = mount(MarkdownContent, { props: { source: src } });
    const btn = w.find(".code-copy");
    expect(btn.exists()).toBe(true);
    await btn.trigger("click");
    expect(writeText).toHaveBeenCalledWith("const a = 1;");
    expect(w.find(".code-copy").text()).toContain("已复制");
    w.unmount();
  });

  it("表格包在可横向滚动的容器里（窄窗口不撑破气泡）", () => {
    const w = mount(MarkdownContent, {
      props: { source: "| a | b |\n| --- | --- |\n| 1 | 2 |" },
    });
    expect(w.find(".table-wrap").exists()).toBe(true);
    expect(w.find(".table-wrap table").exists()).toBe(true);
    w.unmount();
  });

  it("长 URL 与中英混排不产生横向溢出（渲染层保留原样，靠 CSS 折行）", () => {
    const src = "https://example.com/" + "a".repeat(200) + " 中英mixed文字";
    const w = mount(MarkdownContent, { props: { source: src } });
    expect(w.find("a").exists()).toBe(true);
    expect(w.text()).toContain("中英mixed文字");
    w.unmount();
  });
});
