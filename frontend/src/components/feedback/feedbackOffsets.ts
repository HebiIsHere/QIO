/**
 * 反馈浮层的垂直落点（B）。
 *
 * 顶部通知与全局问题面板都浮在「全局栏下方」；非聊天路由（设置/调试/星球外的页面）
 * 的 App 顶部还有一行 `.top-notes`（待审批入口 / 开发任务入口）。如果不给它让位，
 * 通知会直接盖住那行入口 —— 这里统一按真实高度计算，而不是写死坐标。
 */
export const FEEDBACK_TOP_GAP = 12;

/** 读当前全局栏高度（--titlebar-height）并让出一行顶部提示条的高度。 */
export function feedbackTopPx(extraGap = 0): number {
  if (typeof document === "undefined") return 0;
  const rootStyle = globalThis.getComputedStyle?.(document.documentElement);
  /* 可见内容区留白（透明窗）：浮层落点从可见区顶算起，而不是从窗口客户区顶算起。
     读不到 CSS 变量（jsdom / 0.3.1 旧壳）时按 0 —— 与改造前完全一致。 */
  const insetRaw = Number.parseFloat(rootStyle?.getPropertyValue("--window-frame-inset") ?? "");
  const inset = Number.isFinite(insetRaw) && insetRaw > 0 ? insetRaw : 0;
  const parsed = Number.parseFloat(rootStyle?.getPropertyValue("--titlebar-height") ?? "");
  const bar = Number.isFinite(parsed) && parsed > 0 ? parsed : 32;
  let notesHeight = 0;
  const notes = document.querySelector(".top-notes") as HTMLElement | null;
  if (notes) {
    const rect = notes.getBoundingClientRect();
    notesHeight = Number.isFinite(rect.height) ? Math.max(0, Math.round(rect.height)) : 0;
  }
  return inset + bar + FEEDBACK_TOP_GAP + (notesHeight > 0 ? notesHeight + 8 : 0) + extraGap;
}
