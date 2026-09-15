/**
 * 临时球面布局槽位（spec 第 14~17 条）。
 *
 * 话题不再拥有永久球面坐标：位置只属于「当前展示窗口」，由话题编号 + 浏览会话
 * 生成稳定种子后确定性抖动。要求：不重叠、不挤在极区、同一 seed 完全一致
 * （否则刷新一次话题就会跳位）、不同 seed 有差异（否则像数学采样）。
 */
import { describe, expect, it } from "vitest";
import * as THREE from "three";

import { backSlotOrder, randomBackPosition, slotPositions, MIN_SEP } from "../layoutSlots";

function angleBetween(a: THREE.Vector3, b: THREE.Vector3): number {
  return Math.acos(Math.max(-1, Math.min(1, a.clone().normalize().dot(b.clone().normalize()))));
}

describe("slotPositions", () => {
  it("同一 seed 完全一致，不同 seed 有差异", () => {
    const a = slotPositions(12, 4242).map((p) => p.toArray());
    const b = slotPositions(12, 4242).map((p) => p.toArray());
    const c = slotPositions(12, 777).map((p) => p.toArray());

    expect(a).toEqual(b);
    expect(a).not.toEqual(c);
  });

  it("所有槽位都在单位球面上", () => {
    for (const p of slotPositions(12, 1)) {
      expect(p.length()).toBeCloseTo(1, 5);
    }
  });

  it("槽位之间不重叠", () => {
    const positions = slotPositions(12, 99);
    for (let i = 0; i < positions.length; i++) {
      for (let j = i + 1; j < positions.length; j++) {
        expect(angleBetween(positions[i], positions[j])).toBeGreaterThanOrEqual(0.18);
      }
    }
  });

  it("满容量（16）时同样不重叠", () => {
    const positions = slotPositions(16, 31);
    for (let i = 0; i < positions.length; i++) {
      for (let j = i + 1; j < positions.length; j++) {
        expect(angleBetween(positions[i], positions[j])).toBeGreaterThanOrEqual(0.18);
      }
    }
  });

  it("不集中在极区，也不机械平均", () => {
    const positions = slotPositions(12, 7);
    const polars = positions.map((p) => Math.acos(Math.max(-1, Math.min(1, p.y))));

    for (const polar of polars) {
      expect(polar).toBeGreaterThan(THREE.MathUtils.degToRad(50));
      expect(polar).toBeLessThan(THREE.MathUtils.degToRad(130));
    }
    const gaps = polars.map((p, i) => (i === 0 ? 0 : p - polars[i - 1]));
    expect(Math.max(...gaps.slice(1)) - Math.min(...gaps.slice(1))).toBeGreaterThan(0.01);
  });
});

describe("backSlotOrder", () => {
  it("把最背面的槽位排在前面", () => {
    const positions = slotPositions(4, 5);
    const cameraDir = new THREE.Vector3(0, 0, 1);
    // 造 4 个方向：正对相机、右侧、正背面、左侧
    const dirs = [
      new THREE.Vector3(0, 0, 1),
      new THREE.Vector3(1, 0, 0),
      new THREE.Vector3(0, 0, -1),
      new THREE.Vector3(-1, 0, 0),
    ];

    const order = backSlotOrder(dirs, cameraDir);

    expect(order[0]).toBe(2);
    expect(order[order.length - 1]).toBe(0);
    expect(order).toHaveLength(positions.length);
  });
});

describe("randomBackPosition", () => {
  const cameraDir = new THREE.Vector3(0, 0, 1);
  const rng = (seed: number) => {
    let s = seed >>> 0;
    return () => {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s / 4294967296;
    };
  };

  it("新话题只落在球体背面（用户此刻看不见的那一半）", () => {
    const occupied = slotPositions(8, 3);
    for (let i = 0; i < 40; i++) {
      const dir = randomBackPosition(occupied, cameraDir, rng(i));
      expect(dir.length()).toBeCloseTo(1, 5);
      expect(dir.dot(cameraDir)).toBeLessThan(0);
    }
  });

  it("与窗口里已有的话题保持最小角间距", () => {
    const occupied = slotPositions(12, 5);
    for (let i = 0; i < 20; i++) {
      const dir = randomBackPosition(occupied, cameraDir, rng(100 + i));
      for (const other of occupied) {
        expect(angleBetween(dir, other)).toBeGreaterThanOrEqual(MIN_SEP - 1e-6);
      }
    }
  });

  it("同一个随机数序列结果一致，不同序列有差异", () => {
    const occupied = slotPositions(6, 9);
    const a = randomBackPosition(occupied, cameraDir, rng(7)).toArray();
    const b = randomBackPosition(occupied, cameraDir, rng(7)).toArray();
    const c = randomBackPosition(occupied, cameraDir, rng(8)).toArray();

    expect(a).toEqual(b);
    expect(a).not.toEqual(c);
  });

  it("球面被占得很满时仍然返回一个背面位置（不会死循环）", () => {
    const occupied: THREE.Vector3[] = [];
    for (let i = 0; i < 60; i++) {
      const dir = randomBackPosition(occupied, cameraDir, rng(i));
      occupied.push(dir);
    }
    const last = randomBackPosition(occupied, cameraDir, rng(999));

    expect(last.dot(cameraDir)).toBeLessThan(0);
  });
});
