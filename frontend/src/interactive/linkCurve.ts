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

// --- 用户拖动曲线：缩放换算（纯函数） ---------------------------------------

/** 用户拖出的曲线偏移上限（板面像素）。 */
export const CURVE_OFFSET_LIMIT = 240;

/** 判定「开始拖动」的指针抖动阈值（屏幕像素，与缩放无关：缩放只影响板面位移）。 */
export const DRAG_THRESHOLD_PX = 2;

function finiteNumber(value: unknown, fallback = 0): number {
  const result = typeof value === "number" ? value : Number(value);
  return Number.isFinite(result) ? result : fallback;
}

/**
 * 缩放安全值：板面 → 屏幕的缩放倍数。
 *
 * 非正 / 非有限一律按 1（与 viewport / BoardCanvas 的 clampScale 同一条口径），
 * 否则拖动换算会得到 0、NaN 或反向位移。
 */
export function normalizeCurveScale(scale: unknown): number {
  const value = finiteNumber(scale, 1);
  return value > 0 ? value : 1;
}

/** 把曲线偏移限制在 ±limit（默认 ±240 板面像素）。 */
export function clampCurveOffset(value: unknown, limit: unknown = CURVE_OFFSET_LIMIT): number {
  const max = Math.abs(finiteNumber(limit, CURVE_OFFSET_LIMIT));
  const bound = max > 0 ? max : CURVE_OFFSET_LIMIT;
  const number = finiteNumber(value, 0);
  return Math.max(-bound, Math.min(bound, number));
}

export interface CurveDragInput {
  /** 按下时这条关系的曲线偏移（板面坐标）。 */
  origin: LinkCurve;
  startClientX: number;
  startClientY: number;
  clientX: number;
  clientY: number;
  /** 板面 → 屏幕的缩放倍数；缺省 / 非法按 1。 */
  scale?: number;
  /** 偏移上限（板面像素），缺省 ±240。 */
  limit?: number;
}

export interface CurveDragResult {
  /** 换算后的板面偏移（已按缩放折算并截断）。 */
  curve: LinkCurve;
  /** 屏幕位移是否超过抖动阈值；false = 本次仍算单击，不算拖动。 */
  moved: boolean;
  /** 原始屏幕位移（用于诊断 / 测试，不参与几何）。 */
  screenDx: number;
  screenDy: number;
}

/**
 * 指针拖动 → 曲线偏移（纯函数，唯一换算口径）。
 *
 * 本组件画在 `.board-surface` 的 `transform: scale()` 里：屏幕位移 = 板面位移 × scale，
 * 所以 **板面位移 = 屏幕位移 / scale**。50% 缩放下 100px 屏幕位移对应 200px 板面位移
 * （放大 2 倍才跟上视觉），200% 缩放下只对应 50px —— 不按缩放倍数错误放大 / 缩小。
 *
 * 阈值按**屏幕**像素判定（指针抖动与缩放无关）；非法坐标 / 缩放都有有限值兜底。
 */
export function curveFromPointerDrag(input: CurveDragInput): CurveDragResult {
  const scale = normalizeCurveScale(input.scale);
  const screenDx = finiteNumber(input.clientX) - finiteNumber(input.startClientX);
  const screenDy = finiteNumber(input.clientY) - finiteNumber(input.startClientY);
  const moved = Math.hypot(screenDx, screenDy) >= DRAG_THRESHOLD_PX;
  return {
    curve: {
      x: clampCurveOffset(finiteNumber(input.origin?.x, 0) + screenDx / scale, input.limit),
      y: clampCurveOffset(finiteNumber(input.origin?.y, 0) + screenDy / scale, input.limit),
    },
    moved,
    screenDx,
    screenDy,
  };
}
