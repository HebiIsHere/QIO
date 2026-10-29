/**
 * 记忆星球页「展开管理区」的过渡编排（契约见 _MP-COMMON.md §4.1，C 负责并拥有）。
 *
 * 职责边界（很关键，越界就会和 A 的布局、场景里的自转抢状态）：
 * - 本文件只做「进度」：每帧把 `--mp`（0..1，3 位小数）、`data-mp`、`.managing` 写到根元素上，
 *   并把进度换算成星球姿态组的 yaw 交给场景（`planet.setManageYaw`）；
 * - **不碰布局**：位移与管理区宽度由 A 的 CSS 消费 `--mp` 完成；
 * - **不碰 `planetGroup` 的四元数**：yaw 落在场景里独立的姿态组上，空闲自转与聚焦 slerp 照旧。
 *
 * 三条硬要求与实现位置：
 * 1. 非线性缓动取自 CSS 令牌（`--ease-3-settle` / `--ease-3-out`，时长取 `--mo-3-expand` /
 *    `--mo-3-collapse`）—— 见 `readSpec`，令牌读不到才退回与 tokens.css 一致的兜底值；
 * 2. 连续反向从中途位姿与**速度**衔接 —— 见 `planet/manageMotion.ts` 的 `planMotion`（Hermite 段），
 *    这里只负责把「中断那一刻的瞬时速度」传进去；
 * 3. 减少动态效果直接到位（一帧）；卸载/外部打断清理 rAF，不泄漏。
 */
import { onScopeDispose, ref, watch } from "vue";
import type { Ref } from "vue";
import { CSS_SETTLE_FALLBACK, easePointsFromCssValue } from "../utils/easing";
import type { EasePoints } from "../utils/easing";
import { currentMotionMode } from "../utils/motion";
import { planMotion, sampleSegment, segmentVelocity, yawForProgressRaw } from "../planet/manageMotion";
import type { ManageMotionSpec, MotionSegment } from "../planet/manageMotion";

export interface PlanetManageTransition {
  /** 当前进度 0（中央浏览）→ 1（管理展开），每帧更新 */
  progress: Ref<number>;
  /** 目标状态（用户点的是展开还是收起），与 progress 分开 */
  expanded: Ref<boolean>;
  /** 切换；中途反向从当前位姿与速度连续衔接，不重置朝向、不瞬移、不突然反转 */
  setExpanded(next: boolean): void;
  /** 同步落到某个进度（减少动画/初始布局用），不播放动画 */
  applyProgress(p: number): void;
}

export interface PlanetManageTransitionOptions {
  rootRef: Ref<HTMLElement | null>;
  planet: { setManageYaw(radians: number): void };
}

/** `--ease-3-out` 的兜底值（与 tokens.css 一致；令牌读不到时才用） */
const EASE_OUT_FALLBACK: EasePoints = [0.5, 0, 0.24, 1];
/** 令牌读不到时的时长兜底（ms），与 tokens.css 的 `--mo-3-*` 一致 */
const EXPAND_FALLBACK_MS = 520;
const COLLAPSE_FALLBACK_MS = 420;
/** 进度 > 该值时认为「在管理态」：加 `.managing` 类 */
const MANAGING_EPSILON = 0.0005;

function readVar(el: HTMLElement, name: string): string {
  try {
    return getComputedStyle(el).getPropertyValue(name).trim();
  } catch {
    return "";
  }
}

function readMs(el: HTMLElement, name: string, fallback: number): number {
  const raw = readVar(el, name);
  if (!raw) return fallback;
  const value = Number.parseFloat(raw);
  if (!Number.isFinite(value)) return fallback;
  const ms = raw.endsWith("ms") ? value : raw.endsWith("s") ? value * 1000 : value;
  return ms > 0 ? ms : fallback;
}

/**
 * 从根元素读过渡参数。
 *
 * 为什么读**根元素**而不是 `:root`：`?planetdemo=slow` 这类演示开关是把 `--mo-3-*`
 * 覆盖在 `.planet-view` 上的（A 的 PlanetView 里那套 demoStyle）。脚本与 CSS 必须读同一份，
 * 否则慢放演示只慢 CSS、脚本照原速推进 —— 逐帧观察到的就是两条不同的时间线。
 *
 * 另支持 `--mp-speed`（无单位倍数）作为「只放慢管理过渡」的开关：不给就是 1。
 */
