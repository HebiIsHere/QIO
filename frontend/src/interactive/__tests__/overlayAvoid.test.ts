/**
 * 浮层让开几何用例（互动模式契约 2026-10-13 · IRL §7.4 / §5.4）。
 *
 * 锁定的行为：对话窗口被用户移到批量列表那一带时，批量列表要么整体让开、
 * 要么判定「真的没位置」让调用方进入切换显示 —— 不允许叠上去。
 */
import { describe, expect, it } from "vitest";
import { widestFreeBand } from "../overlayAvoid";

const FULL = { left: 16, right: 1424 };

describe("widestFreeBand：基本让开", () => {
  it("没有障碍时返回整条带", () => {
    expect(widestFreeBand(FULL, [], 12)).toEqual(FULL);
  });

  it("障碍在右侧：可用区间缩到它左边", () => {
    const band = widestFreeBand(FULL, [{ x: 1000, y: 400, w: 420, h: 500 }], 12);
    expect(band).toEqual({ left: 16, right: 988 });
  });

  it("障碍在左侧：可用区间缩到它右边", () => {
    const band = widestFreeBand(FULL, [{ x: 16, y: 400, w: 420, h: 500 }], 12);
    expect(band).toEqual({ left: 448, right: 1424 });
  });

  it("障碍在中间：取更宽的一侧", () => {
    const band = widestFreeBand(FULL, [{ x: 500, y: 400, w: 400, h: 500 }], 12);
    expect(band).toEqual({ left: 912, right: 1424 });
  });

  it("多个障碍：取最宽的空隙", () => {
    const band = widestFreeBand(
      FULL,
      [
        { x: 200, y: 400, w: 200, h: 500 },
        { x: 800, y: 400, w: 200, h: 500 },
      ],
      12,
    );
    expect(band).toEqual({ left: 1012, right: 1424 });
  });
});

describe("widestFreeBand：垂直方向不重叠时不必让开", () => {
  it("障碍在竖直区间之外的另一带：整条带都可用", () => {
    const band = widestFreeBand(FULL, [{ x: 1000, y: 0, w: 420, h: 200 }], 12, {
      top: 400,
      bottom: 600,
    });
    expect(band).toEqual(FULL);
  });

  it("竖直区间相交时仍然让开", () => {
    const band = widestFreeBand(FULL, [{ x: 1000, y: 500, w: 420, h: 200 }], 12, {
      top: 400,
      bottom: 600,
    });
    expect(band).toEqual({ left: 16, right: 988 });
  });
});

describe("widestFreeBand：真的没位置", () => {
  it("障碍把整条带吃光 → null（调用方改走切换显示，不许硬叠）", () => {
    const band = widestFreeBand(FULL, [{ x: 16, y: 400, w: 1408, h: 500 }], 12);
    expect(band).toBeNull();
  });

  it("只剩一条缝隙也算有位置", () => {
    const band = widestFreeBand(FULL, [{ x: 200, y: 400, w: 1200, h: 500 }], 12);
    expect(band).toEqual({ left: 16, right: 188 });
  });

  it("障碍完全在带外时不受影响", () => {
    const band = widestFreeBand(FULL, [{ x: 2000, y: 400, w: 100, h: 100 }], 12);
    expect(band).toEqual(FULL);
  });
});
