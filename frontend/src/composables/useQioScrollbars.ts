/**
 * QIO 统一滚动指示条引擎（共享原语，唯一实现）。
 *
 * 目标（用户规则）：QIO 自有界面里不再出现原生滚动条外观；必要的滚动区域改用
 * 浮在边缘、**不占据布局列**、可隐藏的 QIO 指示条；不需要滚动的区域不预留轨道空白。
 *
 * 三条策略由调用方声明：
 * - auto    —— 实际溢出时允许滚动；指示条默认隐藏，滚动 / 靠近边缘 / 键盘使用该区域时按需显示；
 * - hidden  —— 保留原生滚动与定位能力（scrollTop/scrollLeft、平移、坐标换算照旧），
 *               但**始终不画**指示条。板面、软换行输入区用它；
 * - content —— 不创建独立滚动视口，让内容自然展开 / 换行 / 折叠。
 *               它不是本引擎的策略：意味着**不要**用 overflow 容器，由布局实现
 *               （data-qio-fit / data-qio-scope，见 styles/scroll.css）。
 *
 * 实现自查表：
 * 1. 不接管滚动物理：只读写 scrollTop/scrollLeft；惯性、键盘、选择、输入法全部保留；
 * 2. 不预留布局列：指示条是绝对定位覆盖层，宿主宽度不因它变化；
 * 3. 不可见时没有吞点击的透明遮罩：轨道 pointer-events:none，只有可见滑块可交互；
 * 4. 唤出靠宿主自身的 pointermove 边缘感应，不额外盖感应层；
 * 5. 拖动与卡片拖动/连线/板面平移隔离：pointerdown 即 stopPropagation；
 * 6. ResizeObserver + MutationObserver 合并到一帧一次测量；不轮询、不全页扫描；
 * 7. 监听、观察器、计时器在 destroy / detach 时全部释放。
 */
import { onBeforeUnmount, watch, type Directive, type Ref } from "vue";
import { scrollIndicatorAlwaysOn, subscribeScrollbarPreference } from "../utils/scrollIndicator";

export type QioScrollPolicy = "auto" | "hidden";
export type QioScrollAxis = "y" | "x" | "both";

export interface QioScrollbarOptions {
  /** 默认 auto。hidden 时完全不创建指示条，也不挂监听。 */
  policy?: QioScrollPolicy;
  /** 需要指示条的方向，默认 both。 */
  axis?: QioScrollAxis;
  /** 滑块最小长度（px），保证极长内容下仍可抓取。 */
  minThumb?: number;
  /** 边缘感应区宽度（px）：指针进入该带宽即唤出指示条。 */
  edgeZone?: number;
  /** 停止滚动后指示条保留时长（ms），之后淡出。 */
  lingerMs?: number;
}

export interface QioScrollbars {
  /** 内容变化后主动重算（可选；引擎自己也会观察）。 */
  refresh(): void;
  setOptions(next: QioScrollbarOptions): void;
  destroy(): void;
}

const DEFAULTS = {
  policy: "auto" as QioScrollPolicy,
  axis: "both" as QioScrollAxis,
  minThumb: 24,
  edgeZone: 16,
  lingerMs: 900,
};

/** 1px 取整误差不算「可滚动」。 */
const OVERFLOW_EPS = 1;

let hostSeq = 0;
const controllers = new WeakMap<HTMLElement, QioScrollbars>();

export const QIO_SCROLL_HOST_CLASS = "qio-scroll-host";
export const QIO_SCROLL_RAIL_CLASS = "qio-scroll-rail";
export const QIO_SCROLL_THUMB_CLASS = "qio-scroll-thumb";
/** 实际溢出的方向："y x" / "y" / "x" / "none"（供 CSS 与测试读取）。 */
export const QIO_SCROLL_RANGE_ATTR = "data-qio-range";

function normalize(
  value: QioScrollbarOptions | QioScrollPolicy | undefined | null,
): Required<QioScrollbarOptions> {
  if (typeof value === "string") return { ...DEFAULTS, policy: value };
  const v = value ?? {};
  return {
    policy: v.policy === "hidden" ? "hidden" : "auto",
    axis: v.axis === "x" || v.axis === "y" || v.axis === "both" ? v.axis : DEFAULTS.axis,
    minThumb: typeof v.minThumb === "number" && v.minThumb > 0 ? v.minThumb : DEFAULTS.minThumb,
    edgeZone: typeof v.edgeZone === "number" && v.edgeZone >= 0 ? v.edgeZone : DEFAULTS.edgeZone,
    lingerMs: typeof v.lingerMs === "number" && v.lingerMs >= 0 ? v.lingerMs : DEFAULTS.lingerMs,
  };
}

