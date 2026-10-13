/**
 * useResizeGesture —— 尺寸拖动的公共手势层（互动模式契约 2026-10-13 · IRL §2）
 *
 * 边界：
 * - 只负责「指针 → 尺寸」这一段。改不改板面状态、要不要排队保存，由调用方决定。
 * - 拖动过程中只回调 `onPreview`（跟手，不播放追赶动画）；松手才回调一次 `onCommit`。
 * - **取消即恢复**：指针取消 / Esc / 窗口失焦 / 组件卸载都回到拖动前那一版，
 *   并清掉 document 监听；不留半完成状态。
 * - 全程除零防护：`scale` 返回 0 / NaN 时按 1 处理（画布缩放还没量出来时也跟手）。
 */
import { onScopeDispose, ref, type Ref } from "vue";
import {
  RESIZE_DRAG_THRESHOLD,
  RESIZE_STEP,
  boxEquals,
  resizeBox,
  stepBox,
  type ResizeBox,
  type ResizeHandle,
  type SizeLimits,
} from "../interactive/resize";

export interface ResizeGestureOptions {
  /** 常态框（拖动起点的快照来源）。拖动期间必须保持稳定。 */
  box: () => ResizeBox;
  /** 当前可用区域推出的尺寸上限。 */
  limits: () => SizeLimits;
  /** 指针位移 → 该元素坐标系位移的换算；画布卡片用 `() => 1 / view.scale`。 */
  scale?: () => number;
  /** 拖动中的实时框（只用于显示，不写状态、不排队保存）。 */
  onPreview: (box: ResizeBox) => void;
  /** 松手 / 键盘步进后的最终框，一次。 */
  onCommit: (box: ResizeBox, reason: "pointer" | "keyboard") => void;
  /** 取消：调用方据此把显示恢复成拖动前的框。 */
  onCancel?: (box: ResizeBox) => void;
  step?: number;
}

/** 「这次拖动没有改变任何数值」的容差：只滤掉真正的空操作，不吞掉亚像素位移。 */
const NOOP_EPSILON = 0.01;

interface PointerLike {
  clientX?: number;
  clientY?: number;
  pointerId?: number;
  button?: number;
  isPrimary?: boolean;
  preventDefault?: () => void;
  stopPropagation?: () => void;
}

function finiteScale(value: number | undefined): number {
  return Number.isFinite(value) && (value as number) > 0 ? (value as number) : 1;
}

