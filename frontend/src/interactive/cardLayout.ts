/**
 * cardLayout —— 渲染槽位（**恒等映射**）与新卡落点。
 *
 * 契约：_IML-COMMON.md §1 F1「正式几何是唯一权威」。
 *
 * 这里发生过一次语义收敛：从前 `planExpandedLayout` 会在展开时算出一套**临时位置**
 * （原位放大 + 让位 + 视野夹取），卡片、组框、连线都画那套槽位。结果是缩放 / 平移 /
 * 窗口变化 / 浮层开合都会改变卡片的外框 —— 卡片看起来"自己在动"。
 *
 * 现在只有一套几何：`board.cards[].x/y/w/h`。展开阅读只切**卡内内容**（BoardCard 的
 * reading 态），不改自己或任何别人的外框。因此：
 * - {@link planExpandedLayout} 退化为**恒等映射**：`slots` 逐字段等于正式矩形，
 *   `displaced` 恒为 `[]`（没有"让位"这回事了）；
 * - 让位实现（displaceLegacy / displaceEnhanced / pushAway / candidateMove）与
 *   `expandedRect` 的视野夹取一并删除，不再导出；
 * - {@link EXPANDED_PREVIEW_SIZE} 不再参与任何卡片外框，仅为旧引用保留并标注 deprecated；
 * - 视野（stage / stageOrigin / expandedSize / expandedAvoid / sizedCards）不再参与几何，
 *   这些入参只为兼容旧调用方保留且**被忽略**。
 *
 * 仍是纯函数：不读 DOM、不读 store、不改输入。
 */
import { findFreeSlot, sizeLimits, type SizeLimits } from "./resize";
import { clampCardSize } from "./geometry";

export { CARD_MAX_SIZE, CARD_MIN_SIZE } from "./geometry";

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
  /** 板面状态里的**正式位置**（唯一权威，恒等映射的来源） */
  cards: LayoutCard[];
  /** 当前展开的卡片 id（顺序不再有任何几何含义，只用于回传 applied） */
  expanded: string[];
  /**
   * @deprecated F1 之后展开不改外框，本字段被忽略（仅为兼容旧调用方保留）。
   */
  expandedSize?: { w: number; h: number };
  /**
   * @deprecated 视野不再参与卡片几何，本字段被忽略。
   */
  stage?: { w: number; h: number };
  /**
   * @deprecated 视野不再参与卡片几何，本字段被忽略。
   */
  stageOrigin?: { x: number; y: number };
  /**
   * @deprecated 没有让位了，本字段被忽略。
   */
  gap?: number;
  /**
   * @deprecated 没有让位了，本字段被忽略。
   */
  expandedAvoid?: boolean;
  /**
   * @deprecated 展开不再套阅读尺寸，本字段被忽略。
   */
  sizedCards?: readonly string[];
}

export interface LayoutResult {
  /** 每张卡在这一帧画在哪（逐字段等于正式矩形） */
  slots: Record<string, LayoutRect>;
  /** 输入里真实存在的展开卡片 id（不存在的 id 不产生槽位，也不回传） */
  expanded: string[];
  /**
   * 恒为 `[]`：展开不再让位，也不再有"让不开"的卡片。
   * 字段保留是为了让旧调用方 / 旧断言在类型上仍然可读。
   */
  displaced: string[];
}

/** 让位间距（历史契约值；F1 之后不再参与几何，建议新代码用 geometry.DEFAULT_PLACEMENT_GAP）。 */
export const DEFAULT_LAYOUT_GAP = 16;

/**
 * 展开后的默认阅读区尺寸（历史契约值）。
 *
 * @deprecated 展开不再改变卡片外框（F1），本常量**没有任何生产引用**；
 * 仅保留给旧引用，新代码不得再拿它当卡片尺寸。
 */
export const EXPANDED_PREVIEW_SIZE = { w: 560, h: 420 } as const;

/**
 * 新卡片的默认尺寸。
 *
 * 为什么在这里落一份：`board.ts` 的 DEFAULT_CARD_W/H 没有导出，而新卡落点要按
 * 「实际尺寸」算空白（{@link planNewCardSlot}）。`irl-c-new-card.test.ts` 直接对
 * `board.addCard` 的兜底结果断言这两个数字，一旦 board.ts 改动测试就会红 —— 不允许悄悄漂移。
 */
export const NEW_CARD_SIZE = { w: 260, h: 170 } as const;

function finite(value: unknown, fallback = 0): number {
  const result = typeof value === "number" ? value : Number(value);
  return Number.isFinite(result) ? result : fallback;
}

/** 两个矩形是否有面积重叠（只碰到边不算）。 */
export function rectsOverlap(a: LayoutRect, b: LayoutRect): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

/**
 * 卡片尺寸限制：**只**取合约口径，视野大小**永远不是**卡片上限（F2）。
 *
 * `area` 参数被忽略，仅为兼容旧调用方保留。
 */
export function cardSizeLimits(_area?: { w: number; h: number }): SizeLimits {
  // 口径唯一来自 geometry.clampCardSize：下限 / 上限都是它夹出来的那一对值
  const min = clampCardSize({ w: 0, h: 0 });
  const max = clampCardSize({ w: Number.MAX_SAFE_INTEGER, h: Number.MAX_SAFE_INTEGER });
  return sizeLimits(min.w, min.h, max.w, max.h);
}

/**
 * 计算这一帧的渲染槽位。
 *
 * **恒等映射**（F1）：`slots[card.id]` 逐字段等于 `card.x/y/w/h`，`displaced` 恒为 `[]`。
 * 展开只影响卡内内容的呈现方式，不改变任何卡片的外框、不推开邻卡。
 *
 * 绝不修改 `input.cards` / `input.expanded`，返回的 slots 是新的对象（改它不会写回输入）。
 */
