/**
 * 退出守卫（R9 契约 §11，W5）。
 *
 * 独立成模块的原因：API 客户端与桌面服务都要问「是否已经在退出」，
 * 但 API 客户端不能反过来依赖桌面服务（会形成循环）。这里只放一个纯内存标记，
 * 不碰后端、不碰 Tauri。
 *
 * 语义：`quitting=true` 之后**不再发起新的 turn / 写请求**（退出流程不可逆）；
 * 只有「壳根本不可用（浏览器预览/IAB）」这类硬失败才允许重置，避免页面永远卡死。
 */

export type CloseBehavior = "ask" | "tray" | "quit";

/** 关闭行为的三档（契约 §11）：每次询问 / 最小化到托盘 / 直接退出。 */
export const CLOSE_BEHAVIOR_OPTIONS: { value: CloseBehavior; label: string; desc: string }[] = [
  {
    value: "ask",
    label: "每次询问",
    desc: "关闭主窗口时弹出确认框：最小化到托盘 / 退出 QIO / 取消",
  },
  {
    value: "tray",
    label: "最小化到托盘",
    desc: "关闭主窗口只隐藏窗口，QIO 与后端继续运行；托盘可恢复",
  },
  {
    value: "quit",
    label: "直接退出",
    desc: "关闭主窗口即进入真正退出流程（保存状态、有界收尾、结束后端进程树）",
  },
];

export const DEFAULT_CLOSE_BEHAVIOR: CloseBehavior = "ask";

/** 旧客户端/后端没有该字段时的保守默认：先问用户，不静默杀掉或藏起来。 */
export function normalizeCloseBehavior(value: unknown): CloseBehavior {
  return value === "tray" || value === "quit" || value === "ask"
    ? value
    : DEFAULT_CLOSE_BEHAVIOR;
}

let quitting = false;

export function isQuitting(): boolean {
  return quitting;
}

/** 第一个调用者返回 true；重复调用（例如标题条 + 系统关闭同时到达）返回 false。 */
export function beginQuitGuard(): boolean {
  if (quitting) return false;
  quitting = true;
  return true;
}

/** 只有「壳不可用」这类硬失败才重置：让用户还能重试，而不是卡在假退出状态。 */
export function resetQuitGuard(): void {
  quitting = false;
}
