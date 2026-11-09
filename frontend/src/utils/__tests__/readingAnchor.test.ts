/**
 * 阅读位置锚点工具（修复提示词 §二）：稳定身份优先、像素只做回退。
 * jsdom 不做布局，这里用可控的 getBoundingClientRect 桩验证计算本身；
 * 真实虚拟列表测量变化由浏览器回归场景覆盖。
 */
import { describe, expect, it, afterEach } from "vitest";
import {
  anchorKeyOf,
  anchorOffsetOf,
  captureReadingAnchor,
  findAnchorElement,
  findSnippetElement,
  normalizeSnippet,
  textAnchorAtPoint,
  turnKeyOf,
} from "../readingAnchor";

function rect(top: number, height: number, left = 0, width = 800): DOMRect {
  return {
    top,
    height,
    bottom: top + height,
    left,
    right: left + width,
    width,
    x: left,
    y: top,
    toJSON: () => ({}),
  } as DOMRect;
}

function stubRect(el: HTMLElement, box: DOMRect): void {
  el.getBoundingClientRect = () => box;
}

afterEach(() => {
  document.body.innerHTML = "";
  delete (document as unknown as { caretRangeFromPoint?: unknown }).caretRangeFromPoint;
  delete (document as unknown as { elementFromPoint?: unknown }).elementFromPoint;
});

describe("turnKeyOf / normalizeSnippet", () => {
  it("轮次身份优先 turn_id，旧历史退到首条消息 id", () => {
    expect(turnKeyOf({ turnId: "turn_9", items: [{ id: "m1" }] })).toBe("turn_9");
    expect(turnKeyOf({ turnId: "", items: [{ id: "m1" }] })).toBe("m1");
    expect(turnKeyOf({ turnId: "  ", items: [] })).toBe("");
  });

  it("文本片段压成一行并限长；非字符串不抛错", () => {
    expect(normalizeSnippet("  第一行\n\n第二行   继续  ")).toBe("第一行 第二行 继续");
    expect(normalizeSnippet("abcdefghij", 4)).toBe("abcd");
    expect(normalizeSnippet(null)).toBe("");
    expect(normalizeSnippet(42)).toBe("42");
  });
});

describe("anchorKeyOf / findAnchorElement", () => {
  it("消息 id 优先，其次 data-test，最后第一个 class", () => {
    const msg = document.createElement("div");
    msg.setAttribute("data-message-id", "m1");
    msg.className = "message assistant";
    expect(anchorKeyOf(msg)).toBe("msg:m1");

    const proc = document.createElement("section");
    proc.setAttribute("data-test", "turn-process");
    proc.className = "turn-process";
    expect(anchorKeyOf(proc)).toBe("test:turn-process");

    const plain = document.createElement("div");
    plain.className = "continue-bar extra";
    expect(anchorKeyOf(plain)).toBe("class:continue-bar");
    expect(anchorKeyOf(null)).toBeNull();
  });

  it("在轮次块里按身份找回锚定元素", () => {
    const turn = document.createElement("div");
    const a = document.createElement("div");
    a.setAttribute("data-message-id", "m1");
    const b = document.createElement("section");
    b.setAttribute("data-test", "turn-process");
    turn.append(a, b);
    expect(findAnchorElement(turn, "msg:m1")).toBe(a);
    expect(findAnchorElement(turn, "test:turn-process")).toBe(b);
    expect(findAnchorElement(turn, "msg:missing")).toBeNull();
  });
});

describe("文本锚点：长回答内部位置", () => {
  it("findSnippetElement 跨空白匹配文本节点", () => {
    const container = document.createElement("div");
    const p = document.createElement("p");
    p.textContent = "这是回答里的  一段关键文字，用于定位。";
    container.appendChild(p);
    document.body.appendChild(container);
    expect(findSnippetElement(container, "回答里的 一段关键文字")).toBe(p);
    expect(findSnippetElement(container, "不存在的片段")).toBeNull();
    expect(findSnippetElement(container, "短")).toBeNull(); // 太短不作为锚点
  });

  it("textAnchorAtPoint 用 caretRangeFromPoint 取到具体文本节点", () => {
    const container = document.createElement("div");
    const p = document.createElement("p");
    const text = document.createTextNode("第一段正文内容足够长，可以定位。");
    p.appendChild(text);
    container.appendChild(p);
    document.body.appendChild(container);
    stubRect(container, rect(100, 600));
    stubRect(p, rect(140, 24));
    (document as unknown as { caretRangeFromPoint: (x: number, y: number) => Range }).caretRangeFromPoint =
      () => ({ startContainer: text, startOffset: 0 } as unknown as Range);
    const anchor = textAnchorAtPoint(container, 400, 118);
    expect(anchor).not.toBeNull();
    expect(anchor?.snippet).toContain("第一段正文内容");
    expect(anchor?.offset).toBe(40); // 140 - 100
  });

  it("没有 caretRangeFromPoint 时退回 elementFromPoint 的第一个文本节点", () => {
    const container = document.createElement("div");
    const p = document.createElement("p");
    p.textContent = "回退路径同样要能定位到这段文字。";
    container.appendChild(p);
    document.body.appendChild(container);
    stubRect(container, rect(0, 600));
    stubRect(p, rect(10, 24));
    (document as unknown as { elementFromPoint: (x: number, y: number) => Element }).elementFromPoint =
      () => p;
    const anchor = textAnchorAtPoint(container, 400, 18);
    expect(anchor?.snippet).toContain("回退路径");
    expect(anchor?.offset).toBe(10);
  });
});

describe("captureReadingAnchor", () => {
  it("取视口里第一个仍可见的轮次与轮内第一个可见块", () => {
    const container = document.createElement("div");
    const turn1 = document.createElement("div");
    turn1.setAttribute("data-turn-key", "turn_1");
    const turn2 = document.createElement("div");
    turn2.setAttribute("data-turn-key", "turn_2");
    container.append(turn1, turn2);
    document.body.appendChild(container);
    stubRect(container, rect(0, 400));
    stubRect(turn1, rect(-420, 380)); // 完全在视口上方（bottom = -40）
    stubRect(turn2, rect(60, 300));
    const meta = document.createElement("div");
    meta.className = "turn-meta";
    stubRect(meta, rect(60, 20));
    const user = document.createElement("div");
    user.setAttribute("data-message-id", "m1");
    stubRect(user, rect(90, 40));
    const process = document.createElement("section");
    process.setAttribute("data-test", "turn-process");
    stubRect(process, rect(140, 200));
    turn2.append(meta, user, process);

    const capture = captureReadingAnchor(container);
    expect(capture).not.toBeNull();
    expect(capture?.turnKey).toBe("turn_2");
    expect(capture?.anchorKey).toBe("msg:m1");
    expect(capture?.anchorOffset).toBe(90);
    expect(capture?.turnOffset).toBe(60);
    expect(capture?.textSnippet).toBeNull();
  });

  it("没有渲染中的轮次时返回 null（调用方走像素回退，不假装恢复成功）", () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    stubRect(container, rect(0, 400));
    expect(captureReadingAnchor(container)).toBeNull();
  });

  it("anchorOffsetOf：元素不在文档或已脱离时返回 null", () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    stubRect(container, rect(100, 400));
    const el = document.createElement("div");
    container.appendChild(el);
    stubRect(el, rect(160, 20));
    expect(anchorOffsetOf(container, el)).toBe(60);
    expect(anchorOffsetOf(container, null)).toBeNull();
    const detached = document.createElement("div");
    expect(anchorOffsetOf(container, detached)).toBeNull();
  });
});
