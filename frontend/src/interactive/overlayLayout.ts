/**
 * 跨浮层的可用区域与并排策略（子智能体 D 负责实现，主智能体先给出契约骨架）。
 *
 * 契约：docs/interactive-mode-contract.md §9.6。
 *
 * 为什么要有这一层：底部工具栏、右下聊天、右上批量列表、卡片局部工具栏与中央确认框
 * 过去各自用「窗口高度减一个固定值」算位置，窄窗口里必然互相遮挡（上一轮实测两面板相交约 45080px²）。
 * 这里统一按**实际可用区域**算出每个浮层的尺寸上限与位置偏移，并把「并排 / 上下 / 二选一」的模式显式化，
 * 组件只消费结果，不各自猜。
 *
 * 纯函数、无副作用、可单测；不读 DOM，由调用方把量到的数字传进来。
 *
 * 三条几何事实（本模块的全部假设，改版面时必须一起改）：
 * 1. 浮层挂在板面舞台上，舞台上沿 + {@link OVERLAY_EDGE} 是最高的可用行；
 * 2. 底部工具栏顶边以上才是可用列，聊天面板的底边还要再让开它自己的入口按钮
 *    （{@link OVERLAY_CHAT_FOOTER}：工具栏顶边 → 聊天面板底边）；
 * 3. 右侧是固定列：聊天永远贴右（right = {@link OVERLAY_EDGE}），批量列表在并排时被推到它的左边。
 */

export interface OverlayInput {
  /** 视口尺寸（CSS 像素） */
  viewport: { width: number; height: number };
  /** 板面舞台相对视口的位置与高度（浮层的定位父级） */
  stage: { top: number; height: number };
  /** 底部工具栏顶边（相对视口），浮层不许越过它 */
  toolbarTop: number;
  chatOpen: boolean;
  batchOpen: boolean;
  /** 两个面板各自的最小可用尺寸（低于它就没法读） */
  chatMin: { width: number; height: number };
  batchMin: { width: number; height: number };
}

export interface OverlayGeometry {
  /** side-by-side：宽窗口并排；stacked：上下叠放；switched：空间不足，同一时间只显示一个 */
  mode: "side-by-side" | "stacked" | "switched";
  chatMaxWidth: number;
  chatMaxHeight: number;
  chatRight: number;
  batchMaxWidth: number;
  batchMaxHeight: number;
  batchRight: number;
  /** 两个浮层之间的间距 */
  gap: number;
}

/** 面板与舞台边缘、视口边缘之间的最小间距 */
export const OVERLAY_EDGE = 16;
/** 两个面板之间的最小间距 */
export const OVERLAY_GAP = 12;
/**
 * 底部工具栏顶边 → 聊天面板底边的距离。
 *
 * 由来：聊天容器自己的底边在工具栏顶边上方 24px（16px 边距 + 8px 测量余量），
 * 容器里「面板在上、入口按钮在下」，入口按钮实测约 34px 高、与面板间隔 8px。
 * 24 + 34 + 8 = 66；这里取 78 作为**保守值**（入口按钮换行变高也不会被吃掉），
 * 运行时可以按量到的入口按钮高度把多算的部分还回去（见 overlayLayout 的调用方）。
 */
export const OVERLAY_CHAT_FOOTER = 78;
/** 聊天容器的底边：工具栏顶边往上 16px 边距 + 8px 余量（ChatDock 自己的口径） */
export const OVERLAY_CHAT_DOCK_LIFT = 24;
/** 低于这个视口宽度不与聊天并排：ChatDock 在 ≤560px 会变成左右各 12px 的整宽布局 */
export const OVERLAY_SIDE_BY_SIDE_MIN_WIDTH = 640;
/** 聊天面板的偏好宽度（与 ChatDock 自己的 420px 一致） */
export const CHAT_PREFERRED_WIDTH = 420;
/** 聊天面板自身的宽度上限比例（ChatDock 写死 min(420px, 92vw)） */
export const CHAT_WIDTH_RATIO = 0.92;
/** 批量列表的偏好宽度 / 高度 */
export const BATCH_PREFERRED_WIDTH = 400;
export const BATCH_PREFERRED_HEIGHT = 360;

export interface OverlayRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

function num(value: unknown, fallback = 0): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function clamp(value: number, low: number, high: number): number {
  if (!(high > low)) return low;
  return Math.min(Math.max(value, low), high);
}

/** 量到的数字一律取整，避免出现 0.5px 的抖动反复触发重排 */
function px(value: number): number {
  return Math.round(value);
}

