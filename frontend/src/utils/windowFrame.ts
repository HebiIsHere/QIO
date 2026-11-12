/**
 * 窗口外框几何（0.3.2：透明留白 + CSS 20px 圆角）。
 *
 * 事实来源只有一处：styles/tokens.css 的 --window-frame-pad / --window-radius /
 * --window-frame-inset。本文件只做「读 CSS 变量 → 像素数」，不维护第二套基准。
 *
 * 为什么需要它：WebView 的 CSS 视口仍是**整个客户区**（window.innerWidth/innerHeight、
 * 100vw/100vh、position:fixed; inset:0 都包含留白带）。凡是「全窗 fixed 层 / 顶部锚定 /
 * 用视口尺寸做钳制」的代码，都必须换成可见内容区，否则内容会落进透明留白带或圆角外。
 *
 * 不适用：getBoundingClientRect() 系量测。rect 与 fixed 同在 CSS 视口坐标系，
 * 留白只改变元素在视口中的位置，不改变 rect 语义（全仓 145 处 rect 用量测均不需要换算）。
 */

/** 令牌读不到时的兜底（与 tokens.css 默认值一致；native/dwm/最大化/贴靠由 data 属性短路为 0）。 */
const FALLBACK_PAD = 28;
const FALLBACK_RADIUS = 20;

function rootDataset(): DOMStringMap | null {
  if (typeof document === "undefined") return null;
  return document.documentElement.dataset;
}

/** 读一个 px 级 CSS 变量；读不到 / 非 px 时返回 null（jsdom 与首帧都走兜底）。 */
function readPxVar(name: string): number | null {
  if (typeof document === "undefined" || typeof getComputedStyle !== "function") return null;
  try {
    const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    if (!/^-?\d+(\.\d+)?px$/i.test(raw)) return null;
    const value = Number.parseFloat(raw);
    return Number.isFinite(value) ? value : null;
  } catch {
    return null;
  }
}

/** 当前是否处于「无透明留白」的窗口模式/状态（native、dwm、maximized、snapped）。 */
export function isFrameZeroed(): boolean {
  const data = rootDataset();
  if (!data) return false;
  return (
    data.windowFrame === "native" ||
    data.windowFrame === "dwm" ||
    data.windowState === "maximized" ||
    data.windowState === "snapped"
  );
}

/**
 * CSS 变量读不到时的兜底：
 * - 真实应用里 useWindowFrame 会在 setup 阶段同步写 <html data-window-frame="css">，
 *   此时按默认 28 兜底（样式表尚未应用的一帧也不会跳）；
 * - jsdom/单测/旧壳没有这个标记 → 0，不把「测试环境没加载 CSS」误当成 28px 留白。
 */
function cssVarFallback(): number {
  const data = rootDataset();
  return data && data.windowFrame ? FALLBACK_PAD : 0;
}

/** 透明留白（CSS px）。正常 css 模式 = 28；native/dwm/最大化/贴靠 = 0。 */
export function framePadPx(): number {
  if (isFrameZeroed()) return 0;
  const pad = readPxVar("--window-frame-pad");
  if (pad !== null) return Math.max(0, pad);
  return cssVarFallback();
}

/** 全窗 fixed 层的 inset（CSS px）。与 framePadPx 同源，单独命名是为了语义清楚。 */
export function frameInsetPx(): number {
  if (isFrameZeroed()) return 0;
  const inset = readPxVar("--window-frame-inset");
  if (inset !== null) return Math.max(0, inset);
  return framePadPx();
}

/** 可见内容区圆角（CSS px）。正常 = 20；native/dwm/最大化/贴靠 = 0。 */
export function windowRadiusPx(): number {
  if (isFrameZeroed()) return 0;
  const radius = readPxVar("--window-radius");
  if (radius !== null) return Math.max(0, radius);
  const data = rootDataset();
  return data && data.windowFrame ? FALLBACK_RADIUS : 0;
}

export interface ContentRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
  width: number;
  height: number;
}

export interface ContentViewport {
  width: number;
  height: number;
  inset: number;
}

/** 可见内容区在 CSS 视口中的矩形（fixed 元素坐标可直接与之比较/求交）。 */
export function contentRect(): ContentRect {
  const inset = frameInsetPx();
  const viewportWidth = typeof window === "undefined" ? 0 : Math.max(0, window.innerWidth || 0);
  const viewportHeight = typeof window === "undefined" ? 0 : Math.max(0, window.innerHeight || 0);
  const left = Math.min(inset, viewportWidth / 2);
  const top = Math.min(inset, viewportHeight / 2);
  return {
    left,
    top,
    right: Math.max(left, viewportWidth - inset),
    bottom: Math.max(top, viewportHeight - inset),
    width: Math.max(0, viewportWidth - inset * 2),
    height: Math.max(0, viewportHeight - inset * 2),
  };
}

/** 可见内容区的尺寸（给「以视口做钳制/断点」的既有纯函数当入参）。 */
export function contentViewport(): ContentViewport {
  const rect = contentRect();
  return { width: rect.width, height: rect.height, inset: frameInsetPx() };
}

/**
 * 把 fixed 浮层矩形钳进可见内容区（左右锚点菜单/提示/下拉的共用规则）：
 * 保持既有 margin 语义，但边界是可见内容区而不是整个 WebView 视口，
 * 因此不会落到透明留白带或圆角外（spike 复现过 16 CSS px 越界）。
 */
export function clampToContentRect(
  left: number,
  top: number,
  width: number,
  height: number,
  margin = 0,
): { left: number; top: number; clampedX: boolean; clampedY: boolean } {
  const rect = contentRect();
  const minLeft = rect.left + margin;
  const minTop = rect.top + margin;
  const maxLeft = Math.max(minLeft, rect.right - margin - width);
  const maxTop = Math.max(minTop, rect.bottom - margin - height);
  const nextLeft = Math.min(Math.max(left, minLeft), maxLeft);
  const nextTop = Math.min(Math.max(top, minTop), maxTop);
  return {
    left: nextLeft,
    top: nextTop,
    clampedX: Math.abs(nextLeft - left) > 0.5,
    clampedY: Math.abs(nextTop - top) > 0.5,
  };
}
