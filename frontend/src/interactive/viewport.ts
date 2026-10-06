/**
 * 板面视口（平移 / 缩放）的坐标换算（子智能体 B 负责实现，主智能体先给出契约骨架）。
 *
 * 契约：docs/interactive-mode-contract.md §8.2 / §8.4。
 *
 * 规则：
 * - 板面坐标 = 卡片自己的 x/y 坐标系；屏幕坐标 = 浏览器视口坐标；
 *   surfaceRect 是**板面内容容器**的 getBoundingClientRect()（已包含滚动与位移）。
 * - 平移缩放只改变查看状态：不形成表达、不调用 QIO、不触发保存。
 */

export interface Viewport {
  scale: number;
  x: number;
  y: number;
}

export interface Point {
  x: number;
  y: number;
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** 屏幕矩形的最小形状（方便在测试里直接传字面量） */
export interface DOMRectLike {
  left: number;
  top: number;
}

export const IDENTITY_VIEWPORT: Viewport = { scale: 1, x: 0, y: 0 };

/** 当前实现的最小可用版本；B 负责补齐边界（缩放上下限、指针中心缩放、取消等）。 */
export function toBoardPoint(viewport: Viewport, client: Point, rect: DOMRectLike): Point {
  return {
    x: (client.x - rect.left - viewport.x) / viewport.scale,
    y: (client.y - rect.top - viewport.y) / viewport.scale,
  };
}

export function toScreenPoint(viewport: Viewport, point: Point, rect: DOMRectLike): Point {
  return {
    x: point.x * viewport.scale + viewport.x + rect.left,
    y: point.y * viewport.scale + viewport.y + rect.top,
  };
}

export function zoomAt(
  viewport: Viewport,
  factor: number,
  client: Point,
  rect: DOMRectLike,
): Viewport {
  const scale = Math.max(0.25, Math.min(2.5, viewport.scale * factor));
  const before = toBoardPoint(viewport, client, rect);
  return {
    scale,
    x: client.x - rect.left - before.x * scale,
    y: client.y - rect.top - before.y * scale,
  };
}

export function rectFromDrag(
  viewport: Viewport,
  start: Point,
  end: Point,
  rect: DOMRectLike,
): Rect {
  const from = toBoardPoint(viewport, start, rect);
  const to = toBoardPoint(viewport, end, rect);
  return {
    x: Math.min(from.x, to.x),
    y: Math.min(from.y, to.y),
    w: Math.abs(to.x - from.x),
    h: Math.abs(to.y - from.y),
  };
}

export function rectsIntersect(a: Rect, b: Rect): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}