export function useResizeGesture(options: ResizeGestureOptions) {
  const active = ref(false);

  let snapshot: ResizeBox | null = null;
  let handle: ResizeHandle | null = null;
  let originX = 0;
  let originY = 0;
  /** 最近一次指针位移（屏幕像素，**未**乘 scale）—— 阈值判定与松手复算都用它。 */
  let lastScreenDx = 0;
  let lastScreenDy = 0;
  let pointerId: number | null = null;
  let started = false;
  let latest: ResizeBox | null = null;
  let attached = false;

  function detach(): void {
    if (!attached) return;
    attached = false;
    document.removeEventListener("pointermove", onPointerMove, true);
    document.removeEventListener("pointerup", onPointerUp, true);
    document.removeEventListener("pointercancel", onPointerCancel, true);
    document.removeEventListener("keydown", onKeyDown, true);
    window.removeEventListener("blur", onBlur);
  }

  function reset(): void {
    detach();
    snapshot = null;
    handle = null;
    pointerId = null;
    started = false;
    latest = null;
    lastScreenDx = 0;
    lastScreenDy = 0;
    active.value = false;
  }

  /** 取消：恢复拖动前状态。已经提交过（松手）就不再回调。 */
  function cancel(): void {
    const restore = snapshot;
    const wasDragging = started;
    reset();
    if (wasDragging && restore) options.onCancel?.(restore);
  }

  function attach(): void {
    if (attached) return;
    attached = true;
    document.addEventListener("pointermove", onPointerMove, true);
    document.addEventListener("pointerup", onPointerUp, true);
    document.addEventListener("pointercancel", onPointerCancel, true);
    document.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("blur", onBlur);
  }

  function onHandleDown(nextHandle: ResizeHandle, event: PointerLike | null | undefined): void {
    if (!event) return;
    // 只接主键 / 主指针：右键菜单与多指手势不进入尺寸调整。
    if (typeof event.button === "number" && event.button !== 0) return;
    if (event.isPrimary === false) return;
    event.preventDefault?.();
    event.stopPropagation?.();
    cancel();
    snapshot = { ...options.box() };
    handle = nextHandle;
    originX = Number(event.clientX ?? 0);
    originY = Number(event.clientY ?? 0);
    pointerId = typeof event.pointerId === "number" ? event.pointerId : null;
    started = false;
    latest = null;
    attach();
  }

  function onPointerMove(event: PointerLike | null | undefined): void {
    if (!event || !snapshot || !handle) return;
    if (pointerId !== null && typeof event.pointerId === "number" && event.pointerId !== pointerId) return;
    // 阈值必须在**屏幕位移**上判定：低缩放下（zoom 0.25 → 系数 4）1px 抖动
    // 换算成 4 个板面单位就会越过 2px 阈值，变成「什么都没拖却改了尺寸」。
    lastScreenDx = Number(event.clientX ?? 0) - originX;
    lastScreenDy = Number(event.clientY ?? 0) - originY;
    if (!started) {
      if (Math.abs(lastScreenDx) < RESIZE_DRAG_THRESHOLD && Math.abs(lastScreenDy) < RESIZE_DRAG_THRESHOLD) {
        return;
      }
      started = true;
      active.value = true;
    }
    const scale = finiteScale(options.scale?.());
    latest = resizeBox(snapshot, handle, lastScreenDx * scale, lastScreenDy * scale, options.limits());
    options.onPreview(latest);
  }

  function onPointerUp(event: PointerLike | null | undefined): void {
    if (pointerId !== null && typeof event?.pointerId === "number" && event.pointerId !== pointerId) return;
    const base = started ? snapshot : null;
    const startedHandle = handle;
    // reset() 会把 latest 置空，所以**先**把它取出来 —— 否则下面的差异守卫恒真，
    // 变成「每次松手都无条件多推一次 onPreview」（E 复核的 R4）。
    const lastPreview = latest;
    // 松手时按**当下**的限制再算一次：拖动期间窗口可能被缩小，直接交最后一次预览
    // 会把一个此刻已经超界的框提交出去（E 复核的 W5）。
    // scale 只取一次：非纯实现两次调用可能给出不同系数，x / y 会各用一套。
    const scale = finiteScale(options.scale?.());
    const commit =
      base && startedHandle
        ? resizeBox(base, startedHandle, lastScreenDx * scale, lastScreenDy * scale, options.limits())
        : null;
    reset();
    if (!commit || !base) return;
    // 拖动又回到起点：数值没变就不提交（不产生一次无意义的板面保存）。
    // 容差取 0.01 而不是 boxEquals 默认的 0.5：0.5 会把「低缩放下的半像素位移」也当成没动，
    // 于是 2px 屏幕位移在 zoom 0.25 下变成一次都不提交（E 用例 F2 的分界矩阵）。
    if (boxEquals(commit, base, NOOP_EPSILON)) return;
    // 复算结果与最后一次预览不同（拖动期间限制变了）时，先把最终框推给显示层再提交，
    // 否则用户会看到「松手之后自己跳一下」（E 复核的 R2：预览 420 → 提交 640）。
    if (!lastPreview || !boxEquals(commit, lastPreview, NOOP_EPSILON)) options.onPreview(commit);
    options.onCommit(commit, "pointer");
  }

  function onPointerCancel(event: PointerLike | null | undefined): void {
    // 别的手指（另一个 pointerId）的 cancel 不能回滚**这次**拖动。
    if (pointerId !== null && typeof event?.pointerId === "number" && event.pointerId !== pointerId) return;
    cancel();
  }

  function onKeyDown(event: { key?: string } | null | undefined): void {
    if (event?.key !== "Escape") return;
    cancel();
  }

  function onBlur(): void {
    cancel();
  }

  /** 键盘 −/＋：一次一步，直接提交（没有拖动过程，不需要 preview）。 */
  function nudge(axis: "w" | "h", direction: 1 | -1): void {
    const next = stepBox(options.box(), axis, direction, options.limits(), options.step ?? RESIZE_STEP);
    options.onCommit(next, "keyboard");
  }

  /** 供拖动中的容器加 `.resizing` 类：拖动期间必须关掉尺寸/位置过渡。 */
  function previewing(): boolean {
    return active.value;
  }

  onScopeDispose(() => {
    // 组件卸载时还在拖动：先恢复，再清理监听。
    cancel();
  });

  return { active: active as Ref<boolean>, onHandleDown, nudge, cancel, previewing };
}
