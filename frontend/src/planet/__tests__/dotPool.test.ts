/**
 * 渲染对象池（spec 第 62~64 条）。
 *
 * 话题不断进出窗口，但 Three.js 对象必须固定数量：池子按可见容量一次建好，
 * 之后只改「这个对象现在代表谁」。回收 ≠ dispose，离开窗口只是隐藏。
 */
import { describe, expect, it, vi } from "vitest";
import * as THREE from "three";

import { DotPool } from "../dotPool";
import type { BrowseTopic } from "../browseSession";

function topic(n: number): BrowseTopic {
  return { topic_id: `t${n}`, title: `话题 ${n}`, fragment_count: n, last_activity: null };
}

function positions(capacity: number): THREE.Vector3[] {
  return Array.from({ length: capacity }, (_, i) => {
    const angle = (i / capacity) * Math.PI * 2;
    return new THREE.Vector3(Math.cos(angle), 0.2, Math.sin(angle)).normalize();
  });
}

function makePool(capacity = 4) {
  const created: THREE.Mesh[] = [];
  const disposed: THREE.Mesh[] = [];
  const pool = new DotPool({
    capacity,
    radius: 1,
    createMesh: () => {
      const mesh = new THREE.Mesh(new THREE.CircleGeometry(0.03, 12), new THREE.MeshBasicMaterial());
      created.push(mesh);
      return mesh;
    },
  });
  return { pool, created, disposed };
}

describe("DotPool", () => {
  it("按容量一次性建好对象，之后不再新建", () => {
    const { pool, created } = makePool(4);
    expect(created).toHaveLength(4);

    pool.applyWindow([topic(1), topic(2), topic(3), topic(4)], positions(4));
    pool.applyWindow([topic(5), topic(6), topic(7), topic(8)], positions(4));
    pool.applyWindow([topic(9), null, null, null], positions(4));

    expect(created).toHaveLength(4);
  });

  it("窗口更新复用同一批 mesh，不销毁对象", () => {
    const { pool } = makePool(3);
    pool.applyWindow([topic(1), topic(2), topic(3)], positions(3));
    const before = pool.all().map((m) => m.uuid);
    const disposeSpies = pool.all().map((m) => vi.spyOn(m.geometry, "dispose"));

    pool.applyWindow([topic(4), topic(5), topic(6)], positions(3));

    expect(pool.all().map((m) => m.uuid)).toEqual(before);
    for (const spy of disposeSpies) expect(spy).not.toHaveBeenCalled();
  });

  it("离开窗口的话题只是隐藏，不会被移除", () => {
    const { pool } = makePool(2);
    pool.applyWindow([topic(1), topic(2)], positions(2));

    pool.applyWindow([topic(1), null], positions(2));

    expect(pool.windowTopicIds()).toEqual(["t1", null]);
    expect(pool.all()).toHaveLength(2);
    expect(pool.all()[1].visible).toBe(false);
    expect(pool.all()[1].userData.active).toBe(false);
  });

  it("单个槽位替换只影响该槽位", () => {
    const { pool } = makePool(3);
    const slots = positions(3);
    pool.applyWindow([topic(1), topic(2), topic(3)], slots);

    pool.place(1, topic(9), slots[1]);

    expect(pool.windowTopicIds()).toEqual(["t1", "t9", "t3"]);
    expect(pool.meshOfTopic("t2")).toBeNull();
    expect(pool.meshOfTopic("t9")).toBe(pool.meshOfSlot(1));
  });

  it("话题点在球面上朝外摆放", () => {
    const { pool } = makePool(1);
    const dir = new THREE.Vector3(1, 0, 0);

    pool.place(0, topic(1), dir);

    const mesh = pool.meshOfSlot(0)!;
    expect(mesh.position.length()).toBeGreaterThan(0);
    const normal = new THREE.Vector3(0, 0, 1).applyQuaternion(mesh.quaternion);
    expect(normal.x).toBeCloseTo(1, 5);
  });
});