interface RailState {
  axis: "x" | "y";
  rail: HTMLElement;
  thumb: HTMLElement;
  overflow: boolean;
}

export function createQioScrollbars(
  host: HTMLElement,
  options: QioScrollbarOptions = {},
): QioScrollbars {
  let opt = normalize(options);
  let destroyed = false;
  /** hidden 策略下暂挂：不测量、不监听、不画指示条。 */
  let paused = opt.policy === "hidden";
  let attached = false;
  let rafId = 0;
  let rafViaTimeout = false;
  let lingerTimer: ReturnType<typeof setTimeout> | null = null;
  let active = false;
  let rails: RailState[] = [];
  let ro: ResizeObserver | null = null;
  let mo: MutationObserver | null = null;
  let unsubscribePref: (() => void) | null = null;
  let observedChildren = new Set<Element>();
  let lastRange = "";
  let dragging: {
    axis: "x" | "y";
    startCoord: number;
    startScroll: number;
    scrollRange: number;
    track: number;
    size: number;
  } | null = null;

  host.classList.add(QIO_SCROLL_HOST_CLASS);
  host.dataset.qioScroll = opt.policy;
  if (!host.id) {
    hostSeq += 1;
    host.id = "qio-scroll-" + hostSeq;
  }

  function applyRange(value: string) {
    if (lastRange === value) return;
    lastRange = value;
    host.setAttribute(QIO_SCROLL_RANGE_ATTR, value);
  }

  function ensureRail(axis: "x" | "y"): RailState {
    const existing = rails.find((r) => r.axis === axis);
    if (existing) return existing;
    const rail = document.createElement("div");
    rail.className = QIO_SCROLL_RAIL_CLASS;
    rail.dataset.axis = axis;
    rail.setAttribute("role", "scrollbar");
    rail.setAttribute("aria-orientation", axis === "y" ? "vertical" : "horizontal");
    rail.setAttribute("aria-valuemin", "0");
    rail.setAttribute("aria-valuemax", "100");
    rail.setAttribute("aria-valuenow", "0");
    rail.setAttribute("aria-controls", host.id);
    rail.setAttribute("aria-label", axis === "y" ? "纵向滚动位置" : "横向滚动位置");
    // 不进入无意义的 Tab 序列：键盘滚动仍作用在原区域上
    rail.tabIndex = -1;
    const thumb = document.createElement("div");
    thumb.className = QIO_SCROLL_THUMB_CLASS;
    rail.appendChild(thumb);
    host.appendChild(rail);
    const state: RailState = { axis, rail, thumb, overflow: false };
    rails.push(state);
    thumb.addEventListener("pointerdown", onThumbDown);
    return state;
  }

  function removeRails() {
    for (const r of rails) {
      r.thumb.removeEventListener("pointerdown", onThumbDown);
      r.rail.remove();
    }
    rails = [];
  }

  /**
   * 指示条是宿主的绝对定位子节点，而宿主自己就是滚动容器 ——
   * 滚动容器里的 absolute 子节点会**随内容一起滚走**（只有 fixed 不会，但 fixed 会被
   * 祖先的 transform 改变包含块，板面缩放场景不可用）。
   * 所以这里按当前滚动偏移反向平移：指示条始终贴在可视边缘。
   */
  function syncRailOffset() {
    if (!rails.length) return;
    const tx = host.scrollLeft;
    const ty = host.scrollTop;
    for (const r of rails) {
      r.rail.style.transform = "translate(" + tx + "px," + ty + "px)";
    }
  }

  function canScroll(axis: "x" | "y"): boolean {
    // 折叠 / 隐藏的宿主在某一轴上是零尺寸：那里既没有可视区域，也不该有「可滚动范围」
    // （例如收起状态的话题栏 clientWidth=0，内容宽度仍会算进 scrollWidth → 假溢出 + 假轨道）。
    const view = axis === "y" ? host.clientHeight : host.clientWidth;
    if (view <= 0) return false;
    return axis === "y"
      ? host.scrollHeight - host.clientHeight > OVERFLOW_EPS
      : host.scrollWidth - host.clientWidth > OVERFLOW_EPS;
  }

  function measure() {
    rafId = 0;
    rafViaTimeout = false;
    if (destroyed || paused) return;
    const axes: Array<"x" | "y"> = opt.axis === "both" ? ["y", "x"] : [opt.axis];
    const overflowing: string[] = [];
    for (const axis of axes) {
      const can = canScroll(axis);
      let rail = rails.find((r) => r.axis === axis);
      if (can && !rail) rail = ensureRail(axis);
      if (!rail) continue;
      rail.overflow = can;
      if (!can) {
        rail.rail.classList.remove("is-visible", "is-persistent");
        continue;
      }
      overflowing.push(axis);
      const track = axis === "y" ? rail.rail.clientHeight : rail.rail.clientWidth;
      const view = axis === "y" ? host.clientHeight : host.clientWidth;
      const content = axis === "y" ? host.scrollHeight : host.scrollWidth;
      const scrollPos = axis === "y" ? host.scrollTop : host.scrollLeft;
      const range = Math.max(0, content - view);
      const visible = Math.min(1, view / Math.max(1, content));
      const size = Math.max(opt.minThumb, Math.round(track * visible));
      const maxOffset = Math.max(0, track - size);
      const offset = range <= 0 ? 0 : Math.round((scrollPos / range) * maxOffset);
      rail.thumb.style.setProperty("--qio-thumb-size", size + "px");
      rail.thumb.style.setProperty("--qio-thumb-offset", offset + "px");
      const ratio = range <= 0 ? 0 : Math.round((scrollPos / range) * 100);
      rail.rail.setAttribute("aria-valuenow", String(Math.max(0, Math.min(100, ratio))));
    }
    syncRailOffset();
    applyRange(overflowing.length ? overflowing.join(" ") : "none");
    applyVisibility();
  }

  function scheduleMeasure() {
    if (destroyed || paused || rafId) return;
    if (typeof requestAnimationFrame === "function") {
      rafViaTimeout = false;
      rafId = requestAnimationFrame(() => measure());
    } else {
      rafViaTimeout = true;
      rafId = setTimeout(() => measure(), 16) as unknown as number;
    }
  }

  function applyVisibility() {
    const always = scrollIndicatorAlwaysOn();
    for (const r of rails) {
      const show = r.overflow && (active || always);
      r.rail.classList.toggle("is-visible", show);
      r.rail.classList.toggle("is-persistent", r.overflow && always && !active);
    }
  }

  function clearLinger() {
    if (lingerTimer !== null) {
      clearTimeout(lingerTimer);
      lingerTimer = null;
    }
  }

  function scheduleLinger(ms = opt.lingerMs) {
    clearLinger();
    lingerTimer = setTimeout(() => {
      lingerTimer = null;
      if (dragging) return;
      active = false;
      applyVisibility();
    }, ms);
  }

  function activate() {
    if (!active) {
      active = true;
      applyVisibility();
    }
    clearLinger();
    if (!dragging) scheduleLinger();
  }

  // ---- 事件（全部可释放）----
  const onScroll = () => {
    activate();
    // 同步贴合：只做样式写入，避免指示条在快速滚动时出现一帧的位移滞后
    syncRailOffset();
    scheduleMeasure();
  };

  const onPointerMove = (e: PointerEvent) => {
    if (dragging) return;
    const rect = host.getBoundingClientRect();
    const nearY = canScroll("y") && rect.right - e.clientX <= opt.edgeZone;
    const nearX = canScroll("x") && rect.bottom - e.clientY <= opt.edgeZone;
    if (nearY || nearX) {
      activate();
      return;
    }
    if (active) scheduleLinger(240);
  };

  const onPointerLeave = () => {
    if (dragging) return;
    clearLinger();
    active = false;
    applyVisibility();
  };

  const SCROLL_KEYS = new Set([
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "PageUp",
    "PageDown",
    "Home",
    "End",
    " ",
  ]);
  const onKeyDown = (e: KeyboardEvent) => {
    if (!SCROLL_KEYS.has(e.key)) return;
    activate();
    scheduleLinger(1400);
  };

  const onFocusIn = () => activate();
  const onFocusOut = () => {
    if (!dragging) scheduleLinger(500);
  };

  // ---- 滑块拖动（与卡片拖动 / 连线 / 板面平移隔离）----
  function onThumbDown(e: PointerEvent) {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    const railEl = (e.currentTarget as HTMLElement).parentElement;
    const axis: "x" | "y" = railEl?.dataset.axis === "x" ? "x" : "y";
    const state = rails.find((r) => r.axis === axis);
    if (!state || !state.overflow) return;
    e.preventDefault();
    // 同一个指针操作不得再触发卡片拖动 / 连线创建 / 板面平移
    e.stopPropagation();
    const track = axis === "y" ? state.rail.clientHeight : state.rail.clientWidth;
    const view = axis === "y" ? host.clientHeight : host.clientWidth;
    const content = axis === "y" ? host.scrollHeight : host.scrollWidth;
    const visible = Math.min(1, view / Math.max(1, content));
    dragging = {
      axis,
      startCoord: axis === "y" ? e.clientY : e.clientX,
      startScroll: axis === "y" ? host.scrollTop : host.scrollLeft,
      scrollRange: Math.max(0, content - view),
      track,
      size: Math.max(opt.minThumb, Math.round(track * visible)),
    };
    clearLinger();
    active = true;
    host.classList.add("is-qio-scroll-dragging");
    applyVisibility();
    try {
      state.thumb.setPointerCapture(e.pointerId);
    } catch {
      // 不支持指针捕获时仍靠 thumb 上的 move/up 事件收尾
    }
    state.thumb.addEventListener("pointermove", onThumbMove);
    state.thumb.addEventListener("pointerup", onThumbUp);
    state.thumb.addEventListener("pointercancel", onThumbUp);
    state.thumb.addEventListener("lostpointercapture", onThumbUp);
  }

  function onThumbMove(e: PointerEvent) {
    if (!dragging) return;
    e.preventDefault();
    e.stopPropagation();
    const d = dragging;
    const maxOffset = Math.max(1, d.track - d.size);
    const coord = d.axis === "y" ? e.clientY : e.clientX;
    const ratio = Math.max(0, Math.min(1, (coord - d.startCoord) / maxOffset));
    const next = d.startScroll + ratio * d.scrollRange;
    if (d.axis === "y") host.scrollTop = next;
    else host.scrollLeft = next;
    syncRailOffset();
    scheduleMeasure();
  }

  function onThumbUp(e: PointerEvent) {
    if (!dragging) return;
    e.stopPropagation();
    const thumb = e.currentTarget as HTMLElement;
    thumb.removeEventListener("pointermove", onThumbMove);
    thumb.removeEventListener("pointerup", onThumbUp);
    thumb.removeEventListener("pointercancel", onThumbUp);
    thumb.removeEventListener("lostpointercapture", onThumbUp);
    try {
      if (typeof thumb.hasPointerCapture === "function" && thumb.hasPointerCapture(e.pointerId)) {
        thumb.releasePointerCapture(e.pointerId);
      }
    } catch {
      // 已释放
    }
    dragging = null;
    host.classList.remove("is-qio-scroll-dragging");
    scheduleLinger(700);
    scheduleMeasure();
  }

  // ---- 观察：宿主 + 直接子元素尺寸；子树增删 / 文本变化合并到一帧 ----
  function syncChildren() {
    if (!ro) return;
    const next = new Set<Element>(Array.from(host.children));
    for (const child of next) {
      if (child.classList.contains(QIO_SCROLL_RAIL_CLASS)) continue;
      if (!observedChildren.has(child)) {
        ro.observe(child);
        observedChildren.add(child);
      }
    }
    for (const child of [...observedChildren]) {
      if (!next.has(child) || !child.isConnected) {
        ro.unobserve(child);
        observedChildren.delete(child);
      }
    }
  }

  function startObserving() {
    if (typeof ResizeObserver === "function") {
      ro = new ResizeObserver(() => scheduleMeasure());
      ro.observe(host);
      syncChildren();
    }
    if (typeof MutationObserver === "function") {
      mo = new MutationObserver(() => {
        syncChildren();
        scheduleMeasure();
      });
      mo.observe(host, { childList: true, subtree: true, characterData: true });
    }
  }

  function stopObserving() {
    ro?.disconnect();
    ro = null;
    observedChildren = new Set();
    mo?.disconnect();
    mo = null;
  }

  function attach() {
    if (attached || destroyed) return;
    attached = true;
    host.addEventListener("scroll", onScroll, { passive: true });
    host.addEventListener("pointermove", onPointerMove, { passive: true });
    host.addEventListener("pointerleave", onPointerLeave, { passive: true });
    host.addEventListener("keydown", onKeyDown);
    host.addEventListener("focusin", onFocusIn);
    host.addEventListener("focusout", onFocusOut);
    unsubscribePref = subscribeScrollbarPreference(() => applyVisibility());
    startObserving();
    scheduleMeasure();
  }

  function detach() {
    if (!attached) return;
    attached = false;
    if (rafId) {
      if (rafViaTimeout) clearTimeout(rafId as unknown as ReturnType<typeof setTimeout>);
      else if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(rafId);
      rafId = 0;
    }
    clearLinger();
    active = false;
    dragging = null;
    host.removeEventListener("scroll", onScroll);
    host.removeEventListener("pointermove", onPointerMove);
    host.removeEventListener("pointerleave", onPointerLeave);
    host.removeEventListener("keydown", onKeyDown);
    host.removeEventListener("focusin", onFocusIn);
    host.removeEventListener("focusout", onFocusOut);
    unsubscribePref?.();
    unsubscribePref = null;
    stopObserving();
    removeRails();
    host.classList.remove("is-qio-scroll-dragging");
  }

  if (paused) applyRange("none");
  else attach();

  return {
    refresh: scheduleMeasure,
    setOptions(next) {
      const n = normalize(next);
      const policyChanged = n.policy !== opt.policy;
      opt = n;
      host.dataset.qioScroll = opt.policy;
      if (policyChanged) {
        paused = opt.policy === "hidden";
        if (paused) {
          detach();
          applyRange("none");
          host.classList.add(QIO_SCROLL_HOST_CLASS);
        } else {
          attach();
        }
        return;
      }
      scheduleMeasure();
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      detach();
      host.classList.remove(QIO_SCROLL_HOST_CLASS);
      delete host.dataset.qioScroll;
      host.removeAttribute(QIO_SCROLL_RANGE_ATTR);
      lastRange = "";
    },
  };
}

