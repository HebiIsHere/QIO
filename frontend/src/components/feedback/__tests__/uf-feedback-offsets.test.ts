/**
 * 反馈浮层落点：全局栏下方，并为顶部提示条（.top-notes）真实让位。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { feedbackTopPx, FEEDBACK_TOP_GAP } from "../feedbackOffsets";

function fakeRect(height: number): DOMRect {
  return {
    x: 0, y: 0, top: 0, left: 0, right: 0, bottom: height, width: 0, height,
    toJSON: () => ({}),
  } as DOMRect;
}

describe("feedbackTopPx：按真实全局栏与顶部提示条高度计算", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
    document.documentElement.style.removeProperty("--titlebar-height");
  });
  afterEach(() => {
    document.documentElement.style.removeProperty("--titlebar-height");
  });

  it("没有顶部提示条时 = 全局栏高度 + 间距", () => {
    document.documentElement.style.setProperty("--titlebar-height", "70px");
    expect(feedbackTopPx()).toBe(70 + FEEDBACK_TOP_GAP);
  });

  it("存在 .top-notes 时让出它的真实高度（不写死坐标）", () => {
    document.documentElement.style.setProperty("--titlebar-height", "32px");
    const notes = document.createElement("div");
    notes.className = "top-notes";
    notes.getBoundingClientRect = () => fakeRect(44);
    document.body.appendChild(notes);
    expect(feedbackTopPx()).toBe(32 + FEEDBACK_TOP_GAP + 44 + 8);
  });

  it("读不到 --titlebar-height 时回落到 32px，不返回 0", () => {
    expect(feedbackTopPx()).toBeGreaterThan(FEEDBACK_TOP_GAP);
  });
});
