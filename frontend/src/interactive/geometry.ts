/**
 * geometry —— 卡片几何约束的**唯一**服务（_IML-COMMON.md §1 F1 / §2 F2）。
 *
 * 边界（与 resize.ts 同一条）：
 * - 只做算术：不读 DOM、不读 store、不排队保存、不调用模型、不产生表达；
 * - 输入里的对象一个字段都不改（返回的矩形永远是新的普通对象）；
 * - 同样的输入永远得到同样的输出（确定性，便于测试与复现）。
 *
 * 三条口径：
 * 1. **正式几何是唯一权威**（F1）：本模块只算「这一帧落到哪」，绝不产生第二套位置；
 *    `board.cards[].x/y/w/h` 仍然只由调用方在自己那一帧写回板面状态。
 * 2. **视野不是卡片上限**（F2）：尺寸只在 [CARD_MIN_SIZE, CARD_MAX_SIZE] 里收敛，
 *    视口多大与卡片能长到多大无关；卡片可以大于视野、可以部分 / 完全在屏外。
 * 3. **绝不移动别的卡片**：`bounded`（拉伸）只把**本次被拉伸的那条边**停在
 *    「第一个碰撞邻卡边界 − gap」处；`free`（拖动 / QIO 移动 / 新建）只用
 *    `resize.findFreeSlot` 在确定性顺序下给自己找空位。
 */
import { findFreeSlot } from "./resize";

/** 板面坐标矩形（与 board.cards 同一坐标系）。 */
export interface GeoRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/**
 * 落点策略：
 * - `free`：拖动落点 / QIO 移动 / 新建 —— 期望位置被占就自己找空位，不动别人；
 * - `bounded`：用户拉伸尺寸 —— 被拉伸的那条边在邻卡边界前停住，不动别人。
 */
export type PlacementPolicy = "free" | "bounded";

export interface PlacementInput {
  /** 期望矩形（尺寸已按 {@link clampCardSize} 收敛） */
  rect: GeoRect;
  /** 其他存活卡片（不含自己） */
  others: readonly GeoRect[];
  /** 拉伸 -> "bounded"；移动 / 新建 -> "free" */
  policy: PlacementPolicy;
  /** 与邻卡保持的间距，默认 16 */
  gap?: number;
  /**
   * `bounded` 专用：本次调整**开始前**的矩形。
   *
   * 为什么必须有：契约签名里的 `rect + others + policy` 只能表达「现在想要这个框」，
   * 无法回答「是哪条边被拉了」。有了起始框才能把**被拉伸的那条边**单独停在邻卡边界上，
   * 而固定不动的对边分毫不动（这正是「拉伸不改别的卡、也不改自己的对边」）。
   * 缺省时 `bounded` 退化为 `free`（没有可比对的起点，就只能给自己找空位）。
   */
  anchor?: GeoRect;
}

export interface PlacementResult {
  rect: GeoRect;
  /** 实际矩形是否与期望矩形不同 */
  adjusted: boolean;
  reason: "ok" | "bounded" | "resolved" | "overlap";
}

/** 卡片常态尺寸的下限（板面单位）。 */
export const CARD_MIN_SIZE = { w: 180, h: 96 } as const;
/** 卡片常态尺寸的上限：与视野无关（卡片可以大于可视区）。 */
export const CARD_MAX_SIZE = { w: 1600, h: 1200 } as const;
/** 约束服务缺省间距（与 cardLayout.DEFAULT_LAYOUT_GAP 同一口径）。 */
export const DEFAULT_PLACEMENT_GAP = 16;

/** 小于这个量的边位移不算「这条边被拉了」：避免浮点噪声把固定边判成移动边。 */
const EDGE_EPSILON = 1e-6;

function finite(value: unknown, fallback = 0): number {
  const result = typeof value === "number" ? value : Number(value);
  return Number.isFinite(result) ? result : fallback;
}

function clampNumber(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.max(min, Math.min(max, value));
}

/** 归一化：非有限数按 0 / 最小尺寸处理，宽高不为负。 */
function normalize(rect: GeoRect | null | undefined): GeoRect {
  return {
    x: finite(rect?.x, 0),
    y: finite(rect?.y, 0),
    w: Math.max(0, finite(rect?.w, 0)),
    h: Math.max(0, finite(rect?.h, 0)),
  };
}

/** 两个矩形是否有**面积**重叠（只碰到边不算）。 */
export function rectsOverlap(a: GeoRect, b: GeoRect): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

function sameRect(a: GeoRect, b: GeoRect): boolean {
  return a.x === b.x && a.y === b.y && a.w === b.w && a.h === b.h;
}

