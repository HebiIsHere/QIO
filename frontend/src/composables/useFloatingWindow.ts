/**
 * useFloatingWindow —— QIO 浮动窗口核心（Task B · §12.2）
 *
 * 职责：
 * - 拖拽：pointer + mouse 双事件 + document 级 move/up；位移 >3px 判定为拖动（拖动不触发组件点击行为）。
 * - 贴靠：edge 吸附最近边（保留垂直/水平位置）、corner 吸附最近角；与其它已贴靠组件做矩形相交检测自动避让。
 * - 贴靠隐藏：贴靠后若允许隐藏则淡化/收起；mouseenter 展开、mouseleave 重新隐藏。
 * - 平滑过渡：贴靠时加 .fw-snapping（left/top .3s cubic-bezier(.22,.8,.24,1)）+ 强制 reflow。
 * - 定位全程用像素 left/top（不用 transform 定位）。
 * - 清理：onScopeDispose 移除 document/window/元素监听与定时器。
 */
import { computed, onScopeDispose, ref, watch, type Ref } from "vue";
import {
  floatingState,
  loadHidePrefs,
  loadPositions,
  savePositions,
  type DockId,
  type DockTarget,
} from "./floatingState";

export interface DefaultPos {
  x: number;
  y: number;
}
export type DefaultPosFn = (el: HTMLElement, viewport: { width: number; height: number }) => DefaultPos;

export interface UseFloatingWindowOptions {
  id: DockId;
  /** edge：贴边（Composer / PlanetDock）；corner：贴角（SettingsFloat） */
  dockMode: "edge" | "corner";
  /** 初始位置（顶部-左角像素）；或按元素尺寸/视口计算的函数 */
  defaultPos: DefaultPos | DefaultPosFn;
  /** 拖拽把手（默认整个元素） */
  dragHandle?: Ref<HTMLElement | null> | (() => HTMLElement | null);
}

const SNAP_PAD = 4;
const DRAG_THRESHOLD = 3;
const SNAP_DURATION = 330;

function clamp(v: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, v));
}
function overlap(a: { left: number; top: number; right: number; bottom: number }, b: { left: number; top: number; right: number; bottom: number }): boolean {
  return !(a.right <= b.left || a.left >= b.right || a.bottom <= b.top || a.top >= b.bottom);
}

