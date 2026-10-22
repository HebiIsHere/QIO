/**
 * 组框轮廓纯函数测试（用户决策 2 / F）。
 * 覆盖：单成员 / 相邻 / 不相邻 / 重叠 / 包含 / 顶行与非顶行。
 *
 * 关键口径：轮廓只围住成员的 16px 扩张区（顶行顶部 58 给组头），
 * 成员空隙里的非成员卡片不会被围进组框。
 */
import { describe, expect, it } from "vitest";

import {
  GROUP_OUTLINE_PADDING,
  groupOutline,
  type GroupOutlineSegment,
} from "../groupOutline";

function member(cardId: string, x: number, y: number, w = 100, h = 60) {
  return { cardId, rect: { x, y, w, h } };
}

/** 点是否在轮廓内部（线段是闭合正交边界，用水平射线与竖直线段交点奇偶判断）。 */
function insideOutline(segments: GroupOutlineSegment[], x: number, y: number): boolean {
  let inside = false;
  for (const segment of segments) {
    if (segment.x1 !== segment.x2) continue; // 只看竖直线段
    const minY = Math.min(segment.y1, segment.y2);
    const maxY = Math.max(segment.y1, segment.y2);
    if (y <= minY || y >= maxY) continue;
    if (x >= segment.x1) continue; // 射线向右：只数右侧的竖直线段
    inside = !inside;
  }
  return inside;
}

describe("组框轮廓：基础形状", () => {
  it("没有成员时返回 null（不画大空框）", () => {
    expect(groupOutline([])).toBeNull();
    expect(groupOutline(null)).toBeNull();
    expect(groupOutline([{ cardId: "a", rect: { x: 0, y: 0, w: 0, h: 0 } }])).toBeNull();
  });

  it("单成员：左右 16 / 顶 58 / 下 16，边界是四段矩形", () => {
    const result = groupOutline([member("a", 100, 100)]);
    expect(result).not.toBeNull();
    if (!result) return;
    expect(result.members).toEqual([
      { cardId: "a", x: 84, y: 42, w: 132, h: 134 },
    ]);
    expect(result.bounds).toEqual({ x: 84, y: 42, w: 132, h: 134 });
    expect(result.segments).toHaveLength(4);
    // 四段：左右两条竖线 + 上下两条横线
    expect(result.segments.filter((segment) => segment.x1 === segment.x2).map((segment) => segment.x1).sort((a, b) => a - b)).toEqual([84, 216]);
    expect(result.segments.filter((segment) => segment.y1 === segment.y2).map((segment) => segment.y1).sort((a, b) => a - b)).toEqual([42, 176]);
    // 浮动头部在轮廓左上、顶行留白区内
    expect(result.header.x).toBe(100);
    expect(result.header.y).toBe(48);
    expect(result.header.w).toBe(132);
    expect(insideOutline(result.segments, 166, 100)).toBe(true);
  });

  it("相邻成员（扩张区相接）：并成一个矩形轮廓，不是两个框", () => {
    const result = groupOutline([member("a", 0, 0), member("b", 116, 0)]);
    expect(result).not.toBeNull();
    if (!result) return;
    // a 的扩张区 -16..116，b 的 100..232 → 相接 → 单元矩形
    expect(result.bounds).toEqual({ x: -16, y: -58, w: 248, h: 134 });
    expect(result.segments).toHaveLength(4);
    expect(insideOutline(result.segments, 120, 10)).toBe(true);
  });

  it("不相邻成员：两条独立轮廓（8 段），中间空隙是空的", () => {
    const result = groupOutline([member("a", 0, 0), member("b", 400, 0)]);
    expect(result).not.toBeNull();
    if (!result) return;
    expect(result.segments).toHaveLength(8);
    // a 的扩张区 -16..116，b 的 384..516；并集边界 -16..516
    expect(result.bounds).toEqual({ x: -16, y: -58, w: 532, h: 134 });
    expect(insideOutline(result.segments, 50, 10)).toBe(true);
    expect(insideOutline(result.segments, 250, 10)).toBe(false);
  });

  it("重叠成员：轮廓为并集（L 形），不会围进两卡片之外的空白区", () => {
    const result = groupOutline([member("a", 0, 0), member("b", 80, 100)]);
    expect(result).not.toBeNull();
    if (!result) return;
    expect(result.segments.length).toBeGreaterThanOrEqual(6);
    // 两个成员各在自己的扩张区内
    expect(insideOutline(result.segments, 50, 10)).toBe(true);
    expect(insideOutline(result.segments, 120, 130)).toBe(true);
    // 右上角（b 右上方、a 扩张区之外）不被围进去
    expect(insideOutline(result.segments, 260, 10)).toBe(false);
  });

  it("包含关系：轮廓只等于外层成员的扩张区", () => {
    const result = groupOutline([member("big", 0, 0, 200, 120), member("small", 50, 40, 40, 30)]);
    expect(result).not.toBeNull();
    if (!result) return;
    expect(result.bounds).toEqual({ x: -16, y: -58, w: 232, h: 194 });
    expect(result.header.w).toBe(232);
    expect(result.segments).toHaveLength(4);
    expect(insideOutline(result.segments, 100, 60)).toBe(true);
    expect(insideOutline(result.segments, 240, 60)).toBe(false);
  });
});

describe("组框轮廓：顶行与非顶行", () => {
  it("顶行成员顶部留 58，非顶行成员顶部只留 16", () => {
    const result = groupOutline([member("a", 0, 0), member("b", 0, 200)]);
    expect(result).not.toBeNull();
    if (!result) return;
    expect(result.members[0]).toMatchObject({ cardId: "a", y: -58, h: 134 });
    expect(result.members[1]).toMatchObject({ cardId: "b", y: 184, h: 92 });
    expect(result.bounds).toEqual({ x: -16, y: -58, w: 132, h: 334 });
    expect(result.header.y).toBe(-52);
  });

  it("同一行的两个成员都按顶行处理", () => {
    const result = groupOutline([member("a", 0, 0), member("b", 200, 0)]);
    expect(result).not.toBeNull();
    if (!result) return;
    expect(result.members.every((fill) => fill.y === -58)).toBe(true);
  });
});

describe("组框轮廓：留白可覆盖（与 board.ts 常量同源）", () => {
  it("默认留白与 board.ts 的组框留白一致（16 / 58 / 16）", () => {
    expect(GROUP_OUTLINE_PADDING).toMatchObject({ x: 16, top: 58, topInner: 16, bottom: 16 });
  });

  it("传入自定义留白时按自定义值扩张", () => {
    const result = groupOutline([member("a", 100, 100)], { x: 0, top: 0, topInner: 0, bottom: 0 });
    expect(result).not.toBeNull();
    if (!result) return;
    expect(result.members[0]).toMatchObject({ x: 100, y: 100, w: 100, h: 60 });
  });
});
