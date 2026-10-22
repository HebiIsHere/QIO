/**
 * 组框轮廓（用户决策 2 / 任务 F）：成员矩形 → 贴合成员的“并集轮廓”。
 *
 * 为什么要轮廓而不是矩形包围盒：矩形包围盒会把落在成员空隙里的**非成员卡片**
 * 也围进组框，用户会以为那张卡也属于这个组。并集轮廓只描成员自己的扩张区边界，
 * 非成员卡片除非落进成员 16px 的扩张带，否则不会被围进去。
 *
 * 口径（与 board.ts 的组框留白同源）：
 * - 每个成员向外扩张：左右 16px、下 16px；
 * - 顶行成员顶部留 58px（组头区），非顶行成员顶部只留 16px；
 * - 输出每个成员的圆角填充矩形 + 并集边界线段（正交多边形边界；不求环，输出线段即可）。
 *
 * 纯算术：不读 DOM / 不写状态 / 不改输入；同样输入永远同样输出。
 */

export interface GroupOutlineRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface GroupOutlineMemberInput {
  cardId: string;
  rect: GroupOutlineRect;
}

export interface GroupOutlineFill extends GroupOutlineRect {
  cardId: string;
}

/** 正交边界线段（板面坐标；水平线 y1 === y2，垂直线 x1 === x2）。 */
export interface GroupOutlineSegment {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface GroupOutlinePadding {
  /** 左右留白 */
  x: number;
  /** 顶行成员顶部留白（组头区） */
  top: number;
  /** 非顶行成员顶部留白 */
  topInner: number;
  /** 底部留白 */
  bottom: number;
  /** 组头高度（组名 / 成员入口；只用于计算头部区位置，不参与轮廓） */
  header: number;
}

export const GROUP_OUTLINE_PADDING: GroupOutlinePadding = {
  x: 16,
  top: 58,
  topInner: 16,
  bottom: 16,
  header: 58,
};

/** 判定“同一行顶部”的容差（浮点坐标不要求逐位相等）。 */
const TOP_ROW_EPSILON = 0.5;
const EPSILON = 1e-6;

export interface GroupOutlineResult {
  /** 每个成员的扩张矩形（圆角填充用；顺序与输入一致） */
  members: GroupOutlineFill[];
  /** 所有扩张矩形的并集边界 */
  bounds: GroupOutlineRect;
  /** 并集轮廓的正交边界线段（已合并同一直线上的相邻段） */
  segments: GroupOutlineSegment[];
  /** 浮动头部区（轮廓左上角之上；宽度按内容上限收敛） */
  header: GroupOutlineRect;
}

function finite(value: unknown, fallback = 0): number {
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function normalizeRect(rect: GroupOutlineRect | null | undefined): GroupOutlineRect {
  return {
    x: finite(rect?.x),
    y: finite(rect?.y),
    w: Math.max(0, finite(rect?.w)),
    h: Math.max(0, finite(rect?.h)),
  };
}

interface RawSegment {
  orientation: "h" | "v";
  fixed: number;
  from: number;
  to: number;
}

/** 合并同一直线上相邻 / 重叠的线段（浮点容差）。 */
function mergeSegments(raw: RawSegment[]): GroupOutlineSegment[] {
  const merged: RawSegment[] = [];
  const groups = new Map<string, RawSegment[]>();
  for (const segment of raw) {
    const key = segment.orientation + ":" + segment.fixed.toFixed(6);
    const list = groups.get(key) ?? [];
    list.push(segment);
    groups.set(key, list);
  }
  for (const key of [...groups.keys()].sort()) {
    const list = groups
      .get(key)!
      .slice()
      .sort((a, b) => a.from - b.from);
    let current: RawSegment | null = null;
    for (const segment of list) {
      if (!current) {
        current = { ...segment };
        continue;
      }
      if (segment.from <= current.to + EPSILON) {
        current.to = Math.max(current.to, segment.to);
      } else {
        merged.push(current);
        current = { ...segment };
      }
    }
    if (current) merged.push(current);
  }
  return merged
    .map((segment) =>
      segment.orientation === "h"
        ? { x1: segment.from, y1: segment.fixed, x2: segment.to, y2: segment.fixed }
        : { x1: segment.fixed, y1: segment.from, x2: segment.fixed, y2: segment.to },
    )
    .sort((a, b) => a.y1 - b.y1 || a.x1 - b.x1 || a.y2 - b.y2 || a.x2 - b.x2);
}

/**
 * 由成员矩形算出贴合轮廓。
 * @param members 组成员的**显示矩形**（board 坐标；已含拖动 / 尺寸预览）
 * @param padding 留白（默认与 board.ts 的组框留白同口径）
 * @returns 没有有效成员时返回 null（调用方据此不画大空框）
 */
export function groupOutline(
  members: GroupOutlineMemberInput[] | null | undefined,
  padding: Partial<GroupOutlinePadding> = {},
): GroupOutlineResult | null {
  const pad: GroupOutlinePadding = { ...GROUP_OUTLINE_PADDING, ...padding };
  const list = (members ?? []).filter(
    (member) => member && member.cardId && normalizeRect(member.rect).w > 0 && normalizeRect(member.rect).h > 0,
  );
  if (!list.length) return null;
  const normalized = list.map((member) => ({
    cardId: member.cardId,
    rect: normalizeRect(member.rect),
  }));
  const topY = Math.min(...normalized.map((member) => member.rect.y));
  const fills: GroupOutlineFill[] = normalized.map((member) => {
    const isTopRow = member.rect.y <= topY + TOP_ROW_EPSILON;
    const top = Math.max(0, pad.top);
    const topInner = Math.max(0, pad.topInner);
    return {
      cardId: member.cardId,
      x: member.rect.x - Math.max(0, pad.x),
      y: member.rect.y - (isTopRow ? top : topInner),
      w: member.rect.w + Math.max(0, pad.x) * 2,
      h: member.rect.h + (isTopRow ? top : topInner) + Math.max(0, pad.bottom),
    };
  });

  // 坐标压缩网格：每个网格单元中心是否被任一扩张矩形覆盖 → 边界就是“覆盖 ↔ 未覆盖”的单元边。
  const xs = [...new Set(fills.flatMap((fill) => [fill.x, fill.x + fill.w]))].sort((a, b) => a - b);
  const ys = [...new Set(fills.flatMap((fill) => [fill.y, fill.y + fill.h]))].sort((a, b) => a - b);
  const cols = Math.max(0, xs.length - 1);
  const rows = Math.max(0, ys.length - 1);
  const covered: boolean[][] = Array.from({ length: cols }, () => Array.from({ length: rows }, () => false));
  for (let i = 0; i < cols; i += 1) {
    for (let j = 0; j < rows; j += 1) {
      const cx = (xs[i] + xs[i + 1]) / 2;
      const cy = (ys[j] + ys[j + 1]) / 2;
      covered[i][j] = fills.some(
        (fill) => cx > fill.x && cx < fill.x + fill.w && cy > fill.y && cy < fill.y + fill.h,
      );
    }
  }
  const raw: RawSegment[] = [];
  for (let i = 0; i < cols; i += 1) {
    for (let j = 0; j < rows; j += 1) {
      if (!covered[i][j]) continue;
      const left = covered[i - 1]?.[j] === true;
      const right = covered[i + 1]?.[j] === true;
      const above = covered[i]?.[j - 1] === true;
      const below = covered[i]?.[j + 1] === true;
      if (!left) raw.push({ orientation: "v", fixed: xs[i], from: ys[j], to: ys[j + 1] });
      if (!right) raw.push({ orientation: "v", fixed: xs[i + 1], from: ys[j], to: ys[j + 1] });
      if (!above) raw.push({ orientation: "h", fixed: ys[j], from: xs[i], to: xs[i + 1] });
      if (!below) raw.push({ orientation: "h", fixed: ys[j + 1], from: xs[i], to: xs[i + 1] });
    }
  }

  const minX = Math.min(...fills.map((fill) => fill.x));
  const minY = Math.min(...fills.map((fill) => fill.y));
  const maxX = Math.max(...fills.map((fill) => fill.x + fill.w));
  const maxY = Math.max(...fills.map((fill) => fill.y + fill.h));
  const bounds = { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
  // 头部只受轮廓宽度与内容上限约束：比轮廓两侧留白窄就会把组名截断，所以不扣两边留白。
  const headerWidth = Math.max(120, Math.min(bounds.w, 420));
  const header = {
    x: bounds.x + Math.max(0, pad.x),
    y: bounds.y + 6,
    w: headerWidth,
    h: Math.max(28, pad.header - 12),
  };
  return { members: fills, bounds, segments: mergeSegments(raw), header };
}
