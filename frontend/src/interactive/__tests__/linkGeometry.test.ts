/**
 * 关系线几何纯函数测试（用户决策 2 / B）。
 * 覆盖：水平 / 垂直 / 斜向 / 贴边（极近）/ 重叠 / 包含 / 标签中点与避让 / 箭头间距。
 */
import { describe, expect, it } from "vitest";

import {
  linkGeometry,
  nearestEdgePair,
  placeLinkLabel,
  rectCenter,
  rectEdgePoint,
  rectDistance,
  rectsOverlap,
  type LinkRect,
} from "../linkGeometry";

function rect(x: number, y: number, w = 100, h = 100): LinkRect {
  return { x, y, w, h };
}

function labelOverlaps(x: number, y: number, size: { w: number; h: number }, cards: LinkRect[]): boolean {
  const label = { x: x - size.w / 2, y: y - size.h / 2, w: size.w, h: size.h };
  return cards.some((card) => rectsOverlap(label, card));
}

describe("边框交点：水平 / 垂直 / 斜向", () => {
  it("水平：两边框交点 + 箭头让出 2px，标签在中点", () => {
    const result = linkGeometry(rect(0, 0), rect(200, 0));
    expect([result.x1, result.y1]).toEqual([100, 50]);
    expect([result.borderX2, result.borderY2]).toEqual([200, 50]);
    expect([result.x2, result.y2]).toEqual([198, 50]);
    expect([result.labelX, result.labelY]).toEqual([150, 50]);
    expect(result.length).toBe(100);
    expect(result.fallback).toBe(false);
    expect(result.labelPlacement).toBe("center");
  });

  it("垂直：上 → 下", () => {
    const result = linkGeometry(rect(0, 0), rect(0, 200));
    expect([result.x1, result.y1]).toEqual([50, 100]);
    expect([result.borderX2, result.borderY2]).toEqual([50, 200]);
    expect([result.x2, result.y2]).toEqual([50, 198]);
    expect([result.labelX, result.labelY]).toEqual([50, 150]);
  });

  it("斜向：交点在边框上（中心连线与边框的交点）", () => {
    const result = linkGeometry(rect(0, 0), rect(200, 200));
    expect(result.x1).toBeCloseTo(100, 0);
    expect(result.y1).toBeCloseTo(100, 0);
    expect(result.borderX2).toBeCloseTo(200, 0);
    expect(result.borderY2).toBeCloseTo(200, 0);
    expect(result.labelX).toBeCloseTo(150, 0);
    expect(result.labelY).toBeCloseTo(150, 0);
    expect(result.fallback).toBe(false);
  });

  it("箭头间距可配置；为 0 时终点就是边框交点", () => {
    const result = linkGeometry(rect(0, 0), rect(200, 0), { arrowGap: 0 });
    expect([result.x2, result.y2]).toEqual([200, 50]);
    expect(result.labelX).toBe(150);
  });
});

describe("重叠 / 包含 / 极近：退回最近边中点", () => {
  it("贴边（距离 0）：用最近边中点，不画穿进卡片的线", () => {
    const result = linkGeometry(rect(0, 0), rect(100, 0));
    expect(result.fallback).toBe(true);
    expect([result.x1, result.y1]).toEqual([100, 50]);
    expect([result.borderX2, result.borderY2]).toEqual([100, 50]);
    expect(result.length).toBe(0);
  });

  it("重叠：兜底为一对边中点，标签放到线段一侧且不压任何卡片", () => {
    const a = rect(0, 0);
    const b = rect(50, 50);
    const result = linkGeometry(a, b);
    expect(result.fallback).toBe(true);
    const pair = nearestEdgePair(a, b);
    expect([result.x1, result.y1]).toEqual([pair.start.x, pair.start.y]);
    expect([result.borderX2, result.borderY2]).toEqual([pair.end.x, pair.end.y]);
    expect(labelOverlaps(result.labelX, result.labelY, { w: 64, h: 20 }, [a, b])).toBe(false);
  });

  it("包含：小的在大的里面也不从中心画线", () => {
    const big = rect(0, 0, 300, 300);
    const small = rect(50, 50, 50, 50);
    const result = linkGeometry(big, small);
    expect(result.fallback).toBe(true);
    expect(labelOverlaps(result.labelX, result.labelY, { w: 64, h: 20 }, [big, small])).toBe(false);
  });

  it("极近（间隙小于 minVisibleLength）：按极近兜底", () => {
    const a = rect(0, 0);
    const b = rect(103, 0);
    const result = linkGeometry(a, b);
    expect(result.fallback).toBe(true);
    const pair = nearestEdgePair(a, b);
    expect([result.x1, result.y1]).toEqual([pair.start.x, pair.start.y]);
  });
});

describe("标签避让", () => {
  it("中心不压卡片时就在可见线段中点", () => {
    const a = rect(0, 0);
    const b = rect(300, 0);
    const result = linkGeometry(a, b);
    expect([result.labelX, result.labelY]).toEqual([200, 50]);
    expect(result.labelPlacement).toBe("center");
  });

  it("中心压住卡片时沿垂直方向让开（半个标签高 + 间距）", () => {
    const placed = placeLinkLabel({ x: 0, y: 0 }, { x: 100, y: 0 }, { w: 64, h: 20 }, [rect(0, -40, 100, 35)], 6);
    expect(placed.placement).toBe("offset");
    expect(placed.y).toBeCloseTo(16, 0);
    expect(placed.x).toBe(50);
  });

  it("垂直方向也放不下时放到线段一侧，且不与任一卡片相交", () => {
    const a = rect(0, 0);
    const b = rect(50, 50);
    const result = linkGeometry(a, b);
    expect(["offset", "side"]).toContain(result.labelPlacement);
    expect(labelOverlaps(result.labelX, result.labelY, { w: 64, h: 20 }, [a, b])).toBe(false);
  });
});

describe("基础几何辅助", () => {
  it("rectEdgePoint：从中心朝外与边框相交；目标在内部时退回最近边中点", () => {
    expect(rectEdgePoint(rect(0, 0), { x: 500, y: 50 })).toEqual({ x: 100, y: 50 });
    expect(rectEdgePoint(rect(0, 0), { x: 50, y: 500 })).toEqual({ x: 50, y: 100 });
    // 目标在内部：按主方向退回最近的边中点（|dx| == |dy| 时走水平方向）
    expect(rectEdgePoint(rect(0, 0), { x: 25, y: 25 })).toEqual({ x: 0, y: 50 });
  });

  it("rectCenter / rectDistance / rectsOverlap 与既有几何口径一致", () => {
    expect(rectCenter(rect(10, 20, 30, 40))).toEqual({ x: 25, y: 40 });
    expect(rectDistance(rect(0, 0), rect(200, 0))).toBe(100);
    expect(rectsOverlap(rect(0, 0), rect(99, 0))).toBe(true);
    expect(rectsOverlap(rect(0, 0), rect(100, 0))).toBe(false);
  });
});
