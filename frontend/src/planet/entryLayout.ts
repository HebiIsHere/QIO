/**
 * 球态画布是否已经落到"球布局"的尺寸。
 *
 * 为什么需要它：入口尺度是「入口半径 ÷ 球体在当前画布上的半径」。启动/收起过程中
 * 画布会经历「整窗尺寸 → 球尺寸（`--ball-size`，108px）」的过渡；在整窗尺寸上算出来的
 * 尺度会小一大截（实测同一窗口里 k=0.34，正常是 0.88），球看起来就"缩没了"。
 *
 * 所以对齐之前必须先确认画布已经是球尺寸；没到位就等下一帧，**不能拿过渡帧的几何写死尺度**。
 */
export const BALL_SIZE_FALLBACK = 108;

/** 画布宽度是否已经算"球布局就位"（留 1.5 倍容差，避免取整误差） */
export function ballLayoutReady(canvasWidth: number, ballSize = BALL_SIZE_FALLBACK): boolean {
  if (!Number.isFinite(canvasWidth) || canvasWidth <= 0) return true; // 量不到就别卡住
  return canvasWidth <= ballSize * 1.5;
}

/** 读 CSS 令牌里的球尺寸（`--ball-size`）；读不到用 108（与令牌同值） */
export function readBallSize(root: HTMLElement | null | undefined): number {
  try {
    const raw = parseFloat(
      getComputedStyle(root ?? document.documentElement).getPropertyValue("--ball-size"),
    );
    return Number.isFinite(raw) && raw > 0 ? raw : BALL_SIZE_FALLBACK;
  } catch {
    return BALL_SIZE_FALLBACK;
  }
}
