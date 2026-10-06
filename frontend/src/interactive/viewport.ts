/**
 * 板面视口（平移 / 缩放）的坐标换算（子智能体 B 负责实现）。
 *
 * 契约：docs/interactive-mode-contract.md §8.2 / §8.4。
 *
 * 规则：
 * - 板面坐标 = 卡片自己的 x/y 坐标系；屏幕坐标 = 浏览器视口坐标；
 *   surfaceRect 是**板面内容容器**的 getBoundingClientRect()（已包含滚动与位移）。
 * - 平移缩放只改变查看状态：不形成表达、不调用 QIO、不触发保存。
 *
 * 组件里的统一用法（BoardCanvas 全部走这里，不再自己算坐标）：
 *
 *     const rect = effectiveRect(viewportEl.getBoundingClientRect(), viewportEl.scrollLeft, viewportEl.scrollTop);
 *     const point = toBoardPoint(view, { x: event.clientX, y: event.clientY }, rect);
 *
 * 为什么需要 effectiveRect：surfaceRect 是**板面内容原点在屏幕上的位置**，
 * 滚动之后它等于「容器可视区左上角 - 滚动偏移」；直接拿容器的 boundingClientRect
 * 会把滚动量丢掉（平移 / 缩放后卡片拖动与框选就会偏）。
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

/** 缩放上下限：太小看不清、太大只剩一两张卡片。 */
export const MIN_SCALE = 0.25;
export const MAX_SCALE = 2.5;

/** 指针附近缩放的滚轮灵敏度：每 100px 滚动约 ×1.16。 */
export const WHEEL_ZOOM_SENSITIVITY = 0.0015;

function finite(value: unknown, fallback: number): number {
  const result = typeof value === "number" ? value : Number(value);
  return Number.isFinite(result) ? result : fallback;
}

/** 把缩放收敛到 [MIN_SCALE, MAX_SCALE]。 */
export function clampScale(scale: number): number {
  return Math.max(MIN_SCALE, Math.min(MAX_SCALE, finite(scale, 1)));
}

/**
 * 板面内容原点（surfaceRect 左上角）在屏幕上的真实位置。
 * 传容器自身的 boundingClientRect 与它的滚动量即可。
 */
export function effectiveRect(container: DOMRectLike, scrollLeft = 0, scrollTop = 0): DOMRectLike {
  return {
    left: finite(container.left, 0) - finite(scrollLeft, 0),
    top: finite(container.top, 0) - finite(scrollTop, 0),
  };
}

/** 屏幕坐标 → 板面坐标。 */
export function toBoardPoint(viewport: Viewport, client: Point, rect: DOMRectLike): Point {
  const scale = clampScale(viewport.scale);
  const x = finite(viewport.x, 0);
  const y = finite(viewport.y, 0);
  return {
    x: (finite(client.x, 0) - finite(rect.left, 0) - x) / scale,
    y: (finite(client.y, 0) - finite(rect.top, 0) - y) / scale,
  };
}

/** 板面坐标 → 屏幕坐标（toBoardPoint 的逆运算）。 */
export function toScreenPoint(viewport: Viewport, point: Point, rect: DOMRectLike): Point {
  const scale = clampScale(viewport.scale);
  const x = finite(viewport.x, 0);
  const y = finite(viewport.y, 0);
  return {
    x: finite(point.x, 0) * scale + x + finite(rect.left, 0),
    y: finite(point.y, 0) * scale + y + finite(rect.top, 0),
  };
}

/** 平移：只改查看位置。 */
export function panBy(viewport: Viewport, dx: number, dy: number): Viewport {
  return {
    scale: clampScale(viewport.scale),
    x: finite(viewport.x, 0) + finite(dx, 0),
    y: finite(viewport.y, 0) + finite(dy, 0),
  };
}

/**
 * 以指针附近为缩放中心：指针下的那个板面点在缩放前后**落在同一屏幕位置**。
 * 只改查看状态，不动任何卡片坐标。
 *
 * 用于「内容用 transform 缩放 + 容器滚动查看」的场景：为了让指针下的点不动，
 * 需要同时改缩放和滚动（滚动量由 clampScroll 收敛）。
 */
export function zoomAtScroll(
  scroll: Point,
  scale: number,
  factor: number,
  client: Point,
  rect: DOMRectLike,
  viewSize: { w: number; h: number },
  content: { w: number; h: number },
): { scale: number; scroll: Point } {
  const nextScale = clampScale(finite(scale, 1) * finite(factor, 1));
  const viewport: Viewport = { scale: finite(scale, 1), x: -finite(scroll.x, 0), y: -finite(scroll.y, 0) };
  const anchor = toBoardPoint(viewport, client, rect);
  const raw = {
    x: anchor.x * nextScale - (finite(client.x, 0) - finite(rect.left, 0)),
    y: anchor.y * nextScale - (finite(client.y, 0) - finite(rect.top, 0)),
  };
  return { scale: nextScale, scroll: clampScroll(raw, nextScale, viewSize, content) };
}

