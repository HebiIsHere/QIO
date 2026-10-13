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
 * 1. 按 `expanded` 数组顺序处理；展开卡在原位置原位放大到收敛后的阅读尺寸，
 *    放不下时向内收敛（先收敛尺寸，再收敛位置），绝不越出可用区域；
 * 2. 与展开矩形相交的**未展开**卡按最短方向让开（右 / 下优先，其次左 / 上），最多 8 轮；
 *    让开后仍相交的记进 `displaced`：界面不许隐藏它，也不再强行移动它；
 * 3. 缺省口径下多个展开卡之间**不互相让位**（契约 §3.6 原文，既有测试锁定）；
 *    生产路径传 `expandedAvoid: true` 时改成「展开卡之间也互相避让」（多张同时阅读时
 *    不许互相遮挡），并继续处理让位造成的**新碰撞**（见 {@link LayoutInput.expandedAvoid}）；
 * 4. 让位只在可用区域内进行：宁可如实记 `displaced`，也不把卡片推到看不见的地方。
 *
 * 可用区域（{@link LayoutInput.stage} + {@link LayoutInput.stageOrigin}）：
 * 调用方要把**平移 / 滚动后的可视起点**、缩放比例、自绘标题栏、底部工具栏、
 * 对话窗口与审批浮层都扣掉之后再传进来（BoardCanvas 负责量屏幕几何）。
 * 缺省 `stageOrigin = {0,0}`、`stage` 只用宽高时，行为与加展开预览之前逐字段一致。
 *
 * 单位一律是**板面坐标**（与 `board.cards` 同一坐标系）。CSS 的 transform: scale / 平移
 * 只改变查看方式：缩放平移后仍然按板面坐标算，因此视觉位置正确。
 *
 * 不读 DOM、不读 store、不改输入：同样的输入永远得到同样的输出。
 */
import { findFreeSlot, sizeLimits, type SizeLimits } from "./resize";

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
  /** 展开后的目标尺寸（放不下时向可用空间收敛） */
  expandedSize: { w: number; h: number };
  /** 可用区域尺寸（板面坐标）：展开卡不得越出它 */
  stage: { w: number; h: number };
  /**
   * 可用区域左上角在板面坐标里的位置 = **平移 / 滚动后的可视起点**。
   * 缺省 {x:0,y:0}：与「只用 stage 宽高、原点固定在板面原点」的既有口径逐字段一致。
   */
  stageOrigin?: { x: number; y: number };
  /** 让位时与展开矩形保持的间距，默认 16 */
  gap?: number;
  /**
   * 展开卡之间是否也互相避让（缺省 false）。
   *
   * 为什么要有这个开关：契约 §3.6 冻结的旧口径是「多个展开卡之间不互相让位」，
   * `src/interactive/__tests__/cardLayout.test.ts` 逐条锁定了它。多卡同时阅读是本轮新增的
   * 使用方式（任务书 §7.2），生产路径（BoardCanvas）恒传 true：后展开的卡给先展开的卡让位，
   * 让位造成的新碰撞最多再迭代 {@link MAX_DISPLACE_ROUNDS} 轮，仍然让不开就如实记 `displaced`。
   */
  expandedAvoid?: boolean;
  /**
   * 用户**亲自调过尺寸**的卡片 id：展开时沿用用户尺寸，不再套默认阅读尺寸。
   *
   * 为什么需要：卡片常态尺寸就是 `card.w/card.h`，用户在展开状态下把卡改小 / 改大之后，
   * 如果展开槽位仍然取 `max(卡尺寸, 阅读尺寸)`，用户会看到「我调了但它自己弹回去」——
   * 尺寸明明已经落到 `card.w/h`，显示却不认。缺省空数组 = 既有口径。
   */
  sizedCards?: readonly string[];
}

export interface LayoutResult {
  /** 只给渲染用：每张卡在这一帧画在哪（未展开的卡就是正式位置） */
  slots: Record<string, LayoutRect>;
  /** 真正被展开的卡片 id（输入里不存在的 id 不产生槽位，也不回传） */
  expanded: string[];
  /**
   * 推了最多 MAX_DISPLACE_ROUNDS 轮后**仍与展开矩形相交**的卡片（契约原文口径）。
   * 它们不被隐藏、也不再被硬推：界面应如实呈现「让不开」。
   * `expandedAvoid` 打开时，被让位后仍与别人重叠的卡片同样记在这里（诚实报告新遮挡）。
   */
  displaced: string[];
}

