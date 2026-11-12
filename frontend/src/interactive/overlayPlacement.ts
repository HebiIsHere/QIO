/**
 * 卡片局部工具栏的落点决策 —— 纯函数，便于在 jsdom 里逐条断言。
 *
 * 规则（2026-10-14 第二轮修，用户报告「工具栏与卡片有偏移、松手后还会漂」）：
 * 1. 工具栏左边缘 = 所属卡片**实际显示外框**的左边缘（0 偏差）；
 * 2. 工具栏底边 = 卡片顶边上方固定的 gap 屏幕像素；
 * 3. 不做横向夹取、不做上下翻转、不做侧移 —— 任何视口边缘都不改变这个相对位置。
 *    显隐由调用方决定：**卡片与板面可视区相交、且工具栏屏幕外框也与它相交**时才显示
 *    （严格相交；边贴边不算），见 {@link rectsIntersect}。隐藏只作用于显隐，不改位置。
 *
 * 为什么删掉了旧的避让语义（placeCardOverlay）：它把「不压住卡片」放在「固定相对位置」之上 ——
 * 上方放不下就翻到下方、右边放不下就侧移，于是拖动 / 松手 / 缩放时工具栏会跳到别处；
 * 而用户在同一时刻盯着卡片与工具栏，位置一跳就是「偏移」。现在只认卡片外框这一个锚点。
 *
 * 输入全部是**屏幕坐标**（getBoundingClientRect 原样），输出是**宿主定位原点**的像素：
 * 宿主（.card-toolbar-host）与所属卡片的相对关系是它唯一的坐标来源。
 * 宿主若真的处在缩放祖先下（实测 hostRect.width / host.offsetWidth ≠ 1），按同一实测比例逆变换 ——
 * 不用任何经验补偿常数。
 */
export interface OverlayRect {
  top: number;
  bottom: number;
  left: number;
  right: number;
}

/** 相对宿主定位原点的像素位置（直接写进行内 left / top）。 */
export interface ToolbarPlacement {
  left: number;
  top: number;
}

/** 宿主外框与自身布局盒的实测比例（1 = 无缩放祖先）。 */
export interface ToolbarScale {
  x: number;
  y: number;
}

export interface ToolbarAnchor {
  /** 所属卡片的真实显示外框（屏幕坐标，含边框） */
  cardRect: OverlayRect;
  /** 工具栏宿主的外框（屏幕坐标） */
  hostRect: OverlayRect;
  /** 工具栏实测高度（屏幕像素）；量不到时调用方用 TOOLBAR_H 兜底 */
  toolbarHeight: number;
  /** 工具栏实测宽度（屏幕像素）；提供时用于判断竖卡改贴左边 */
  toolbarWidth?: number;
  /** 工具栏底边与卡片顶边之间的固定间距（屏幕像素） */
  gap: number;
  /** 宿主处于缩放祖先下时的实测比例；缺省 / 非法都按 1（不缩放）处理 */
  hostScale?: ToolbarScale;
  /** true = 直接以卡片左上角为按钮坐标系原点（逐按钮换边用）。 */
  anchorAtCard?: boolean;
}

function safeScale(value: unknown): number {
  const scale = typeof value === "number" ? value : Number(value);
  return Number.isFinite(scale) && scale > 0 ? scale : 1;
}

/**
 * 固定锚点：左对齐卡片左边缘；底边在卡片顶边上方 gap。
 * 只做一次「屏幕坐标 → 宿主定位原点」的换算，外加宿主缩放比例的逆变换。
 */
