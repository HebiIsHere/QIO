/**
 * resize —— 尺寸调整的公共算术（互动模式契约 2026-10-13 · IRL §2）
 *
 * 边界（与 docs/interactive-mode-contract.md §3.5 / §3.6 同一条）：
 * - 这个模块**只做算术**：不读 DOM、不写状态、不排队保存、不调用模型、不产生表达。
 * - 尺寸调整是「查看」而不是「材料内容」：调用方提交时走既有板面路径，
 *   后端 `submission.py` 会把纯几何变化归为 `layout_only`（DISPLAY_ONLY_EXPRESSIONS）。
 *
 * 现在只被板面上的卡片使用（`BoardResizeLayer.vue`）：坐标是板面逻辑单位。
 * 旧的可拖动对话窗（ChatDock.vue）已删除，v2 对话区不再可缩放，因此这里只剩板面一种坐标系；
 * 通用算法保留，换算仍由调用方通过 `scale` 负责。
 */

/** 八个方向：四条边 + 四个角（角的位移同时影响两条边）。 */
export type ResizeHandle = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";

export interface ResizeBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface SizeLimits {
  minW: number;
  minH: number;
  maxW: number;
  maxH: number;
}

export interface Point {
  x: number;
  y: number;
}

/** 顺序即渲染顺序：四条边在前、四个角在后（角压住边的接缝）。 */
export const RESIZE_HANDLES: readonly ResizeHandle[] = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];

export const CORNER_HANDLES: readonly ResizeHandle[] = ["ne", "nw", "se", "sw"];

/** 键盘 −/＋ 一步的默认步长（屏幕像素 / 板面单位由调用方决定）。 */
export const RESIZE_STEP = 24;

/** 拖动判定阈值：小于它视为点击，不改尺寸（与既有 useFloatingWindow 的 3px 同量级）。 */
export const RESIZE_DRAG_THRESHOLD = 2;

export function clampNumber(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.max(min, Math.min(max, value));
}

/** 位移净化：非有限数（NaN / ±Infinity 造成的 NaN 中间量）按「没动」处理。 */
function delta(value: number): number {
  return Number.isFinite(value) ? value : 0;
}

/** 可用区域是否已经量到：0 / 负数 / NaN 一律算「还不知道」。 */
export function usableArea(area: { w: number; h: number } | null | undefined): boolean {
  if (!area) return false;
  return Number.isFinite(area.w) && Number.isFinite(area.h) && area.w > 0 && area.h > 0;
}

/**
 * 归一化上下限。
 * 关键：`min` 不允许超过 `max`，否则会出现「最小值比屏幕还宽」的死结（任务书 §3.5）。
 */
export function sizeLimits(minW: number, minH: number, maxW: number, maxH: number): SizeLimits {
  const maxWidth = Math.max(1, maxW);
  const maxHeight = Math.max(1, maxH);
  const minWidth = Math.max(1, Math.min(minW, maxWidth));
  const minHeight = Math.max(1, Math.min(minH, maxHeight));
  return { minW: minWidth, minH: minHeight, maxW: maxWidth, maxH: maxHeight };
}

/**
 * 由**可用区域**推上限：上限 = 可用区域（减内边距），最小值也向内收敛。
 * 窗口（或板面可视范围）不足时优先重排 / 收敛，而不是强行套用超过可用宽度的最小值。
 */
export function limitsForArea(
  area: { w: number; h: number },
  min: { w: number; h: number },
  pad = 0,
): SizeLimits {
  // 布局还没量到（首帧 / 容器 display:none / jsdom）时**不能**给出 {maxW:1,maxH:1}：
  // 那会让一次拖动或一次恢复把卡片 / 窗口压成 1×1（IRL-E 的第一阶段复核 W4）。
  // 「还不知道可用区域」的正确语义是「没有上界」，等真的量到再收紧。
  if (!usableArea(area)) {
    return sizeLimits(min.w, min.h, Number.POSITIVE_INFINITY, Number.POSITIVE_INFINITY);
  }
  const availW = Math.max(1, area.w - Math.max(0, pad));
  const availH = Math.max(1, area.h - Math.max(0, pad));
  return sizeLimits(min.w, min.h, availW, availH);
}

