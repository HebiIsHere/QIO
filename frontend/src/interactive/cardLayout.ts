/**
 * 临时布局：把「哪几张卡正在展开预览」翻译成**只给渲染用**的槽位（契约 2026-10-12 §3.6）。
 *
 * 为什么必须是纯函数：
 * - 展开、让位、收起都**不写入板面状态**，不产生表达、不触发意图 / 任务、不调用 QIO；
 * - `board.cards[].x/y/w/h` 永远是用户摆放的正式位置；这里返回的 `slots` 只是这一帧画在哪；
 * - 收起 = 下一次调用里 `expanded` 里没有它 → 槽位逐字段回到正式位置（布局自然恢复）；
 *   用户展开期间手动移动过的卡，其正式位置已被用户改过，所以收起后停在**用户的新位置**。
 *
 * 硬规则（与契约 §3.6 一一对应）：
 * 1. 按 `expanded` 数组顺序处理；展开卡在原位置原位放大到 `expandedSize`，
 *    放不下时向内收敛（先收敛尺寸，再收敛位置），绝不越出 stage；
 * 2. 与展开矩形相交的**未展开**卡按最短方向让开（右 / 下优先，其次左 / 上），最多 8 轮；
 *    让开后仍相交的记进 `displaced`：界面不许隐藏它，也不再强行移动它；
 * 3. 多个展开卡之间**不互相让位**（两张卡重叠是用户自己的摆放，不是让位对象）；
 * 4. 让位只在 stage 内进行：宁可如实记 `displaced`，也不把卡片推到看不见的地方。
 *
 * 单位一律是**板面坐标**（与 `board.cards` 同一坐标系）。CSS 的 transform: scale / 平移
 * 只改变查看方式：缩放平移后仍然按板面坐标算，因此视觉位置正确。
 *
 * 不读 DOM、不读 store、不改输入：同样的输入永远得到同样的输出。
 */