/**
 * 以指针附近为缩放中心（translate 形式；契约 §8.4 冻结签名）。
 * 组件实际用的是 zoomAtScroll（滚动模型），两者在数学上是同一件事。
 */
export function zoomAt(
  viewport: Viewport,
  factor: number,
  client: Point,
  rect: DOMRectLike,
): Viewport {
  const scale = clampScale(finite(viewport.scale, 1) * finite(factor, 1));
  const before = toBoardPoint(viewport, client, rect);
  return {
    scale,
    x: finite(client.x, 0) - finite(rect.left, 0) - before.x * scale,
    y: finite(client.y, 0) - finite(rect.top, 0) - before.y * scale,
  };
}

/**
 * 滚动量的合法范围：浏览器不接受负滚动量，所以永远是 [0, 内容缩放后尺寸 - 容器尺寸]。
 * 内容比容器小时只能贴 0（此时没有可滚动的查看空间）。
 */
export function clampScroll(
  scroll: Point,
  scale: number,
  viewSize: { w: number; h: number },
  content: { w: number; h: number },
  margin = 0,
): Point {
  const s = clampScale(scale);
  const gap = Math.max(0, finite(margin, 0));
  const clampAxis = (value: number, view: number, size: number): number => {
    const scaled = Math.max(0, size) * s;
    const max = Math.max(0, scaled - Math.max(0, view) + gap);
    return Math.max(0, Math.min(max, finite(value, 0)));
  };
  return {
    x: clampAxis(scroll.x, viewSize.w, content.w),
    y: clampAxis(scroll.y, viewSize.h, content.h),
  };
}

/** 把视口位置换算成滚动量（两者是同一件事的两种写法：scroll = -translate）。 */
export function scrollFromViewport(viewport: Viewport): Point {
  return { x: -finite(viewport.x, 0), y: -finite(viewport.y, 0) };
}

/** 滚轮增量（已归一化为像素）：横向滚轮不算缩放。 */
export function wheelDeltaY(event: { deltaY: number; deltaMode?: number }): number {
  const raw = finite(event.deltaY, 0);
  const mode = finite(event.deltaMode, 0);
  if (mode === 1) return raw * 16; // 按行
  if (mode === 2) return raw * 400; // 按页
  return raw;
}

/** 由滚轮增量算缩放倍率。 */
export function wheelZoomFactor(deltaY: number, sensitivity = WHEEL_ZOOM_SENSITIVITY): number {
  return Math.exp(-finite(deltaY, 0) * finite(sensitivity, WHEEL_ZOOM_SENSITIVITY));
}

/**
 * 把视口收敛到「内容还能被看见」的范围：
 * 内容比容器小的时候贴住原点（不出现莫名其妙的空白），否则允许在两侧各留一点余量。
 */
export function clampViewport(
  viewport: Viewport,
  container: { w: number; h: number },
  content: { w: number; h: number },
  margin = 48,
): Viewport {
  const scale = clampScale(viewport.scale);
  const cw = Math.max(0, finite(container.w, 0));
  const ch = Math.max(0, finite(container.h, 0));
  const sw = Math.max(0, finite(content.w, 0)) * scale;
  const sh = Math.max(0, finite(content.h, 0)) * scale;
  const gap = Math.max(0, finite(margin, 0));
  // 内容放得下 → 贴住原点（offset = 0）；放不下 → 允许两边各留 gap 的余量。
  const clampAxis = (value: number, view: number, size: number): number => {
    if (size <= view) return 0;
    return Math.max(view - size - gap, Math.min(gap, value));
  };
  return {
    scale,
    x: clampAxis(finite(viewport.x, 0), cw, sw),
    y: clampAxis(finite(viewport.y, 0), ch, sh),
  };
}

/** 拖动两个屏幕点 → 板面坐标矩形（框选范围）。 */
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

/** 两个矩形是否有面积重叠（只碰到边不算）。 */
export function rectsIntersect(a: Rect, b: Rect): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

/** 重叠面积（无重叠为 0）。 */
export function overlapArea(a: Rect, b: Rect): number {
  const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  if (w <= 0 || h <= 0) return 0;
  return w * h;
}

/** 点是否在矩形内（含边界）。 */
export function pointInRect(point: Point, rect: Rect): boolean {
  return (
    finite(point.x, 0) >= rect.x &&
    finite(point.x, 0) <= rect.x + rect.w &&
    finite(point.y, 0) >= rect.y &&
    finite(point.y, 0) <= rect.y + rect.h
  );
}