/** 只把宽高夹进限制；位置不动（位置另有专门的收敛函数）。 */
export function clampBox(box: ResizeBox, limits: SizeLimits): ResizeBox {
  return {
    x: box.x,
    y: box.y,
    w: clampNumber(box.w, limits.minW, limits.maxW),
    h: clampNumber(box.h, limits.minH, limits.maxH),
  };
}

/**
 * 八向拖动。
 *
 * `dx` / `dy` 必须是**已经换算到该元素坐标系**的位移：
 * 板面卡片要除以画布缩放（`scale: () => 1 / view.scale`），对话窗口直接是屏幕像素。
 * 拉西 / 北边时对边固定（右下角不动），所以宽高被夹住后位置要跟着回算 ——
 * 否则会出现「已经到最小值，框却还在往右跑」的漂移。
 */
export function resizeBox(
  start: ResizeBox,
  handle: ResizeHandle,
  dx: number,
  dy: number,
  limits: SizeLimits,
): ResizeBox {
  const right = start.x + start.w;
  const bottom = start.y + start.h;
  const ddx = delta(dx);
  const ddy = delta(dy);
  let { x, y, w, h } = start;

  if (handle.includes("e")) {
    w = clampNumber(start.w + ddx, limits.minW, limits.maxW);
  }
  if (handle.includes("w")) {
    w = clampNumber(start.w - ddx, limits.minW, limits.maxW);
    x = right - w;
  }
  if (handle.includes("s")) {
    h = clampNumber(start.h + ddy, limits.minH, limits.maxH);
  }
  if (handle.includes("n")) {
    h = clampNumber(start.h - ddy, limits.minH, limits.maxH);
    y = bottom - h;
  }
  return { x, y, w, h };
}

/**
 * 键盘 −/＋：只改一条边，左上角固定（可预期、不会把卡片推走）。
 * 方向 1 = 变大，-1 = 变小。
 */
export function stepBox(
  start: ResizeBox,
  axis: "w" | "h",
  direction: 1 | -1,
  limits: SizeLimits,
  step = RESIZE_STEP,
): ResizeBox {
  const amount = (direction >= 0 ? 1 : -1) * Math.abs(Number.isFinite(step) ? step : RESIZE_STEP);
  if (axis === "w") {
    return {
      x: start.x,
      y: start.y,
      w: clampNumber(start.w + amount, limits.minW, limits.maxW),
      h: start.h,
    };
  }
  return {
    x: start.x,
    y: start.y,
    w: start.w,
    h: clampNumber(start.h + amount, limits.minH, limits.maxH),
  };
}

/**
 * 恢复已保存的框：先按当前可用区域收敛宽高，再把它挪回可见范围。
 * 用于「刷新 / 重启 / 换窗口尺寸后恢复」——绝不把窗口恢复到屏幕外。
 */
export function convergeBox(
  box: ResizeBox,
  area: { w: number; h: number },
  origin: Point = { x: 0, y: 0 },
): ResizeBox {
  // 还不知道可用区域时只保证「尺寸有效」：把 0 / NaN 变成一个像素，
  // 但**不**把用户的框压成 1×1（首帧量不到布局就缩窗口是 E 复核的 W4）。
  if (!usableArea(area)) {
    const finite = (value: number, fallback: number) =>
      Number.isFinite(value) && value > 0 ? value : fallback;
    return {
      x: Math.max(origin.x, Number.isFinite(box.x) ? box.x : origin.x),
      y: Math.max(origin.y, Number.isFinite(box.y) ? box.y : origin.y),
      w: finite(box.w, 1),
      h: finite(box.h, 1),
    };
  }
  const availW = Math.max(1, area.w);
  const availH = Math.max(1, area.h);
  const w = clampNumber(box.w, 1, availW);
  const h = clampNumber(box.h, 1, availH);
  const maxX = origin.x + Math.max(0, availW - w);
  const maxY = origin.y + Math.max(0, availH - h);
  return {
    x: clampNumber(box.x, origin.x, Math.max(origin.x, maxX)),
    y: clampNumber(box.y, origin.y, Math.max(origin.y, maxY)),
    w,
    h,
  };
}

export function boxEquals(a: ResizeBox, b: ResizeBox, epsilon = 0.5): boolean {
  return (
    Math.abs(a.x - b.x) <= epsilon &&
    Math.abs(a.y - b.y) <= epsilon &&
    Math.abs(a.w - b.w) <= epsilon &&
    Math.abs(a.h - b.h) <= epsilon
  );
}

