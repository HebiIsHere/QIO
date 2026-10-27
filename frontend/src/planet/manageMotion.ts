/**
 * 管理展开过渡的纯计算：progress → 朝向映射、令牌曲线采样、**中途反向的连续衔接**。
 *
 * 为什么单独抽一个纯函数模块：
 * 1. 这些计算全是数学，不碰 DOM、不碰 three、不碰 rAF —— 可以逐条断言（见 __tests__）；
 * 2. 「中途反向不瞬移、不突然反转」这条要求最难用肉眼验收，只能靠对曲线与速度的断言证明；
 * 3. 曲线一律来自 CSS 令牌（`easePointsFromCssValue` 采样 `--ease-3-settle` / `--ease-3-out`），
 *    这里只负责「怎么用」，不硬编码任何贝塞尔。
 */
import type { EasePoints } from "../utils/easing";
import { evalCubicBezier } from "../utils/easing";

/**
 * 管理展开时星球绕姿态轴的旋转总额（弧度 ≈ 35.5°）。
 *
 * 取值依据（在 1440×900、聚焦态、画布 800×830 的几何下算过）：
 * - 默认浏览态里，「当前讨论话题」在球体正前方（屏幕 x = 内容区中线）；
 * - 展开后球心落到内容区左边缘（x=0），正前方的点会正好压在裁切边界上、被切掉一半；
 * - 绕 Y 轴转 0.62 rad 后，该点（世界方向 (sinθ, 0, cosθ)）投影到画布中心右侧约 0.33×画布宽处，
 *   即落在**可见右半球的中部**：既不会贴边被切，也不会被右侧管理区盖住。
 * - 再大（>0.75 rad）会让原正前方内容压到右边缘；再小（<0.5 rad）则「看起来没转」。
 */
export const MANAGE_YAW = 0.62;

/** 反向衔接时进度速度的上限（进度/秒）：避免极端情况下 Hermite 段过冲太多 */
const MAX_SEGMENT_VELOCITY = 6;

/** 一段动画：从 from 到 to，时长 durationMs，起点进度速度 v0（进度/秒）。 */
export interface MotionSegment {
  from: number;
  to: number;
  /** 起点速度（进度/秒）。0 = 从静止开始（走令牌曲线）；非 0 = 反向衔接（走三次 Hermite） */
  v0: number;
  durationMs: number;
  /** 令牌曲线（起点速度为零时使用） */
  points: EasePoints;
  /** 该段开始的时刻（performance.now()；0 表示尚未开始） */
  startedAt: number;
}

export interface ManageMotionSpec {
  /** 展开（0→1）：--ease-3-settle + --mo-3-expand */
  expand: { points: EasePoints; durationMs: number };
  /** 收起（1→0）：--ease-3-out + --mo-3-collapse */
  collapse: { points: EasePoints; durationMs: number };
  /** 反向衔接的最短时长（ms）：太短会让反向看起来像「卡了一下」 */
  minDurationMs?: number;
}

const clamp01 = (value: number): number => (value < 0 ? 0 : value > 1 ? 1 : value);
const finite = (value: number | undefined, fallback: number): number =>
  Number.isFinite(value as number) ? (value as number) : fallback;

/** progress → 姿态组的 yaw。progress 可以是 0..1（返回 0..MANAGE_YAW）。 */
export function yawForProgress(progress: number): number {
  return MANAGE_YAW * clamp01(finite(progress, 0));
}

/** 反向时允许的进度外溢（比分栏更细）：yaw 允许一点点过冲，才有「有生命感的收束」 */
export function yawForProgressRaw(progress: number): number {
  return MANAGE_YAW * finite(progress, 0);
}

/** 三次 Hermite：p0 → p1，起点速度 m0（已换算到 s 空间），终点速度 0。 */
function hermite(p0: number, p1: number, m0: number, s: number): number {
  const s2 = s * s;
  const s3 = s2 * s;
  const h00 = 2 * s3 - 3 * s2 + 1;
  const h10 = s3 - 2 * s2 + s;
  const h01 = -2 * s3 + 3 * s2;
  return h00 * p0 + h10 * m0 + h01 * p1;
}

function hermiteVelocity(p0: number, p1: number, m0: number, s: number): number {
  const s2 = s * s;
  const d00 = 6 * s2 - 6 * s;
  const d10 = 3 * s2 - 4 * s + 1;
  const d01 = -6 * s2 + 6 * s;
  return d00 * p0 + d10 * m0 + d01 * p1;
}

