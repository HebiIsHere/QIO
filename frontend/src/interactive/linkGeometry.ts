/**
 * 关系线几何（用户决策 2 / 任务 B）：两矩形 → 边框交点、可见线段中点、标签避让。
 *
 * 边界（与 geometry.ts 同一条）：
 * - 只做算术：不读 DOM、不写板面、不排队保存、不产生表达；
 * - 输入矩形一个字段都不改；同样的输入永远得到同样的输出；
 * - 这里**不解释**关系的方向与含义，只算线画在哪里。
 *
 * 口径：
 * 1. 两卡片分离时：起点 / 终点 = 中心连线与各自边框的交点（斜向也对）；
 * 2. 重叠 / 包含 / 极近（可见线段太短）时：退回“朝对方最近的边中点”那一对点，
 *    绝不画一条从中心出发、穿进对方卡片的线；
 * 3. 箭头端沿线段方向让出 2px，箭头贴在边框上而不是压在边框里；
 * 4. 标签位置 = 两条边框之间可见线段的中点；标签矩形与任一卡片相交时沿垂直方向
 *    让开（半个标签高 + 间距），仍相交就放到线段一侧（都不相交的那一侧）。
 */

export interface LinkRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface LinkPoint {
  x: number;
  y: number;
}

export interface LinkLabelSize {
  w: number;
  h: number;
}

export type LabelPlacement = "center" | "offset" | "side";

export interface LinkGeometryResult {
  /** 起点（源卡片边框上） */
  x1: number;
  y1: number;
  /** 终点（目标卡片边框上，已沿方向让出 arrowGap，给箭头留位置） */
  x2: number;
  y2: number;
  /** 未让出箭头间距的边框交点（标签中点用它算，避免箭头间距把标签带偏） */
  borderX2: number;
  borderY2: number;
  /** 标签中心 */
  labelX: number;
  labelY: number;
  labelPlacement: LabelPlacement;
  /** 两边框交点之间的可见线段长度 */
  length: number;
  /** true = 重叠 / 包含 / 极近，走的是“最近边中点”兜底 */
  fallback: boolean;
}

export interface LinkGeometryOptions {
  /** 箭头端让出的屏幕/板面距离（默认 2px） */
  arrowGap?: number;
  /** 标签尺寸（不传按 64×20 处理） */
  label?: LinkLabelSize;
  /** 标签避让间距（默认 6） */
  labelGap?: number;
  /** 可见线段短于它就按“极近”处理（默认 6） */
  minVisibleLength?: number;
}

const EPSILON = 1e-6;

function finite(value: unknown, fallback = 0): number {
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : fallback;
}

export function rectCenter(rect: LinkRect): LinkPoint {
  return { x: finite(rect?.x) + Math.max(0, finite(rect?.w)) / 2, y: finite(rect?.y) + Math.max(0, finite(rect?.h)) / 2 };
}

/** 两个矩形是否有**面积**重叠（只碰到边不算）。 */
export function rectsOverlap(a: LinkRect, b: LinkRect): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

/** 一个矩形是否包含另一个矩形。 */
export function rectContains(outer: LinkRect, inner: LinkRect): boolean {
  return (
    outer.x <= inner.x &&
    outer.y <= inner.y &&
    outer.x + outer.w >= inner.x + inner.w &&
    outer.y + outer.h >= inner.y + inner.h
  );
}

/** 点到矩形边框的距离（在内部为 0）。 */
function distanceToRect(point: LinkPoint, rect: LinkRect): number {
  const dx = Math.max(rect.x - point.x, 0, point.x - (rect.x + rect.w));
  const dy = Math.max(rect.y - point.y, 0, point.y - (rect.y + rect.h));
  return Math.hypot(dx, dy);
}

/** 两矩形之间的最短距离（重叠为 0）。 */
export function rectDistance(a: LinkRect, b: LinkRect): number {
  const dx = Math.max(a.x - (b.x + b.w), b.x - (a.x + a.w), 0);
  const dy = Math.max(a.y - (b.y + b.h), b.y - (a.y + a.h), 0);
  return Math.hypot(dx, dy);
}

