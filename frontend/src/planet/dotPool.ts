/**
 * 话题点的渲染对象池。
 *
 * 星球上话题不断进出（每次旋转都可能换掉背面槽位），但 Three.js 对象数量
 * 必须固定：池子按可见容量一次建好，之后只改「这个对象现在代表哪个话题」。
 * 回收 = 换数据 + 隐藏，不是 dispose —— 否则长时间浏览会不断 new/dispose。
 */
import * as THREE from "three";

import type { BrowseTopic } from "./browseSession";

const FORWARD = new THREE.Vector3(0, 0, 1);
/** 圆点半径系数：真正半径 = 系数 × 话题的大小档位。 */
export const DOT_BASE_SIZE = 0.028;

export interface DotPoolOptions {
  capacity: number;
  /** 创建单个可复用对象（几何与材质由调用方共享，避免每点一份）。 */
  createMesh: () => THREE.Mesh;
  /** 圆点离球心的距离。 */
  radius: number;
  /** 每个话题的稳定大小档位（0.85~1.3 之类），缺省 1。 */
  sizeOf?: (topic: BrowseTopic) => number;
}

export class DotPool {
  readonly capacity: number;

  private readonly meshes: THREE.Mesh[] = [];
  private readonly radius: number;
  private readonly sizeOf: (topic: BrowseTopic) => number;

  constructor(options: DotPoolOptions) {
    this.capacity = Math.max(1, Math.floor(options.capacity));
    this.radius = options.radius;
    this.sizeOf = options.sizeOf ?? (() => 1);
    for (let slot = 0; slot < this.capacity; slot++) {
      const mesh = options.createMesh();
      mesh.userData.slot = slot;
      mesh.userData.topicId = null;
      mesh.userData.title = "";
      mesh.userData.active = false;
      mesh.userData.base = DOT_BASE_SIZE;
      mesh.visible = false;
      this.meshes.push(mesh);
    }
  }

  all(): THREE.Mesh[] {
    return this.meshes;
  }

  meshOfSlot(slot: number): THREE.Mesh | undefined {
    return this.meshes[slot];
  }

  meshOfTopic(topicId: string): THREE.Mesh | null {
    return this.meshes.find((m) => m.userData.topicId === topicId) ?? null;
  }

  windowTopicIds(): (string | null)[] {
    return this.meshes.map((m) => (m.userData.topicId as string | null) ?? null);
  }

  /** 整窗重建（打开星球、窗口初始化）。 */
  applyWindow(items: (BrowseTopic | null)[], positions: THREE.Vector3[]): void {
    for (let slot = 0; slot < this.capacity; slot++) {
      this.place(slot, items[slot] ?? null, positions[slot] ?? FORWARD);
    }
  }

  /**
   * 单个槽位替换（浏览流动用）：只改这个对象代表的话题、位置与尺寸。
   * 不做任何 new / dispose，也不碰其它槽位。
   */
  place(slot: number, topic: BrowseTopic | null, position: THREE.Vector3): THREE.Mesh {
    const mesh = this.meshes[slot];
    if (!mesh) {
      throw new RangeError(`slot out of range: ${slot}`);
    }
    const dir = position.clone().normalize();
    const base = topic ? DOT_BASE_SIZE * this.sizeOf(topic) : DOT_BASE_SIZE;
    mesh.userData.topicId = topic ? topic.topic_id : null;
    mesh.userData.title = topic ? topic.title : "";
    mesh.userData.active = topic !== null;
    mesh.userData.base = base;
    mesh.scale.setScalar(base / DOT_BASE_SIZE);
    mesh.position.copy(dir).multiplyScalar(this.radius);
    mesh.quaternion.setFromUnitVectors(FORWARD, dir);
    // 可见性每帧由渲染循环按「是否在球体背面」决定；这里只声明有没有内容。
    mesh.visible = false;
    return mesh;
  }
}
