/**
 * 桌面壳能力封装（R9 契约 §11，W5）。
 *
 * 只有这一个模块直接与 Tauri 交互：组件与 store 不直接写 `invoke`，
 * 浏览器预览（IAB）里也不会因为缺少 `__TAURI_INTERNALS__` 抛异常。
 *
 * 关闭与退出的分工（与 Rust 侧一一对应；真机 E2E 后的约定）：
 * 1. 关闭行为由设置 `ui.close_behavior` 决定；前端把结果**同步一份给壳**，
 *    壳在系统关闭事件里自己执行（ask 才通知前端弹框）—— 前端 IPC 异常时点 X 也不会没反应。
 * 2. 真正退出：前端只负责置守卫；`request_close(quit)` 后由壳完成
 *    「POST 后端有界收尾 → 等/结束本实例后端进程树 → 核验 → 退出应用」。
 */
import { beginQuitGuard } from "./quitGuard";

export type CloseAction = "ask" | "tray" | "quit";

export interface CloseOutcome {
  /** ask = 壳要求前端弹确认框；hidden = 已隐藏到托盘；quitting = 已进入真正退出 */
  action: string;
  hidden: boolean;
  quitting: boolean;
  detail: string;
}

/** 是否在 Tauri 壳里（浏览器预览没有它：窗口按钮/关闭行为都不出现）。 */
export function inTauriShell(): boolean {
  return Boolean((globalThis as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__);
}

/** 调用壳的统一关闭入口（R9 §11：标题条/系统关闭/Alt+F4 都走这里）。 */
export async function requestClose(action: CloseAction): Promise<CloseOutcome> {
  const { invoke } = await import("@tauri-apps/api/core");
  return (await invoke("request_close", { action })) as CloseOutcome;
}

/** 监听壳发来的事件（关闭问询 / 退出通知）。浏览器预览里返回空取消函数。 */
export async function listenShellEvent(
  event: "qio://close-requested" | "qio://quit-requested",
  handler: (payload?: unknown) => void,
): Promise<() => void> {
  if (!inTauriShell()) return () => {};
  try {
    const { listen } = await import("@tauri-apps/api/event");
    const unlisten = await listen(event, (raw) => handler((raw as { payload?: unknown } | null)?.payload));
    return () => {
      try {
        unlisten();
      } catch {
        // 卸载时取消订阅失败不影响退出：事件监听随页面卸载一起消失
      }
    };
  } catch (err) {
    console.error(`[qio] 监听桌面事件失败：${event}`, err);
    return () => {};
  }
}

/**
 * 把关闭行为同步给壳（壳侧兜底用）。
 *
 * 真机 install_e2e 教训：关闭行为不能只由前端判定 —— 前端启动握手异常时点 X 会毫无反应。
 * 壳缓存这一份；读不到时壳还会自己请求后端设置。失败只记日志，不影响 UI 设置本身。
 *
 * 顺序保证（F05）：连续切换会产生多次同步，IPC 的完成顺序不由前端控制。
 * 两次 invoke 并发时，先发的晚到就会让壳缓存旧值 —— 下一次点 X 按旧行为执行。
 * 因此这里把同步**串行化**并保留每一次请求的顺序：最后发起的那次最后到达，
 * 壳最终一定拿着最新有效选择。单次失败只记日志，不阻塞后面的同步。
 */
let closeBehaviorShellChain: Promise<void> = Promise.resolve();

export function syncCloseBehaviorToShell(behavior: CloseAction): Promise<void> {
  if (!inTauriShell()) return Promise.resolve();
  const next = closeBehaviorShellChain.then(async () => {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("qio_set_close_behavior", { behavior });
    } catch (err) {
      console.warn("[qio] 同步关闭行为到壳失败（壳会自己读后端设置）", err);
    }
  });
  closeBehaviorShellChain = next;
  return next;
}

/** 确认收到壳的关闭问询（ask 档）：壳 1.8s 内没等到会走「隐藏到托盘」兜底。 */
export async function ackCloseRequest(): Promise<void> {
  if (!inTauriShell()) return;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("qio_close_ack");
  } catch (err) {
    console.warn("[qio] 关闭问询 ACK 失败（壳会走兜底）", err);
  }
}

/** 退出通知到达时置前端守卫：不再发起新 turn（退出本身由壳完成）。 */
export function noteShellQuit(): void {
  beginQuitGuard();
}