export function useFloatingWindow(elRef: Ref<HTMLElement | null>, options: UseFloatingWindowOptions) {
  const entry = floatingState[options.id];
  /** 拖动是否发生过（供组件点击处理抑制误触发：拖完不点击） */
  const moved = ref(false);

  let boundHandle: HTMLElement | null = null;
  let boundEl: HTMLElement | null = null;
  let initedEl: HTMLElement | null = null;
  let dragging = false;
  let startX = 0;
  let startY = 0;
  let originX = 0;
  let originY = 0;
  /** 是否已越过阈值、进入真实拖动（单击永不改变位置/贴靠状态） */
  let dragStarted = false;
  let snapTimer: ReturnType<typeof setTimeout> | null = null;

  function getHandle(): HTMLElement | null {
    if (options.dragHandle) {
      const h = typeof options.dragHandle === "function" ? options.dragHandle() : options.dragHandle.value;
      if (h) return h;
    }
    return elRef.value;
  }

  function rectOf(el: HTMLElement) {
    const r = el.getBoundingClientRect();
    const width = r.width || el.offsetWidth;
    const height = r.height || el.offsetHeight;
    return { left: r.left, top: r.top, width, height, right: r.left + width, bottom: r.top + height };
  }

  function place(el: HTMLElement, x: number, y: number) {
    el.style.left = `${x}px`;
    el.style.top = `${y}px`;
    el.style.right = "auto";
    el.style.bottom = "auto";
  }

  function initPosition(el: HTMLElement) {
    const vp = { width: window.innerWidth, height: window.innerHeight };
    const r = rectOf(el);
    const saved = loadPositions()[options.id];
    const hidePref = loadHidePrefs()[options.id];
    let x: number;
    let y: number;
    if (saved) {
      x = saved.x;
      y = saved.y;
      entry.docked = saved.docked;
      entry.dockedTo = saved.dockedTo;
    } else {
      const def = typeof options.defaultPos === "function" ? options.defaultPos(el, vp) : options.defaultPos;
      x = def.x;
      y = def.y;
      entry.docked = false;
      entry.dockedTo = null;
    }
    // 贴靠隐藏偏好独立持久化：设置页开关优先于位置键里的旧值
    entry.hideEnabled = hidePref ?? saved?.hideEnabled ?? false;
    const maxX = Math.max(SNAP_PAD, vp.width - r.width - SNAP_PAD);
    const maxY = Math.max(SNAP_PAD, vp.height - r.height - SNAP_PAD);
    x = clamp(x, SNAP_PAD, maxX);
    y = clamp(y, SNAP_PAD, maxY);
    entry.x = x;
    entry.y = y;
    entry.width = r.width;
    entry.height = r.height;
    entry.hidden = false;
    el.style.position = "fixed";
    place(el, x, y);
    if (entry.docked && entry.dockedTo) {
      el.dataset.fwDocked = "1";
      el.dataset.fwTarget = entry.dockedTo;
    } else {
      delete el.dataset.fwDocked;
      delete el.dataset.fwTarget;
    }
    // 初始状态跟随开关：允许隐藏 → 挂载即进入隐藏态（hover 展开/移出再隐藏）
    if (entry.hideEnabled) maybeHide(el);
  }

  /** 拖起即释放占用：清除贴靠标记，其它组件不再避让本组件 */
  function releaseDocked(el: HTMLElement) {
    entry.docked = false;
    entry.dockedTo = null;
    delete el.dataset.fwDocked;
    delete el.dataset.fwTarget;
  }

  function restore(el: HTMLElement) {
    if (entry.hidden) {
      entry.hidden = false;
      el.classList.remove("fw-hidden");
    }
  }

  function maybeHide(el: HTMLElement) {
    if (!entry.hideEnabled) return;
    entry.hidden = true;
    el.classList.add("fw-hidden");
  }

  /** 取消进行中的贴靠过渡（拖拽开始/卸载时调用） */
  function cancelSnap() {
    if (snapTimer) {
      clearTimeout(snapTimer);
      snapTimer = null;
    }
    if (boundEl) boundEl.classList.remove("fw-snapping");
  }

  function dockedOthers() {
    const others: { left: number; top: number; right: number; bottom: number }[] = [];
    for (const id of Object.keys(floatingState) as DockId[]) {
      if (id === options.id) continue;
      const o = floatingState[id];
      if (o.docked && o.width > 0 && o.height > 0) {
        others.push({ left: o.x, top: o.y, right: o.x + o.width, bottom: o.y + o.height });
      }
    }
    return others;
  }

  function snap(el: HTMLElement) {
    const vp = { width: window.innerWidth, height: window.innerHeight };
    const r = rectOf(el);
    const w = r.width;
    const h = r.height;
    // 用实时尺寸刷新共享 entry（避免 Composer 自动增高后互斥避让用过期的宽高）
    entry.width = w;
    entry.height = h;
    const cx = r.left + w / 2;
    const cy = r.top + h / 2;
    const clampV = (v: number, max: number) => clamp(v, SNAP_PAD, max);
    const maxL = Math.max(SNAP_PAD, vp.width - w - SNAP_PAD);
    const maxT = Math.max(SNAP_PAD, vp.height - h - SNAP_PAD);
    const others = dockedOthers();
    let rect: { left: number; top: number } | null = null;
    let target: DockTarget | null = null;

    if (options.dockMode === "corner") {
      // 贴角：按距离排序，跳过与已贴靠组件重叠的角
      const corners = (
        [
          { c: "tl" as DockTarget, left: SNAP_PAD, top: SNAP_PAD },
          { c: "tr" as DockTarget, left: maxL, top: SNAP_PAD },
          { c: "bl" as DockTarget, left: SNAP_PAD, top: maxT },
          { c: "br" as DockTarget, left: maxL, top: maxT },
        ] as const
      )
        .map((o) => ({ ...o, d: Math.hypot(o.left + w / 2 - cx, o.top + h / 2 - cy) }))
        .sort((a, b) => a.d - b.d);
      for (const c of corners) {
        const cr = { left: c.left, top: c.top, right: c.left + w, bottom: c.top + h };
        if (!others.some((o) => overlap(cr, o))) {
          rect = { left: c.left, top: c.top };
          target = c.c;
          break;
        }
      }
      if (!rect) {
        rect = { left: corners[0].left, top: corners[0].top };
        target = corners[0].c;
      }
    } else {
      // 贴边：按中心距排序；优先保留垂直/水平位置，重叠则沿边微调（4 / max / 中点）
      const edges: { e: DockTarget; d: number; tops?: number[]; lefts?: number[] }[] = [
        { e: "left" as DockTarget, d: cx, tops: [clampV(r.top, maxT), SNAP_PAD, maxT, maxT / 2] },
        { e: "right" as DockTarget, d: vp.width - cx, tops: [clampV(r.top, maxT), SNAP_PAD, maxT, maxT / 2] },
        { e: "top" as DockTarget, d: cy, lefts: [clampV(r.left, maxL), SNAP_PAD, maxL, maxL / 2] },
        { e: "bottom" as DockTarget, d: vp.height - cy, lefts: [clampV(r.left, maxL), SNAP_PAD, maxL, maxL / 2] },
      ].sort((a, b) => a.d - b.d);

      for (const ed of edges) {
        if (ed.e === "left") {
          for (const t of ed.tops ?? []) {
            const er = { left: SNAP_PAD, top: t, right: SNAP_PAD + w, bottom: t + h };
            if (!others.some((o) => overlap(er, o))) {
              rect = { left: SNAP_PAD, top: t };
              target = ed.e;
              break;
            }
          }
        } else if (ed.e === "right") {
          for (const t of ed.tops ?? []) {
            const er = { left: maxL, top: t, right: maxL + w, bottom: t + h };
            if (!others.some((o) => overlap(er, o))) {
              rect = { left: maxL, top: t };
              target = ed.e;
              break;
            }
          }
        } else if (ed.e === "top") {
          for (const l of ed.lefts ?? []) {
            const er = { left: l, top: SNAP_PAD, right: l + w, bottom: SNAP_PAD + h };
            if (!others.some((o) => overlap(er, o))) {
              rect = { left: l, top: SNAP_PAD };
              target = ed.e;
              break;
            }
          }
        } else {
          for (const l of ed.lefts ?? []) {
            const er = { left: l, top: maxT, right: l + w, bottom: maxT + h };
            if (!others.some((o) => overlap(er, o))) {
              rect = { left: l, top: maxT };
              target = ed.e;
              break;
            }
          }
        }
        if (rect) break;
      }
    }

    if (!rect) {
      rect = { left: clampV(r.left, maxL), top: clampV(r.top, maxT) };
      target = null;
    }

    // 平滑过渡：贴靠时加 transition class + 强制 reflow
    if (snapTimer) {
      clearTimeout(snapTimer);
      snapTimer = null;
    }
    el.classList.add("fw-snapping");
    void el.offsetWidth;
    place(el, rect.left, rect.top);
    entry.x = rect.left;
    entry.y = rect.top;
    entry.docked = true;
    entry.dockedTo = target;
    entry.hidden = false;
    el.dataset.fwDocked = "1";
    if (target) el.dataset.fwTarget = target;
    else delete el.dataset.fwTarget;
    savePositions();

    snapTimer = setTimeout(() => {
      snapTimer = null;
      el.classList.remove("fw-snapping");
      maybeHide(el);
    }, SNAP_DURATION);
  }

  function onPointerDown(e: Event) {
    const ev = e as PointerEvent | MouseEvent;
    if (dragging || ev.button !== 0) return;
    e.preventDefault();
    dragging = true;
    moved.value = false;
    dragStarted = false;
    startX = ev.clientX;
    startY = ev.clientY;
    const el = elRef.value;
    if (!el) return;
    const r = rectOf(el);
    originX = r.left;
    originY = r.top;
    // 这里不释放贴靠、不取消过渡：pointerdown 也可能是一次普通点击
  }

  /** 越过拖动阈值才真正「拖起」：取消过渡、释放贴靠占用、解除隐藏 */
  function beginDrag(el: HTMLElement) {
    if (dragStarted) return;
    dragStarted = true;
    cancelSnap();
    releaseDocked(el);
    restore(el); // 拖起时若处于隐藏态先还原
    el.classList.add("fw-dragging");
  }

  function onPointerMove(e: Event) {
    if (!dragging) return;
    const ev = e as PointerEvent | MouseEvent;
    const el = elRef.value;
    if (!el) return;
    if (Math.abs(ev.clientX - startX) > DRAG_THRESHOLD || Math.abs(ev.clientY - startY) > DRAG_THRESHOLD) {
      moved.value = true;
    }
    if (!moved.value) return; // 未超过阈值不移动（点击/微抖动不触发拖动）
    beginDrag(el);
    const vp = { width: window.innerWidth, height: window.innerHeight };
    const r = rectOf(el);
    const maxX = Math.max(SNAP_PAD, vp.width - r.width - SNAP_PAD);
    const maxY = Math.max(SNAP_PAD, vp.height - r.height - SNAP_PAD);
    const x = clamp(originX + (ev.clientX - startX), SNAP_PAD, maxX);
    const y = clamp(originY + (ev.clientY - startY), SNAP_PAD, maxY);
    place(el, x, y);
  }

  function onPointerUp() {
    if (!dragging) return;
    dragging = false;
    const wasMoved = moved.value;
    const el = elRef.value;
    if (!el) return;
    el.classList.remove("fw-dragging");
    dragStarted = false;
    if (!wasMoved) {
      // 单击：不贴靠、不写位置，保持原状态
      moved.value = false;
      return;
    }
    snap(el);
    // 拖动结束后的 click（若有）在 mouseup 之后同步触发，这里已用 moved 抑制；
    // 延迟到下一 tick 重置 moved，避免之后键盘激活（Enter/Space）被误吞。
    setTimeout(() => {
      moved.value = false;
    }, 0);
  }

  // 贴靠隐藏的「hover 展开、移出再隐藏」由各组件 CSS :hover 实现
  // （浏览器按真实几何逐帧命中，无 JS 事件时序问题）；这里只维护 hidden 状态。

  function onResize() {
    if (dragging) return;
    const el = elRef.value;
    if (!el) return;
    const vp = { width: window.innerWidth, height: window.innerHeight };
    const r = rectOf(el);
    entry.width = r.width;
    entry.height = r.height;
    const maxX = Math.max(SNAP_PAD, vp.width - r.width - SNAP_PAD);
    const maxY = Math.max(SNAP_PAD, vp.height - r.height - SNAP_PAD);
    // 贴靠关系随窗口尺寸保持：right/bottom 距新边缘仍是同一个 margin，
    // 而不是留在旧的 absolute x/y；未贴靠的组件也被钳制回可视区。
    let x = r.left;
    let y = r.top;
    switch (entry.dockedTo) {
      case "left":
        x = SNAP_PAD;
        break;
      case "right":
        x = maxX;
        break;
      case "top":
        y = SNAP_PAD;
        break;
      case "bottom":
        y = maxY;
        break;
      case "tl":
        x = SNAP_PAD;
        y = SNAP_PAD;
        break;
      case "tr":
        x = maxX;
        y = SNAP_PAD;
        break;
      case "bl":
        x = SNAP_PAD;
        y = maxY;
        break;
      case "br":
        x = maxX;
        y = maxY;
        break;
      default:
        break;
    }
    x = clamp(x, SNAP_PAD, maxX);
    y = clamp(y, SNAP_PAD, maxY);
    if (x !== r.left || y !== r.top) {
      place(el, x, y);
      entry.x = x;
      entry.y = y;
      savePositions();
    }
  }

  function bind(handle: HTMLElement, el: HTMLElement) {
    boundHandle = handle;
    boundEl = el;
    handle.addEventListener("pointerdown", onPointerDown);
    handle.addEventListener("mousedown", onPointerDown);
    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("mousemove", onPointerMove);
    document.addEventListener("pointerup", onPointerUp);
    document.addEventListener("mouseup", onPointerUp);
    window.addEventListener("resize", onResize);
  }

  function unbind() {
    if (boundHandle) {
      boundHandle.removeEventListener("pointerdown", onPointerDown);
      boundHandle.removeEventListener("mousedown", onPointerDown);
      boundHandle = null;
    }
    if (boundEl) boundEl = null;
    document.removeEventListener("pointermove", onPointerMove);
    document.removeEventListener("mousemove", onPointerMove);
    document.removeEventListener("pointerup", onPointerUp);
    document.removeEventListener("mouseup", onPointerUp);
    window.removeEventListener("resize", onResize);
  }

  // 元素就绪（模板 ref / 测试直传）后初始化并绑定；handle 变化时重绑
  const handleTarget = computed(() => {
    if (!elRef.value) return null;
    return getHandle() ?? elRef.value;
  });
  watch(
    handleTarget,
    (target) => {
      unbind();
      if (!target) return;
      const el = elRef.value;
      if (!el) return;
      if (initedEl !== el) {
        initedEl = el;
        initPosition(el);
      }
      bind(target, el);
    },
    { flush: "post", immediate: true },
  );

  // 开关状态变化时立即同步视觉：开启 → 隐藏，关闭 → 解除隐藏态
  watch(
    () => entry.hideEnabled,
    (enabled) => {
      const el = elRef.value;
      if (!el) return;
      if (enabled) maybeHide(el);
      else if (entry.hidden) restore(el);
    },
  );

  onScopeDispose(() => {
    unbind();
    cancelSnap();
  });

  return { entry, moved };
}