/**
 * Vue 指令：v-qio-scrollbar / v-qio-scrollbar="{ policy: 'hidden' }" / v-qio-scrollbar="'hidden'"。
 * 用在**已经存在**的滚动容器上，不改变它的布局（宿主若不是定位元素，由
 * .qio-scroll-host 的 CSS 补 position: relative）。
 */
export const vQioScrollbar: Directive<
  HTMLElement,
  QioScrollbarOptions | QioScrollPolicy | undefined
> = {
  mounted(el, binding) {
    controllers.set(el, createQioScrollbars(el, normalize(binding.value)));
  },
  updated(el, binding) {
    const ctrl = controllers.get(el);
    if (!ctrl) {
      controllers.set(el, createQioScrollbars(el, normalize(binding.value)));
      return;
    }
    const prev = normalize(binding.oldValue ?? {});
    const next = normalize(binding.value ?? {});
    if (JSON.stringify(prev) !== JSON.stringify(next)) ctrl.setOptions(next);
  },
  unmounted(el) {
    controllers.get(el)?.destroy();
    controllers.delete(el);
  },
};

/**
 * 组合式用法：自己持有 ref 的组件用这个（内部同样是上表那份引擎）。
 * options 可以是静态对象或 getter（策略随状态变化时用 getter）。
 */
export function useQioScrollbars(
  el: Ref<HTMLElement | null | undefined>,
  options: QioScrollbarOptions | (() => QioScrollbarOptions) = {},
): { refresh: () => void } {
  let ctrl: QioScrollbars | null = null;
  const resolve = () => (typeof options === "function" ? options() : options);

  const stop = watch(
    el,
    (next) => {
      ctrl?.destroy();
      ctrl = next ? createQioScrollbars(next, resolve()) : null;
    },
    { immediate: true, flush: "post" },
  );

  const stopOpts = watch(
    () => JSON.stringify(resolve()),
    () => {
      if (ctrl && el.value) ctrl.setOptions(resolve());
    },
  );

  onBeforeUnmount(() => {
    stop();
    stopOpts();
    ctrl?.destroy();
    ctrl = null;
  });

  return { refresh: () => ctrl?.refresh() };
}
