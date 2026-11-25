/**
 * 板面视口（平移 / 缩放）的坐标换算（子智能体 B 负责实现）。
 *
 * 契约：docs/interactive-mode-contract.md §8.2 / §8.4。
 *
 * 规则：
 * - 板面坐标 = 卡片自己的 x/y 坐标系（**有符号**，可以为负）；屏幕坐标 = 浏览器视口坐标；
 *   rect 是 .board-viewport 自身的 getBoundingClientRect()（原始矩形，不减滚动量）。
 * - 平移缩放只改变查看状态：不形成表达、不调用 QIO、不触发保存。
 *
 * 组件里的统一用法（BoardCanvas 全部走这里，不再自己算坐标）：
 *
 *     // rect 取 .board-viewport 自身的屏幕矩形（**原始**矩形，不减任何滚动量）
 *     const point = toBoardPoint(view, { x: event.clientX, y: event.clientY }, rect);
 *     const screen = toScreenPoint(view, boardPoint, rect);
 *
 * 2026-11-25（无限画布）：板面不再用「内容撑大 + 容器滚动」的有限模型。
 * `view.x / view.y` 是**世界原点在视口内的 CSS 像素偏移**，内容层只做一次
 * `translate(x, y) scale(scale)`（transform-origin: 0 0），视口自己裁切。
 * 于是空板也能向四个方向连续平移，负坐标卡片与正坐标卡片走同一条路径。
 *
 * 因此 effectiveRect / clampScroll / zoomAtScroll / scrollFromViewport 属于**旧滚动模型**，
 * 只为历史调用方与既有用例保留，画布不再使用它们。
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

/**
 * 缩放的**安全值**：非有限 / 非正一律按 1。
 *
 * 与 {@link clampScale} 的区别：这里**不**夹到 [MIN_SCALE, MAX_SCALE]。
 * 坐标换算必须接受「查看全部」算出的、低于 MIN_SCALE 的适配比例 ——
 * 否则投影 / 逆投影会按 0.25 算，而画面按 0.1 画，指针下的板面点立刻漂走。
 */
export function safeScale(scale: number): number {
  const value = finite(scale, 1);
  return value > 0 ? value : 1;
}

/**
 * 把缩放收敛到 [minScale, MAX_SCALE]。
 *
 * `minScale` 默认是 {@link MIN_SCALE}（正常滚轮手感不变）；
 * 「查看全部」需要比 25% 更小的适配比例时，把那个比例作为下限传进来，
 * 后续滚轮缩放才不会在下一帧被夹回 0.25（用户看到的「跳一下」）。
 */
export function clampScale(scale: number, minScale: number = MIN_SCALE): number {
  const floor = Math.max(0, Math.min(MAX_SCALE, finite(minScale, MIN_SCALE)));
  return Math.max(floor, Math.min(MAX_SCALE, finite(scale, 1)));
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
  const scale = safeScale(viewport.scale);
  const x = finite(viewport.x, 0);
  const y = finite(viewport.y, 0);
  return {
    x: (finite(client.x, 0) - finite(rect.left, 0) - x) / scale,
    y: (finite(client.y, 0) - finite(rect.top, 0) - y) / scale,
  };
}

/** 板面坐标 → 屏幕坐标（toBoardPoint 的逆运算）。 */
export function toScreenPoint(viewport: Viewport, point: Point, rect: DOMRectLike): Point {
  const scale = safeScale(viewport.scale);
  const x = finite(viewport.x, 0);
  const y = finite(viewport.y, 0);
  return {
    x: finite(point.x, 0) * scale + x + finite(rect.left, 0),
    y: finite(point.y, 0) * scale + y + finite(rect.top, 0),
  };
}

/**
 * 平移：只改查看位置。
 *
 * 缩放原样保留（不夹取）：「查看全部」可能把比例压到 MIN_SCALE 以下，
 * 平移一次就把它夹回 0.25 会让画面在拖动时突然放大。
 */
