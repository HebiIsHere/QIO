import { describe, expect, it } from "vitest";
import { CSS_SETTLE_FALLBACK, evalCubicBezier } from "../../utils/easing";
import {
  MANAGE_YAW,
  planMotion,
  sampleSegment,
  segmentOvershoot,
  segmentVelocity,
  yawForProgress,
} from "../manageMotion";

/** --ease-3-out（tokens.css: cubic-bezier(.5,0,.24,1)） */
const OUT: [number, number, number, number] = [0.5, 0, 0.24, 1];
const SPEC = {
  expand: { points: CSS_SETTLE_FALLBACK, durationMs: 520 },
  collapse: { points: OUT, durationMs: 420 },
  minDurationMs: 90,
};

describe("yawForProgress", () => {
  it("0 → 0，1 → MANAGE_YAW，且单调", () => {
    expect(yawForProgress(0)).toBe(0);
    expect(yawForProgress(1)).toBeCloseTo(MANAGE_YAW, 6);
    let last = -Infinity;
    for (let i = 0; i <= 20; i += 1) {
      const yaw = yawForProgress(i / 20);
      expect(yaw).toBeGreaterThanOrEqual(last);
      last = yaw;
    }
  });

  it("落在 0.55–0.70 rad 的验收区间（Lead 定：原正前方内容应落到右半球中部）", () => {
    expect(MANAGE_YAW).toBeGreaterThanOrEqual(0.55);
    expect(MANAGE_YAW).toBeLessThanOrEqual(0.7);
  });

  it("越界输入被夹取，非数字退化为 0", () => {
    expect(yawForProgress(-3)).toBe(0);
    expect(yawForProgress(9)).toBeCloseTo(MANAGE_YAW, 6);
    expect(yawForProgress(Number.NaN)).toBe(0);
  });
});

describe("sampleSegment（从静止开始：用令牌曲线）", () => {
  const segment = { from: 0, to: 1, v0: 0, durationMs: 520, points: CSS_SETTLE_FALLBACK, startedAt: 0 };

  it("端点精确、与令牌曲线逐点一致", () => {
    expect(sampleSegment(segment, 0)).toBe(0);
    expect(sampleSegment(segment, 520)).toBe(1);
    for (const x of [0.1, 0.25, 0.5, 0.75, 0.9]) {
      expect(sampleSegment(segment, 520 * x)).toBeCloseTo(evalCubicBezier(CSS_SETTLE_FALLBACK, x), 6);
    }
  });

  it("前 80% 单调递增；settle 曲线只在末段略微过冲（≤3%）后收束到 1", () => {
    let last = -Infinity;
    for (let i = 0; i <= 48; i += 1) {
      const value = sampleSegment(segment, (i / 60) * 520);
      expect(value).toBeGreaterThanOrEqual(last);
      last = value;
    }
    let max = 0;
    for (let i = 0; i <= 120; i += 1) max = Math.max(max, sampleSegment(segment, (i / 120) * 520));
    expect(max).toBeGreaterThan(1);
    expect(max).toBeLessThan(1.04);
    expect(sampleSegment(segment, 520)).toBe(1);
  });

  it("起点速度不为 0（令牌曲线是快速起步、柔性收束），终点速度为 0", () => {
    expect(segmentVelocity(segment, 0)).toBeGreaterThan(1);
    expect(Math.abs(segmentVelocity(segment, 520))).toBeLessThan(1e-6);
  });

  it("时长非法时不炸（0 时长直接返回终点）", () => {
    expect(sampleSegment({ ...segment, durationMs: 0 }, 10)).toBe(1);
    expect(segmentVelocity({ ...segment, durationMs: 0 }, 10)).toBe(0);
  });
});