export function placeToolbarRelativeToCard(input: ToolbarAnchor): ToolbarPlacement {
  const scaleX = safeScale(input.hostScale?.x);
  const scaleY = safeScale(input.hostScale?.y);
  if (input.anchorAtCard) {
    return {
      left: (input.cardRect.left - input.hostRect.left) / scaleX,
      top: (input.cardRect.top - input.hostRect.top) / scaleY,
    };
  }
  const width = input.cardRect.right - input.cardRect.left;
  const height = input.cardRect.bottom - input.cardRect.top;
  const toolbarWidth = Number(input.toolbarWidth);
  /*
   * 长边优先：宽卡贴上边，竖卡贴左边。
   * 工具栏仍是一行图标；贴左边时整体放在卡片左侧、顶部对齐，避开左边中点端口。
   * 不提供 toolbarWidth 的旧调用保持原来的「贴上边」行为。
   */
  const placeLeft = Number.isFinite(toolbarWidth) && toolbarWidth > 0 && height > width * 1.08;
  if (placeLeft) {
    return {
      left: (input.cardRect.left - toolbarWidth - input.gap - input.hostRect.left) / scaleX,
      top: (input.cardRect.top - input.hostRect.top + 8) / scaleY,
    };
  }
  return {
    left: (input.cardRect.left - input.hostRect.left) / scaleX,
    top: (input.cardRect.top - input.hostRect.top - input.toolbarHeight - input.gap) / scaleY,
  };
}

/**
 * 两个矩形是否有交集（严格相交：边贴边不算）。
 * 只用于**可见性**判断 —— 卡片与板面可视区完全不相交时才隐藏工具栏，
 * 部分相交时照常按固定相对位置显示（绝不因此移动工具栏）。
 */
export function rectsIntersect(a: OverlayRect, b: OverlayRect): boolean {
  return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}
// --- #04（PM-QIO032）：卡片工具栏按钮的「按实际可用矩形换边 / 内收」 -------------

/** 工具按钮的边长（屏幕像素，与 .tool-icon 的 30×30 一致）。 */
export const TOOLBAR_BUTTON_SIZE = 30;
/** 相邻按钮的中心步进（px）。 */
export const TOOLBAR_BUTTON_STEP = 36;
/** 首个按钮距卡片左上角的内缩量（px）。 */
export const TOOLBAR_BUTTON_START = 8;
/** 按钮外框与卡片外框的最小间距（px）：必须大于连接点的视觉半径，避免压住连接点。 */
export const TOOLBAR_BUTTON_GAP = 12;
/** 按钮外框与板面可视区边缘的最小留白（px）。 */
export const TOOLBAR_BUTTON_MARGIN = 4;
/** 卡片局部工具栏的按钮数（编辑 / 复制 / 隐藏 / 书签 / 删除）。 */
export const TOOLBAR_BUTTON_COUNT = 5;

/** 按钮相对卡片左上角落在哪条边。 */
export type ToolbarButtonSide = "above" | "below" | "left" | "right" | "inside";

export interface ToolbarButtonBox {
  /** 相对卡片左上角的按钮外框（px）。 */
  x: number;
  y: number;
  w: number;
  h: number;
  side: ToolbarButtonSide;
}

export interface ToolbarButtonLayoutInput {
  /** 卡片这一帧的显示外框（板面坐标）。 */
  card: { x: number; y: number; w: number; h: number };
  /**
   * 板面可用区域（板面坐标）；量不到时传 null —— 此时保持既有「按卡片自身尺寸分列」的行为，
   * 绝不拿 0 当作「可用区域为 0」把按钮挤成一列。
   */
  visible?: { x: number; y: number; w: number; h: number } | null;
  /** 连接点（卡片本地坐标，相对卡片左上角）；「向内调整」时避开它们。 */
  ports?: ReadonlyArray<{ x: number; y: number; r?: number }>;
  count?: number;
  size?: number;
  step?: number;
  start?: number;
  gap?: number;
  margin?: number;
}

function finiteOr(value: unknown, fallback: number): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function clamp(value: number, lo: number, hi: number): number {
  if (!(hi >= lo)) return lo;
  return Math.min(hi, Math.max(lo, value));
}

function buttonBox(x: number, y: number, size: number, side: ToolbarButtonSide): ToolbarButtonBox {
  return { x, y, w: size, h: size, side };
}