interface Bands {
  viewportWidth: number;
  top: number;
  /** 聊天面板可以占的竖直区间 [top, chatBottom] */
  chatBottom: number;
  chatBand: number;
  /** 批量列表可以占的竖直区间 [top, batchBottom]（它上面没有入口按钮，可以更低） */
  batchBottom: number;
  batchBand: number;
  availWidth: number;
}

/** 按实际可用区域算竖直区间与可用宽度（planOverlayGeometry 与 overlayRects 共用，避免两处算法漂移）。 */
function bandsOf(input: OverlayInput): Bands {
  const viewportWidth = Math.max(0, px(num(input?.viewport?.width)));
  const viewportHeight = Math.max(0, px(num(input?.viewport?.height)));
  const stageHeight = Math.max(0, px(num(input?.stage?.height, viewportHeight)));
  const stageTop = clamp(px(num(input?.stage?.top)), 0, viewportHeight);
  const stageBottom = stageTop + stageHeight;
  // 工具栏量不到时按「舞台下沿」算：宁可把浮层排在舞台里，也不越过视口
  const toolbarTop = clamp(px(num(input?.toolbarTop, stageBottom)), stageTop, Math.max(stageTop, stageBottom));
  const top = stageTop + OVERLAY_EDGE;
  const chatBottom = Math.max(top, toolbarTop - OVERLAY_CHAT_FOOTER);
  const batchBottom = Math.max(top, toolbarTop - OVERLAY_EDGE);
  return {
    viewportWidth,
    top,
    chatBottom,
    chatBand: chatBottom - top,
    batchBottom,
    batchBand: batchBottom - top,
    availWidth: Math.max(0, viewportWidth - 2 * OVERLAY_EDGE),
  };
}

function minsOf(input: OverlayInput): { chat: { width: number; height: number }; batch: { width: number; height: number } } {
  return {
    chat: {
      width: Math.max(0, px(num(input?.chatMin?.width))),
      height: Math.max(0, px(num(input?.chatMin?.height))),
    },
    batch: {
      width: Math.max(0, px(num(input?.batchMin?.width))),
      height: Math.max(0, px(num(input?.batchMin?.height))),
    },
  };
}

/**
 * 算浮层几何。
 *
 * 决策顺序（先并排、再上下、最后二选一）：
 * 1. 只有一侧打开 → 没有冲突，各自拿满可用宽度（绝不为「另一个没开的面板」预留宽度）；
 * 2. 两者都开且宽度够 → **并排**：聊天贴右，批量列表整体推到聊天左边，各占满高度；
 * 3. 宽度不够、高度够 → **上下**：批量列表贴舞台上方（有顶无底），聊天贴工具栏上方（有底无顶），
 *    两者的高度上限之和加上间距不超过可用高度；
 * 4. 都不够 → **二选一**：同一时间只显示一个，由调用方保证（本函数只报模式与上限）。
 */
