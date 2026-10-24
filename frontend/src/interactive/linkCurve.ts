/**
 * 关系曲线的显示几何（纯函数）。
 *
 * 端点身份和锚点仍由 linkEndpoints 决定；这里只把两个端点 + 用户曲线偏移
 * 变成三次贝塞尔控制点。只影响显示，不产生第二套端点坐标。
 */

import type { LinkCurve } from "./types";

export interface CurveInput {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  curve?: LinkCurve | null;
}

export interface CurveGeometry {
  c1x: number;
  c1y: number;
  c2x: number;
  c2y: number;
  labelX: number;
  labelY: number;
}

function cubicAt(p0: number, p1: number, p2: number, p3: number, t: number): number {
  const mt = 1 - t;
  return mt * mt * mt * p0 + 3 * mt * mt * t * p1 + 3 * mt * t * t * p2 + t * t * t * p3;
}

/** 默认按端点主方向出线，用户偏移同时作用在两个控制点上。 */
export function curveGeometry(input: CurveInput): CurveGeometry {
  const x1 = Number.isFinite(input.x1) ? input.x1 : 0;
  const y1 = Number.isFinite(input.y1) ? input.y1 : 0;
  const x2 = Number.isFinite(input.x2) ? input.x2 : 0;
  const y2 = Number.isFinite(input.y2) ? input.y2 : 0;
  const dx = x2 - x1;
  const dy = y2 - y1;
  const distance = Math.min(150, Math.max(48, Math.hypot(dx, dy) * 0.36));
  let c1x: number;
  let c1y: number;
  let c2x: number;
  let c2y: number;
  if (Math.abs(dx) >= Math.abs(dy)) {
    const direction = dx >= 0 ? 1 : -1;
    c1x = x1 + direction * distance;
    c1y = y1;
    c2x = x2 - direction * distance;
    c2y = y2;
  } else {
    const direction = dy >= 0 ? 1 : -1;
    c1x = x1;
    c1y = y1 + direction * distance;
    c2x = x2;
    c2y = y2 - direction * distance;
  }
  const curveX = Number.isFinite(Number(input.curve?.x)) ? Number(input.curve?.x) : 0;
  const curveY = Number.isFinite(Number(input.curve?.y)) ? Number(input.curve?.y) : 0;
  c1x += curveX;
  c1y += curveY;
  c2x += curveX;
  c2y += curveY;
  const t = 0.58;
  return {
    c1x,
    c1y,
    c2x,
    c2y,
    labelX: cubicAt(x1, c1x, c2x, x2, t),
    labelY: cubicAt(y1, c1y, c2y, y2, t),
  };
}

export function curvePath(input: CurveInput): string {
  const geometry = curveGeometry(input);
  return (
    "M " + input.x1 + " " + input.y1 +
    " C " + geometry.c1x + " " + geometry.c1y +
    " " + geometry.c2x + " " + geometry.c2y +
    " " + input.x2 + " " + input.y2
  );
}
