/**
 * 浮层（卡片局部工具栏 / 合并提示）的落点决策 —— 纯函数，便于在 jsdom 里逐条断言。
 *
 * 规则（2026-10-14 定，用户报告「他不应该在下面的时候挡住卡片」）：
 * 1. 竖向能放到卡片外面、且**整个浮层都在可视区内**：先上方、后下方；
 * 2. 竖向两边都放不下（卡片比可视区还高）：挪到卡片**横向之外**（右侧优先，其次左侧），
 *    并与卡片可见的顶边对齐 —— 宁可浮在卡片旁边，也不整块压在卡片上；
 * 3. 横向也没有余地（卡片几乎占满可视区）：夹进可视区保底，此时压住卡片无法避免。
 *
 * 为什么不用「viewport.bottom − 30」这种硬编码：它没算浮层自身高度，
 * 于是「下方放得下」和「放不下」都会被夹进卡片内部，而当时的判定只看得见「碰到卡片顶边」，
 * 看不见「落在卡片下半部分」—— 实测 1200×500 窗口下工具栏 442–478 整块落在卡片 104–508 里。
 */
export interface OverlayRect {
  top: number;
  bottom: number;
  left: number;
  right: number;
}

export interface OverlaySize {
  w: number;
  h: number;
}

export interface OverlayPlacement {
  left: number;
  top: number;
}

/** 浮层与可视区边缘的最小留白（原来的 4px 左边缘夹取沿用）。 */
export const OVERLAY_EDGE_MARGIN = 4;
/** 与可视区上下沿的最小留白：2px 保证浮层不贴边。 */
export const OVERLAY_VIEWPORT_PAD = 2;

export function placeCardOverlay(input: {
  card: OverlayRect;
  toolbar: OverlaySize;
  viewport: OverlayRect;
  gap: number;
}): OverlayPlacement {
  const { card, toolbar, viewport, gap } = input;
  const minTop = viewport.top + OVERLAY_VIEWPORT_PAD;
  const maxTop = viewport.bottom - toolbar.h - OVERLAY_VIEWPORT_PAD;
  const aboveTop = card.top - toolbar.h - gap;
  const belowTop = card.bottom + gap;

  let top: number;
  if (aboveTop >= minTop) {
    top = aboveTop; // 上方：天然不与卡片相交
  } else if (belowTop <= maxTop) {
    top = belowTop; // 下方：整个浮层仍在可视区内
  } else {
    top = Math.max(minTop, Math.min(maxTop, belowTop)); // 保底：夹进可视区
  }

  const leftMin = viewport.left + OVERLAY_EDGE_MARGIN;
  const leftMax = viewport.right - toolbar.w - OVERLAY_EDGE_MARGIN;
  let left = Math.max(leftMin, Math.min(leftMax, card.left));

  /** 与卡片矩形（两个轴都要相交才算压住）是否重叠。 */
  const covers = (candidateLeft: number, candidateTop: number) =>
    candidateTop + toolbar.h > card.top + 2 &&
    candidateTop < card.bottom - 2 &&
    candidateLeft + toolbar.w > card.left + 2 &&
    candidateLeft < card.right - 2;

  if (covers(left, top)) {
    // 竖向已经无解：至少横向挪出卡片。与卡片可见顶边对齐（顶边在视口外时贴视口上沿）
    const besideTop = Math.max(minTop, Math.min(maxTop, card.top));
    const rightOfCard = card.right + gap;
    const leftOfCard = card.left - toolbar.w - gap;
    if (rightOfCard <= leftMax) {
      left = rightOfCard;
      top = besideTop;
    } else if (leftOfCard >= leftMin) {
      left = leftOfCard;
      top = besideTop;
    }
  }

  return { left, top };
}
