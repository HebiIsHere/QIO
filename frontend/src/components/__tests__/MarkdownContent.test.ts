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

describe("MarkdownContent 复制的诚实反馈（P0）", () => {
  it("写入被拒绝：显示「复制失败」并带失败样式", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn(async () => { throw new Error("denied"); }) },
    });
    const w = mount(MarkdownContent, { props: { source: "```js\nconst a = 1;\n```" } });
    await w.find(".code-copy").trigger("click");
    await new Promise((r) => setTimeout(r, 0));
    expect(w.find(".code-copy").text()).toBe("复制失败");
    expect(w.find(".code-copy").classes()).toContain("fail");
    w.unmount();
  });

  it("剪贴板 API 不存在：显示「复制失败」，不能显示已复制", async () => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    const w = mount(MarkdownContent, { props: { source: "```js\nconst a = 1;\n```" } });
    await w.find(".code-copy").trigger("click");
    await new Promise((r) => setTimeout(r, 0));
    expect(w.find(".code-copy").text()).toBe("复制失败");
    w.unmount();
  });

  it("失败提示会在短暂停留后回到「复制」，避免按钮永久停在失败态", async () => {
    vi.useFakeTimers();
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    const w = mount(MarkdownContent, { props: { source: "```js\nconst a = 1;\n```" } });
    await w.find(".code-copy").trigger("click");
    await vi.advanceTimersByTimeAsync(2500);
    expect(w.find(".code-copy").text()).toBe("复制");
    w.unmount();
  });

  it("连续复制不同代码块：各自反馈独立，先点的那个也会自己复位", async () => {
    vi.useFakeTimers();
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const src = "```js\nconst a = 1;\n```\n\n```js\nconst b = 2;\n```";
    const w = mount(MarkdownContent, { props: { source: src } });
    const buttons = w.findAll(".code-copy");
    expect(buttons.length).toBe(2);

    await buttons[0].trigger("click");
    await vi.advanceTimersByTimeAsync(50);
    await buttons[1].trigger("click");
    // 两块都显示已复制（各自独立）
    expect(buttons[0].text()).toBe("已复制");
    expect(buttons[1].text()).toBe("已复制");

    await vi.advanceTimersByTimeAsync(1700);
    // 第一块的计时器到点后必须自己复位，不能被第二块的状态卡住
    expect(w.findAll(".code-copy")[0].text()).toBe("复制");
    expect(w.findAll(".code-copy")[1].text()).toBe("复制");
    expect(writeText).toHaveBeenCalledTimes(2);
    w.unmount();
  });

  it("复制失败后仍可再点一次重试，并在再次失败时继续给出失败反馈", async () => {
    const writeText = vi.fn(async () => { throw new Error("denied"); });
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const w = mount(MarkdownContent, { props: { source: "```js\nconst a = 1;\n```" } });
    const btn = w.find(".code-copy");
    await btn.trigger("click");
    await new Promise((r) => setTimeout(r, 0));
    expect(btn.text()).toBe("复制失败");
    await btn.trigger("click");
    await new Promise((r) => setTimeout(r, 0));
    expect(btn.text()).toBe("复制失败");
    expect(writeText).toHaveBeenCalledTimes(2);
    w.unmount();
  });
});

describe("MarkdownContent 生成速度与未闭合块（任务03 C）", () => {
  it("给了真实到达间隔时按它走：paceMs=200 的追加在 200ms 内显示完", async () => {
    vi.useFakeTimers();
    const w = mount(MarkdownContent, { props: { source: "开头", reveal: true, cps: 25, paceMs: 200 } });
    await vi.advanceTimersByTimeAsync(250);
    await w.setProps({ source: "开头加上新到达的一段文字" });
    await vi.advanceTimersByTimeAsync(90);
    // 90ms 时还没走完（说明没有瞬显、确实按 200ms 节奏）
    expect(w.text()).not.toContain("新到达的一段文字");
    await vi.advanceTimersByTimeAsync(160);
    expect(w.text()).toContain("新到达的一段文字");
    w.unmount();
  });

  it("长回答及时追上：一次追加 300 字在 600ms 内显示完，不拖成几秒", async () => {
    vi.useFakeTimers();
    const w = mount(MarkdownContent, { props: { source: "开头", reveal: true, cps: 25 } });
    await vi.advanceTimersByTimeAsync(200);
    const long = "开头" + "长".repeat(300);
    await w.setProps({ source: long });
    await vi.advanceTimersByTimeAsync(650);
    expect(w.text()).toContain("长".repeat(300));
    w.unmount();
  });

  it("未闭合的代码块/列表/表格不会把整段内容藏起来", async () => {
    const cases = [
      "说明文字\n```python\nprint(1)\n", // 未闭合围栏
      "步骤：\n- 第一步\n- 第二步\n- 第三", // 未闭合列表
      "| 列 A | 列 B |\n| --- | --- |\n| 1 | 2 ", // 未闭合表格
    ];
    for (const src of cases) {
      const w = mount(MarkdownContent, { props: { source: src } });
      expect(w.text().trim().length).toBeGreaterThan(0);
      expect(w.text()).not.toContain("```");
      w.unmount();
    }
  });
});
