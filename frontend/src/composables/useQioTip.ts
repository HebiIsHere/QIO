/**
 * 用途说明（tooltip）的唯一实现：状态 + 500ms 触发规则 + 定位算法 + v-qio-tip 指令。
 *
 * 为什么要自己写：
 * - 原生 `title` 的弹出延迟、外观、键盘可达性都由系统决定，无法统一，也不能作为
 *   「图标按钮的用途说明」满足可访问性要求；
 * - 每个组件各写一个浮层会出现「同时两层提示」「卸载后还留着」「路由切换后悬空」
 *   的老问题，所以浮层全局唯一（QTipLayer 由 App 挂载一次），状态只有这一份。
 *
 * 触发规则（与任务书一致）：
 * - 鼠标悬停：500ms 后出现（TIP_SHOW_DELAY_MS）；移出立即消失；
 * - 键盘聚焦：立即出现（延迟对键盘用户没有意义，反而会漏读用途）；
 * - 触控：按压 500ms（长按）出现，抬手消失；点按后由原生 focus 路径兜底；
 * - Esc / 滚动 / 视口变化 / 路由切换：立即消失，不在屏幕上留下过期说明。
 *
 * 定位：优先在锚点下方，下方放不下且上方更宽裕时翻到上方；水平居中并夹在视口内。
 * 浮层不遮内容的关键是「留 8px 视口边距 + 固定 8px 间距」，不靠固定坐标。
 */
import { reactive } from "vue";
import type { ObjectDirective } from "vue";

/** 悬停多久后出现用途说明（契约值，测试直接引用它，不在测试里另写 500） */
export const TIP_SHOW_DELAY_MS = 500;
/** 浮层与锚点的垂直间距 */
export const TIP_GAP_PX = 8;
/** 浮层与视口边缘的最小距离 */
export const TIP_VIEWPORT_MARGIN_PX = 8;
/** 指针点按后紧跟的 focusin 不当作“键盘聚焦”（避免点一下按钮就弹说明） */
const POINTER_FOCUS_GRACE_MS = 300;
/** 浮层宿主的稳定标记：QTipLayer 挂载在 body 时带上它，供“只挂一次”判断 */
export const TIP_LAYER_ATTRIBUTE = "data-qio-tip-layer";

export interface TipAnchorRect {
  top: number;
  left: number;
  right: number;
  bottom: number;
  width: number;
  height: number;
}

export interface TipPosition {
  top: number;
  left: number;
  /** 浮层在锚点下方还是上方（供样式/测试判断，不影响可用性） */
  placement: "top" | "bottom";
}

export interface TipState {
  open: boolean;
  text: string;
  anchor: HTMLElement | null;
  rect: TipAnchorRect | null;
  position: TipPosition | null;
}

export const tipState = reactive<TipState>({
  open: false,
  text: "",
  anchor: null,
  rect: null,
  position: null,
});

let globalListenersAttached = false;

function onGlobalKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") hideTip();
}
function onGlobalViewportChange(): void {
  hideTip();
}

function attachGlobalListeners(): void {
  if (globalListenersAttached || typeof window === "undefined") return;
  globalListenersAttached = true;
  // capture：任何滚动容器里的滚动都会让锚点位置过期，浮层必须消失而不是停在旧位置
  window.addEventListener("keydown", onGlobalKeydown, true);
  window.addEventListener("scroll", onGlobalViewportChange, true);
  window.addEventListener("resize", onGlobalViewportChange, true);
  window.addEventListener("popstate", onGlobalViewportChange);
  window.addEventListener("hashchange", onGlobalViewportChange);
}

function detachGlobalListeners(): void {
  if (!globalListenersAttached || typeof window === "undefined") return;
  globalListenersAttached = false;
  window.removeEventListener("keydown", onGlobalKeydown, true);
  window.removeEventListener("scroll", onGlobalViewportChange, true);
  window.removeEventListener("resize", onGlobalViewportChange, true);
  window.removeEventListener("popstate", onGlobalViewportChange);
  window.removeEventListener("hashchange", onGlobalViewportChange);
}

