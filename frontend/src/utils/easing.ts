/**
 * 把设计令牌里的 `cubic-bezier(...)` 拿到 JS 里求值。
 *
 * 为什么需要它：星球的收缩尾段要换成「球自己的分辨率」渲染（见 PlanetView 的
 * `runCollapseMotion`），换布局会把 CSS 过渡打断 —— 如果重新起一段过渡，
 * 曲线会在中途折一下（用户对「两段动画」很敏感）。正确做法是**继续用同一条曲线**：
 * 每秒算一次进度，把几何写进新的坐标系统。曲线仍然是令牌里的那条，不是抄一份常数。
 *
 * 实现就是 CSS 规范里那条三次贝塞尔：x(s)、y(s) 都是 s 的三次多项式，
 * 给定 x 先解出 s（牛顿法 + 二分兜底），再求 y。
 */

export type EasePoints = [number, number, number, number];

/** `--ease-3-settle` 的兜底值（令牌读不到时用；以 tokens.css 为准） */
export const CSS_SETTLE_FALLBACK: EasePoints = [0.24, 1.04, 0.32, 1];

const cubic = (a: number, b: number, s: number): number => {
  const inv = 1 - s;
  return 3 * inv * inv * s * a + 3 * inv * s * s * b + s * s * s;
};

const derivative = (a: number, b: number, s: number): number => {
  const inv = 1 - s;
  return 3 * inv * inv * a + 6 * inv * s * (b - a) + 3 * s * s * (1 - b);
};

/**
 * 求三次贝塞尔在 x 处的 y。x 会夹到 [0,1]（rAF 迟到的时间戳不能让尺寸飞出画面）；
 * 曲线本身允许 y 略大于 1（柔性收束带一点点过冲）。
 */
export function evalCubicBezier(points: EasePoints, x: number): number {
  const [x1, y1, x2, y2] = points;
  if (!Number.isFinite(x) || x <= 0) return 0;
  if (x >= 1) return 1;
  // 先牛顿迭代（绝大多数情况 3~4 次就收敛）
  let s = x;
  for (let i = 0; i < 8; i++) {
    const dx = cubic(x1, x2, s) - x;
    if (Math.abs(dx) < 1e-7) return cubic(y1, y2, s);
    const d = derivative(x1, x2, s);
    if (Math.abs(d) < 1e-7) break;
    s -= dx / d;
    if (s < 0 || s > 1) break;
  }
  // 牛顿不收敛（导数为 0 的平坦段）时退回二分：n 次迭代后 s 的误差 < 2^-n
  let lo = 0;
  let hi = 1;
  s = Math.min(1, Math.max(0, s));
  for (let i = 0; i < 60; i++) {
    if (cubic(x1, x2, s) < x) lo = s;
    else hi = s;
    s = (lo + hi) / 2;
  }
  return cubic(y1, y2, s);
}

/**
 * 解析 `cubic-bezier(.24,1.04,.32,1)` 这类 CSS 值（含省略 0 的写法）。
 * 解析不出来（`ease` 关键字、空串、被禁用的动效）就退回默认曲线 —— 调用方不猜。
 */
export function easePointsFromCssValue(raw: string, fallback: EasePoints): EasePoints {
  const m = String(raw ?? "").match(/cubic-bezier\(\s*([^)]*)\)/i);
  if (!m) return fallback;
  const nums = m[1]
    .split(",")
    .map((p) => Number.parseFloat(p.trim()))
    .filter((n) => Number.isFinite(n));
  if (nums.length !== 4) return fallback;
  return [nums[0], nums[1], nums[2], nums[3]];
}