/** 让位间距（契约默认 16）。 */
export const DEFAULT_LAYOUT_GAP = 16;
/** 让位最多尝试的轮数（契约冻结：8）。 */
export const MAX_DISPLACE_ROUNDS = 8;
/** 展开后的默认阅读区尺寸（板面坐标；可用区域更小或卡片本来就更大时向内收敛）。 */
export const EXPANDED_PREVIEW_SIZE = { w: 560, h: 420 } as const;

/**
 * 卡片常态尺寸的最小口径（契约 §7.3）：标题 + 主要操作 + 状态 + 内容区仍然可用。
 * 尺寸调整的调用方（BoardResizeLayer / BoardCard 的最小尺寸自适应）都引用这一份。
 */
export const CARD_MIN_SIZE = { w: 180, h: 96 } as const;

/** 卡片常态尺寸的上限：再大也只是把别的东西挤出视野，读起来并不会更好。 */
export const CARD_MAX_SIZE = { w: 1600, h: 1200 } as const;

/**
 * 新卡片的默认尺寸。
 *
 * 为什么在这里落一份：`board.ts` 的 DEFAULT_CARD_W/H 没有导出，而新卡落点要按
 * 「实际尺寸」算空白（{@link planNewCardSlot}）。`irl-c-new-card.test.ts` 直接对
 * `board.addCard` 的兜底结果断言这两个数字，一旦 board.ts 改动测试就会红 —— 不允许悄悄漂移。
 */
export const NEW_CARD_SIZE = { w: 260, h: 170 } as const;

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

/** 两个槽位是否逐字段相同（用于判断「这一轮有没有真的移动」）。 */
function sameRect(a: LayoutRect, b: LayoutRect): boolean {
  return Math.abs(a.x - b.x) < 1e-9 && Math.abs(a.y - b.y) < 1e-9 && Math.abs(a.w - b.w) < 1e-9 && Math.abs(a.h - b.h) < 1e-9;
}

/** 可用区域：原点 + 边长（边长 null = 无界）。 */
interface Region {
  x: number;
  y: number;
  w: number | null;
  h: number | null;
}

function regionOf(stage: { w: number; h: number } | undefined, origin: { x: number; y: number } | undefined): Region {
  return {
    x: finite(origin?.x, 0),
    y: finite(origin?.y, 0),
    w: usableStage(stage?.w),
    h: usableStage(stage?.h),
  };
}

/**
 * 卡片尺寸上限：可用区域与 {@link CARD_MAX_SIZE} 取小。
 * 可用区域比最小尺寸还小时由 `sizeLimits` 把最小值向内收敛（不出现「最小值比屏幕还宽」的死结）。
 */
export function cardSizeLimits(area: { w: number; h: number }): SizeLimits {
  const availW = finite(area?.w, 0);
  const availH = finite(area?.h, 0);
  return sizeLimits(
    CARD_MIN_SIZE.w,
    CARD_MIN_SIZE.h,
    // 面积未知（0 / NaN，首帧还没量到布局）时按上限处理：宁可给大一点，也不把卡片压成 1×1
    availW > 0 ? Math.min(CARD_MAX_SIZE.w, availW) : CARD_MAX_SIZE.w,
    availH > 0 ? Math.min(CARD_MAX_SIZE.h, availH) : CARD_MAX_SIZE.h,
  );
}

/**
 * 展开矩形：在原位置原位放大到收敛后的阅读尺寸。
 * - 永不缩小：卡片本来就比阅读尺寸大时保持原尺寸；
 * - 阅读尺寸按**可用区域**收敛（不再是硬编码 560×420 无脑套用）；
 * - 放不下时先收敛尺寸，再收敛位置（夹进可用区域）。
 */
