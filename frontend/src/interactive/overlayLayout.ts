/**
 * 跨浮层的可用区域与并排策略（子智能体 C 负责实现，主智能体先给出契约骨架）。
 *
 * 契约：docs/interactive-mode-contract.md §11.7 / §11.8（覆盖 §9.6 与 §10.6 里的布局写法）。
 *
 * 为什么要有这一层：底部工具栏、右下聊天、右上批量列表、卡片局部工具栏与中央确认框
 * 过去各自用「窗口高度减一个固定值」算位置，窄窗口里必然互相遮挡（上一轮实测两面板相交约 45080px²）。
 * 这里统一按**实际可用区域**算出每个浮层的尺寸上限与位置偏移，并把「并排 / 上下 / 二选一」的模式显式化，
 * 组件只消费结果，不各自猜。
 *
 * 纯函数、无副作用、可单测；不读 DOM，由调用方把量到的数字传进来。
 * 空间不足（switched）时**保留哪一个面板**也在这里判（{@link resolveOverlayPanes}），
 * 组件只负责把结果写回 store 并渲染切换入口，不自己写一套「谁该被收起」的判断。
 *
 * 四条几何事实（本模块的全部假设，改版面时必须一起改）：
 * 1. 浮层挂在板面舞台上，舞台上沿 + {@link OVERLAY_EDGE} 是最高的可用行；
 * 2. 底部工具栏顶边以上才是可用列，聊天面板的底边还要再让开它自己的入口按钮
 *    （{@link OVERLAY_CHAT_FOOTER}：工具栏顶边 → 聊天面板底边）；
 * 3. 右侧是固定列：聊天永远贴右（right = {@link OVERLAY_EDGE}），批量列表在并排时被推到它的左边；
 * 4. 需要切换显示时，{@link OVERLAY_SWITCH_BAR_HEIGHT} 那一行由**同一个布局状态**决定
 *    （{@link OverlayInput.switched}）：不管当前开着几个面板，竖直区间都按它预留，切换条自己也拿到一个矩形；
 *    但「切换显示」以**确有第二个可展示面板**为前提（{@link OverlayInput.batchAvailable}，
 *    契约 §12.6）—— 没有审批批次时没有可去的面板，就不进切换布局、不预留、不出提示条。
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
  /**
   * 本次布局是否处于「需要切换显示」的状态（由调用方按最新几何算一次后固定传入）。
   *
   * 为什么必须显式传：二选一时界面上还有一条**面板切换条**，它占真实高度。
   * 只看 chatOpen / batchOpen 会得到「关掉一个面板就不必再留位置」的错误结论 ——
   * 切换条此时仍然在，于是压在面板底边上（契约 §11.7 明确要求关掉一个面板后仍为它预留）。
   * 为 true 时无论开着几个面板，竖直区间都按同一个状态算，模式也始终是 switched。
   *
   * 前提（契约 §12.6）：切换显示的意义是「还有另一个可展示的面板可去」——
   * {@link batchAvailable} 为 false（这一轮没有任何可展示的审批批次）时，
   * 就算空间不足也不进入切换布局：不预留、不出虚假提示条，聊天自己用满可用高度。
   */
  switched?: boolean;
  /**
   * 审批列表这一轮是否真的可展示（存在有可展示价值的批次列表）。
   *
   * 缺省视为 true：旧调用方不传时行为与之前完全一致；IntentBatchTray 按
   * 「确实存在一个可以展示的批次」传值。为 false 时即使 {@link switched} 为 true
   * 也不算切换显示（§12.6：没有第二个面板，切换条就是虚假提示）。
   */
  batchAvailable?: boolean;
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
  /**
   * 切换条的**最坏情况**矩形（视口坐标）：不需要切换条时 height = 0。
   *
   * 真实元素比它小（宽度按内容收缩、右对齐），但一定落在它里面 ——
   * 所以验收脚本用它算「切换条是否压到面板 / 工具栏 / 视口边缘」是保守而安全的。
   */
  switchBar: OverlayRect;
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

/**
 * 批量列表底边要预留的高度（工具栏顶边 → 批量列表底边）。
 *
 * 聊天入口按钮是**常驻**浮层（不随聊天面板开合），批量列表同样贴右边，
 * 所以它也要让开这一行：实测按钮底边在工具栏顶边往上 58px、高 39px。
 */
