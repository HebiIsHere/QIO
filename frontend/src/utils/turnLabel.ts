/**
 * 轮次标签（界面文案统一中文）。
 *
 * 之前是 `TURN 01` / `NOW` 这类英文 HUD 标签；视觉语言（等宽、小字、低对比）
 * 保留，但标签本身改成中文，避免中英混排。
 */
export function turnLabel(index: number, isCurrentRunning: boolean): string {
  if (isCurrentRunning) return "本轮";
  return `第 ${String(index).padStart(2, "0")} 轮`;
}