function expandedRect(card: LayoutRect, size: { w: number; h: number }, region: Region, pinned: boolean): LayoutRect {
  // 用户亲自调过尺寸的卡：展开沿用用户尺寸（不放大、也不缩小）
  const targetW = pinned ? Math.max(0, finite(card.w, 0)) : Math.max(card.w, Math.max(0, finite(size?.w, card.w)));
  const targetH = pinned ? Math.max(0, finite(card.h, 0)) : Math.max(card.h, Math.max(0, finite(size?.h, card.h)));
  // 可用区域比卡片本身还小时保持卡片尺寸（宁可溢出，也不把用户的卡压得比它自己还小）
  const w = region.w === null ? targetW : Math.max(Math.min(card.w, targetW), Math.min(targetW, region.w));
  const h = region.h === null ? targetH : Math.max(Math.min(card.h, targetH), Math.min(targetH, region.h));
  const x = region.w === null ? card.x : clamp(card.x, region.x, Math.max(region.x, region.x + region.w - w));
  const y = region.h === null ? card.y : clamp(card.y, region.y, Math.max(region.y, region.y + region.h - h));
  return { x, y, w, h };
}

interface DisplaceMove {
  rect: LayoutRect;
  distance: number;
}

/**
 * 一个方向上的候选让位：把卡片移到**当前所有相交矩形**之外（右 / 下 / 左 / 上各自的最远需求）。
 * 被可用区域边界挡住（无法朝这个方向取得进展，甚至会被夹回去）时返回 null —— 绝不反向移动。
 */
function candidateMove(
  current: LayoutRect,
  hits: LayoutRect[],
  direction: DisplaceDirection,
  gap: number,
  region: Region,
): DisplaceMove | null {
  const limitX = region.w === null ? null : Math.max(region.x, region.x + region.w - current.w);
  const limitY = region.h === null ? null : Math.max(region.y, region.y + region.h - current.h);
  if (direction === "right" || direction === "left") {
    const raw = direction === "right"
      ? Math.max(...hits.map((rect) => rect.x + rect.w)) + gap
      : Math.min(...hits.map((rect) => rect.x)) - gap - current.w;
    /**
     * 边界挡路时这一方向**不算候选**：夹取只能给出「推了一点但没让开」的假进展，
     * 下一轮又会再推一次（同一张卡来回蹭）。宁可换别的方向，或如实记 displaced。
     */
    if (limitX !== null && (raw > limitX || raw < region.x)) return null;
    const distance = direction === "right" ? raw - current.x : current.x - raw;
    if (distance < PROGRESS_EPSILON) return null;
    return { rect: { ...current, x: raw }, distance };
  }
  const raw = direction === "down"
    ? Math.max(...hits.map((rect) => rect.y + rect.h)) + gap
    : Math.min(...hits.map((rect) => rect.y)) - gap - current.h;
  if (limitY !== null && (raw > limitY || raw < region.y)) return null;
  const distance = direction === "down" ? raw - current.y : current.y - raw;
  if (distance < PROGRESS_EPSILON) return null;
  return { rect: { ...current, y: raw }, distance };
}

/**
 * 把一张卡片推离所有阻塞矩形：每轮取**最短**的可行方向（右 / 下优先，其次左 / 上），
 * 最多 8 轮；推不动（被可用区域边界锁死）或推满 8 轮仍相交就诚实停下。
 */
function pushAway(
  start: LayoutRect,
  blockers: LayoutRect[],
  gap: number,
  region: Region,
): { rect: LayoutRect; cleared: boolean } {
  let current = copyRect(start);
  const clear = () => !blockers.some((rect) => rectsOverlap(current, rect));
  for (let round = 0; round < MAX_DISPLACE_ROUNDS; round += 1) {
    if (clear()) return { rect: current, cleared: true };
    const hits = blockers.filter((rect) => rectsOverlap(current, rect));
    let best: DisplaceMove | null = null;
    // DIRECTIONS 的顺序就是平手时的优先级：先出现的（右 / 下）胜出
    for (const direction of DIRECTIONS) {
      const move = candidateMove(current, hits, direction, gap, region);
      if (!move) continue;
      if (!best || move.distance < best.distance - 1e-9) best = move;
    }
    if (!best) break;
    current = best.rect;
  }
  return { rect: current, cleared: clear() };
}

