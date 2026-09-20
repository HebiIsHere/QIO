import * as THREE from "three";
import { smin as sminCore } from "./ringField";

/**
 * Smooth minimum（IQ 公式）。要求 k > 0：
 * - k = 0 且 a === b 时 h 为 0/0 → NaN；
 * - k < 0 会破坏融合语义（h 被钳制后结果无意义）。
 * 远距离（|a-b| >> k）退化为 min(a,b)；近距离产生小于两者的融合值。
 *
 * 数学本体在 `ringField.ts`（无依赖）。入口小球（PlanetOrb）用的是同一个函数，
 * 这样「压缩态」与「全屏 Planet」的环形状来自同一套等值线，而不是各画一套。
 */
export function smin(a: number, b: number, k: number): number {
  return sminCore(a, b, k);
}

export interface TopicRingSource { pos: THREE.Vector3; w: number; }

/**
 * 球面角距离场：对每个话题计算 p 到该环的角距离并做 smooth-min 融合。
 * 前置条件：p 与 t.pos 均为单位向量（acos(clamp(p·t.pos)) 仅在单位向量下才是夹角）。
 * topics 为空时返回 +∞ 哨兵 1e9（表示全空间皆为正距离）。
 */
export function levelField(p: THREE.Vector3, topics: TopicRingSource[], rL: number, k: number): number {
  // three 的 Vector3 只是「带 dot 的向量」：转成纯数据后走同一份实现
  let f = 1e9;
  for (const t of topics) {
    const ang = Math.acos(Math.max(-1, Math.min(1, p.dot(t.pos))));
    f = sminCore(f, ang - t.w * rL, k);
  }
  return f;
}