export function boxesOverlap(a: ResizeBox, b: ResizeBox): boolean {
  return !(a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y);
}

/** 与障碍的重叠面积（用于「实在放不下时挑伤害最小的一处」）。 */
export function overlapArea(a: ResizeBox, b: ResizeBox): number {
  const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  if (w <= 0 || h <= 0) return 0;
  return w * h;
}

export interface FreeSlotInput {
  size: { w: number; h: number };
  /** 当前可见范围（板面坐标） */
  area: ResizeBox;
  /** 已有卡片 / 浮层（板面坐标） */
  obstacles: readonly ResizeBox[];
  gap?: number;
  /** 候选网格的参数：步进越大越快，但可能漏掉窄缝 */
  stepX?: number;
  stepY?: number;
}

/**
 * 新卡片的落点：在**当前可见范围的空白位置**里挑第一处放得下的地方。
 *
 * 为什么不再用「固定四列 + 数量取模」：那种排法在平移 / 缩放之后会把新卡放到
 * 视口之外（用户看不到刚落下的卡），也会反复落在同一处已有的卡上。
 *
 * 顺序是确定性的（自上而下、自左而右），所以测试与用户看到的行为一致。
 * 全部候选都放不下时，退化为「重叠面积最小 + 仍然靠上靠左」的位置 ——
 * 宁可让用户看到新卡压到一点边，也不要把它丢到不可见的地方。
 */
export function findFreeSlot(input: FreeSlotInput): ResizeBox {
  const gap = Math.max(0, input.gap ?? 16);
  const w = clampNumber(input.size.w, 1, Math.max(1, input.area.w));
  const h = clampNumber(input.size.h, 1, Math.max(1, input.area.h));
  const stepX = Math.max(8, input.stepX ?? Math.max(24, Math.round(w / 2)));
  const stepY = Math.max(8, input.stepY ?? Math.max(24, Math.round(h / 2)));
  const maxX = input.area.x + Math.max(0, input.area.w - w);
  const maxY = input.area.y + Math.max(0, input.area.h - h);

  let best: ResizeBox | null = null;
  let bestOverlap = Number.POSITIVE_INFINITY;

  function score(x: number, y: number): number {
    let overlap = 0;
    for (const obstacle of input.obstacles) {
      overlap += overlapArea(
        { x: x - gap, y: y - gap, w: w + gap * 2, h: h + gap * 2 },
        obstacle,
      );
      if (overlap >= bestOverlap) return overlap;
    }
    return overlap;
  }

  function tryAt(x: number, y: number): ResizeBox | null {
    if (x < input.area.x || y < input.area.y || x > maxX || y > maxY) return null;
    const overlap = score(x, y);
    if (overlap === 0) return { x, y, w, h };
    if (overlap < bestOverlap) {
      bestOverlap = overlap;
      best = { x, y, w, h };
    }
    return null;
  }

  // 第一轮：**贴着障碍边缘**的候选。
  // 为什么要有这一轮：均匀网格的步长如果比空档还大，就会从空档上跨过去 ——
  // 明明放得下，却报「放不下」。障碍的右 / 下边缘 + gap 正是最可能出现空档的位置。
  const xs = new Set<number>([input.area.x, maxX]);
  const ys = new Set<number>([input.area.y, maxY]);
  for (const obstacle of input.obstacles) {
    xs.add(obstacle.x + obstacle.w + gap);
    xs.add(obstacle.x - w - gap);
    ys.add(obstacle.y + obstacle.h + gap);
    ys.add(obstacle.y - h - gap);
  }
  const sortedX = [...xs].sort((a, b) => a - b);
  const sortedY = [...ys].sort((a, b) => a - b);
  for (const y of sortedY) {
    for (const x of sortedX) {
      const hit = tryAt(x, y);
      if (hit) return hit;
    }
  }

  // 第二轮：均匀网格兜底（覆盖大面积空白，且保证确定性顺序）。
  for (let y = input.area.y; y <= maxY; y += stepY) {
    for (let x = input.area.x; x <= maxX; x += stepX) {
      const hit = tryAt(x, y);
      if (hit) return hit;
    }
  }
  if (best) return best;
  // 可用区域比卡片还小：直接按可见范围左上角落位（并收敛尺寸）。
  return { x: input.area.x, y: input.area.y, w, h };
}
