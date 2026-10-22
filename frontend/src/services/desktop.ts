/**
 * 桌面壳能力封装（R9 契约 §11，W5）。
 *
 * 只有这一个模块直接与 Tauri 交互：组件与 store 不直接写 `invoke`，
 * 浏览器预览（IAB）里也不会因为缺少 `__TAURI_INTERNALS__` 抛异常。
 *
 * 关闭与退出的分工（与 Rust 侧一一对应）：
 * 1. 关闭行为由设置 `ui.close_behavior` 决定（store 里是最新值）；
 * 2. 真正退出：先置前端守卫（不再发起新 turn）→ `POST /api/lifecycle/shutdown`
 *    （后端有界收尾，保存状态、保留有效待审批）→ 调用壳命令 `request_close`
 *    （action=quit）：壳按 pid 等 sidecar 退出、超时 `taskkill /PID <pid> /T /F`
 *    兜底并核验 pid 消失，最后退出应用。
 */
import { api } from "./api";
import { beginQuitGuard, resetQuitGuard } from "./quitGuard";

export type CloseAction = "ask" | "tray" | "quit";

export interface CloseOutcome {
  /** ask = 壳要求前端弹确认框；hidden = 已隐藏到托盘；quitting = 已进入真正退出 */
  action: string;
  hidden: boolean;
  quitting: boolean;
  detail: string;
}

export interface ShutdownReport {
  clean: boolean;
  unfinished: string[];
  phases: string[];
  detail: string;
}

export interface ShutdownResponse {
  done: boolean;
  already: boolean;
  timeout: boolean;
  grace_seconds: number;
  approvals_restored: number;
  report: ShutdownReport;
}

export interface QuitResult {
  ok: boolean;
  detail: string;
  shutdown: ShutdownResponse | null;
}

/** 是否在 Tauri 壳里（浏览器预览没有它：窗口按钮/关闭行为都不出现）。 */
export function inTauriShell(): boolean {
  return Boolean((globalThis as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__);
}

function describeError(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err ?? "未知错误");
}

/** 调用壳的统一关闭入口（R9 §11：标题条/系统关闭/Alt+F4 都走这里）。 */
export async function requestClose(action: CloseAction): Promise<CloseOutcome> {
  const { invoke } = await import("@tauri-apps/api/core");
  return (await invoke("request_close", { action })) as CloseOutcome;
}

/** 监听壳发来的事件（关闭请求 / 托盘退出）。浏览器预览里返回空取消函数。 */
export async function listenShellEvent(
  event: "qio://close-requested" | "qio://tray-quit",
  handler: () => void,
): Promise<() => void> {
  if (!inTauriShell()) return () => {};
  try {
    const { listen } = await import("@tauri-apps/api/event");
    const unlisten = await listen(event, () => handler());
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

let quitInFlight: Promise<QuitResult> | null = null;

/**
 * 真正退出（可重复调用，幂等）。
 *
 * 返回 `ok=false` 只发生在「壳命令本身失败」（进程仍活着）：此时重置前端守卫，
 * 让用户还能继续用/重试 —— 绝不假装已经退出。后端收尾失败不属于这一类：
 * 壳仍会按 pid 结束后端进程树并核验，退出不会因此卡住。
 */
export function startQuitFlow(): Promise<QuitResult> {
  if (quitInFlight) return quitInFlight;
  const run = (async (): Promise<QuitResult> => {
    beginQuitGuard(); // 从这一刻起不再发起新 turn / 写请求
    let shutdown: ShutdownResponse | null = null;
    try {
      shutdown = await api.shutdownBackend();
    } catch (err) {
      // 后端可能已经退出/不可达：如实记录，但仍然继续走壳退出（壳会按 pid 核验）。
      console.error("[qio] 后端退出收尾请求失败（继续按 pid 结束本实例进程树）", err);
    }
    try {
      const outcome = await requestClose("quit");
      return { ok: true, detail: outcome.detail, shutdown };
    } catch (err) {
      const detail = `无法完成桌面退出：${describeError(err)}`;
      console.error("[qio]", detail);
      resetQuitGuard();
      return { ok: false, detail, shutdown };
    }
  })();
  quitInFlight = run;
  // 失败只发生在壳命令不可用（进程仍在）：允许用户重试。成功路径进程马上就会退出，
  // 这里清掉引用不影响幂等 —— 重复调用会被退出守卫/壳的 quitting 标记挡住。
  void run.finally(() => {
    if (quitInFlight === run) quitInFlight = null;
  });
  return run;
}