export const OVERLAY_BATCH_FOOTER = 100;
/** 聊天容器的底边：工具栏顶边往上 16px 边距 + 8px 余量（ChatDock 自己的口径） */
export const OVERLAY_CHAT_DOCK_LIFT = 24;
/**
 * 聊天容器里「面板 ↔ 入口按钮」的间距（ChatDock 自己的 --sp-2 = 8px）。
 *
 * 与 {@link OVERLAY_GAP}（两个浮层之间的最小间距 = 12px）**不是**同一个数：
 * 混用会让抬升量差 4px，实机测量就会对不上（480×600：面板底边 413 = 工具栏顶边 484 - 24 - 8 - 39）。
 */
export const CHAT_DOCK_TOGGLE_GAP = 8;
/** 面板标识：右侧聊天 / 右上批量列表 */
export type OverlayPane = "chat" | "batch";
/**
 * 空间不足（switched）时「面板切换条」占用的高度（契约 §11.7）。
 *
 * 为什么要有它：二选一时必须**明确告诉用户还能切换**，这条提示不能压在面板或工具栏上。
 * 它占用聊天面板下方的一行（见 bandsOf 的 reserveSwitchBar）：位置在未预留的底边之上、
 * 聊天入口按钮之上，所以两个面板的实际高度都不会碰到它，也不需要观察它自己的尺寸。
 * 高度是常量、由组件按同一个值写进行内样式（box-sizing: border-box），
 * 所以「计划里预留的高度」与「界面上真实占的高度」是同一个数字，不会因为量自己而抖动。
 */
export const OVERLAY_SWITCH_BAR_HEIGHT = 34;
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
  /** 面板可以占的竖直区间 [top, chatBottom]（需要切换条时已经把它那一行让出来） */
  chatBottom: number;
  chatBand: number;
  /** 批量列表不预留切换条时可以更低（它上面没有入口按钮） */
  batchBottom: number;
  batchBand: number;
  availWidth: number;
  /** 切换条占用的那一行 [switchTop, switchBottom]（不需要切换条时两者都是 0） */
  switchTop: number;
  switchBottom: number;
}

/**
 * 按实际可用区域算竖直区间与可用宽度（planOverlayGeometry 与 overlayRects 共用，避免两处算法漂移）。
 *
 * reserveSwitchBar = true 时把面板区间抬高「面板切换条 + 上下各一个间距」：
 * 先判定够不够、再留位置，而且**必然**是缩小可用高度，所以不会出现「留位置反而放得下」的循环依赖。
 * 切换条自己占的那一行也在这里算出来（同一个函数），组件与验收脚本不必各自推一遍。
 */
function bandsOf(input: OverlayInput, reserveSwitchBar = false): Bands {
  const viewportWidth = Math.max(0, px(num(input?.viewport?.width)));
  const viewportHeight = Math.max(0, px(num(input?.viewport?.height)));
  const stageHeight = Math.max(0, px(num(input?.stage?.height, viewportHeight)));
  const stageTop = clamp(px(num(input?.stage?.top)), 0, viewportHeight);
  const stageBottom = stageTop + stageHeight;
  // 工具栏量不到时按「舞台下沿」算：宁可把浮层排在舞台里，也不越过视口
  const toolbarTop = clamp(px(num(input?.toolbarTop, stageBottom)), stageTop, Math.max(stageTop, stageBottom));
  const top = stageTop + OVERLAY_EDGE;
  // 未预留时的面板底边：工具栏顶边往上让开聊天入口按钮那一行
  const baseChatBottom = Math.max(top, toolbarTop - OVERLAY_CHAT_FOOTER);
  const switchBottom = reserveSwitchBar ? Math.max(top, baseChatBottom - OVERLAY_GAP) : 0;
  const switchTop = reserveSwitchBar ? Math.max(top, switchBottom - OVERLAY_SWITCH_BAR_HEIGHT) : 0;
  const chatBottom = reserveSwitchBar ? Math.max(top, switchTop - OVERLAY_GAP) : baseChatBottom;
  /**
   * 批量列表的底边。
   *
   * 右下角**始终**浮着聊天入口按钮（实机实测：按钮底边在工具栏顶边往上 58px、高 39px），
   * 批量列表也贴右边，所以它同样不能盖住那个按钮 —— 否则用户点不到聊天入口（契约 §11.7）。
   * 预留值取 58 + 39 = 97，再留一点余量。
   */
  const batchBottom = Math.max(top, toolbarTop - OVERLAY_BATCH_FOOTER);
  return {
    viewportWidth,
    top,
    chatBottom,
    chatBand: chatBottom - top,
    batchBottom,
    batchBand: batchBottom - top,
    availWidth: Math.max(0, viewportWidth - 2 * OVERLAY_EDGE),
    switchTop,
    switchBottom,
  };
}

