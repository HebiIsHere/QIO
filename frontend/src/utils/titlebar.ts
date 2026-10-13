/**
 * 自定义标题条（无边框窗口的「页眉」）几何。
 *
 * 高度的事实来源是 CSS 令牌 `--titlebar-height`（styles/tokens.css）——组件用 CSS 读它，
 * 需要像素数的 JS（浮动组件的默认位置、顶部锚定控件的下移量）读这里，
 * 两边不允许各自写死一个 32：写死过一次就会在改高度时只改一半，
 * 结果正是「某个控件被标题条压住、点不到」。
 */
const FALLBACK_PX = 32;

/** 令牌缺失或环境读不到（jsdom / 非浏览器）时的兜底值，注释里给出依据：Windows 标题栏惯例。 */
export function titlebarHeight(): number {
  try {
    const raw = getComputedStyle(document.documentElement).getPropertyValue("--titlebar-height").trim();
    const value = parseFloat(raw);
    if (!Number.isFinite(value) || value <= 0) return FALLBACK_PX;
    // 令牌按规范只写 px；写成别的单位时宁可用兜底值，也不猜一个错的像素数。
    return /px$/i.test(raw) ? value : FALLBACK_PX;
  } catch {
    return FALLBACK_PX;
  }
}