/** 「向内调整」后的按钮可能与连接点重叠：整体下移 / 上移到不压住连接点的位置。 */
function avoidPortsForInside(
  boxes: ToolbarButtonBox[],
  ports: ReadonlyArray<{ x: number; y: number; r?: number }>,
  localVisible: { x0: number; y0: number; x1: number; y1: number },
  margin: number,
): ToolbarButtonBox[] {
  if (!ports.length || !boxes.length) return boxes;
  if (!boxes.some((box) => box.side === "inside")) return boxes;
  let shifted = boxes;
  for (let pass = 0; pass < 4; pass += 1) {
    let delta = 0;
    for (const port of ports) {
      const radius = Math.max(0, finiteOr(port.r, 8));
      for (const box of shifted) {
        if (box.side !== "inside") continue;
        const nearestX = clamp(port.x, box.x, box.x + box.w);
        const nearestY = clamp(port.y, box.y, box.y + box.h);
        if (Math.hypot(port.x - nearestX, port.y - nearestY) >= radius) continue;
        // 需要的最小下移量：让按钮上沿离开连接点圆的下沿。
        delta = Math.max(delta, port.y + radius + margin - box.y);
      }
    }
    if (delta <= 0) break;
    shifted = shifted.map((box) => (box.side === "inside" ? { ...box, y: box.y + delta } : box));
  }
  // 夹回可视区：整体上移 / 下移，保证按钮完整可点。
  const inside = shifted.filter((box) => box.side === "inside");
  if (!inside.length) return shifted;
  const top = Math.min(...inside.map((box) => box.y));
  const bottom = Math.max(...inside.map((box) => box.y + box.h));
  let dy = 0;
  if (bottom > localVisible.y1 - margin) dy = localVisible.y1 - margin - bottom;
  else if (top < localVisible.y0 + margin) dy = localVisible.y0 + margin - top;
  return dy ? shifted.map((box) => (box.side === "inside" ? { ...box, y: box.y + dy } : box)) : shifted;
}

/**
 * 既有的「按卡片自身尺寸分列」口径（无可用矩形时原样保留）：
 * 上边容量用尽后，最左侧按钮移到左边并压住前一个；左边容量也用尽后自动进入第二列。
 */
function legacyToolbarButtons(input: {
  count: number;
  size: number;
  step: number;
  start: number;
  gap: number;
  w: number;
  h: number;
}): ToolbarButtonBox[] {
  const margin = 14;
  const topCapacity = Math.max(0, Math.min(input.count, Math.floor((input.w / 2 - margin) / input.step)));
  const leftCount = input.count - topCapacity;
  const leftCapacity = Math.max(1, Math.floor((input.h / 2 - margin) / input.step));
  const out: ToolbarButtonBox[] = [];
  for (let i = 0; i < input.count; i += 1) {
    if (i < leftCount) {
      const rank = leftCount - 1 - i;
      const column = Math.floor(rank / leftCapacity);
      const row = rank % leftCapacity;
      out.push(buttonBox(-input.size - input.gap - column * input.step, input.start + row * input.step, input.size, "left"));
    } else {
      out.push(buttonBox(input.start + (i - leftCount) * input.step, -input.size - input.gap, input.size, "above"));
    }
  }
  return out;
}

/** 把一行按钮整体推到不与连接点重叠的位置（只用于「向内调整」的兜底）。 */
function shiftRowAwayFromPorts(
  boxes: ToolbarButtonBox[],
  ports: ReadonlyArray<{ x: number; y: number; r?: number }>,
  localVisible: { x0: number; y0: number; x1: number; y1: number },
  margin: number,
): ToolbarButtonBox[] {
  if (!ports.length || !boxes.length) return boxes;
  let shifted = boxes;
  for (const port of ports) {
    const radius = Math.max(0, finiteOr(port.r, 8));
    const hit = () =>
      shifted.some((box) => {
        const nearestX = clamp(port.x, box.x, box.x + box.w);
        const nearestY = clamp(port.y, box.y, box.y + box.h);
        return Math.hypot(port.x - nearestX, port.y - nearestY) < radius;
      });
    if (!hit()) continue;
    let next = shifted;
    for (const direction of [1, -1] as const) {
      const delta = direction * (radius + TOOLBAR_BUTTON_MARGIN);
      const candidate = shifted.map((box) => ({ ...box, y: box.y + delta }));
      const fits = candidate.every((box) => box.y >= localVisible.y0 + margin && box.y + box.h <= localVisible.y1 - margin);
      if (fits) {
        next = candidate;
        break;
      }
    }
    shifted = next;
  }
  return shifted;
}

