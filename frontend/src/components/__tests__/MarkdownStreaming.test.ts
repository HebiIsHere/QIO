/**
 * 流式 Markdown（Task 9 / WS F）——「解析 ≠ 动画帧」的回归证据。
 *
 * 旧实现：`shown` 由 requestAnimationFrame 每帧推进，而 `html` computed 依赖
 * `renderSource`（source 的前缀），所以**每个动画帧都会对整篇已显示文本做一次
 * 完整 Markdown 解析 + 代码块全量高亮**。长回答 / 长代码块在流式期间的解析次数
 * 约等于「帧数」（60 次/秒），成本随文本长度线性增长。
 *
 * 本文件观测的是**真实行为**：解析入口的调用次数、highlight.js 的调用次数、
 * 以及落定后的 DOM。不做任何实现细节的断言。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import hljs from "highlight.js/lib/core";
import MarkdownContent, { markdownPipeline } from "../MarkdownContent.vue";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** 把长文按字符切成 n 片，模拟模型增量到达。 */
function chunks(text: string, n: number): string[] {
  const size = Math.ceil(text.length / n);
  const out: string[] = [];
  for (let i = 0; i < text.length; i += size) out.push(text.slice(0, i + size));
  return out;
}

const RICH = [
  "## 二级标题",
  "",
  "正文里有 **粗体**、~~删除线~~、[外链](https://example.com) 和 `行内代码`。",
  "",
  ":::note",
  "指令容器里的内容也要保留。",
  ":::",
  "",
  "- 第一层",
  "  - 第二层",
  "    - 第三层",
  "",
  "- [x] 已完成项",
  "- [ ] 未完成项",
  "",
  "| 列 A | 列 B |",
  "| --- | --- |",
  "| 1 | 2 |",
  "",
  "```js",
  "const answer = 40 + 2;",
  "console.log(answer);",
  "```",
  "",
  "```python",
  "def add(a, b):",
  "    return a + b",
  "```",
  "",
  "```json",
  '{"name": "qio", "ok": true}',
  "```",
  "",
  ("长段落：" + "流式渲染必须保证最终结果与一次性渲染一致。".repeat(8) + "\n\n").repeat(6),
].join("\n");

function codeBlock(lines: number): string {
  const body = Array.from({ length: lines }, (_, i) => `const v${i} = ${i};`).join("\n");
  return "```js\n" + body + "\n```";
}

describe("流式 Markdown：最终 DOM 与一次性渲染一致", () => {
  it("长文 / 多语言代码块 / 表格 / 嵌套列表 / GFM / directive 混排：落定后逐字一致", async () => {
    vi.useFakeTimers();
    const streamed = mount(MarkdownContent, { props: { source: "", reveal: true, cps: 50 } });
    for (const partial of chunks(RICH, 40)) {
      await streamed.setProps({ source: partial });
      await vi.advanceTimersByTimeAsync(30);
    }
    // 流式结束：落定必须立刻完整渲染，不需要等下一个解析节拍
    await streamed.setProps({ reveal: false });
    await nextTick();

    const once = mount(MarkdownContent, { props: { source: RICH } });
    expect(streamed.find(".markdown-body").html()).toBe(once.find(".markdown-body").html());

    // 关键结构确实在（防止「一致但都错」）
    expect(streamed.find("table").exists()).toBe(true);
    expect(streamed.findAll("pre code.hljs").length).toBe(3);
    expect(streamed.find("del").exists()).toBe(true);
    // 注：现有渲染器把 listItem 的子块拉平（不产出嵌套 <ul>），这里只断言列表项都在
    expect(streamed.findAll("li").length).toBeGreaterThanOrEqual(3);
    expect(streamed.text()).toContain("第三层");
    expect(streamed.text()).toContain("指令容器里的内容也要保留。");
    expect(streamed.text()).toContain("长段落：");

    streamed.unmount();
    once.unmount();
  });

  it("流式过程中不因前缀解析而丢内容：任意中间状态都是 source 的前缀渲染", async () => {
    vi.useFakeTimers();
    const w = mount(MarkdownContent, { props: { source: "", reveal: true, cps: 50 } });
    await w.setProps({ source: "第一段文字" });
    await vi.advanceTimersByTimeAsync(400);
    expect(w.text()).toContain("第一段文字");

    await w.setProps({ source: "第一段文字加上更多内容" });
    await vi.advanceTimersByTimeAsync(400);
    expect(w.text()).toContain("第一段文字加上更多内容");
    w.unmount();
  });
});