function readSpec(el: HTMLElement): ManageMotionSpec {
  const settle = easePointsFromCssValue(readVar(el, "--ease-3-settle"), CSS_SETTLE_FALLBACK);
  const out = easePointsFromCssValue(readVar(el, "--ease-3-out"), EASE_OUT_FALLBACK);
  const speedRaw = Number.parseFloat(readVar(el, "--mp-speed"));
  const speed = Number.isFinite(speedRaw) && speedRaw > 0 ? Math.min(50, speedRaw) : 1;
  return {
    expand: { points: settle, durationMs: readMs(el, "--mo-3-expand", EXPAND_FALLBACK_MS) * speed },
    collapse: { points: out, durationMs: readMs(el, "--mo-3-collapse", COLLAPSE_FALLBACK_MS) * speed },
    minDurationMs: 90 * speed,
  };
}

export function usePlanetManageTransition(options: PlanetManageTransitionOptions): PlanetManageTransition {
  const progress = ref(0);
  const expanded = ref(false);

  /** 当前段（没有在跑时为 null） */
  let segment: MotionSegment | null = null;
  let startedAt = 0;
  /** 中断那一刻的瞬时速度（进度/秒），反向衔接要用 */
  let velocity = 0;
  let raf = 0;
  let disposed = false;

  const prefersReduced = (): boolean =>
    currentMotionMode() === "reduced";

  function cancelFrame(): void {
    if (raf) {
      cancelAnimationFrame(raf);
      raf = 0;
    }
    segment = null;
    velocity = 0;
  }

  /** 只写 DOM（不改变进度值）：--mp / data-mp / .managing。 */
  function writeDom(value: number): void {
    const el = options.rootRef.value;
    if (!el) return;
    const text = value.toFixed(3);
    el.style.setProperty("--mp", text);
    el.setAttribute("data-mp", text);
    if (value > MANAGING_EPSILON) el.classList.add("managing");
    else el.classList.remove("managing");
  }

  /** 进度与姿态一起落到当前值（也用于每一帧）。raw 允许一点点过冲，只给 yaw 用。 */
  function commit(value: number, raw = value): void {
    const clamped = value < 0 ? 0 : value > 1 ? 1 : value;
    progress.value = clamped;
    writeDom(clamped);
    options.planet.setManageYaw(yawForProgressRaw(raw));
  }

  function finish(target: number): void {
    cancelFrame();
    commit(target, target);
    expanded.value = target >= 0.5;
  }

  function tick(now: number): void {
    raf = 0;
    if (disposed) return;
    const current = segment;
    if (!current) return;
    // 运行中被切成「减少动画」：立刻落到目标，不留半截状态
    if (prefersReduced()) {
      finish(current.to);
      return;
    }
    const elapsed = now - startedAt;
    const value = sampleSegment(current, elapsed);
    velocity = segmentVelocity(current, elapsed);
    commit(value, value);
    if (elapsed >= current.durationMs) {
      finish(current.to);
      return;
    }
    raf = requestAnimationFrame(tick);
  }

  function applyProgress(p: number): void {
    const clamped = Number.isFinite(p) ? (p < 0 ? 0 : p > 1 ? 1 : p) : 0;
    cancelFrame();
    commit(clamped, clamped);
    expanded.value = clamped >= 0.5;
  }

  function setExpanded(next: boolean): void {
    const target = next ? 1 : 0;
    if (disposed) return;
    expanded.value = next;
    const el = options.rootRef.value;
    // 已经在目标且没有动画在跑：什么都不用做（重复点同一个按钮不是「重播动画」）
    if (!segment && Math.abs(progress.value - target) < 1e-4) return;
    if (prefersReduced() || !el) {
      finish(target);
      return;
    }
    // 记下中断那一刻的速度：反向衔接要靠它保证「速度连续、不突然反转」
    const carried = segment ? velocity : 0;
    const planned = planMotion({
      current: progress.value,
      target,
      velocity: carried,
      spec: readSpec(el),
    });
    cancelFrame();
    if (planned.durationMs <= 0) {
      finish(target);
      return;
    }
    segment = planned;
    startedAt = performance.now();
    raf = requestAnimationFrame(tick);
  }

  // 根元素可能晚于 composable 就绪（v-if / 转场挂载）：一到 DOM 上就把当前进度补写回去，
  // 否则第一帧的 --mp 是空的（A 的 CSS 会退回 0，看起来是「闪一下」）。
  watch(
    () => options.rootRef.value,
    (el) => {
      if (!el) return;
      writeDom(progress.value);
      options.planet.setManageYaw(yawForProgressRaw(progress.value));
    },
    { immediate: true },
  );

  onScopeDispose(() => {
    disposed = true;
    cancelFrame();
  });

  return { progress, expanded, setExpanded, applyProgress };
}
