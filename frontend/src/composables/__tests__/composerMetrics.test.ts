/**
 * 输入区尺寸的共享量测：同一份尺寸只量一次，多个订阅者共用。
 *
 * 修复提示词 §6 要求「避免重复计算同一份输入区高度」——以前消息流底部缓冲、
 * 底部内容块让位、按钮锚定各写一遍 `querySelector(".composer") + getBoundingClientRect()`。
 */
import { describe, expect, it, afterEach, vi } from "vitest";
import { measureComposer, subscribeComposerMetrics } from "../composerMetrics";

function fakeComposer(height: number, top: number, inputTop = top + 44): HTMLElement {
  const el = document.createElement("div");
  el.className = "composer";
  // 面板里真的有一个 textarea：按钮要贴的是它的上缘，不是面板上缘
  const input = document.createElement("textarea");
  input.id = "composer-input";
  input.getBoundingClientRect = () =>
    ({ height: 64, top: inputTop, bottom: inputTop + 64, left: 0, right: 700, width: 700, x: 0, y: inputTop, toJSON: () => ({}) }) as DOMRect;
  el.appendChild(input);
  el.getBoundingClientRect = vi.fn(
    () =>
      ({
        height,
        top,
        bottom: top + height,
        left: 0,
        right: 860,
        width: 860,
        x: 0,
        y: top,
        toJSON: () => ({}),
      }) as DOMRect,
  );
  document.body.appendChild(el);
  return el;
}

async function settle(): Promise<void> {
  await new Promise((r) => requestAnimationFrame(() => r(null)));
  await new Promise((r) => requestAnimationFrame(() => r(null)));
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("composerMetrics", () => {
  it("没挂输入框时明确 found=false（不编造尺寸）", () => {
    expect(measureComposer()).toEqual({
      found: false,
      height: 0,
      top: 0,
      inputTop: 0,
      centerX: 0,
      scale: 1,
    });
  });

  it("订阅时有输入框就同步给出尺寸（首帧就能让位）", () => {
    fakeComposer(120, 700);
    const seen: number[] = [];
    const off = subscribeComposerMetrics((m) => seen.push(m.height));
    expect(seen).toEqual([120]);
    off();
  });

  it("多个订阅者共用同一次测量（同一帧只读一次布局）", async () => {
    const composer = fakeComposer(120, 700);
    const reads = composer.getBoundingClientRect as unknown as ReturnType<typeof vi.fn>;
    const a: number[] = [];
    const b: number[] = [];
    const offA = subscribeComposerMetrics((m) => a.push(m.height));
    const offB = subscribeComposerMetrics((m) => b.push(m.height));
    reads.mockClear();

    window.dispatchEvent(new Event("resize"));
    await settle();

    // 两个订阅者都收到更新，但布局只读了一次
    expect(a.length).toBeGreaterThan(1);
    expect(b.length).toBeGreaterThan(1);
    expect(reads.mock.calls.length).toBe(1);

    offA();
    offB();
  });

  it("全部退订后不再监听窗口变化", async () => {
    const composer = fakeComposer(120, 700);
    const reads = composer.getBoundingClientRect as unknown as ReturnType<typeof vi.fn>;
    const seen: number[] = [];
    const off = subscribeComposerMetrics((m) => seen.push(m.height));
    off();
    reads.mockClear();

    window.dispatchEvent(new Event("resize"));
    await settle();

    expect(reads.mock.calls.length).toBe(0);
  });
});
