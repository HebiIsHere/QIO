// PLACEHOLDER(mp-a)：真实实现在 C 分支，接口以 _MP-COMMON §4.1 为准；集成时由 Lead 以 C 版本覆盖本文件。
/**
 * 管理区展开/收起的进度（薄占位）。
 *
 * 契约（_MP-COMMON §4.1）：这个 composable 负责在 rootRef 上写 `--mp`（0..1，3 位小数）、
 * `data-mp`，并在 progress > 0 时加类 `managing`。A 的样式只消费 `--mp` 与 `.managing`，
 * 不自己再造一套进度控制。
 *
 * 占位实现只做「进度 + 落变量 + 落类」，**不碰球体旋转的视觉**：
 * yaw 按 progress 线性映射后交给 `planet.setManageYaw`（若场景提供了这个方法）；
 * 真实的旋转编排、缓动曲线与打断衔接由 C 在场景内实现。
 *
 * 减少动态效果（`prefers-reduced-motion` 或 `html[data-motion="reduced"]`）时直接落到目标，
 * 与页面其它动效的降级口径一致。
 */
import { onScopeDispose, ref, type Ref } from "vue";

export interface PlanetManageTransition {
  /** 当前进度 0（中央浏览）→ 1（管理展开），每帧更新 */
  progress: Ref<number>;
  /** 目标状态（用户点的是展开还是收起），与 progress 分开 */
  expanded: Ref<boolean>;
  /** 切换；占位实现从当前进度继续走，不重置 */
  setExpanded(next: boolean): void;
  /** 同步落到某个进度（减少动画 / 初始布局用），不播放动画 */
  applyProgress(p: number): void;
}

/** 占位时长：真实实现由 C 按 `--mo-*` 令牌给出（这里只保证能动、能量） */
const PLACEHOLDER_MS = 260;

function clamp01(v: number): number {
  if (!Number.isFinite(v)) return 0;
  return Math.max(0, Math.min(1, v));
}

/** 与页面同一份减少动画判定（不新开口径） */
export function reduceMotion(): boolean {
  if (typeof document === "undefined") return false;
  const attr = document.documentElement.getAttribute("data-motion");
  if (attr === "reduced") return true;
  if (attr === "standard") return false;
  return typeof window !== "undefined" && Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
}

/** C 的接口签名（yaw 由场景消费）：占位实现里场景可能还没加这个方法，因此先接受任意对象 */
export type ManageYawTarget = { setManageYaw(radians: number): void };

export function usePlanetManageTransition(opts: {
  rootRef: Ref<HTMLElement | null>;
  planet: object;
}): PlanetManageTransition {
  const yawTarget = opts.planet as Partial<ManageYawTarget>;
  const progress = ref(0);
  const expanded = ref(false);
  let raf = 0;
  let from = 0;
  let target = 0;
  let startedAt = 0;

  function paint(value: number) {
    const p = clamp01(value);
    progress.value = p;
    const root = opts.rootRef.value;
    if (root) {
      root.style.setProperty("--mp", p.toFixed(3));
      root.setAttribute("data-mp", p.toFixed(3));
      root.classList.toggle("managing", p > 0);
    }
    // 占位：yaw 线性跟随进度（真实实现在 C：独立的 yaw 组，含缓动与打断衔接）
    yawTarget.setManageYaw?.(p * (Math.PI / 2));
  }

  function stop() {
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
  }

  function run() {
    stop();
    if (typeof requestAnimationFrame === "undefined") {
      paint(target);
      return;
    }
    const duration = reduceMotion() ? 0 : PLACEHOLDER_MS;
    startedAt = 0;
    const step = (now: number) => {
      if (!startedAt) startedAt = now;
      const t = duration <= 0 ? 1 : Math.min(1, (now - startedAt) / duration);
      // ease-out cubic：起步快、收束稳（与页面低频曲线同族）
      const eased = 1 - Math.pow(1 - t, 3);
      paint(from + (target - from) * eased);
      if (t < 1) raf = requestAnimationFrame(step);
      else {
        raf = 0;
        paint(target);
      }
    };
    raf = requestAnimationFrame(step);
  }

  function applyProgress(p: number) {
    stop();
    const next = clamp01(p);
    from = next;
    target = next;
    expanded.value = next > 0;
    paint(next);
  }

  function setExpanded(next: boolean) {
    expanded.value = next;
    from = progress.value;
    target = next ? 1 : 0;
    if (reduceMotion() || typeof requestAnimationFrame === "undefined") {
      stop();
      paint(target);
      return;
    }
    run();
  }

  // 初始布局：同步落到 0（不播放动画）
  applyProgress(0);
  onScopeDispose(stop);

  return { progress, expanded, setExpanded, applyProgress };
}