function edgeMidpoint(rect: LinkRect, side: "top" | "right" | "bottom" | "left"): LinkPoint {
  const cx = finite(rect.x) + Math.max(0, finite(rect.w)) / 2;
  const cy = finite(rect.y) + Math.max(0, finite(rect.h)) / 2;
  switch (side) {
    case "top":
      return { x: cx, y: finite(rect.y) };
    case "right":
      return { x: finite(rect.x) + Math.max(0, finite(rect.w)), y: cy };
    case "bottom":
      return { x: cx, y: finite(rect.y) + Math.max(0, finite(rect.h)) };
    default:
      return { x: finite(rect.x), y: cy };
  }
}

/**
 * 从矩形中心沿“朝目标点”的方向射出，与矩形边框的交点。
 * 目标点在矩形内部 / 与中心重合时退回“朝目标最近的边中点”（斜向时退化为水平）。
 */
export function rectEdgePoint(rect: LinkRect, toward: LinkPoint): LinkPoint {
  const center = rectCenter(rect);
  const dx = finite(toward?.x) - center.x;
  const dy = finite(toward?.y) - center.y;
  const inside =
    finite(toward?.x) >= finite(rect.x) &&
    finite(toward?.x) <= finite(rect.x) + Math.max(0, finite(rect.w)) &&
    finite(toward?.y) >= finite(rect.y) &&
    finite(toward?.y) <= finite(rect.y) + Math.max(0, finite(rect.h));
  if (inside || (Math.abs(dx) < EPSILON && Math.abs(dy) < EPSILON)) {
    if (Math.abs(dx) >= Math.abs(dy)) {
      return edgeMidpoint(rect, dx >= 0 ? "right" : "left");
    }
    return edgeMidpoint(rect, dy >= 0 ? "bottom" : "top");
  }
  const halfW = Math.max(0, finite(rect.w)) / 2;
  const halfH = Math.max(0, finite(rect.h)) / 2;
  let t = Number.POSITIVE_INFINITY;
  if (Math.abs(dx) > EPSILON) {
    const tx = (dx > 0 ? halfW : -halfW) / dx;
    if (tx > 0) t = Math.min(t, tx);
  }
  if (Math.abs(dy) > EPSILON) {
    const ty = (dy > 0 ? halfH : -halfH) / dy;
    if (ty > 0) t = Math.min(t, ty);
  }
  if (!Number.isFinite(t)) return center;
  return { x: Math.round((center.x + dx * t) * 1000) / 1000, y: Math.round((center.y + dy * t) * 1000) / 1000 };
}

/** 重叠 / 包含 / 极近时的一对兜底点：朝对方最近的边中点。 */
export function nearestEdgePair(a: LinkRect, b: LinkRect): { start: LinkPoint; end: LinkPoint } {
  const ca = rectCenter(a);
  const cb = rectCenter(b);
  const dx = cb.x - ca.x;
  const dy = cb.y - ca.y;
  if (Math.abs(dx) >= Math.abs(dy)) {
    return dx >= 0
      ? { start: edgeMidpoint(a, "right"), end: edgeMidpoint(b, "left") }
      : { start: edgeMidpoint(a, "left"), end: edgeMidpoint(b, "right") };
  }
  return dy >= 0
    ? { start: edgeMidpoint(a, "bottom"), end: edgeMidpoint(b, "top") }
    : { start: edgeMidpoint(a, "top"), end: edgeMidpoint(b, "bottom") };
}

function labelOverlaps(label: LinkRect, cards: LinkRect[]): boolean {
  return cards.some((card) => rectsOverlap(label, card));
}

function labelRect(center: LinkPoint, size: LinkLabelSize): LinkRect {
  return { x: center.x - size.w / 2, y: center.y - size.h / 2, w: size.w, h: size.h };
}

function overlapArea(a: LinkRect, b: LinkRect): number {
  const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  return w > 0 && h > 0 ? w * h : 0;
}