/**
 * 计算卡片局部工具栏五个按钮这一帧的位置（相对卡片左上角）。
 *
 * 规则（PM-QIO032 #04）：
 * 1. 优先贴卡片上方；上方放不下（卡片贴板面上沿 / 缩放到很大）就换到下方；
 *    横排放不下就改贴左 / 右边，再不行就「向内调整」到可视区里 —— 编辑按钮始终保持完整可点。
 * 2. 任何落点都夹进板面可视区（可用矩形），不把按钮推出可视区外；
 *    只做沿边滑动 / 换边，不做任何经验补偿常数。
 * 3. 按钮与卡片外框至少留 {@link TOOLBAR_BUTTON_GAP}（大于连接点视觉半径），
 *    向内调整时额外避开传入的连接点。
 * 4. 可用矩形量不到时退回既有行为（按卡片自身尺寸分列）。
 */
export function toolbarButtonLayout(input: ToolbarButtonLayoutInput): ToolbarButtonBox[] {
  const count = Math.max(0, Math.floor(finiteOr(input.count, TOOLBAR_BUTTON_COUNT)));
  const size = Math.max(1, finiteOr(input.size, TOOLBAR_BUTTON_SIZE));
  const step = Math.max(1, finiteOr(input.step, TOOLBAR_BUTTON_STEP));
  const start = Math.max(0, finiteOr(input.start, TOOLBAR_BUTTON_START));
  const gap = Math.max(0, finiteOr(input.gap, TOOLBAR_BUTTON_GAP));
  const margin = Math.max(0, finiteOr(input.margin, TOOLBAR_BUTTON_MARGIN));
  const cardW = Math.max(0, finiteOr(input.card.w, 0));
  const cardH = Math.max(0, finiteOr(input.card.h, 0));
  if (!count) return [];
  if (!cardW || !cardH) return legacyToolbarButtons({ count, size, step, start, gap, w: cardW, h: cardH });

  const visible = input.visible;
  const hasVisible = Boolean(visible && finiteOr(visible.w, 0) > 0 && finiteOr(visible.h, 0) > 0);
  if (!hasVisible) return legacyToolbarButtons({ count, size, step, start, gap, w: cardW, h: cardH });

  // 可视区换到「卡片本地坐标」：x / y 相对卡片左上角。
  const vx0 = finiteOr(visible!.x, 0) - finiteOr(input.card.x, 0);
  const vy0 = finiteOr(visible!.y, 0) - finiteOr(input.card.y, 0);
  const vx1 = vx0 + finiteOr(visible!.w, 0);
  const vy1 = vy0 + finiteOr(visible!.h, 0);

  // 沿用既有「按卡片自身尺寸分列」的容量口径：保证宽卡一行、窄卡分列。
  const legacyMargin = 14;
  const rowCapacity = Math.max(0, Math.min(count, Math.floor((cardW / 2 - legacyMargin) / step)));
  const columnCapacity = Math.max(1, Math.floor((cardH / 2 - legacyMargin) / step));
  const columnCount = count - Math.min(count, rowCapacity);
  const rowCount = count - columnCount;
  const rowLength = rowCount ? start + (rowCount - 1) * step + size : 0;
  const columnLength = columnCount ? start + (columnCount - 1) * step + size : 0;

  // 可用矩形在卡片本地坐标系里的起点 / 终点：卡片上方有空间 = 本地 y 的负方向空间足够。
  const room = {
    above: -vy0 >= gap + size + margin,
    below: vy1 - cardH >= gap + size + margin,
    left: -vx0 >= gap + size + margin,
    right: vx1 - cardW >= gap + size + margin,
  };
  const rowFitsVisible = rowLength > 0 && rowLength <= vx1 - vx0 - 2 * margin;
  const columnFitsVisible = columnLength > 0 && columnLength <= vy1 - vy0 - 2 * margin;

  // 1) 五钮一排的宽卡：只选上 / 下一条横排（换边），再沿边滑动夹进可视区。
  if (!columnCount) {
    const side: ToolbarButtonSide | null = room.above ? "above" : room.below ? "below" : null;
    if (side && rowFitsVisible) {
      const y = side === "above" ? -size - gap : cardH + gap;
      const x0 = clamp(start, vx0 + margin, vx1 - margin - rowLength);
      return Array.from({ length: count }, (_, index) => buttonBox(x0 + index * step, y, size, side));
    }
    // 上 / 下都放不下（贴边或放大到卡片比可视区还大）→ 向内调整到可视区顶部。
    const innerX = clamp(start, vx0 + margin, vx1 - margin - rowLength);
    const innerY = clamp(vy0 + margin, vy0 + margin, Math.max(vy0 + margin, vy1 - margin - size));
    return shiftRowAwayFromPorts(
      Array.from({ length: count }, (_, index) => buttonBox(innerX + index * step, innerY, size, "inside")),
      input.ports ?? [],
      { x0: vx0, y0: vy0, x1: vx1, y1: vy1 },
      margin,
    );
  }

  // 2) 窄卡：前面几个按钮走竖排（优先左边，左边没位置就换右边），其余贴上 / 下横排。
  const verticalSide: ToolbarButtonSide | null = room.left
    ? "left"
    : room.right
      ? "right"
      : columnFitsVisible
        ? "left"
        : null;
  const horizontalSide: ToolbarButtonSide | null = room.above ? "above" : room.below ? "below" : null;
  const boxes: ToolbarButtonBox[] = [];

  if (verticalSide) {
    const x = verticalSide === "left" ? -size - gap : cardW + gap;
    let y0 = clamp(start, vy0 + margin, vy1 - margin - columnLength);
    // 左边 / 右边没空间时向内调整：贴到可视区左边或右边（沿边滑动）。
    let innerX = x;
    if (innerX < vx0 + margin) innerX = vx0 + margin;
    if (innerX + size > vx1 - margin) innerX = Math.max(vx0 + margin, vx1 - margin - size);
    const sideForInner: ToolbarButtonSide = verticalSide;
    const vertical = Array.from({ length: columnCount }, (_, index) => buttonBox(innerX === x ? x : innerX, y0 + index * step, size, sideForInner));
    boxes.push(...vertical);
  } else {
    // 左右都没有空间：竖排也向内调整（沿可视区左缘向下排）。
    const innerX = clamp(0, vx0 + margin, Math.max(vx0 + margin, vx1 - margin - size));
    const y0 = clamp(start, vy0 + margin, Math.max(vy0 + margin, vy1 - margin - columnLength));
    boxes.push(...Array.from({ length: columnCount }, (_, index) => buttonBox(innerX, y0 + index * step, size, "inside")));
  }

  if (horizontalSide && rowFitsVisible) {
    const y = horizontalSide === "above" ? -size - gap : cardH + gap;
    const x0 = clamp(start, vx0 + margin, vx1 - margin - rowLength);
    boxes.push(...Array.from({ length: rowCount }, (_, index) => buttonBox(x0 + index * step, y, size, horizontalSide)));
  } else {
    // 横排也放不下：同一行向内调整到竖排之后的下方，夹进可视区。
    const innerX = clamp(start, vx0 + margin, Math.max(vx0 + margin, vx1 - margin - rowLength));
    const innerY = clamp(
      boxes.length ? boxes[boxes.length - 1].y + step : vy0 + margin,
      vy0 + margin,
      Math.max(vy0 + margin, vy1 - margin - size),
    );
    boxes.push(...Array.from({ length: rowCount }, (_, index) => buttonBox(innerX + index * step, innerY, size, "inside")));
  }
  return avoidPortsForInside(boxes, input.ports ?? [], { x0: vx0, y0: vy0, x1: vx1, y1: vy1 }, margin);
}