/** 采样某段动画在 elapsedMs 时刻的进度值（不做 0..1 夹取：反向过冲要留出来）。 */
export function sampleSegment(segment: MotionSegment, elapsedMs: number): number {
  const duration = Math.max(1e-6, segment.durationMs);
  if (elapsedMs <= 0) return segment.from;
  if (elapsedMs >= duration) return segment.to;
  const s = elapsedMs / duration;
  if (Math.abs(segment.v0) < 1e-9) {
    return segment.from + (segment.to - segment.from) * evalCubicBezier(segment.points, s);
  }
  // 起点速度非零：用三次 Hermite 保证「速度连续」（贝塞尔曲线的起点斜率是固定的，
  // 从半途反向时直接换段会在那一帧产生速度跳变 —— 看起来就是「突然反转」）。
  const m0 = segment.v0 * (duration / 1000);
  return hermite(segment.from, segment.to, m0, s);
}

/** 某段动画在 elapsedMs 时刻的进度速度（进度/秒）。 */
export function segmentVelocity(segment: MotionSegment, elapsedMs: number): number {
  const duration = Math.max(1e-6, segment.durationMs);
  const t = Math.max(0, elapsedMs);
  if (t >= duration) return 0;
  if (Math.abs(segment.v0) < 1e-9) {
    // 令牌曲线段的瞬时速度**由曲线自己决定**：起点并不慢（settle 是快速起步），
    // 所以这里用前向差分取曲线导数，而不是返回 v0（=0）。
    const dt = Math.min(8, Math.max(1, duration / 16));
    const a = sampleSegment(segment, t);
    const b = sampleSegment(segment, Math.min(duration, t + dt));
    const v = (b - a) / (dt / 1000);
    return Number.isFinite(v) ? v : 0;
  }
  const m0 = segment.v0 * (duration / 1000);
  const value = hermiteVelocity(segment.from, segment.to, m0, t / duration);
  // 换算回「进度/秒」：d(progress)/dt = d(value)/ds / duration(s)
  return value / (duration / 1000);
}

export interface PlanMotionOptions {
  /** 当前进度（0..1，可能含一点点过冲） */
  current: number;
  /** 目标进度（0 或 1） */
  target: number;
  /** 当前进度速度（进度/秒）：中断那一刻的瞬时速度，用于连续衔接 */
  velocity?: number;
  spec: ManageMotionSpec;
  /** 减少动画：直接落到目标（duration = 0） */
  instant?: boolean;
}

/**
 * 规划下一段动画。
 *
 * - **从静止开始**（velocity ≈ 0）：时长按距离比例取令牌时长，曲线用令牌曲线本身。
 * - **半途反向**（速度方向与新目标相反）：时长同样按剩余距离比例，但起点速度照搬，
 *   用三次 Hermite 让「速度连续、终点速度归零」—— 表现为「先顺惯性收住，再在新方向加速」，
 *   不会瞬移、不会在那一帧突然反号。
 * - **减少动画**：duration = 0，一帧落到目标。
 */
export function planMotion(options: PlanMotionOptions): MotionSegment {
  const spec = options.spec;
  const current = finite(options.current, 0);
  const target = clamp01(finite(options.target, 0));
  const distance = Math.abs(target - current);
  const base = target >= current ? spec.expand : spec.collapse;
  const points = base.points;
  const minDuration = Math.max(0, spec.minDurationMs ?? 90);
  if (options.instant || distance < 1e-4) {
    return { from: target, to: target, v0: 0, durationMs: 0, points, startedAt: 0 };
  }
  const rawVelocity = finite(options.velocity, 0);
  // 速度与「还差的方向」相反 → 这是反向衔接；同向（例如目标变远）也照搬速度，同样平滑
  const reversing = Math.abs(rawVelocity) > 0.05;
  const velocity = reversing ? Math.max(-MAX_SEGMENT_VELOCITY, Math.min(MAX_SEGMENT_VELOCITY, rawVelocity)) : 0;
  // 时长按**剩余距离**线性缩放（与「被打断的 CSS 过渡」同一口径），下限保证末段不闪。
  // 反向衔接再放宽 10%：同一段距离里要先把旧方向的惯性收住，再往新方向走。
  const scaled = base.durationMs * clamp01(distance);
  const durationMs = Math.round(Math.max(minDuration, reversing ? scaled * 1.1 : scaled));
  return { from: current, to: target, v0: velocity, durationMs, points, startedAt: 0 };
}

/**
 * 段超出 [from, to] 区间的最大偏移（「惯性过冲」）。
 *
 * 注意不能拿「离 to 最远的值」当过冲：from 本身就离 to 很远，那样量到的是整段距离。
 * 这里量的是**冲过目标/起点之外**的那一点点，用来判断反向衔接的惯性有没有失控。
 */
export function segmentOvershoot(segment: MotionSegment, samples = 240): number {
  if (segment.durationMs <= 0) return 0;
  const lo = Math.min(segment.from, segment.to);
  const hi = Math.max(segment.from, segment.to);
  let over = 0;
  for (let i = 0; i <= samples; i += 1) {
    const value = sampleSegment(segment, (i / samples) * segment.durationMs);
    if (value > hi) over = Math.max(over, value - hi);
    if (value < lo) over = Math.max(over, lo - value);
  }
  return Number(over.toFixed(4));
}