export function panBy(viewport: Viewport, dx: number, dy: number): Viewport {
  return {
    scale: safeScale(viewport.scale),
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
 *
 * 指针下的那个板面点在缩放前后**落在同一屏幕位置**；只改查看状态，不动任何卡片坐标。
 * `minScale` 默认 {@link MIN_SCALE}；「查看全部」把比例压到 25% 以下之后，
 * 后续滚轮缩放要把同一个下限传进来，否则下一帧就被夹回 0.25。
 */
export function zoomAt(
  viewport: Viewport,
  factor: number,
  client: Point,
  rect: DOMRectLike,
  minScale: number = MIN_SCALE,
): Viewport {
  const scale = clampScale(finite(viewport.scale, 1) * finite(factor, 1), minScale);
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

// --- 无限画布：视野、适配与点阵相位 -----------------------------------------

/** 板面坐标矩形（与 Viewport 同一坐标系）。 */
export interface WorldRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** 视口尺寸（CSS 像素）。 */
export interface ViewSize {
  w: number;
  h: number;
}

/**
 * 当前视口覆盖的**板面坐标**矩形（世界坐标）。
 *
 * 视口左上角对应板面点 = (−x, −y) / scale；宽高 = 视口尺寸 / scale。
 * 空板同样成立：没有任何卡片时它照样是一块有限的板面区域，只是没有内容。
 */
export function visibleWorldRect(viewport: Viewport, viewSize: ViewSize): WorldRect {
  const scale = safeScale(viewport.scale);
  const w = Math.max(0, finite(viewSize.w, 0));
  const h = Math.max(0, finite(viewSize.h, 0));
  // +0 归一：-0 与 0 在数学上相等，但会让「逐字段相等」的断言与序列化结果看起来不同
  return {
    x: -finite(viewport.x, 0) / scale + 0,
    y: -finite(viewport.y, 0) / scale + 0,
    w: w / scale,
    h: h / scale,
  };
}

/** 一组板面矩形的世界包围盒；空数组返回 null（没有内容，不编造一个框）。 */
export function worldBoundsOf(rects: readonly WorldRect[]): WorldRect | null {
  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const rect of rects) {
    if (!rect) continue;
    const x = finite(rect.x, 0);
    const y = finite(rect.y, 0);
    const w = Math.max(0, finite(rect.w, 0));
    const h = Math.max(0, finite(rect.h, 0));
    minX = Math.min(minX, x);
    minY = Math.min(minY, y);
    maxX = Math.max(maxX, x + w);
    maxY = Math.max(maxY, y + h);
  }
  if (!Number.isFinite(minX) || !Number.isFinite(minY)) return null;
  return { x: minX, y: minY, w: Math.max(0, maxX - minX), h: Math.max(0, maxY - minY) };
}

export interface FitViewOptions {
  /** 目标屏幕矩形（一般是扣掉遮挡后的可用区），屏幕坐标 */
  area: { left: number; top: number; right: number; bottom: number };
  /** 视口自身的屏幕矩形（只取 left / top 作为换算原点） */
  rect: DOMRectLike;
  /** 四周留白（屏幕像素）：留出能辨认卡片边缘的余量 */
  margin?: number;
  /** 适配比例上限（单卡居中时不被异常放大），默认 1 */
  maxScale?: number;
  /** 适配比例下限，默认 MIN_SCALE */
  minScale?: number;
}

/**
 * 「查看全部」：把世界包围盒放进可用区。
 *
 * - 比例 = min(可用宽 / 包围盒宽, 可用高 / 包围盒高)，再夹进 [minScale, maxScale]；
 *   两张远卡需要低于 25% 才能同屏时，这里如实给出那个更小的比例（调用方把它作为
 *   后续缩放的**下限**，下一次滚轮才不会跳回 0.25）。
 * - 位置：包围盒中心对准可用区中心。
 * - 空包围盒（没有内容）返回 null，由调用方回到初始视野。
 */
export function fitViewToBounds(
  bounds: WorldRect | null,
  viewSize: ViewSize,
  options: FitViewOptions,
): Viewport | null {
  if (!bounds) return null;
  const area = options.area;
  const availW = Math.max(0, finite(area.right, 0) - finite(area.left, 0));
  const availH = Math.max(0, finite(area.bottom, 0) - finite(area.top, 0));
  if (!(availW > 0) || !(availH > 0)) return null;
  const margin = Math.max(0, finite(options.margin, 0));
  const innerW = Math.max(1, availW - margin * 2);
  const innerH = Math.max(1, availH - margin * 2);
  const maxScale = Math.max(0.01, finite(options.maxScale, 1));
  const minScale = Math.max(0.01, Math.min(maxScale, finite(options.minScale, MIN_SCALE)));
  const boundsW = Math.max(0, finite(bounds.w, 0));
  const boundsH = Math.max(0, finite(bounds.h, 0));
  // 单点 / 零尺寸内容：不放大，按 1 倍居中（「单卡居中且不被异常放大」）
  const rawScale =
    boundsW > 0 || boundsH > 0
      ? Math.min(boundsW > 0 ? innerW / boundsW : Number.POSITIVE_INFINITY, boundsH > 0 ? innerH / boundsH : Number.POSITIVE_INFINITY)
      : 1;
  const scale = Math.max(minScale, Math.min(maxScale, Number.isFinite(rawScale) ? rawScale : 1));
  const centerX = finite(bounds.x, 0) + boundsW / 2;
  const centerY = finite(bounds.y, 0) + boundsH / 2;
  // 可用区中心（相对视口原点）→ 世界原点偏移
  const areaCenterX = (finite(area.left, 0) + finite(area.right, 0)) / 2 - finite(options.rect.left, 0);
  const areaCenterY = (finite(area.top, 0) + finite(area.bottom, 0)) / 2 - finite(options.rect.top, 0);
  void viewSize;
  return {
    scale,
    x: areaCenterX - centerX * scale,
    y: areaCenterY - centerY * scale,
  };
}

/**
 * 点阵相位：世界原点在视口内的偏移对间距取模，结果永远落在 [0, spacing)。
 *
 * 负偏移必须用 `((offset % spacing) + spacing) % spacing` 收口 ——
 * 直接取模会得到负数，点阵在跨过零点时跳一格（往返平移就能看出来）。
 */
export function gridPhase(offset: number, spacing: number): number {
  const step = finite(spacing, 0);
  if (!(step > 0)) return 0;
  const value = finite(offset, 0) % step;
  return ((value % step) + step) % step;
}

/**
 * 点阵这一帧的屏幕间距与相位。
 *
 * - 间距 = 基准点距 × 缩放（100% 时仍是 24px）；
 * - 缩得很小时按 2 的倍数合并点（避免糊成一片），相位用**同一个世界原点**算，
 *   所以合并前后不会跳格；
 * - 返回的 spacing 是最终屏幕间距，phase 是第一个点相对视口左上角的偏移。
 */
export function dotGridMetrics(
  viewport: Viewport,
  baseSpacing = 24,
  minScreenSpacing = 8,
): { spacing: number; phaseX: number; phaseY: number } {
  const scale = safeScale(viewport.scale);
  const base = Math.max(1, finite(baseSpacing, 24));
  const floor = Math.max(1, finite(minScreenSpacing, 8));
  let step = base;
  let spacing = step * scale;
  // 屏幕间距太小就按 2 的倍数合并：世界间距翻倍，屏幕间距跟着翻倍
  let guard = 0;
  while (spacing < floor && guard < 8) {
    step *= 2;
    spacing = step * scale;
    guard += 1;
  }
  return {
    spacing,
    phaseX: gridPhase(finite(viewport.x, 0), spacing),
    phaseY: gridPhase(finite(viewport.y, 0), spacing),
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

// --- 当前可用板面区域（显示态桥） -------------------------------------------
//
// 为什么要有它：底部「添加」菜单（AddMenu）与画布是两个组件。新卡片要落在
// **画布算出来的可用区域**（已扣平移后的可视起点、自绘标题栏、底部工具栏、对话窗口、
// 审批浮层）里的空白处 —— 让菜单自己去量 DOM 会得到第二份坐标换算，迟早和画布漂移。
// 所以画布每次重算就发布一次，需要的人读这一份（与 chatFrameState 同一个模式：
// 只读显示态，不进板面状态、不产生表达、不排保存）。
//
// 还没算过 / 画布没挂载时返回 null：调用方据此回落到既有兜底行为（不传坐标）。
let liveArea: Rect | null = null;

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function publishBoardArea(area: Rect | null): void {
  // 任何一个数字无效（NaN / 未量到）都不发布：调用方据此回落到既有兜底，而不是拿到一个假区域
  if (!area || !isFiniteNumber(area.x) || !isFiniteNumber(area.y) || !isFiniteNumber(area.w) || !isFiniteNumber(area.h)) {
    liveArea = null;
    return;
  }
  liveArea = area.w > 0 && area.h > 0 ? { x: area.x, y: area.y, w: area.w, h: area.h } : null;
}

/** 当前可用板面区域（板面坐标）的副本；没有发布过时 null。 */
export function readBoardArea(): Rect | null {
  return liveArea ? { ...liveArea } : null;
}