describe("planMotion", () => {
  it("完整展开用 --mo-3-expand 的时长", () => {
    const segment = planMotion({ current: 0, target: 1, velocity: 0, spec: SPEC });
    expect(segment.from).toBe(0);
    expect(segment.to).toBe(1);
    expect(segment.v0).toBe(0);
    expect(segment.durationMs).toBe(SPEC.expand.durationMs);
  });

  it("完整收起用 --mo-3-collapse 的时长", () => {
    const segment = planMotion({ current: 1, target: 0, velocity: 0, spec: SPEC });
    expect(segment.durationMs).toBe(SPEC.collapse.durationMs);
    expect(segment.points).toEqual(OUT);
  });

  it("半程出发时长按距离缩短（但不少于下限）", () => {
    const half = planMotion({ current: 0.5, target: 1, velocity: 0, spec: SPEC });
    expect(half.durationMs).toBeLessThan(SPEC.expand.durationMs);
    expect(half.durationMs).toBeGreaterThanOrEqual(SPEC.minDurationMs);
    const tiny = planMotion({ current: 0.995, target: 1, velocity: 0, spec: SPEC });
    expect(tiny.durationMs).toBe(SPEC.minDurationMs);
  });

  it("减少动画：一帧到位（时长 0、from=to=目标）", () => {
    const segment = planMotion({ current: 0.42, target: 1, velocity: 3, spec: SPEC, instant: true });
    expect(segment.durationMs).toBe(0);
    expect(segment.from).toBe(1);
    expect(segment.to).toBe(1);
  });

  it("已经在目标：不产生空转动画", () => {
    const segment = planMotion({ current: 1, target: 1, velocity: 0, spec: SPEC });
    expect(segment.durationMs).toBe(0);
  });
});

describe("中途反向：速度连续、不瞬移、过冲有界", () => {
  // 展开到 0.5 附近（还在向 1 冲）时用户点了收起
  const current = 0.5;
  const velocity = segmentVelocity(
    { from: 0, to: 1, v0: 0, durationMs: 520, points: CSS_SETTLE_FALLBACK, startedAt: 0 },
    260,
  );
  const reversed = planMotion({ current, target: 0, velocity, spec: SPEC });

  it("起点就是当前进度（不瞬移）", () => {
    expect(sampleSegment(reversed, 0)).toBeCloseTo(current, 6);
    expect(reversed.to).toBe(0);
  });

  it("起点速度延续原方向的惯性（不突然反号）", () => {
    // 反向那一刻仍然在向 1 走：头几毫秒的位移必须为正
    expect(sampleSegment(reversed, 4)).toBeGreaterThan(current);
    expect(segmentVelocity(reversed, 0)).toBeCloseTo(velocity, 1);
    expect(segmentVelocity(reversed, 0)).toBeGreaterThan(0);
  });

  it("随后在 1/3 段内完成换向，终点精确落在 0 且速度为 0", () => {
    const switchAt = reversed.durationMs / 3;
    expect(sampleSegment(reversed, reversed.durationMs)).toBe(0);
    expect(segmentVelocity(reversed, reversed.durationMs)).toBe(0);
    // 换向之后必须已经开始往目标方向走
    expect(sampleSegment(reversed, switchAt)).toBeLessThan(current);
  });

  it("惯性过冲有界（≤ 0.06），不会冲出画面语义", () => {
    expect(Math.abs(segmentOvershoot(reversed))).toBeLessThan(0.06);
  });

  it("反向段逐帧连续（不跳变）：相邻采样差远小于 0.16 的瞬移阈值", () => {
    let maxStep = 0;
    let prev = sampleSegment(reversed, 0);
    for (let t = 8; t <= reversed.durationMs; t += 8) {
      const value = sampleSegment(reversed, t);
      maxStep = Math.max(maxStep, Math.abs(value - prev));
      prev = value;
    }
    expect(maxStep).toBeLessThan(0.16);
  });

  it("速度极大时被夹取（避免 Hermite 过冲失控）", () => {
    const wild = planMotion({ current: 0.5, target: 0, velocity: 99, spec: SPEC });
    expect(Math.abs(wild.v0)).toBeLessThanOrEqual(6);
    expect(Math.abs(segmentOvershoot(wild))).toBeLessThan(0.25);
  });
});
