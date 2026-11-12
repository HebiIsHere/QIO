/**
 * 窗口外框状态通道（0.3.2）。
 *
 * 唯一职责：把壳的窗口状态落成 <html data-window-frame / data-window-state>，
 * 让 CSS 令牌（见 styles/tokens.css 的 --window-frame-*）驱动：
 * - normal：28px 透明留白 + 20px 圆角 + 自绘阴影；
 * - maximized / snapped：留白/圆角/阴影归零（内容铺满可见区）；
 * - frame=css / native / dwm：模式差异（native/dwm 无透明留白）。
 *
 * 数据来源（壳侧 T2）：
 * 1) 首屏 invoke qio_get_window_frame；
 * 2) 之后 listen qio://window-state（最大化/贴靠/还原/DPI/frame 变化）。
 * 旧壳（0.3.1）与浏览器预览两者都没有：降级为 css/normal，页面照常工作。
 *
 * 为什么在 App.setup 里就调用：BootStatus 期间窗口已经是透明留白窗，
 * 等后端就绪再写 data 属性会出现「先按 0.3.1 全窗布局画一遍、再跳成留白」的闪动。
 */
import { readonly, ref } from "vue";
import {
  getWindowFrame,
  listenFrameReady,
  listenWindowState,
  type QioWindowFrameMode,
  type QioWindowStateName,
  type QioWindowStatePayload,
  type WindowRectPx,
} from "../services/desktop";

interface WindowFrameState {
  /** normal / maximized / snapped（壳判定；snapped 含系统贴靠布局）。 */
  state: QioWindowStateName;
  /** css = 透明留白窗；native = 系统窗框；dwm = 0.3.1 现状对照。 */
  frame: QioWindowFrameMode;
  zone: string | null;
  rect: WindowRectPx | null;
  workArea: WindowRectPx | null;
  dpi: number | null;
  scaleFactor: number | null;
  /** 壳侧留白/圆角（物理 px），用于和 CSS 令牌换算值对照。 */
  paddingPx: number | null;
  radiusPx: number | null;
  titlebar: "native" | "dom" | null;
  passthrough: string | null;
  ts: number | null;
}

const DEFAULT_STATE: WindowFrameState = {
  state: "normal",
  frame: "css",
  zone: null,
  rect: null,
  workArea: null,
  dpi: null,
  scaleFactor: null,
  paddingPx: null,
  radiusPx: null,
  titlebar: null,
  passthrough: null,
  ts: null,
};

const frameState = ref<WindowFrameState>({ ...DEFAULT_STATE });
let started = false;
let unlistenState: (() => void) | null = null;
let unlistenFrameReady: (() => void) | null = null;

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function rect(value: unknown): WindowRectPx | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  return {
    x: num(raw.x) ?? 0,
    y: num(raw.y) ?? 0,
    w: num(raw.w) ?? 0,
    h: num(raw.h) ?? 0,
  };
}

/** 把任意 payload 归一化成稳定的前端状态（字段缺失/类型不对都按安全默认处理）。 */
export function normalizeWindowState(payload: unknown): WindowFrameState {
  const raw = payload && typeof payload === "object" ? (payload as Record<string, unknown>) : {};
  const state = raw.state;
  const frame = raw.frame;
  const titlebar = raw.titlebar;
  return {
    state: state === "maximized" || state === "snapped" ? state : "normal",
    frame: frame === "native" || frame === "dwm" ? frame : "css",
    zone: typeof raw.zone === "string" ? raw.zone : null,
    rect: rect(raw.rect),
    workArea: rect(raw.workArea),
    dpi: num(raw.dpi),
    scaleFactor: num(raw.scaleFactor),
    /* 壳 qio_get_window_frame 主字段叫 pad，window-state 叫 padding：两个都读 */
    paddingPx: num(raw.padding) ?? num(raw.pad),
    radiusPx: num(raw.radius),
    titlebar: titlebar === "native" || titlebar === "dom" ? titlebar : null,
    passthrough: typeof raw.passthrough === "string" ? raw.passthrough : null,
    ts: num(raw.ts),
  };
}

/** 写 <html data-window-frame / data-window-state>（CSS 令牌的唯一开关）。 */
export function applyWindowFrameState(next: WindowFrameState): void {
  frameState.value = next;
  if (typeof document === "undefined") return;
  document.documentElement.dataset.windowFrame = next.frame;
  document.documentElement.dataset.windowState = next.state;
}

/** 供非组件模块（上报/定位）读取当前窗口状态。 */
export function currentWindowFrameState(): WindowFrameState {
  return frameState.value;
}

async function start(): Promise<void> {
  // 先落默认值，保证首帧 CSS 就是 css/normal（native/dwm 会由首屏 get 立刻纠正）。
  applyWindowFrameState({ ...DEFAULT_STATE });
  const initial = await getWindowFrame();
  if (initial) applyWindowFrameState(normalizeWindowState(initial));
  unlistenFrameReady = await listenFrameReady((payload) => {
    applyWindowFrameState(normalizeWindowState(payload));
  });
  unlistenState = await listenWindowState((payload) => {
    applyWindowFrameState(normalizeWindowState(payload));
  });
}

/**
 * 在 App.setup 调用一次（幂等）：订阅状态通道并暴露只读状态。
 * 组件里需要响应式状态可直接读返回值；只需要像素几何时用 utils/windowFrame。
 */
export function useWindowFrame() {
  if (!started) {
    started = true;
    if (typeof window !== "undefined") void start();
  }
  return { windowFrame: readonly(frameState) };
}