/**
 * 尺寸口径：只夹进 [CARD_MIN_SIZE, CARD_MAX_SIZE]。
 *
 * **视野大小永远不是卡片上限**（F2）：调用方不许再按可视区域二次收紧，
 * 卡片可以比屏幕还大、可以部分 / 完全在屏外。
 */
export function clampCardSize(size: { w: number; h: number }): { w: number; h: number } {
  return {
    w: clampNumber(finite(size?.w, CARD_MIN_SIZE.w), CARD_MIN_SIZE.w, CARD_MAX_SIZE.w),
    h: clampNumber(finite(size?.h, CARD_MIN_SIZE.h), CARD_MIN_SIZE.h, CARD_MAX_SIZE.h),
  };
}

/**
 * 这条边被拉了吗：`edge` 是期望矩形的边坐标，`start` 是起始框的同一侧边坐标。
 * 只有「往外扩」才算被拉伸（收缩到比起点还小属于夹紧，不参与邻卡约束）。
 */
function grewTo(edge: number, start: number): boolean {
  return edge > start + EDGE_EPSILON;
}

function shrankTo(edge: number, start: number): boolean {
  return edge < start - EDGE_EPSILON;
}

/**
 * `free`：不重叠就保持期望位置；重叠就用 `resize.findFreeSlot` 在确定性顺序下
 * 找附近空位（先只看与本框相撞的邻卡附近，实在没有空位再扩大到全体邻卡）。
 * 绝不随机挤动其他卡片；全部候选都被占时返回「重叠最小」的一处并标记 `overlap`。
 */
function resolveFree(desired: GeoRect, others: readonly GeoRect[], gap: number): PlacementResult {
  if (!others.some((other) => rectsOverlap(desired, other))) {
    return { rect: desired, adjusted: false, reason: "ok" };
  }
  const blockers = others.filter((other) => rectsOverlap(desired, other));
  for (const scope of [blockers, others]) {
    const area = searchArea(desired, scope, gap);
    const slot = findFreeSlot({
      size: { w: desired.w, h: desired.h },
      area,
      obstacles: others,
      gap,
    });
    const rect: GeoRect = { x: slot.x, y: slot.y, w: desired.w, h: desired.h };
    if (!others.some((other) => rectsOverlap(rect, other))) {
      return { rect, adjusted: !sameRect(rect, desired), reason: "resolved" };
    }
  }
  // 实在放不下：退化为「重叠最小」的一处，如实报告（不假装让开了）
  const area = searchArea(desired, others, gap);
  const slot = findFreeSlot({ size: { w: desired.w, h: desired.h }, area, obstacles: others, gap });
  const rect: GeoRect = { x: slot.x, y: slot.y, w: desired.w, h: desired.h };
  return { rect, adjusted: !sameRect(rect, desired), reason: "overlap" };
}

/**
 * 搜索范围：期望矩形 ∪ 本次要避开的矩形，四边各留「一个卡片尺寸 + gap」的余量。
 *
 * 这样 `findFreeSlot` 的「贴着障碍边缘」候选（邻卡边界 ± gap）一定落在范围内，
 * 于是重叠时给出的位置紧挨着冲突处 —— 而不是把卡片丢到板面另一头。
 */
function searchArea(desired: GeoRect, scope: readonly GeoRect[], gap: number): GeoRect {
  let minX = desired.x;
  let minY = desired.y;
  let maxX = desired.x + desired.w;
  let maxY = desired.y + desired.h;
  for (const rect of scope) {
    minX = Math.min(minX, rect.x);
    minY = Math.min(minY, rect.y);
    maxX = Math.max(maxX, rect.x + rect.w);
    maxY = Math.max(maxY, rect.y + rect.h);
  }
  const padX = desired.w + gap;
  const padY = desired.h + gap;
  return {
    x: minX - padX,
    y: minY - padY,
    w: Math.max(1, maxX - minX + padX * 2),
    h: Math.max(1, maxY - minY + padY * 2),
  };
}

/**
 * `bounded`：只沿**本次被拉伸的那条边**在「第一个碰撞邻卡边界 − gap」处停住。
 * 对边、另一轴、以及所有其他卡片都不动。
 *
 * 无法满足时（起点就已经重叠 / 边被多个邻卡封死 / 边界比最小尺寸还近）如实返回
 * 最接近可行的矩形并保持 `reason: "bounded"`，由调用方给出简短原因。
 */
