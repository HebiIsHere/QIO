/**
 * 「回到最新消息」按钮的锚定计算（纯函数，便于单测）。
 *
 * 需求（见修复提示词 §6）：按钮要更靠下 —— **下缘距输入框上缘 8–12px**，
 * 水平对齐对话内容列中心。位置必须由**真实元素边界**决定，不用固定的大 `bottom` 数值补偿。
 *
 * 这里的「锚线」取输入框（`.composer`）与底部内容块（`.bottom-cluster`，知识候选卡 /
 * 兼容提示 / 话题切换提示）里**更靠上**的那条边：
 *
 * - 底部内容块为空时它高度为 0、上边就等于输入框上缘 → 锚线 = 输入框上缘；
 * - 有候选卡时锚线抬到卡片上缘 → 按钮停在卡片上方，不会被卡片盖住，
 *   也不会为了避让一路抬到消息区中部（只抬卡片那点高度）。
 *
 * 返回值是按钮应该距离**视口底部**多少像素（配合 `position: fixed; bottom: 0` 的层使用）。
 */
export const LATEST_BUTTON_GAP_PX = 10;

/** 距离下限：按钮不可能低于它该在的位置（视口高度异常时给个安全边界） */
const MIN_ROOM_ABOVE_BOTTOM_PX = 48;

export function latestButtonBottom(opts: {
  /** 输入框上缘（视口坐标），拿不到传 null */
  composerTop: number | null;
  /** 底部内容块上缘（视口坐标，高度为 0 时等于输入框上缘）；没有这一块传 null */
  clusterTop: number | null;
  viewportHeight: number;
  gap?: number;
}): number | null {
  const { composerTop, clusterTop, viewportHeight } = opts;
  const gap = opts.gap ?? LATEST_BUTTON_GAP_PX;
  const tops = [composerTop, clusterTop].filter(
    (v): v is number => typeof v === "number" && Number.isFinite(v),
  );
  if (!tops.length) return null;
  const anchorTop = Math.min(...tops);
  const raw = viewportHeight - anchorTop + gap;
  return Math.max(MIN_ROOM_ABOVE_BOTTOM_PX, Math.round(raw));
}

/**
 * 锚线（输入区上缘 / 底部内容块上缘）在视口里的目标位置：按钮**下缘**该落在哪。
 */
export function latestButtonTargetBottom(anchorTop: number, gap?: number): number {
  return anchorTop - (gap ?? LATEST_BUTTON_GAP_PX);
}

/**
 * 从「锚点写 `bottom: 0` 时它下缘实际落在哪」推算需要的 `bottom` 值。
 *
 * 为什么不能直接用视口高度：`position: fixed` 的参考是**包含块底边**，而它不一定等于
 * `window.innerHeight` —— 实测（页面被 CSS zoom 缩放时）包含块底边在 885、视口高度 900，
 * 差了 15px，按钮因此飘高、间距从 10px 变成 25px。这里改成用实测的原点 + 比例换算，
 * 不再依赖任何关于视口高度的假设。
 *
 * @param baseBottom 锚点 `bottom: 0` 时的下缘（视口坐标）= 包含块底边
 * @param targetBottom 按钮下缘的目标位置（视口坐标）
 * @param scale 局部像素 → 视口像素（1 表示没被缩放）
 */
export function latestButtonOffsetFromBase(opts: {
  baseBottom: number;
  targetBottom: number;
  scale: number;
}): number {
  const { baseBottom, targetBottom, scale } = opts;
  const factor = scale > 0 ? scale : 1;
  return Math.max(0, (baseBottom - targetBottom) / factor);
}

/** 水平方向同理：从「锚点 `left: 0` 时它的左缘在哪」推算需要的 `left` 值 */
export function latestButtonLeftFromBase(opts: {
  baseLeft: number;
  targetCenterX: number;
  /** 锚点自身宽度（局部像素）：外层用 translateX(-50%) 居中，所以要补回半个宽度 */
  anchorWidth: number;
  scale: number;
}): number {
  const { baseLeft, targetCenterX, anchorWidth, scale } = opts;
  const factor = scale > 0 ? scale : 1;
  // left 定位的是左缘，而目标是让「中心」落在 targetCenterX 上
  return (targetCenterX - baseLeft) / factor - anchorWidth / 2;
}