/** 契约 §3.6 的既有让位口径：只把**相交的未展开卡**推离展开矩形，展开卡之间互不干扰。 */
function displaceLegacy(
  cardList: LayoutCard[],
  slots: Record<string, LayoutRect>,
  expandedIds: Set<string>,
  expandedRects: LayoutRect[],
  gap: number,
  region: Region,
): string[] {
  const displaced: string[] = [];
  if (!expandedRects.length) return displaced;
  for (const blocker of expandedRects) {
    for (const card of cardList) {
      if (expandedIds.has(card.id) || displaced.includes(card.id)) continue;
      if (!rectsOverlap(slots[card.id], blocker)) continue;
      const pushed = pushAway(slots[card.id], expandedRects, gap, region);
      slots[card.id] = pushed.rect;
      if (!pushed.cleared) displaced.push(card.id);
    }
  }
  return displaced;
}

/**
 * 多卡同时阅读的让位口径（`expandedAvoid: true`）。
 *
 * 两段：
 * 1. 展开卡按数组顺序逐个落定：后展开的避开**已落定**的展开矩形（先展开的保持原位放大）；
 * 2. 未展开卡离开展开矩形；被让位后与「原本并不重叠」的卡片新产生的遮挡，最多再迭代
 *    {@link MAX_DISPLACE_ROUNDS} 轮去化解（不允许「推开了原来的重叠、又制造新遮挡」）。
 *    用户本来就叠在一起的卡片不算新遮挡：那是用户自己的摆放。
 *
 * 最后统一校验并如实报告：仍然相交的展开卡对、仍压着展开矩形的卡片、
 * 以及让位后仍有新遮挡的卡片都进 `displaced`。
 */
function displaceEnhanced(
  cardList: LayoutCard[],
  slots: Record<string, LayoutRect>,
  expandedOrder: string[],
  expandedIds: Set<string>,
  gap: number,
  region: Region,
): string[] {
  const expandedRects: LayoutRect[] = [];
  const displaced: string[] = [];

  // 1) 展开卡：先展开的保持原位，后展开的让开已落定的那些
  for (const id of expandedOrder) {
    let rect = copyRect(slots[id]);
    const settled = expandedRects.map((item) => copyRect(item));
    if (settled.length) {
      const pushed = pushAway(rect, settled, gap, region);
      rect = pushed.rect;
    }
    slots[id] = rect;
    expandedRects.push(rect);
  }

  // 2) 未展开卡：离开展开矩形 + 处理让位造成的新遮挡
  const order = cardList.filter((card) => !expandedIds.has(card.id));
  const official = new Map<string, LayoutRect>();
  const current = new Map<string, LayoutRect>();
  for (const card of order) {
    const rect = copyRect(slots[card.id]);
    official.set(card.id, rect);
    current.set(card.id, copyRect(rect));
  }
  const movedIds = new Set<string>();
  for (let round = 0; round < MAX_DISPLACE_ROUNDS; round += 1) {
    let progress = false;
    for (const card of order) {
      const rect = current.get(card.id)!;
      const hits: LayoutRect[] = [];
      for (const expanded of expandedRects) {
        if (rectsOverlap(rect, expanded)) hits.push(expanded);
      }
      if (movedIds.has(card.id)) {
        for (const other of order) {
          if (other.id === card.id) continue;
          const otherRect = current.get(other.id)!;
          if (!rectsOverlap(rect, otherRect)) continue;
          // 用户本来就把它俩叠在一起：不是本次让位造成的新遮挡，不动它
          if (rectsOverlap(official.get(card.id)!, official.get(other.id)!)) continue;
          hits.push(otherRect);
        }
      }
      if (!hits.length) continue;
      const pushed = pushAway(rect, hits, gap, region);
      if (sameRect(pushed.rect, rect)) continue;
      current.set(card.id, pushed.rect);
      movedIds.add(card.id);
      progress = true;
    }
    if (!progress) break;
  }
  for (const card of order) slots[card.id] = current.get(card.id)!;

  // 3) 如实报告：让不开的展开卡对、仍压着展开矩形的卡、让位后仍有新遮挡的卡
  const flagged = new Set<string>();
  for (let i = 0; i < expandedOrder.length; i += 1) {
    for (let j = i + 1; j < expandedOrder.length; j += 1) {
      if (!rectsOverlap(expandedRects[i], expandedRects[j])) continue;
      flagged.add(expandedOrder[i]);
      flagged.add(expandedOrder[j]);
    }
  }
  for (const card of order) {
    const rect = current.get(card.id)!;
    if (expandedRects.some((expanded) => rectsOverlap(rect, expanded))) {
      flagged.add(card.id);
      continue;
    }
    if (!movedIds.has(card.id)) continue;
    for (const other of order) {
      if (other.id === card.id) continue;
      if (!rectsOverlap(rect, current.get(other.id)!)) continue;
      if (rectsOverlap(official.get(card.id)!, official.get(other.id)!)) continue;
      flagged.add(card.id);
      break;
    }
  }
  for (const id of [...expandedOrder, ...order.map((card) => card.id)]) {
    if (flagged.has(id)) displaced.push(id);
  }
  return displaced;
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
  const region = regionOf(input?.stage, input?.stageOrigin);
  const size = input?.expandedSize ?? { w: 0, h: 0 };
  const avoid = input?.expandedAvoid === true;
  const pinned = new Set<string>(
    (input?.sizedCards ?? []).filter((id): id is string => typeof id === "string" && id.length > 0),
  );

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

  // 1) 展开卡：原位放大（缺省口径下互不让位；expandedAvoid 时逐个避让已落定的展开矩形）
  const applied: string[] = [];
  const expandedIds = new Set<string>();
  const expandedOrder: string[] = [];
  for (const id of input?.expanded ?? []) {
    if (typeof id !== "string" || !byId.has(id) || expandedIds.has(id)) continue;
    slots[id] = expandedRect(slots[id], size, region, pinned.has(id));
    expandedIds.add(id);
    expandedOrder.push(id);
    applied.push(id);
  }

  // 2) 未展开卡：与展开矩形相交就让开（第八轮后仍相交 → displaced，不再硬推）
  const displaced = avoid
    ? displaceEnhanced(cardList, slots, expandedOrder, expandedIds, gap, region)
    : displaceLegacy(cardList, slots, expandedIds, expandedOrder.map((id) => slots[id]), gap, region);

  return { slots, expanded: applied, displaced };
}