/** 切换条矩形：只有切换显示状态才有高度，且绝不越出舞台可用区 */
function switchBarRectOf(bands: Bands, needed: boolean): OverlayRect {
  const height = needed ? clamp(OVERLAY_SWITCH_BAR_HEIGHT, 0, Math.max(0, bands.switchBottom - bands.top)) : 0;
  return {
    x: OVERLAY_EDGE,
    y: height > 0 ? bands.switchTop : 0,
    width: bands.availWidth,
    height,
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
 * 决策顺序（先认显式状态，再并排、再上下、最后二选一）：
 * 0. 调用方声明 {@link OverlayInput.switched}（需要切换显示）→ 直接按 switched 算：
 *    无论当前开着几个面板，竖直区间都为切换条预留，模式也保持 switched
 *    （契约 §11.7 的「同一个明确布局状态」；关掉一个面板不会把这一行还回去）；
 * 1. 只有一侧打开 → 没有冲突，各自拿满可用宽度（绝不为「另一个没开的面板」预留宽度）；
 * 2. 两者都开且宽度够 → **并排**：聊天贴右，批量列表整体推到聊天左边，各占满高度；
 * 3. 宽度不够、高度够 → **上下**：批量列表贴舞台上方（有顶无底），聊天贴工具栏上方（有底无顶），
 *    两者的高度上限之和加上间距不超过可用高度；
 * 4. 都不够 → **二选一**：同一时间只显示一个，由调用方保证（本函数只报模式与上限）。
 *
 * 空间判定只用「未预留切换条」的区间：判定必须只由空间决定，与当前是否已经在切换显示无关，
 * 否则会自我实现（把 switched 传进来就永远放不下、退不回去）。
 */
export function planOverlayGeometry(input: OverlayInput): OverlayGeometry {
  const gap = OVERLAY_GAP;
  const mins = minsOf(input);
  const both = Boolean(input?.chatOpen) && Boolean(input?.batchOpen);
  const plain = bandsOf(input, false);
  /**
   * 切换显示只在自己「真的有第二个面板可去」时成立（契约 §12.6）。
   *
   * cramped 只说明「两个面板同时打开放不下」；当审批列表这一轮根本没有可展示的批次时，
   * 就算调用方声明 switched（可能还是上一轮的切换状态），也不能再预留一行虚假的切换条、
   * 更不能显示切换提示 —— 那样聊天会被白抬一两行、还看到点不动的按钮。
   * 这里的批次列表消失直接让模式退回单开布局，组件侧下一次测量就会把预留还回去。
   */
  const wantsSwitchBar = input?.switched === true && input?.batchAvailable !== false;

  const chatCap = Math.min(CHAT_PREFERRED_WIDTH, Math.floor(plain.viewportWidth * CHAT_WIDTH_RATIO));
  const batchCap = Math.min(BATCH_PREFERRED_WIDTH, plain.availWidth);
  const fitsSideBySide =
    plain.viewportWidth >= OVERLAY_SIDE_BY_SIDE_MIN_WIDTH &&
    plain.availWidth >= mins.chat.width + gap + mins.batch.width;
  const fitsStacked = plain.chatBand >= mins.chat.height + gap + mins.batch.height;

  let mode: OverlayGeometry["mode"];
  if (wantsSwitchBar) mode = "switched";
  else if (!both) mode = "side-by-side";
  else if (fitsSideBySide) mode = "side-by-side";
  else if (fitsStacked) mode = "stacked";
  else mode = "switched";

  // 预留只发生在真正要切换显示时；切换条矩形与面板上限出自同一次 bandsOf，两处不会漂移
  const bands = bandsOf(input, mode === "switched");
  const switchBar = switchBarRectOf(bands, mode === "switched");
  const chatWidth = Math.max(mins.chat.width, Math.min(chatCap, bands.availWidth));
  const batchWidth = Math.max(mins.batch.width, Math.min(batchCap, bands.availWidth));

  if (mode === "switched") {
    // 二选一：两个面板共用切换条之上那一列（同一时间只显示一个由调用方保证），谁都不会压到切换条
    return {
      mode,
      chatMaxWidth: chatWidth,
      chatMaxHeight: bands.chatBand,
      chatRight: OVERLAY_EDGE,
      batchMaxWidth: batchWidth,
      batchMaxHeight: bands.chatBand,
      batchRight: OVERLAY_EDGE,
      gap,
      switchBar,
    };
  }

  if (!both) {
    // 单开：不为未打开的面板预留任何空间（宽度上）；但两块面板贴的是**同一列**（right = 16），
    // 而右下角始终有聊天入口按钮：批量列表如果按「可以更低」的区间长下去，会盖住「打开对话」
    // 这个主要入口（实机 elementFromPoint 实测被面板里的按钮挡住）。所以单开时两块共用同一个竖直区间。
    return {
      mode: "side-by-side",
      chatMaxWidth: chatWidth,
      chatMaxHeight: bands.chatBand,
      chatRight: OVERLAY_EDGE,
      batchMaxWidth: batchWidth,
      /**
       * 单开时两块面板贴同一列（right = 16），但底边各按**自己的**区间：
       * 批量列表不是聊天容器的一部分，它必须让开整个入口按钮（含按钮自身高度），
       * 否则按钮会被面板挡住、用户点不到「打开对话」（§11.7，实机 elementFromPoint 实测）。
       */
      batchMaxHeight: bands.batchBand,
      batchRight: OVERLAY_EDGE,
      gap,
      switchBar,
    };
  }

  if (mode === "side-by-side") {
    // 并排：聊天先占右下，批量列表用剩下的宽度，右边贴在聊天左边
    const chatMaxWidth = clamp(chatCap, mins.chat.width, bands.availWidth - gap - mins.batch.width);
    const batchMaxWidth = clamp(batchCap, mins.batch.width, bands.availWidth - gap - chatMaxWidth);
    return {
      mode,
      chatMaxWidth,
      chatMaxHeight: bands.chatBand,
      chatRight: OVERLAY_EDGE,
      batchMaxWidth,
      batchMaxHeight: bands.batchBand,
      batchRight: OVERLAY_EDGE + chatMaxWidth + gap,
      gap,
      switchBar,
    };
  }

  // 上下：批量列表贴顶、聊天贴底，两块上限之和 + 间距 = 可用高度（最坏情况刚好相切，绝不相交）
  const batchMaxHeight = clamp(
    BATCH_PREFERRED_HEIGHT,
    mins.batch.height,
    bands.chatBand - gap - mins.chat.height,
  );
  const chatMaxHeight = Math.max(mins.chat.height, bands.chatBand - gap - batchMaxHeight);
  return {
    mode: "stacked",
    chatMaxWidth: chatWidth,
    chatMaxHeight,
    chatRight: OVERLAY_EDGE,
    batchMaxWidth: batchWidth,
    batchMaxHeight,
    batchRight: OVERLAY_EDGE,
    gap,
    switchBar,
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
  // 模式决定要不要为切换条留位置：与 planOverlayGeometry 用同一条判据，两处不会漂移
  const bands = bandsOf(input, geometry?.mode === "switched");
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

/**
 * 切换显示时聊天面板要抬多高，才能真的停在切换条上方（契约 §11.7 的「真实高度」）。
 *
 * 为什么需要：聊天面板的底边不是几何计划说了算 —— ChatDock 把整个聊天容器贴在
 * 「工具栏顶边往上 {@link OVERLAY_CHAT_DOCK_LIFT}」处，容器里「面板在上、入口按钮在下」，
 * 所以面板真实底边 = 工具栏顶边 - {@link OVERLAY_CHAT_DOCK_LIFT} - {@link OVERLAY_GAP} - 入口按钮高度。
 * 只限制 max-height 时面板仍从**自己的底边**向上长，依旧压在切换条上
 * （独立实机复测：480×600 下相交 11934px²）。
 *
 * 抬升量 = 面板真实底边与预留底边之差。量不到入口按钮高度时取**最大**值：
 * 宁可多抬几像素，也不许压住切换条（与「保守页脚」同一个取向）。
 */
export function chatSwitchLift(toggleHeight: number): number {
  const full =
    OVERLAY_CHAT_FOOTER +
    OVERLAY_SWITCH_BAR_HEIGHT +
    2 * OVERLAY_GAP -
    OVERLAY_CHAT_DOCK_LIFT -
    CHAT_DOCK_TOGGLE_GAP;
  const height = px(num(toggleHeight, Number.NaN));
  if (!Number.isFinite(height) || height <= 0) return full;
  return Math.max(0, full - height);
}

/**
 * 空间同时放不下两个面板：把两个都当作打开再算一次，模式为 switched 就是不够。
 *
 * 刻意**与当前开合无关** —— 契约 §10.6 要求「窗口缩放 / 工具栏变高」立即按最新几何决定，
 * 不能等用户再点一次面板才发现空间不够。
 */
export function overlaysAreCramped(input: OverlayInput): boolean {
  // switched 强制为 false：这条判据只回答「按空间够不够」，不能被调用方上一次的结论带偏
  return planOverlayGeometry({ ...input, chatOpen: true, batchOpen: true, switched: false }).mode === "switched";
}

export interface PaneResolution {
  chatOpen: boolean;
  batchOpen: boolean;
  /** 因为空间不足而被收起的面板（null = 没有被迫改变任何开合） */
  closed: OverlayPane | null;
  /** 空间不足时保留的那一个（空间够时等于传入的 preferred，不改变它） */
  preferred: OverlayPane;
}

/**
 * 空间不足时按「用户最近打开的面板」只保留一个（契约 §10.6）。
 *
 * 三条硬规则都落在这里，所以可以被单测直接证明：
 * 1. **幂等**：同样的输入永远得到同样的输出；把结果再喂回来不会再收任何东西 ——
 *    这是「开合不来回反弹」的根据（组件只在结果与当前状态不同时才写 store）。
 * 2. 空间够（cramped=false）时**绝不改开合**：从小窗口恢复大窗口不会自动弹出面板。
 * 3. 只关闭不打开：任何情况下都不会把 close 的面板自己打开（只保留其中一个）。
 *
 * batchAvailable=false（这一批已经处理完、没有可展示的列表）时不会保留批量列表，
 * 否则会出现「保留了一个其实不存在的东西、另一个被关掉」的黑洞。
 */
export function resolveOverlayPanes(input: {
  cramped: boolean;
  chatOpen: boolean;
  batchOpen: boolean;
  preferred: OverlayPane;
  batchAvailable?: boolean;
}): PaneResolution {
  const chatOpen = Boolean(input?.chatOpen);
  const batchOpen = Boolean(input?.batchOpen);
  const preferred: OverlayPane = input?.preferred === "batch" ? "batch" : "chat";
  const batchAvailable = input?.batchAvailable !== false;
  const unchanged = { chatOpen, batchOpen, closed: null as OverlayPane | null, preferred };

  if (!input?.cramped) return unchanged;
  if (!chatOpen && !batchOpen) return unchanged;
  if (chatOpen !== batchOpen) {
    // 只有一个打开：保留它自己，绝不顺手打开另一个
    return unchanged;
  }
  // 两个都开：按最近打开的决定保留谁
  let keep: OverlayPane = preferred;
  if (keep === "batch" && !batchAvailable) keep = "chat";
  const closed: OverlayPane = keep === "chat" ? "batch" : "chat";
  return {
    chatOpen: keep === "chat",
    batchOpen: keep === "batch",
    closed,
    preferred: keep,
  };
}

// --- 板面定位与浮条锚定（契约 M8，2026-10-09 收尾轮） ----------------------
//
// 反例 19 的三个入口共用同一批几何事实，统一收口在这里（不按入口写特例）：
//   ① 预览右 / 下 / 左 / 上任一方向离屏，定位都要把它移进实际可用区域；
//   ② 完全离屏的预览矩形不作为浮条的可见锚点；
//   ③ 操作浮条优先满足视口约束（留在视口内），其次才轮到浮层互避让。
// 全部输入先过 {@link validRect}：NaN / Infinity 不产生位移或位置，也不产生「假反例」。

export interface BoxRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/** 矩形四个数字全部有限才有效；任一无效返回 null（调用方当作「没有量到这个矩形」）。 */
export function validRect(rect: BoxRect | null | undefined): BoxRect | null {
  if (!rect) return null;
  const left = num(rect.left, Number.NaN);
  const top = num(rect.top, Number.NaN);
  const right = num(rect.right, Number.NaN);
  const bottom = num(rect.bottom, Number.NaN);
  if (![left, top, right, bottom].every((value) => Number.isFinite(value))) return null;
  return { left, top, right, bottom };
}

/** 矩形与视口有**可见交集**才算可见：完全离屏、零面积与无效数值都不是可见锚点。 */
export function rectVisibleIn(rect: BoxRect | null | undefined, viewport: BoxRect): boolean {
  const target = validRect(rect);
  const view = validRect(viewport);
  if (!target || !view) return false;
  const width = Math.min(target.right, view.right) - Math.max(target.left, view.left);
  const height = Math.min(target.bottom, view.bottom) - Math.max(target.top, view.top);
  return width > 0 && height > 0;
}

export interface PanRevealInput {
  /** 可用区域（屏幕坐标：板面视口矩形的四边） */
  viewport: BoxRect;
  /** 目标矩形当前的屏幕位置 */
  target: BoxRect;
  /** 四边留白 */
  margin: number;
}

/**
 * 把目标矩形移进可用区域所需的位移（{@link PanRevealInput}）。
 *
 * 返回的 dx / dy 是**目标需要的位移**：右侧越出 → dx 为负（内容向左移动进视口），
 * 与既有卡片定位的 scroll 换算同一条语义。四个方向都判（基线只判左 / 上，右 / 下不动）；
 * 目标比可用区还宽时按左 / 上对齐（与既有行为一致）；任何无效输入 → {0,0}。
 */
export function panDeltaToReveal(input: PanRevealInput): { dx: number; dy: number } {
  const view = validRect(input?.viewport);
  const target = validRect(input?.target);
  if (!view || !target) return { dx: 0, dy: 0 };
  const margin = Math.max(0, num(input?.margin));
  let dx = 0;
  if (target.left < view.left + margin) dx = view.left + margin - target.left;
  else if (target.right > view.right - margin) dx = view.right - margin - target.right;
  let dy = 0;
  if (target.top < view.top + margin) dy = view.top + margin - target.top;
  else if (target.bottom > view.bottom - margin) dy = view.bottom - margin - target.bottom;
  return { dx, dy };
}

export type DockStripMode = "anchored-below" | "anchored-above" | "anchored-left" | "anchored-right" | "fallback";

export interface DockStripPlan {
  left: number;
  top: number;
  mode: DockStripMode;
}

export interface DockStripInput {
  /** 视口尺寸（CSS 像素） */
  viewport: { width: number; height: number };
  /** 浮条自身尺寸 */
  strip: { width: number; height: number };
  /** 板面上可见预览的屏幕矩形（已量到）；完全离屏 / 无效 → 内部按无锚点处理 */
  anchor: BoxRect | null;
  /** 必须避开的浮层矩形（聊天 / 批量列表 / 工具栏…） */
  avoid: BoxRect[];
  /** 底部硬边界（工具栏顶边）：浮条底边不得超过 */
  bottomLimit: number;
  /** 顶部硬边界（顶部条下沿） */
  topLimit: number;
  /** 视口边缘间距 */
  edge: number;
  /** 贴近锚点时的间距 */
  gap: number;
}

interface StripCandidate extends DockStripPlan {}

/**
 * 给操作浮条挑一个位置（契约 M8 的共同机制）。
 *
 * 决策顺序：
 * 1. 锚点可见（与可用区有交集）时按「下方 → 上方 → 左侧 → 右侧」出候选；
 *    每个候选先**夹进视口**（视口约束优先），夹不进（可用高度不足）或会**压住锚点本身**
 *    （贴完定位反而遮住预览）的候选直接丢弃；
 * 2. 候选与必须避开的浮层按相交面积取最小（浮层互避让是其次）；
 * 3. 没有锚点或全部候选被丢弃 → 退回板面下沿居中（fallback），同样夹进视口；
 *    极端情况下连 fallback 都放不下时取顶部边界——**宁可贴顶，不出视口**。
 */
export function planDockStrip(input: DockStripInput): DockStripPlan {
  const viewWidth = Math.max(0, px(num(input?.viewport?.width)));
  const viewHeight = Math.max(0, px(num(input?.viewport?.height, Number.NaN)));
  const edge = Math.max(0, num(input?.edge, OVERLAY_EDGE));
  const gap = Math.max(0, num(input?.gap, OVERLAY_GAP));
  const stripWidth = Math.max(0, Math.min(px(num(input?.strip?.width)), viewWidth - 2 * edge));
  const stripHeight = Math.max(0, px(num(input?.strip?.height)));
  const topLimit = num(input?.topLimit, 0);
  const bottomLimit = num(input?.bottomLimit, Number.isFinite(viewHeight) ? viewHeight : Number.NaN);
  const clampX = (left: number): number => clamp(left, edge, Math.max(edge, viewWidth - edge - stripWidth));
  const roomY = bottomLimit - stripHeight - topLimit;
  const clampY = (top: number): number | null =>
    roomY < 0 ? null : clamp(top, topLimit, bottomLimit - stripHeight);
  const penaltyOf = (candidate: StripCandidate): number => {
    const box: BoxRect = {
      left: candidate.left,
      top: candidate.top,
      right: candidate.left + stripWidth,
      bottom: candidate.top + stripHeight,
    };
    return (input?.avoid ?? []).reduce((sum, item) => sum + overlapArea(box, item), 0);
  };

  const candidates: StripCandidate[] = [];
  const anchor = validRect(input?.anchor ?? null);
  const usable: BoxRect | null =
    Number.isFinite(topLimit) && Number.isFinite(bottomLimit)
      ? { left: 0, top: topLimit, right: viewWidth, bottom: bottomLimit }
      : null;
  if (anchor && usable && rectVisibleIn(anchor, usable)) {
    const centerX = anchor.left + (anchor.right - anchor.left) / 2;
    const belowTop = clampY(anchor.bottom + gap);
    if (belowTop !== null) {
      const left = clampX(centerX - stripWidth / 2);
      if (!covers(anchor, left, belowTop, stripWidth, stripHeight)) {
        candidates.push({ left, top: belowTop, mode: "anchored-below" });
      }
    }
    const aboveTop = clampY(anchor.top - gap - stripHeight);
    if (aboveTop !== null) {
      const left = clampX(centerX - stripWidth / 2);
      if (!covers(anchor, left, aboveTop, stripWidth, stripHeight)) {
        candidates.push({ left, top: aboveTop, mode: "anchored-above" });
      }
    }
    const sideTop = clampY(anchor.top);
    if (sideTop !== null) {
      const leftTop = clampX(anchor.left - gap - stripWidth);
      if (leftTop > edge || anchor.left - gap - stripWidth >= edge) {
        if (!covers(anchor, leftTop, sideTop, stripWidth, stripHeight)) {
          candidates.push({ left: leftTop, top: sideTop, mode: "anchored-left" });
        }
      }
      const rightLeft = clampX(anchor.right + gap);
      if (anchor.right + gap + stripWidth <= viewWidth - edge) {
        if (!covers(anchor, rightLeft, sideTop, stripWidth, stripHeight)) {
          candidates.push({ left: rightLeft, top: sideTop, mode: "anchored-right" });
        }
      }
    }
  }

  // fallback：板面下沿居中，同样夹进视口；实在放不下时贴顶部边界（视口约束优先）
  const fallbackLeft = clampX((viewWidth - stripWidth) / 2);
  const fallbackTop = roomY < 0 ? topLimit : clamp(bottomLimit - gap - stripHeight, topLimit, bottomLimit - stripHeight);
  candidates.push({ left: fallbackLeft, top: fallbackTop, mode: "fallback" });

  let best = candidates[candidates.length - 1];
  let bestPenalty = Number.POSITIVE_INFINITY;
  for (const candidate of candidates) {
    const score = penaltyOf(candidate);
    if (score < bestPenalty) {
      bestPenalty = score;
      best = candidate;
    }
    if (score === 0) break;
  }
  return { left: Math.round(best.left), top: Math.round(best.top), mode: best.mode };
}

/** 浮条矩形是否压住锚点（放置结果把用户正看着的预览盖住 → 该候选不可用）。 */
function covers(anchor: BoxRect, left: number, top: number, width: number, height: number): boolean {
  const width_overlap = Math.min(anchor.right, left + width) - Math.max(anchor.left, left);
  const height_overlap = Math.min(anchor.bottom, top + height) - Math.max(anchor.top, top);
  return width_overlap > 0 && height_overlap > 0;
}

function overlapArea(a: BoxRect, b: BoxRect): number {
  const width = Math.min(a.right, b.right) - Math.max(a.left, b.left);
  const height = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
  if (width <= 0 || height <= 0) return 0;
  return width * height;
}