/** 标签避让：中心 → 垂直方向让开半高+间距 → 线段一侧（都不相交的一侧）。 */
export function placeLinkLabel(
  start: LinkPoint,
  end: LinkPoint,
  size: LinkLabelSize,
  cards: LinkRect[],
  gap = 6,
): { x: number; y: number; placement: LabelPlacement } {
  const mid = { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 };
  if (!labelOverlaps(labelRect(mid, size), cards)) {
    return { ...mid, placement: "center" };
  }
  const length = Math.hypot(end.x - start.x, end.y - start.y);
  const px = length > EPSILON ? -(end.y - start.y) / length : 0;
  const py = length > EPSILON ? (end.x - start.x) / length : 1;
  const offset = size.h / 2 + gap;
  const candidates = [
    { x: mid.x + px * offset, y: mid.y + py * offset },
    { x: mid.x - px * offset, y: mid.y - py * offset },
  ];
  for (const candidate of candidates) {
    if (!labelOverlaps(labelRect(candidate, size), cards)) {
      return { ...candidate, placement: "offset" };
    }
  }
  // 仍相交：放到线段一侧（离两卡片都够远）。取两个方向里“需要让开距离更小”的那个。
  const maxProjection = (sign: number): number => {
    let max = 0;
    for (const card of cards) {
      const corners = [
        { x: card.x, y: card.y },
        { x: card.x + card.w, y: card.y },
        { x: card.x, y: card.y + card.h },
        { x: card.x + card.w, y: card.y + card.h },
      ];
      for (const corner of corners) {
        const projection = (corner.x - mid.x) * px * sign + (corner.y - mid.y) * py * sign;
        if (projection > max) max = projection;
      }
    }
    return max;
  };
  const sideCandidates = [1, -1].map((sign) => {
    const distance = maxProjection(sign) + size.h / 2 + gap;
    return {
      x: mid.x + px * sign * distance,
      y: mid.y + py * sign * distance,
      area: 0,
      distance,
    };
  });
  for (const candidate of sideCandidates) {
    candidate.area = cards.reduce((sum, card) => sum + overlapArea(labelRect(candidate, size), card), 0);
  }
  sideCandidates.sort((left, right) => left.area - right.area || left.distance - right.distance);
  const best = sideCandidates[0];
  return { x: best.x, y: best.y, placement: "side" };
}

/**
 * 关系线的完整几何（纯函数）。
 *
 * @param a 源卡片矩形（板面坐标）
 * @param b 目标卡片矩形（板面坐标）
 */
export function linkGeometry(
  a: LinkRect,
  b: LinkRect,
  options: LinkGeometryOptions = {},
): LinkGeometryResult {
  const arrowGap = Math.max(0, finite(options.arrowGap, 2));
  const label = options.label ?? { w: 64, h: 20 };
  const labelGap = Math.max(0, finite(options.labelGap, 6));
  const minVisible = Math.max(0, finite(options.minVisibleLength, 6));
  const centerA = rectCenter(a);
  const centerB = rectCenter(b);
  const separated =
    !rectsOverlap(a, b) && !rectContains(a, b) && !rectContains(b, a);
  const near = rectDistance(a, b) <= minVisible;
  let start: LinkPoint;
  let end: LinkPoint;
  let fallback = false;
  if (!separated || near) {
    const pair = nearestEdgePair(a, b);
    start = pair.start;
    end = pair.end;
    // 重叠 / 包含 / 极近一律按“最近边中点”兜底（绝不从中心画穿进卡片的线）
    fallback = true;
  } else {
    start = rectEdgePoint(a, centerB);
    end = rectEdgePoint(b, centerA);
    const length = Math.hypot(end.x - start.x, end.y - start.y);
    if (length <= minVisible) {
      const pair = nearestEdgePair(a, b);
      start = pair.start;
      end = pair.end;
      fallback = true;
    }
  }
  const borderX2 = end.x;
  const borderY2 = end.y;
  const length = Math.hypot(end.x - start.x, end.y - start.y);
  const labelPosition = placeLinkLabel(start, end, label, [a, b], labelGap);
  let x2 = end.x;
  let y2 = end.y;
  if (length > arrowGap && arrowGap > 0) {
    x2 = end.x - ((end.x - start.x) / length) * arrowGap;
    y2 = end.y - ((end.y - start.y) / length) * arrowGap;
  }
  return {
    x1: start.x,
    y1: start.y,
    x2: Math.round(x2 * 1000) / 1000,
    y2: Math.round(y2 * 1000) / 1000,
    borderX2,
    borderY2,
    labelX: Math.round(labelPosition.x * 1000) / 1000,
    labelY: Math.round(labelPosition.y * 1000) / 1000,
    labelPlacement: labelPosition.placement,
    length: Math.round(length * 1000) / 1000,
    fallback,
  };
}
