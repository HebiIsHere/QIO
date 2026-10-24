import { describe, expect, it } from "vitest";
import { curveGeometry, curvePath } from "../linkCurve";

describe("关系曲线几何", () => {
  it("默认按主方向出线，曲线不改变端点", () => {
    const geometry = curveGeometry({ x1: 0, y1: 0, x2: 400, y2: 0 });
    expect(geometry.c1x).toBeGreaterThan(0);
    expect(geometry.c2x).toBeLessThan(400);
    expect(geometry.labelX).toBeGreaterThan(100);
    expect(geometry.labelX).toBeLessThan(300);
    expect(curvePath({ x1: 0, y1: 0, x2: 400, y2: 0 })).toMatch(/^M 0 0 C /);
  });

  it("用户拖出的偏移作用在两个控制点上并改变标签位置", () => {
    const straight = curveGeometry({ x1: 0, y1: 0, x2: 400, y2: 0 });
    const bent = curveGeometry({ x1: 0, y1: 0, x2: 400, y2: 0, curve: { x: 20, y: -30 } });
    expect(bent.c1x).toBe(straight.c1x + 20);
    expect(bent.c1y).toBe(straight.c1y - 30);
    expect(bent.c2x).toBe(straight.c2x + 20);
    expect(bent.c2y).toBe(straight.c2y - 30);
    expect(bent.labelY).not.toBe(straight.labelY);
  });

  it("竖向端点按竖向出线", () => {
    const geometry = curveGeometry({ x1: 0, y1: 0, x2: 0, y2: 400 });
    expect(geometry.c1y).toBeGreaterThan(0);
    expect(geometry.c2y).toBeLessThan(400);
  });
});