/** 渲染用矩形（板面坐标）。 */
export interface LayoutRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** 输入里的卡片：只需要正式位置（BoardCard 的子集，避免把整个板面状态拖进来）。 */
export interface LayoutCard {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface LayoutInput {
  /** 板面状态里的**正式位置**（不因展开而改变） */
  cards: LayoutCard[];
  /** 当前展开的卡片 id（顺序 = 让位处理顺序） */
  expanded: string[];
  /** 展开后的目标尺寸（放不下时向内收敛） */
  expandedSize: { w: number; h: number };
  /** 板面可视范围（板面坐标）：展开卡不得越出它 */
  stage: { w: number; h: number };
  /** 让位时与展开矩形保持的间距，默认 16 */
  gap?: number;
}

export interface LayoutResult {
  /** 只给渲染用：每张卡在这一帧画在哪（未展开的卡就是正式位置） */
  slots: Record<string, LayoutRect>;
  /** 真正被展开的卡片 id（输入里不存在的 id 不产生槽位，也不回传） */
  expanded: string[];
  /**
   * 推了最多 MAX_DISPLACE_ROUNDS 轮后**仍与展开矩形相交**的卡片（契约原文口径）。
   * 它们不被隐藏、也不再被硬推：界面应如实呈现「让不开」。
   */
  displaced: string[];
}

/** 让位间距（契约默认 16）。 */
export const DEFAULT_LAYOUT_GAP = 16;
/** 让位最多尝试的轮数（契约冻结：8）。 */
export const MAX_DISPLACE_ROUNDS = 8;
/** 展开后的默认阅读区尺寸（板面坐标；stage 更小或卡片本来就更大时向内收敛）。 */
export const EXPANDED_PREVIEW_SIZE = { w: 560, h: 420 } as const;

/** 小于半像素的移动不算「推开了」：避免浮点噪声变成死循环或假让位。 */
const PROGRESS_EPSILON = 0.5;

/** 让位方向优先级：先右、再下、再左、再上（代价相同时按这个顺序取）。 */
const DIRECTIONS = ["right", "down", "left", "up"] as const;
type DisplaceDirection = (typeof DIRECTIONS)[number];

function finite(value: unknown, fallback = 0): number {
  const result = typeof value === "number" ? value : Number(value);
  return Number.isFinite(result) ? result : fallback;
}

/** stage 的有效边长：非有限 / 非正 → null（按「无界」处理，而不是把卡片压成 0 尺寸）。 */
function usableStage(value: unknown): number | null {
  const result = finite(value, 0);
  return result > 0 ? result : null;
}

function clamp(value: number, min: number, max: number): number {
  if (max < min) return min;
  return Math.min(max, Math.max(min, value));
}

/** 两个矩形是否有面积重叠（只碰到边不算）。 */
export function rectsOverlap(a: LayoutRect, b: LayoutRect): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

function copyRect(rect: LayoutRect): LayoutRect {
  return { x: rect.x, y: rect.y, w: rect.w, h: rect.h };
}

/**
 * 展开矩形：在原位置原位放大到 expandedSize。
 * - 永不缩小：卡片本来就比 expandedSize 大时保持原尺寸；
 * - 放不下时先收敛尺寸（min(目标, stage)），再收敛位置（夹进 [0, stage - 尺寸]）。
 */
function expandedRect(card: LayoutRect, size: { w: number; h: number }, stage: { w: number; h: number }): LayoutRect {
  const targetW = Math.max(card.w, Math.max(0, finite(size?.w, card.w)));
  const targetH = Math.max(card.h, Math.max(0, finite(size?.h, card.h)));
  const stageW = usableStage(stage?.w);
  const stageH = usableStage(stage?.h);
  const w = stageW === null ? targetW : Math.min(targetW, stageW);
  const h = stageH === null ? targetH : Math.min(targetH, stageH);
  const x = stageW === null ? card.x : clamp(card.x, 0, Math.max(0, stageW - w));
  const y = stageH === null ? card.y : clamp(card.y, 0, Math.max(0, stageH - h));
  return { x, y, w, h };
}

interface DisplaceMove {
  rect: LayoutRect;
  distance: number;
}

/**
 * 一个方向上的候选让位：把卡片移到**当前所有相交展开矩形**之外（右 / 下 / 左 / 上各自的最远需求）。
 * 被 stage 边界挡住（无法朝这个方向取得进展，甚至会被夹回去）时返回 null —— 绝不反向移动。
 */
function candidateMove(
  current: LayoutRect,
  hits: LayoutRect[],
  direction: DisplaceDirection,
  gap: number,
  stage: { w: number; h: number },
): DisplaceMove | null {
  const stageW = usableStage(stage?.w);
  const stageH = usableStage(stage?.h);
  if (direction === "right" || direction === "left") {
    const raw = direction === "right"
      ? Math.max(...hits.map((rect) => rect.x + rect.w)) + gap
      : Math.min(...hits.map((rect) => rect.x)) - gap - current.w;
    const limit = stageW === null ? null : Math.max(0, stageW - current.w);
    /**
     * stage 挡路时这一方向**不算候选**：夹取只能给出「推了一点但没让开」的假进展，
     * 下一轮又会再推一次（同一张卡来回蹭）。宁可换别的方向，或如实记 displaced。
     */
    if (limit !== null && (raw > limit || raw < 0)) return null;
    const distance = direction === "right" ? raw - current.x : current.x - raw;
    if (distance < PROGRESS_EPSILON) return null;
    return { rect: { ...current, x: raw }, distance };
  }
  const raw = direction === "down"
    ? Math.max(...hits.map((rect) => rect.y + rect.h)) + gap
    : Math.min(...hits.map((rect) => rect.y)) - gap - current.h;
  const limit = stageH === null ? null : Math.max(0, stageH - current.h);
  if (limit !== null && (raw > limit || raw < 0)) return null;
  const distance = direction === "down" ? raw - current.y : current.y - raw;
  if (distance < PROGRESS_EPSILON) return null;
  return { rect: { ...current, y: raw }, distance };
}

/**
 * 把一张卡片推离所有展开矩形：每轮取**最短**的可行方向（右 / 下优先，其次左 / 上），
 * 最多 8 轮；推不动（被 stage 边界锁死）或推满 8 轮仍相交就诚实停下。
 */
function pushAway(
  start: LayoutRect,
  blockers: LayoutRect[],
  gap: number,
  stage: { w: number; h: number },
): { rect: LayoutRect; cleared: boolean } {
  let current = copyRect(start);
  const clear = () => !blockers.some((rect) => rectsOverlap(current, rect));
  for (let round = 0; round < MAX_DISPLACE_ROUNDS; round += 1) {
    if (clear()) return { rect: current, cleared: true };
    const hits = blockers.filter((rect) => rectsOverlap(current, rect));
    let best: DisplaceMove | null = null;
    // DIRECTIONS 的顺序就是平手时的优先级：先出现的（右 / 下）胜出
    for (const direction of DIRECTIONS) {
      const move = candidateMove(current, hits, direction, gap, stage);
      if (!move) continue;
      if (!best || move.distance < best.distance - 1e-9) best = move;
    }
    if (!best) break;
    current = best.rect;
  }
  return { rect: current, cleared: clear() };
}

/**
 * 计算这一帧的临时布局。
 *
 * 绝不修改 `input.cards` / `input.expanded`，返回的 slots 是新的对象（改它不会写回输入）。
 */
export function planExpandedLayout(input: LayoutInput): LayoutResult {
  const gap = Math.max(0, finite(input?.gap, DEFAULT_LAYOUT_GAP));
  const cardList = (input?.cards ?? []).filter(
    (card): card is LayoutCard => Boolean(card) && typeof card?.id === "string" && card.id.length > 0,
  );
  const stage = input?.stage ?? { w: 0, h: 0 };
  const size = input?.expandedSize ?? { w: 0, h: 0 };

  const slots: Record<string, LayoutRect> = {};
  const byId = new Map<string, LayoutCard>();
  for (const card of cardList) {
    if (byId.has(card.id)) continue; // 重复 id：第一份为准，绝不产生两个槽位
    byId.set(card.id, card);
    slots[card.id] = {
      x: finite(card.x, 0),
      y: finite(card.y, 0),
      w: Math.max(0, finite(card.w, 0)),
      h: Math.max(0, finite(card.h, 0)),
    };
  }

  // 1) 展开卡：原位放大（互不让位，展开顺序 = 数组顺序）
  const applied: string[] = [];
  const expandedIds = new Set<string>();
  const expandedRects: LayoutRect[] = [];
  for (const id of input?.expanded ?? []) {
    if (typeof id !== "string" || !byId.has(id) || expandedIds.has(id)) continue;
    const rect = expandedRect(slots[id], size, stage);
    slots[id] = rect;
    expandedIds.add(id);
    expandedRects.push(rect);
    applied.push(id);
  }

  // 2) 未展开卡：与展开矩形相交就让开（第八轮后仍相交 → displaced，不再硬推）
  const displaced: string[] = [];
  if (expandedRects.length) {
    for (const blocker of expandedRects) {
      for (const card of cardList) {
        if (expandedIds.has(card.id) || displaced.includes(card.id)) continue;
        if (!rectsOverlap(slots[card.id], blocker)) continue;
        const pushed = pushAway(slots[card.id], expandedRects, gap, stage);
        slots[card.id] = pushed.rect;
        if (!pushed.cleared) displaced.push(card.id);
      }
    }
  }

  return { slots, expanded: applied, displaced };
}