function resolveBounded(
  desired: GeoRect,
  anchor: GeoRect,
  others: readonly GeoRect[],
  gap: number,
): PlacementResult {
  if (!others.some((other) => rectsOverlap(desired, other))) {
    return { rect: desired, adjusted: false, reason: "ok" };
  }
  const startRight = anchor.x + anchor.w;
  const startBottom = anchor.y + anchor.h;
  let left = desired.x;
  let top = desired.y;
  let right = desired.x + desired.w;
  let bottom = desired.y + desired.h;

  const movingRight = grewTo(right, startRight);
  const movingLeft = shrankTo(left, anchor.x);
  const movingDown = grewTo(bottom, startBottom);
  const movingUp = shrankTo(top, anchor.y);
  if (!movingRight && !movingLeft && !movingDown && !movingUp) {
    // 纯移动（没有边被拉伸）：交给 free 口径，别把卡片钉死在原处
    return resolveFree(desired, others, gap);
  }

  /** 竖直方向与本框相交的邻卡才挡得住左右边（只碰到边不算）。 */
  const blocksVertically = (other: GeoRect): boolean => other.y < bottom && top < other.y + other.h;
  /** 水平方向与本框相交的邻卡才挡得住上下边。 */
  const blocksHorizontally = (other: GeoRect): boolean => other.x < right && left < other.x + other.w;

  let adjusted = false;
  if (movingRight) {
    let limit = Number.POSITIVE_INFINITY;
    for (const other of others) {
      if (!blocksVertically(other)) continue;
      limit = Math.min(limit, other.x - gap);
    }
    // 只有「邻卡边界在起点之外」才算真约束：否则起点就已经重叠，夹进去会凭空缩小卡片
    if (Number.isFinite(limit) && limit >= startRight && limit < right) {
      right = limit;
      adjusted = true;
    }
  }
  if (movingLeft) {
    let limit = Number.NEGATIVE_INFINITY;
    for (const other of others) {
      if (!blocksVertically(other)) continue;
      limit = Math.max(limit, other.x + other.w + gap);
    }
    if (Number.isFinite(limit) && limit <= anchor.x && limit > left) {
      left = limit;
      adjusted = true;
    }
  }
  if (movingDown) {
    let limit = Number.POSITIVE_INFINITY;
    for (const other of others) {
      if (!blocksHorizontally(other)) continue;
      limit = Math.min(limit, other.y - gap);
    }
    if (Number.isFinite(limit) && limit >= startBottom && limit < bottom) {
      bottom = limit;
      adjusted = true;
    }
  }
  if (movingUp) {
    let limit = Number.NEGATIVE_INFINITY;
    for (const other of others) {
      if (!blocksHorizontally(other)) continue;
      limit = Math.max(limit, other.y + other.h + gap);
    }
    if (Number.isFinite(limit) && limit <= anchor.y && limit > top) {
      top = limit;
      adjusted = true;
    }
  }

  const rect: GeoRect = {
    x: left,
    y: top,
    w: Math.max(1, right - left),
    h: Math.max(1, bottom - top),
  };
  return { rect, adjusted, reason: "bounded" };
}

/**
 * 统一入口：所有几何变化都从这里过一次（用户拖动 / 用户拉伸 / QIO 新建 / 移动 / 审批预览）。
 * 返回的矩形是新的普通对象，输入一个字段都不改。
 */
export function resolvePlacement(input: PlacementInput): PlacementResult {
  const desired = normalize(input?.rect);
  const others = (input?.others ?? []).map((rect) => normalize(rect));
  const gap = Math.max(0, finite(input?.gap, DEFAULT_PLACEMENT_GAP));
  if (input?.policy === "bounded" && input.anchor) {
    return resolveBounded(desired, normalize(input.anchor), others, gap);
  }
  return resolveFree(desired, others, gap);
}

/**
 * 相对摆放（F7）：按锚卡（或锚组）的某条边放一张新卡，程序算坐标，不依赖模型猜。
 * 尺寸走同一份 {@link clampCardSize} 口径。
 */
export function placeRelative(
  anchor: GeoRect,
  side: "left" | "right" | "above" | "below",
  size: { w: number; h: number },
  gap: number,
): GeoRect {
  const base = normalize(anchor);
  const clamped = clampCardSize(size);
  const spacing = Math.max(0, finite(gap, 0));
  switch (side) {
    case "left":
      return { x: base.x - spacing - clamped.w, y: base.y, w: clamped.w, h: clamped.h };
    case "right":
      return { x: base.x + base.w + spacing, y: base.y, w: clamped.w, h: clamped.h };
    case "above":
      return { x: base.x, y: base.y - spacing - clamped.h, w: clamped.w, h: clamped.h };
    case "below":
    default:
      return { x: base.x, y: base.y + base.h + spacing, w: clamped.w, h: clamped.h };
  }
}
