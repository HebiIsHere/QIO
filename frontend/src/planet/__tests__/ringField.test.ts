/**
 * 融合环距离场（无依赖版本）。
 *
 * 这个模块存在的唯一理由是：入口小球（对话页首屏，不能引入 three.js）与全屏 Planet
 * （GPU shader）必须用**同一套等值线定义**。所以这里守两件事：
 * 1) 数学与 sdfRings / shader 一致（smin 常数锁死，环上为 0）；
 * 2) 锚点分布是确定性的（同一个球每次打开都长一样，才像「同一个对象」）。
 */
import { describe, expect, it } from "vitest";
import { levelFieldDir, smin, spreadAnchors } from "../ringField";
import { smin as sminThree } from "../sdfRings";
import { LEVEL_R } from "../topicData";

describe("ringField 与 three 版数学一致", () => {
  it("smin 与 sdfRings 的实现完全一致", () => {
    const cases: [number, number, number][] = [
      [0.1, 0.9, 0.16],
      [0.01, 0.01, 0.16],
      [0.1, 0.1, 0.16],
      [0.34, 0.22, 0.16],
    ];
    for (const [a, b, k] of cases) expect(smin(a, b, k)).toBe(sminThree(a, b, k));
  });

  it("单锚点：环上距离为 0，远处为正", () => {
    const topics = [{ x: 0, y: 0, z: 1, w: 1 }];
    const ang = LEVEL_R[0];
    const onRing = { x: Math.sin(ang), y: 0, z: Math.cos(ang) };
    expect(Math.abs(levelFieldDir(onRing, topics, ang, 0.16))).toBeLessThan(1e-3);
    expect(levelFieldDir({ x: 0, y: 1, z: 0 }, topics, ang, 0.16)).toBeGreaterThan(0.5);
  });

  it("无锚点时返回哨兵（+∞ 语义，不返回 NaN）", () => {
    const f = levelFieldDir({ x: 0, y: 0, z: 1 }, [], LEVEL_R[0], 0.16);
    expect(f).toBeGreaterThan(1e8);
  });

  it("多锚点融合：融合后的距离不大于任一单独距离（smin 的语义）", () => {
    const a = { x: 0, y: 0, z: 1, w: 1 };
    const b = { x: 0.2, y: 0, z: 0.98, w: 1 };
    const p = { x: 0.1, y: 0, z: 0.995 };
    const norm = Math.hypot(p.x, p.y, p.z);
    const pn = { x: p.x / norm, y: p.y / norm, z: p.z / norm };
    const fa = levelFieldDir(pn, [a], LEVEL_R[0], 0.16);
    const fb = levelFieldDir(pn, [b], LEVEL_R[0], 0.16);
    const fused = levelFieldDir(pn, [a, b], LEVEL_R[0], 0.16);
    expect(fused).toBeLessThanOrEqual(Math.min(fa, fb) + 1e-9);
  });
});

describe("spreadAnchors 确定性", () => {
  it("同一 seed 得到完全相同的方向与权重", () => {
    const a = spreadAnchors(11, 7);
    const b = spreadAnchors(11, 7);
    expect(a).toEqual(b);
    expect(a).toHaveLength(11);
  });

  it("锚点是单位向量（否则角距离不是夹角）", () => {
    for (const t of spreadAnchors(11, 7)) {
      expect(Math.hypot(t.x, t.y, t.z)).toBeCloseTo(1, 6);
      expect(t.w).toBeGreaterThan(0.8);
    }
  });

  it("不同 seed 得到不同的球（但不影响同一会话内的可复现性）", () => {
    expect(spreadAnchors(11, 7)).not.toEqual(spreadAnchors(11, 8));
  });
});