/** 立即显示（指令在 500ms 定时器到点后调用；测试也可直接调用） */
export function showTip(text: string, anchor: HTMLElement): void {
  if (!text || typeof window === "undefined" || !anchor.isConnected) return;
  const r = anchor.getBoundingClientRect();
  tipState.text = text;
  tipState.anchor = anchor;
  tipState.rect = { top: r.top, left: r.left, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
  // 位置由 QTipLayer 量到真实尺寸后再算；先清空，避免复用上一条的坐标闪一下
  tipState.position = null;
  tipState.open = true;
  attachGlobalListeners();
}

/** 立即隐藏并清理（幂等：重复调用无副作用） */
export function hideTip(): void {
  if (!tipState.open && !tipState.text) {
    detachGlobalListeners();
    return;
  }
  tipState.open = false;
  tipState.text = "";
  tipState.anchor = null;
  tipState.rect = null;
  tipState.position = null;
  detachGlobalListeners();
}

/** 供 QTipLayer 在量到浮层真实尺寸后写回位置 */
export function setTipPosition(position: TipPosition | null): void {
  tipState.position = position;
}

/**
 * 纯函数：给定锚点矩形、浮层尺寸、视口尺寸，算出浮层左上角坐标。
 * 独立成纯函数是为了能直接测「下方放不下时翻上方」「靠近右边缘时夹回视口」，
 * 不依赖 jsdom 的布局能力（它不做真正的排版）。
 */
export function computeTipPosition(
  anchor: TipAnchorRect,
  tipSize: { width: number; height: number },
  viewport: { width: number; height: number },
): TipPosition {
  const margin = TIP_VIEWPORT_MARGIN_PX;
  const gap = TIP_GAP_PX;
  const spaceBelow = viewport.height - anchor.bottom;
  const spaceAbove = anchor.top;
  const fitsBelow = spaceBelow >= tipSize.height + gap + margin;
  const fitsAbove = spaceAbove >= tipSize.height + gap + margin;
  const placeBelow = fitsBelow || !fitsAbove;
  const rawTop = placeBelow ? anchor.bottom + gap : anchor.top - gap - tipSize.height;
  const top = Math.max(margin, Math.min(rawTop, viewport.height - tipSize.height - margin));
  const rawLeft = anchor.left + anchor.width / 2 - tipSize.width / 2;
  const left = Math.max(margin, Math.min(rawLeft, Math.max(margin, viewport.width - tipSize.width - margin)));
  return { top: Math.round(top), left: Math.round(left), placement: placeBelow ? "bottom" : "top" };
}

export type TipBinding = string | { text: string; delay?: number };

interface TipRecord {
  text: string;
  delay: number;
  timer: number | null;
  handlers: {
    enter: () => void;
    leave: () => void;
    down: () => void;
    focusIn: () => void;
    focusOut: () => void;
  };
}

const RECORDS = new WeakMap<HTMLElement, TipRecord>();

function normalizeBinding(value: TipBinding | undefined | null): { text: string; delay: number } {
  if (typeof value === "string") return { text: value, delay: TIP_SHOW_DELAY_MS };
  if (value && typeof value === "object" && typeof value.text === "string") {
    return { text: value.text, delay: typeof value.delay === "number" ? Math.max(0, value.delay) : TIP_SHOW_DELAY_MS };
  }
  return { text: "", delay: TIP_SHOW_DELAY_MS };
}

function clearRecordTimer(record: TipRecord): void {
  if (record.timer !== null) {
    window.clearTimeout(record.timer);
    record.timer = null;
  }
}

function attachTip(el: HTMLElement, binding: TipBinding | undefined | null): void {
  detachTip(el);
  const { text, delay } = normalizeBinding(binding);
  if (!text) return;

  let lastPointerDownAt = 0;
  const record: TipRecord = {
    text,
    delay,
    timer: null,
    handlers: {
      enter: () => undefined,
      leave: () => undefined,
      down: () => undefined,
      focusIn: () => undefined,
      focusOut: () => undefined,
    },
  };
  const schedule = (ms: number) => {
    clearRecordTimer(record);
    record.timer = window.setTimeout(() => {
      record.timer = null;
      showTip(record.text, el);
    }, ms);
  };
  record.handlers.enter = () => {
    lastPointerDownAt = 0;
    schedule(record.delay);
  };
  record.handlers.leave = () => {
    clearRecordTimer(record);
    if (tipState.anchor === el) hideTip();
  };
  record.handlers.down = () => {
    lastPointerDownAt = Date.now();
    clearRecordTimer(record);
    // 触控长按：按住 500ms 也能看到用途（不依赖 :hover）
    schedule(record.delay);
  };
  record.handlers.focusIn = () => {
    // 键盘/程序聚焦立即出现；指针刚落下的聚焦延迟到 500ms，避免点按就弹说明
    if (Date.now() - lastPointerDownAt < POINTER_FOCUS_GRACE_MS) schedule(record.delay);
    else showTip(record.text, el);
  };
  record.handlers.focusOut = () => {
    clearRecordTimer(record);
    if (tipState.anchor === el) hideTip();
  };

  el.addEventListener("pointerenter", record.handlers.enter);
  el.addEventListener("pointerleave", record.handlers.leave);
  el.addEventListener("pointerdown", record.handlers.down);
  el.addEventListener("focusin", record.handlers.focusIn);
  el.addEventListener("focusout", record.handlers.focusOut);
  RECORDS.set(el, record);
}

function detachTip(el: HTMLElement): void {
  const record = RECORDS.get(el);
  if (!record) return;
  clearRecordTimer(record);
  el.removeEventListener("pointerenter", record.handlers.enter);
  el.removeEventListener("pointerleave", record.handlers.leave);
  el.removeEventListener("pointerdown", record.handlers.down);
  el.removeEventListener("focusin", record.handlers.focusIn);
  el.removeEventListener("focusout", record.handlers.focusOut);
  RECORDS.delete(el);
  if (tipState.anchor === el) hideTip();
}

/**
 * 用法：
 *   <button v-qio-tip="'用途说明'">…</button>
 *   <button v-qio-tip="{ text: '用途说明', delay: 0 }">…</button>
 * 第二个参数形态主要给「聚焦即说明」的非常规控件用；默认一律 500ms。
 */
export const vQioTip: ObjectDirective<HTMLElement, TipBinding> = {
  mounted(el, binding) {
    attachTip(el, binding.value);
  },
  updated(el, binding) {
    if (binding.value !== binding.oldValue) attachTip(el, binding.value);
  },
  unmounted(el) {
    detachTip(el);
  },
};

/** 仅供测试/调试读取当前记录（不参与生产逻辑） */
export function hasTipRecord(el: HTMLElement): boolean {
  return RECORDS.has(el);
}