export function planOverlayGeometry(input: OverlayInput): OverlayGeometry {
  const gap = OVERLAY_GAP;
  const bands = bandsOf(input);
  const mins = minsOf(input);
  const both = Boolean(input?.chatOpen) && Boolean(input?.batchOpen);

  const chatCap = Math.min(CHAT_PREFERRED_WIDTH, Math.floor(bands.viewportWidth * CHAT_WIDTH_RATIO));
  const batchCap = Math.min(BATCH_PREFERRED_WIDTH, bands.availWidth);

  if (!both) {
    // 单开：不为未打开的面板预留任何空间
    return {
      mode: "side-by-side",
      chatMaxWidth: Math.max(mins.chat.width, Math.min(chatCap, bands.availWidth)),
      chatMaxHeight: bands.chatBand,
      chatRight: OVERLAY_EDGE,
      batchMaxWidth: Math.max(mins.batch.width, Math.min(batchCap, bands.availWidth)),
      batchMaxHeight: bands.batchBand,
      batchRight: OVERLAY_EDGE,
      gap,
    };
  }

  const fitsSideBySide =
    bands.viewportWidth >= OVERLAY_SIDE_BY_SIDE_MIN_WIDTH &&
    bands.availWidth >= mins.chat.width + gap + mins.batch.width;
  const fitsStacked = bands.chatBand >= mins.chat.height + gap + mins.batch.height;

  if (fitsSideBySide) {
    // 并排：聊天先占右下，批量列表用剩下的宽度，右边贴在聊天左边
    const chatMaxWidth = clamp(chatCap, mins.chat.width, bands.availWidth - gap - mins.batch.width);
    const batchMaxWidth = clamp(batchCap, mins.batch.width, bands.availWidth - gap - chatMaxWidth);
    return {
      mode: "side-by-side",
      chatMaxWidth,
      chatMaxHeight: bands.chatBand,
      chatRight: OVERLAY_EDGE,
      batchMaxWidth,
      batchMaxHeight: bands.batchBand,
      batchRight: OVERLAY_EDGE + chatMaxWidth + gap,
      gap,
    };
  }

  if (fitsStacked) {
    // 上下：批量列表贴顶、聊天贴底，两块上限之和 + 间距 = 可用高度（最坏情况刚好相切，绝不相交）
    const batchMaxHeight = clamp(
      BATCH_PREFERRED_HEIGHT,
      mins.batch.height,
      bands.chatBand - gap - mins.chat.height,
    );
    const chatMaxHeight = Math.max(mins.chat.height, bands.chatBand - gap - batchMaxHeight);
    return {
      mode: "stacked",
      chatMaxWidth: Math.max(mins.chat.width, Math.min(chatCap, bands.availWidth)),
      chatMaxHeight,
      chatRight: OVERLAY_EDGE,
      batchMaxWidth: Math.max(mins.batch.width, Math.min(batchCap, bands.availWidth)),
      batchMaxHeight,
      batchRight: OVERLAY_EDGE,
      gap,
    };
  }

  // 二选一：两个都可以拿满各自的区间，但不允许同时出现
  return {
    mode: "switched",
    chatMaxWidth: Math.max(mins.chat.width, Math.min(chatCap, bands.availWidth)),
    chatMaxHeight: bands.chatBand,
    chatRight: OVERLAY_EDGE,
    batchMaxWidth: Math.max(mins.batch.width, Math.min(batchCap, bands.availWidth)),
    batchMaxHeight: bands.batchBand,
    batchRight: OVERLAY_EDGE,
    gap,
  };
}

/**
 * 把几何换算成两个面板的**最坏情况**外接矩形（用于单测与验收脚本算相交面积）。
 *
 * 聊天贴底、批量列表贴顶，所以「高度取上限」就是各自最坏的一侧；只要这两块不相交，
 * 实际尺寸（≤ 上限）的两块一定也不相交。两个面板只按自己的开合状态取值。
 */
export function overlayRects(
  input: OverlayInput,
  geometry: OverlayGeometry,
): { chat: OverlayRect; batch: OverlayRect } {
  const bands = bandsOf(input);
  const chatWidth = Math.max(0, px(geometry?.chatMaxWidth));
  const chatHeight = Math.max(0, px(geometry?.chatMaxHeight));
  const batchWidth = Math.max(0, px(geometry?.batchMaxWidth));
  const batchHeight = Math.max(0, px(geometry?.batchMaxHeight));
  const chatRight = Math.max(0, px(geometry?.chatRight));
  const batchRight = Math.max(0, px(geometry?.batchRight));
  return {
    chat: {
      x: bands.viewportWidth - chatRight - chatWidth,
      y: bands.chatBottom - chatHeight,
      width: chatWidth,
      height: chatHeight,
    },
    batch: {
      x: bands.viewportWidth - batchRight - batchWidth,
      y: bands.top,
      width: batchWidth,
      height: batchHeight,
    },
  };
}

/** 两个矩形的相交面积（px²）；不相交时为 0。验收脚本与单测都用它，不再各自写一遍。 */
export function intersectionArea(a: OverlayRect, b: OverlayRect): number {
  const width = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
  const height = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
  if (width <= 0 || height <= 0) return 0;
  return width * height;
}

/**
 * 把计划里「保守多算」的聊天页脚还回去。
 *
 * 计划按 {@link OVERLAY_CHAT_FOOTER} 算，运行时量到入口按钮的真实高度后，可以放宽
 * （只在测量值**小于**保守值时放宽，绝不收紧到实测之外）。测的是入口按钮，不是面板，
 * 所以不会因为自己写下的尺寸而反复变化。
 */
export function chatHeightRelaxation(toggleHeight: number): number {
  // 量不到（0 / NaN）就一点都不放宽：宁可少几像素，也不许把面板顶进批量列表
  const height = px(num(toggleHeight, Number.NaN));
  if (!Number.isFinite(height) || height <= 0) return 0;
  const measured = OVERLAY_CHAT_DOCK_LIFT + OVERLAY_GAP + height;
  return Math.max(0, OVERLAY_CHAT_FOOTER - measured);
}
