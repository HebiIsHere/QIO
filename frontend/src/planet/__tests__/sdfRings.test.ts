import { describe, expect, it } from "vitest";
import { smin, levelField } from "../sdfRings";
import * as THREE from "three";
describe("sdfRings", () => {
  it("smin 在远距离取最小值", () => {
    expect(smin(0.1, 0.9, 0.16)).toBeCloseTo(0.1, 5);
  });
  it("smin 在近距离产生融合（小于两者）", () => {
    expect(smin(0.01, 0.01, 0.16)).toBeLessThan(0.01);
  });
  it("levelField：单话题在环上为 0，远离为正", () => {
    const topics = [{ pos: new THREE.Vector3(0, 0, 1), w: 1 }];
    const onRing = new THREE.Vector3(Math.sin(0.14), 0, Math.cos(0.14)).normalize();
    expect(Math.abs(levelField(onRing, topics, 0.14, 0.16))).toBeLessThan(1e-3);
    const far = new THREE.Vector3(0, 1, 0).normalize();
    expect(levelField(far, topics, 0.14, 0.16)).toBeGreaterThan(0.5);
  });
});
