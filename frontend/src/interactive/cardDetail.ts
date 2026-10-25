/**
 * 卡片详细状态的纯几何判断（第二迭代）。
 *
 * 只按卡片自身真实像素判断，不接收板面缩放：板面缩放改变查看方式，
 * 不改变卡片的内容容量。进入/退出阈值分离，尺寸在边界附近时保持现状，
 * 避免拖动过程中反复切换。
 */

export const DETAIL_ENTER_WIDTH = 420;
export const DETAIL_ENTER_HEIGHT = 280;
export const DETAIL_EXIT_WIDTH = 360;
export const DETAIL_EXIT_HEIGHT = 240;

/** 图片自动详细的进入/退出口径：短边 + 面积，避免竖图和长图被普通双阈值卡死。 */
export const IMAGE_DETAIL_ENTER_AREA = 117_600;
export const IMAGE_DETAIL_ENTER_SHORT = 200;
export const IMAGE_DETAIL_EXIT_AREA = 100_800;
export const IMAGE_DETAIL_EXIT_SHORT = 180;
/** 达到这个比例视为极端长图/竖图；当前未自动详细时给出兜底入口。 */
export const IMAGE_EXTREME_RATIO = 3;

export interface CardDetailInput {
  width: number;
  height: number;
  /** 有值表示图片卡；null/undefined 表示普通卡片。 */
  aspectRatio?: number | null;
  previousDetailed?: boolean;
}

function safeSize(value: number): number {
  return Number.isFinite(value) && value > 0 ? value : 0;
}

function isImage(input: CardDetailInput): boolean {
  return (
    typeof input.aspectRatio === "number" &&
    Number.isFinite(input.aspectRatio) &&
    input.aspectRatio > 0
  );
}

function isExtremeImage(input: CardDetailInput): boolean {
  if (!isImage(input)) return false;
  const ratio = Number(input.aspectRatio);
  return ratio >= IMAGE_EXTREME_RATIO || ratio <= 1 / IMAGE_EXTREME_RATIO;
}

function imageAutoDetailed(width: number, height: number, previousDetailed: boolean): boolean {
  const short = Math.min(width, height);
  const area = width * height;
  if (previousDetailed) {
    return short >= IMAGE_DETAIL_EXIT_SHORT && area >= IMAGE_DETAIL_EXIT_AREA;
  }
  return short >= IMAGE_DETAIL_ENTER_SHORT && area >= IMAGE_DETAIL_ENTER_AREA;
}

export function shouldAutoDetail(input: CardDetailInput): boolean {
  const width = safeSize(input.width);
  const height = safeSize(input.height);
  if (width === 0 || height === 0) return false;

  if (isImage(input)) {
    return imageAutoDetailed(width, height, Boolean(input.previousDetailed));
  }

  if (input.previousDetailed) {
    return width >= DETAIL_EXIT_WIDTH && height >= DETAIL_EXIT_HEIGHT;
  }
  return width >= DETAIL_ENTER_WIDTH && height >= DETAIL_ENTER_HEIGHT;
}

/**
 * 图片兜底入口：只服务极端比例、且当前尚未自动进入详细的长图/竖图。
 * 正常图片、普通卡片永远不显示这个入口。
 */
export function shouldShowImageDetailFallback(input: CardDetailInput): boolean {
  if (!isExtremeImage(input)) return false;
  return !shouldAutoDetail(input);
}