// --- 新卡落点 ---------------------------------------------------------------

export interface NewCardSlotInput {
  /** 新卡尺寸 */
  size: { w: number; h: number };
  /** 当前**可用**（已扣浮层）的板面区域，含原点 */
  area: LayoutRect;
  /** 已有卡片（板面坐标；一般是未删除的正式位置） */
  cards?: readonly LayoutRect[];
  /** 额外禁区（板面坐标；调用方若已把它们从 area 里扣掉可以不传） */
  overlays?: readonly LayoutRect[];
  gap?: number;
}

/**
 * 新卡片的落点：优先放在**当前可用区域的空白位置**，避开已有卡片与浮层。
 *
 * 为什么不再用「固定四列 + 数量取模」：那种排法在平移 / 缩放之后会把新卡放到
 * 视口之外（用户看不到刚落下的卡），并反复落在同一处已有的卡上。
 * 真正稀疏的搜索与「实在放不下时挑伤害最小的一处」由 `resize.findFreeSlot` 负责，
 * 这里只把界面已知的输入（可见区域 + 卡片 + 浮层）整理成它的入参。
 */
export function planNewCardSlot(input: NewCardSlotInput): LayoutRect {
  const slot = findFreeSlot({
    size: { w: Math.max(1, finite(input?.size?.w, 1)), h: Math.max(1, finite(input?.size?.h, 1)) },
    area: {
      x: finite(input?.area?.x, 0),
      y: finite(input?.area?.y, 0),
      w: Math.max(1, finite(input?.area?.w, 1)),
      h: Math.max(1, finite(input?.area?.h, 1)),
    },
    obstacles: [...(input?.cards ?? []), ...(input?.overlays ?? [])].map((rect) => ({
      x: finite(rect?.x, 0),
      y: finite(rect?.y, 0),
      w: Math.max(0, finite(rect?.w, 0)),
      h: Math.max(0, finite(rect?.h, 0)),
    })),
    gap: input?.gap,
  });
  return { x: slot.x, y: slot.y, w: slot.w, h: slot.h };
}
