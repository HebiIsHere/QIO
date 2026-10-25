/**
 * 顶栏浮层几何（Phase 1-B，纯函数）。
 *
 * 互动页的搜索/保存控件挂在全局栏 #qio-interactive-chrome-extra 里；
 * 全局栏自身 overflow: hidden（契约 §2.1），所以搜索结果浮层必须
 * **Teleport 到 body + position: fixed**，再由这里把矩形钳进视口：
 * 先右对齐触发按钮，装不下就向左收；高度用 max-height 收到视口下沿以内，
 * 内容区自己滚动 —— 浮层永远不被顶栏裁切，也不会被推出屏幕。
 */

export interface AnchorRectLike {
  left: number;
  top: number;
  right?: number;
  bottom?: number;
  width?: number;
  height?: number;
}

export interface ViewportSize {
  width: number;
  height: number;
}

export interface ChromePopoverOptions {
  /** 与视口边缘的安全距离。 */
  margin?: number;
  /** 与触发按钮之间的间距。 */
  gap?: number;
  /** 期望宽度。 */
  width?: number;
  /** 最大宽度。 */
  maxWidth?: number;
}

export interface ChromePopoverPlacement {
  left: number;
  top: number;
  width: number;
  maxHeight: number;
}

export const CHROME_POPOVER_MARGIN = 8;
export const CHROME_POPOVER_GAP = 6;
export const CHROME_SEARCH_WIDTH = 360;
export const CHROME_SEARCH_MAX_WIDTH = 420;

function finite(value: number | undefined): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return 0;
  return value;
}

function nonNegative(value: number | undefined, fallback: number): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return fallback;
  return Math.max(0, value);
}

/**
 * 把浮层矩形钳进视口：右对齐触发按钮，宽度先被视口宽度限制，高度用 max-height 落到视口下沿。
 * 返回 left/top/width/maxHeight，调用方直接写 fixed 定位与 max-height 即可。
 */
export function placeChromePopover(
  anchor: AnchorRectLike,
  viewport: ViewportSize,
  options: ChromePopoverOptions = {},
): ChromePopoverPlacement {
  const margin = nonNegative(options.margin, CHROME_POPOVER_MARGIN);
  const gap = nonNegative(options.gap, CHROME_POPOVER_GAP);
  const desiredWidth = nonNegative(options.width, CHROME_SEARCH_WIDTH);
  const maxWidth = nonNegative(options.maxWidth, CHROME_SEARCH_MAX_WIDTH);
  const viewportWidth = Math.max(0, finite(viewport.width));
  const viewportHeight = Math.max(0, finite(viewport.height));

  const width = Math.max(0, Math.min(desiredWidth, maxWidth, viewportWidth - margin * 2));
  const anchorRight = finite(anchor.right) || finite(anchor.left) + finite(anchor.width);
  const anchorBottom = finite(anchor.bottom) || finite(anchor.top) + finite(anchor.height);

  const minLeft = margin;
  const maxLeft = Math.max(minLeft, viewportWidth - margin - width);
  const left = Math.min(Math.max(anchorRight - width, minLeft), maxLeft);
  const top = Math.min(
    Math.max(anchorBottom + gap, margin),
    Math.max(margin, viewportHeight - margin),
  );
  const maxHeight = Math.max(0, viewportHeight - margin - top);

  return { left, top, width, maxHeight };
}
