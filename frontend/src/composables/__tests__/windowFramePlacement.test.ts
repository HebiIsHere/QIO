/**
 * 浮层钳制改用「可见内容区」而不是整个 WebView 视口（0.3.2 透明留白窗）。
 *
 * 纯函数本身按「视口原点 + 尺寸」工作；新增可选 left/top 让调用方传入内容区原点。
 * 不传（旧调用/旧测试）= 原点 0，行为与改造前完全一致。
 */
import { describe, expect, it } from "vitest";
import { placeChromePopover } from "../../components/interactive/v2/chromePlacement";
import { computeTipPosition } from "../useQioTip";

/** 1200x800 客户区、28px 留白：可见内容区 = (28,28)-(1172,772)，1144x744 */
const CONTENT = { left: 28, top: 28, width: 1144, height: 744 };

describe("placeChromePopover：右/下边界按内容区而不是窗口矩形", () => {
  it("右锚点浮层不越出可见内容区（spike 复现的 16px 级越界）", () => {
    const placements = [
      placeChromePopover({ left: 1100, top: 200, right: 1140, bottom: 224, width: 40, height: 24 }, CONTENT, {
        width: 420,
        maxWidth: 420,
        margin: 8,
      }),
      placeChromePopover({ left: 28, top: 200, right: 68, bottom: 224, width: 40, height: 24 }, CONTENT, {
        width: 120,
        margin: 8,
      }),
    ];
    for (const p of placements) {
      expect(p.left, "left").toBeGreaterThanOrEqual(28 + 8);
      expect(p.left + p.width, "right").toBeLessThanOrEqual(28 + 1144 - 8);
      expect(p.top, "top").toBeGreaterThanOrEqual(28 + 8);
      expect(p.top + p.maxHeight, "bottom").toBeLessThanOrEqual(28 + 744 - 8);
    }
  });

  it("不传原点（旧调用）时行为与改造前一致（原点 0）", () => {
    const p = placeChromePopover(
      { left: 1100, top: 200, right: 1140, bottom: 224, width: 40, height: 24 },
      { width: 1144, height: 744 },
      { width: 420, maxWidth: 420, margin: 8, gap: 6 },
    );
    expect(p.left).toBe(1144 - 8 - 420);
    // 未给 anchor.top/bottom：anchorBottom = height 24，gap 6 → top = 30（原点 0 = 旧行为）
    expect(p.top).toBe(30);
  });
});

describe("computeTipPosition：提示只出现在可见内容区", () => {
  it("靠近内容区左/上边界时被推回（不落进透明留白带）", () => {
    const pos = computeTipPosition(
      { left: 30, top: 30, right: 60, bottom: 50, width: 30, height: 20 },
      { width: 160, height: 36 },
      CONTENT,
    );
    expect(pos.left).toBeGreaterThanOrEqual(28 + 8);
    expect(pos.top).toBeGreaterThanOrEqual(28 + 8);
  });

  it("在内容区下方放不下时翻转向上，且仍不越界", () => {
    const pos = computeTipPosition(
      { left: 600, top: 720, right: 700, bottom: 760, width: 100, height: 40 },
      { width: 200, height: 60 },
      CONTENT,
    );
    expect(pos.placement).toBe("top");
    expect(pos.top + 60).toBeLessThanOrEqual(28 + 744 - 8);
    expect(pos.left + 200).toBeLessThanOrEqual(28 + 1144 - 8);
  });
});
