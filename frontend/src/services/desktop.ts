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

/* ------------------------------------------------------------------------- *
 * 0.3.2 窗口外框通道（壳侧 T2；契约由 Lead 定稿）
 *
 * - invoke qio_get_window_frame：首屏窗口状态（与 qio://window-state 同构）
 * - listen qio://window-state：normal / maximized / snapped、DPI、frame 模式变化
 * - listen qio://caption-hover：{ target:"max", hover, ts }（原生 overlay 吃掉 DOM hover 时的视觉补偿）
 * - invoke qio_set_hit_regions：物理像素命中区（WindowChrome 上报 max/按钮/interactive 可点洞，
 *   否则 overlay 的 HTCAPTION 会把设置/权限/异常/互动挂载点的点击吃掉）
 *
 * 浏览器预览（IAB）与 0.3.1 旧壳没有这些命令/事件：一律静默降级（null / 空取消函数），
 * 不抛错、不阻塞启动（与 requestClose/listenShellEvent 的既有约定一致）。
 * ------------------------------------------------------------------------- */

export interface WindowRectPx {
  x: number;
  y: number;
  w: number;
  h: number;
}

export type QioWindowFrameMode = "css" | "native" | "dwm";
export type QioWindowStateName = "normal" | "maximized" | "snapped";

/** 壳 → 前端窗口状态（qio_get_window_frame 返回值 / qio://window-state payload）。 */
export interface QioWindowStatePayload {
  state?: QioWindowStateName;
  /** 贴靠分区（halves/quarters/thirds/...），normal 时为 null。 */
  zone?: string | null;
  /** 窗口矩形（物理 px）。 */
  rect?: WindowRectPx | null;
  /** 工作区矩形（物理 px）。 */
  workArea?: WindowRectPx | null;
  dpi?: number;
  scaleFactor?: number;
  /** 壳侧声明的留白/圆角（物理 px；与 CSS 令牌换算值对照用）。 */
  padding?: number;
  /** qio_get_window_frame 的主字段名（与 padding 同值；两个都读，兼容壳字段演进）。 */
  pad?: number;
  radius?: number;
  frame?: QioWindowFrameMode;
  titlebar?: "native" | "dom";
  passthrough?: string;
  ts?: number;
}

/** 前端 → 壳命中区（全部物理 px、窗口客户区坐标；rect = getBoundingClientRect()*dpr）。 */
export interface QioHitRegions {
  dpr: number;
  /** 可见内容区留白（物理 px；maximized/snapped/native/dwm 时为 0）。 */
  framePadding: number;
  radius: number;
  cornerRadius: number;
  /** 可见边缘内缩放命中带（物理 px；契约 8，非 css/normal 时 0）。 */
  resizeBorder: number;
  state: QioWindowStateName;
  /** 标题条拖拽面（HTCAPTION 候选）。 */
  titlebar: WindowRectPx | null;
  /** 最大化按钮（HTMAXBUTTON / Snap Layouts）。 */
  maxButton: WindowRectPx | null;
  /** 最小化、关闭等仍由 DOM 处理的按钮。 */
  otherButtons: WindowRectPx[];
  /** 其它必须保持 WebView 可点的洞（设置/权限模式/异常入口/互动挂载点）——
   *  少了它们，overlay 的 HTCAPTION 会把点击吃掉。 */
  interactive: WindowRectPx[];
}

export interface QioCaptionHoverPayload {
  target?: string;
  hover?: boolean;
  ts?: number;
}

/** 新通道的监听（失败只记 warn：旧壳/浏览器里没有这些事件是正常降级）。 */
async function listenQuiet(
  event: string,
  handler: (payload: unknown) => void,
): Promise<() => void> {
  if (!inTauriShell()) return () => {};
  try {
    const { listen } = await import("@tauri-apps/api/event");
    const unlisten = await listen(event, (raw) => handler((raw as { payload?: unknown } | null)?.payload));
    return () => {
      try {
        unlisten();
      } catch {
        // 页面卸载时取消订阅失败不影响任何功能
      }
    };
  } catch (err) {
    console.warn(`[qio] 窗口状态通道监听失败（按旧壳/浏览器预览降级）：${event}`, err);
    return () => {};
  }
}

/** 首屏取窗口外框状态；旧壳/浏览器返回 null（调用方按默认 normal/css 处理）。 */
export async function getWindowFrame(): Promise<QioWindowStatePayload | null> {
  if (!inTauriShell()) return null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const payload = await invoke("qio_get_window_frame");
    return payload && typeof payload === "object" ? (payload as QioWindowStatePayload) : null;
  } catch (err) {
    console.warn("[qio] qio_get_window_frame 不可用（按旧壳/浏览器预览降级）", err);
    return null;
  }
}

/** 监听壳完成建窗后的首帧状态（qio://frame-ready，payload 与 qio_get_window_frame 同构）。 */
export function listenFrameReady(
  handler: (payload: QioWindowStatePayload) => void,
): Promise<() => void> {
  return listenQuiet("qio://frame-ready", (payload) =>
    handler(payload && typeof payload === "object" ? (payload as QioWindowStatePayload) : {}),
  );
}

/** 监听窗口状态（最大化/贴靠/还原/DPI/frame 模式变化）。 */
export function listenWindowState(
  handler: (payload: QioWindowStatePayload) => void,
): Promise<() => void> {
  return listenQuiet("qio://window-state", (payload) =>
    handler(payload && typeof payload === "object" ? (payload as QioWindowStatePayload) : {}),
  );
}

/** 监听最大化按钮的 hover（原生 overlay 覆盖该按钮后 DOM :hover 不会触发）。 */
export function listenCaptionHover(
  handler: (payload: QioCaptionHoverPayload) => void,
): Promise<() => void> {
  return listenQuiet("qio://caption-hover", (payload) =>
    handler(payload && typeof payload === "object" ? (payload as QioCaptionHoverPayload) : {}),
  );
}

/**
 * 上报物理像素命中区。返回是否成功（失败静默：旧壳没有该命令，窗口功能仍可用）。
 * 调用方负责 rAF 合并与节流。
 */
export async function setHitRegions(regions: QioHitRegions): Promise<boolean> {
  if (!inTauriShell()) return false;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("qio_set_hit_regions", { regions });
    return true;
  } catch (err) {
    console.warn("[qio] qio_set_hit_regions 不可用（按旧壳/浏览器预览降级）", err);
    return false;
  }
}
