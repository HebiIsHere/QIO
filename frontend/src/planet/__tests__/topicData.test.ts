import { describe, expect, it } from "vitest";
import * as THREE from "three";
import { buildTopics, MIN_SEP, LEVEL_R, LEVEL_A, LEVEL_HW } from "../topicData";

const clusters = [
  { center: new THREE.Vector3(0.25, 0.3, 1).normalize(), n: 4 },
  { center: new THREE.Vector3(1, -0.15, -0.15).normalize(), n: 3 },
  { center: new THREE.Vector3(-0.85, -0.55, 0.35).normalize(), n: 2 },
];

describe("topicData", () => {
  it("常量锁死设计规格", () => {
    expect(LEVEL_R).toEqual([0.14, 0.22, 0.34]);
    expect(LEVEL_A).toEqual([0.62, 0.44, 0.28]);
    expect(LEVEL_HW).toEqual([5.0, 3.25, 2.0]);
    expect(MIN_SEP).toBe(0.095);
  });

  it("buildTopics 总数等于各簇数量之和，id/权重/单位向量合法", () => {
    const topics = buildTopics(clusters);
    expect(topics).toHaveLength(9);
    let idx = 0;
    clusters.forEach((cl, ci) => {
      for (let k = 0; k < cl.n; k++) {
        const t = topics[idx++];
        expect(t.id).toBe(`${ci}-${k}`);
        expect(t.ci).toBe(ci);
        expect(t.pos.length()).toBeCloseTo(1, 5);
        expect(t.w).toBeGreaterThanOrEqual(0.85);
        expect(t.w).toBeLessThan(1.3);
      }
    });
  });

  it("同簇话题满足最小角间距 MIN_SEP", () => {
    const topics = buildTopics(clusters);
    for (let i = 0; i < topics.length; i++) {
      for (let j = i + 1; j < topics.length; j++) {
        if (topics[i].ci !== topics[j].ci) continue;
        const ang = Math.acos(Math.max(-1, Math.min(1, topics[i].pos.dot(topics[j].pos))));
        expect(ang).toBeGreaterThanOrEqual(MIN_SEP - 1e-6);
      }
    }
  });

  it("固定种子：同一输入生成结果可复现", () => {
    const fmt = (ts: { id: string; pos: THREE.Vector3; w: number }[]) =>
      ts.map((t) => `${t.id}:${t.pos.toArray().map((x) => x.toFixed(6)).join(",")}:${t.w.toFixed(6)}`);
    expect(fmt(buildTopics(clusters))).toEqual(fmt(buildTopics(clusters)));
  });

  it("单话题簇回退到簇中心（单位向量）", () => {
    const topics = buildTopics([{ center: new THREE.Vector3(0, 0, 1), n: 1 }]);
    expect(topics).toHaveLength(1);
    expect(topics[0].pos.length()).toBeCloseTo(1, 5);
  });
});