describe("流式 Markdown：解析次数与动画帧解耦", () => {
  it("40 次增量 + 1.8s：解析次数受批次节拍限制（≤ 100ms 一次），不再等于帧数", async () => {
    vi.useFakeTimers();
    const render = vi.spyOn(markdownPipeline, "render");
    const w = mount(MarkdownContent, { props: { source: "", reveal: true, cps: 50 } });

    const text = ("QIO 流式 Markdown 批次解析，解析次数必须与动画帧数解耦。\n\n").repeat(40);
    const parts = chunks(text, 40);
    const CHUNK_MS = 20;
    for (const partial of parts) {
      await w.setProps({ source: partial });
      await vi.advanceTimersByTimeAsync(CHUNK_MS);
    }
    const TAIL_MS = 1000;
    await vi.advanceTimersByTimeAsync(TAIL_MS);

    const elapsed = parts.length * CHUNK_MS + TAIL_MS;
    const parses = render.mock.calls.length;
    // 旧实现（每帧一次解析，约 60 次/秒）在这段时间里会有 100 次以上
    const frames = Math.floor(elapsed / 16);
    expect(frames).toBeGreaterThan(80);
    expect(parses).toBeLessThanOrEqual(Math.ceil(elapsed / 100) + 3);
    expect(parses).toBeLessThan(frames / 4);

    // 落定后内容完整
    await w.setProps({ reveal: false });
    await nextTick();
    expect(w.text()).toContain("解析次数必须与动画帧数解耦。");
    w.unmount();
  });

  it("解析次数由节拍决定，与文本长度无关（同样时长、8 倍文本，次数同量级）", async () => {
    vi.useFakeTimers();
    const render = vi.spyOn(markdownPipeline, "render");

    async function stream(multiplier: number) {
      render.mockClear();
      const w = mount(MarkdownContent, { props: { source: "", reveal: true, cps: 50 } });
      const base = "解析节拍与文本长度无关。\n\n";
      const text = base.repeat(20 * multiplier);
      for (const partial of chunks(text, 15)) {
        await w.setProps({ source: partial });
        await vi.advanceTimersByTimeAsync(100);
      }
      await vi.advanceTimersByTimeAsync(600);
      const count = render.mock.calls.length;
      w.unmount();
      return count;
    }

    const short = await stream(1);
    const long = await stream(8);
    expect(short).toBeGreaterThan(0);
    // 文本长 8 倍、时长相同：解析次数不应随之增长（旧实现按帧数增长，与文本长度无关但约 60/s）
    expect(long).toBeLessThanOrEqual(short + 4);
    expect(long).toBeLessThanOrEqual(Math.ceil((15 * 100 + 600) / 100) + 3);
  });
});

describe("流式 Markdown：代码高亮记忆化", () => {
  it("代码块已完整、后文继续流式时不再重复高亮同一块", async () => {
    vi.useFakeTimers();
    const highlight = vi.spyOn(hljs, "highlight");
    const w = mount(MarkdownContent, { props: { source: "", reveal: true, cps: 50 } });

    // 前 800 字普通文本 → 一个完整代码块 → 后 800 字普通文本
    const head = "这是代码块之前的说明文字。\n\n".repeat(20);
    const tail = "这是代码块之后的说明文字。\n\n".repeat(20);
    const doc = head + codeBlock(12) + "\n\n" + tail;
    for (const partial of chunks(doc, 40)) {
      await w.setProps({ source: partial });
      await vi.advanceTimersByTimeAsync(50);
    }
    await vi.advanceTimersByTimeAsync(600);
    await w.setProps({ reveal: false });
    await nextTick();

    // 旧实现：每帧解析一次，代码块完整后仍会反复高亮（这段假时间 2.6s ≈ 160 次）
    expect(highlight.mock.calls.length).toBeLessThanOrEqual(12);
    // 渲染结果必须仍然是被高亮过的代码块
    const code = w.find("pre code.hljs");
    expect(code.exists()).toBe(true);
    expect(code.html()).toContain("hljs-keyword");
    w.unmount();
  });

  it("同一 (语言, 代码) 重复渲染结果稳定：缓存不改变高亮结果", () => {
    const src = "```js\nconst a = 1;\n```";
    const first = mount(MarkdownContent, { props: { source: src } });
    const second = mount(MarkdownContent, { props: { source: src } });
    const expected = hljs.highlight("const a = 1;", { language: "js" }).value;
    expect(first.find("pre code").html()).toBe(second.find("pre code").html());
    expect(first.find("pre code").html()).toContain(expected);
    first.unmount();
    second.unmount();
  });
});

describe("流式 Markdown：计时器生命周期", () => {
  it("组件卸载后不再继续解析（计时器已清理）", async () => {
    vi.useFakeTimers();
    const render = vi.spyOn(markdownPipeline, "render");
    const w = mount(MarkdownContent, {
      props: { source: "很长的内容".repeat(60), reveal: true, cps: 20 },
    });
    await vi.advanceTimersByTimeAsync(150);
    w.unmount();
    const afterUnmount = render.mock.calls.length;
    await vi.advanceTimersByTimeAsync(3000);
    expect(render.mock.calls.length).toBe(afterUnmount);
  });
});