export function planExpandedLayout(input: LayoutInput): LayoutResult {
  const cardList = (input?.cards ?? []).filter(
    (card): card is LayoutCard => Boolean(card) && typeof card?.id === "string" && card.id.length > 0,
  );

  const slots: Record<string, LayoutRect> = {};
  const byId = new Set<string>();
  for (const card of cardList) {
    if (byId.has(card.id)) continue; // 重复 id：第一份为准，绝不产生两个槽位
    byId.add(card.id);
    slots[card.id] = {
      x: finite(card.x, 0),
      y: finite(card.y, 0),
      w: Math.max(0, finite(card.w, 0)),
      h: Math.max(0, finite(card.h, 0)),
    };
  }

  const applied: string[] = [];
  const seen = new Set<string>();
  for (const id of input?.expanded ?? []) {
    if (typeof id !== "string" || !byId.has(id) || seen.has(id)) continue;
    seen.add(id);
    applied.push(id);
  }

  return { slots, expanded: applied, displaced: [] };
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
 * 走的是 F2 的 free 口径（`resize.findFreeSlot`，确定性顺序）：
 * 先逐字段试过「可用区左上角 + 每个障碍的边缘 ± gap」，再退回均匀网格。
 *
 * **2026-10-15（D2 修复）**：从前只用可见区域搜一次，可见区域里没有零重叠空位时
 * 就直接接受 findFreeSlot 的「重叠面积最小」兜底 —— 用户看到新建的卡压在既有卡上。
 * 现在分两步，**视野大小不是落点搜索范围的上限**：
 * 1. 先在可见区域里找零重叠落点（既有行为不变）；
 * 2. 找不到就**向右 / 向下各让出一整张卡的空档**再搜一次（坐标可以自由延伸：
 *    画布随内容长大），直到拿到真正零重叠的落点；
 * 3. 只有连扩大后的范围都放不下（病态输入）才退化为「重叠最小」——
 *    此时调用方（AddMenu）会如实说明，绝不假装放得下。
 *
 * 为什么不再用「固定四列 + 数量取模」：那种排法在平移 / 缩放之后会把新卡放到
 * 视口之外（用户看不到刚落下的卡），并反复落在同一处已有的卡上。
 */
export function planNewCardSlot(input: NewCardSlotInput): LayoutRect {
  const size = { w: Math.max(1, finite(input?.size?.w, 1)), h: Math.max(1, finite(input?.size?.h, 1)) };
  const area: LayoutRect = {
    x: finite(input?.area?.x, 0),
    y: finite(input?.area?.y, 0),
    w: Math.max(1, finite(input?.area?.w, 1)),
    h: Math.max(1, finite(input?.area?.h, 1)),
  };
  const obstacles = [...(input?.cards ?? []), ...(input?.overlays ?? [])].map((rect) => ({
    x: finite(rect?.x, 0),
    y: finite(rect?.y, 0),
    w: Math.max(0, finite(rect?.w, 0)),
    h: Math.max(0, finite(rect?.h, 0)),
  }));
  const gap = input?.gap;

  const inView = rectOf(findFreeSlot({ size, area, obstacles, gap }));
  if (isFreeOf(inView, obstacles)) return inView;

  /*
   * 可见区域里没有零重叠落点：扩大搜索范围（向右 / 向下延伸一整张卡）。
   * 用**请求的卡片尺寸**而不是 inView 的宽高：可见区域比卡片还小时 findFreeSlot 会把
   * 尺寸夹进区域，拿那个被夹小的值去算空档会让新卡悄悄变小。
   */
  const wide = extendedArea(area, obstacles, size.w, size.h, gap);
  const widened = rectOf(findFreeSlot({ size, area: wide, obstacles, gap }));
  if (isFreeOf(widened, obstacles)) return widened;

  // 病态输入（障碍物铺满坐标可及范围）才会到这里：如实退化，由调用方说明
  return widened;
}

function rectOf(slot: { x: number; y: number; w: number; h: number }): LayoutRect {
  return { x: slot.x, y: slot.y, w: slot.w, h: slot.h };
}

/** 落点是否与所有障碍**真正零重叠**（只碰到边不算）。 */
function isFreeOf(rect: LayoutRect, obstacles: readonly LayoutRect[]): boolean {
  return !obstacles.some((item) => rectsOverlap(rect, item));
}

/**
 * 扩大后的搜索范围：可见区域 ∪ 全部障碍，再向右 / 向下各让出「一张卡 + gap」的空档。
 *
 * 左 / 上不扩：坐标原点之外的内容永远看不见（负坐标会被 clampCardRect / rebaseToFit 处理），
 * 所以只在右下方向延伸 —— 这也正是用户能滚动到的方向。
 */
function extendedArea(
  area: LayoutRect,
  obstacles: readonly LayoutRect[],
  cardW: number,
  cardH: number,
  gap: number | undefined,
): LayoutRect {
  const spacing = Math.max(0, finite(gap, DEFAULT_LAYOUT_GAP));
  let minX = area.x;
  let minY = area.y;
  let maxX = area.x + area.w;
  let maxY = area.y + area.h;
  for (const item of obstacles) {
    minX = Math.min(minX, item.x);
    minY = Math.min(minY, item.y);
    maxX = Math.max(maxX, item.x + item.w);
    maxY = Math.max(maxY, item.y + item.h);
  }
  const left = Math.max(0, minX);
  const top = Math.max(0, minY);
  return {
    x: left,
    y: top,
    w: Math.max(1, maxX + cardW + spacing - left),
    h: Math.max(1, maxY + cardH + spacing - top),
  };
}
