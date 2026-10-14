/**
 * 展示窗口 → 融合环锚点：**只有真的有话题的槽位才是锚点**。
 *
 * 2026-10-14 修：环的锚点原来按「窗口槽位」生成，空槽（没有圆点、没有标签）也带权重 1 参与融合，
 * 于是在球面上画出一圈圈「里面什么都没有」的环 —— 用户报告「星球这里多了三圈圆环」。
 * 空槽现在用与「未使用 uniform 槽」同一个哨兵权重（-100）中和：场项变成 ang + 100·rL，
 * 远大于三层环半径（0.14 / 0.22 / 0.34），任何方向都不会与环层相交。
 */
import { describe, expect, it } from "vitest";
import * as THREE from "three";
import { EMPTY_SLOT_WEIGHT, LEVEL_R, ringAnchorsForSlots } from "../topicData";
import { levelFieldDir } from "../ringField";
import type { TopicData } from "../topicData";

const K = 0.16;
/** levelFieldDir 收的是 {x,y,z,w}；这里把锚点转成它的入参形状。 */
const dirs = (anchors: TopicData[]) =>
  anchors.map((a) => ({ x: a.pos.x, y: a.pos.y, z: a.pos.z, w: a.w }));
const up = new THREE.Vector3(0, 1, 0);
const right = new THREE.Vector3(1, 0, 0);

/** 与 up 成 ang 夹角的方向（up 是 (0,1,0)，所以绕 z 轴转即可）。 */
function atAngle(ang: number): THREE.Vector3 {
  return new THREE.Vector3(Math.sin(ang), Math.cos(ang), 0).normalize();
}

describe("融合环锚点：空槽不画环", () => {
  it("空槽权重被中和、有话题的槽位保持 1；下标仍然对应槽位", () => {
    const anchors = ringAnchorsForSlots([up, right], [false, true]);
    expect(anchors.map((a) => a.id)).toEqual(["slot-0", "slot-1"]);
    expect(anchors[0].w).toBe(EMPTY_SLOT_WEIGHT);
    expect(anchors[1].w).toBe(1);
    expect(anchors[0].pos.equals(up)).toBe(true);
    expect(anchors[1].pos.equals(right)).toBe(true);
  });

  it("环所在的方向上：有话题时正好落在环上，空槽中和后远离所有环", () => {
    const occupied = ringAnchorsForSlots([up], [true]);
    const empty = ringAnchorsForSlots([up], [false]);
    for (const rL of LEVEL_R) {
      const p = atAngle(rL); // 与锚点成 rL 夹角 = 该层环的等高线
      expect(Math.abs(levelFieldDir(p, dirs(occupied), rL, K)), "有话题时应当正好在环上").toBeLessThan(1e-9);
      expect(Math.abs(levelFieldDir(p, dirs(empty), rL, K)), "空槽不该画出环").toBeGreaterThan(1);
    }
  });

  it("中和的空槽不会把真实话题的环推开", () => {
    const withEmpty = ringAnchorsForSlots([up, right], [false, true]);
    const withoutEmpty = ringAnchorsForSlots([right], [true]);
    for (const rL of LEVEL_R) {
      for (const probe of [up, right, atAngle(0.5), new THREE.Vector3(0, 0, 1)]) {
        const a = levelFieldDir(probe, dirs(withEmpty), rL, K);
        const b = levelFieldDir(probe, dirs(withoutEmpty), rL, K);
        expect(Math.abs(a - b), "空槽影响了真实话题的场").toBeLessThan(1e-9);
      }
    }
  });
});
