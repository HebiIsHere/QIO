import * as THREE from "three";

/**
 * Smooth minimum（IQ 公式）。要求 k > 0：
 * - k = 0 且 a === b 时 h 为 0/0 → NaN；
 * - k < 0 会破坏融合语义（h 被钳制后结果无意义）。
 * 远距离（|a-b| >> k）退化为 min(a,b)；近距离产生小于两者的融合值。
 */
export function smin(a: number, b: number, k: number): number {
  const h = Math.max(0, Math.min(1, 0.5 + 0.5 * (b - a) / k));
  return b * (1 - h) + a * h - k * h * (1 - h);
}

export interface TopicRingSource { pos: THREE.Vector3; w: number; }

/**
 * 球面角距离场：对每个话题计算 p 到该环的角距离并做 smooth-min 融合。
 * 前置条件：p 与 t.pos 均为单位向量（acos(clamp(p·t.pos)) 仅在单位向量下才是夹角）。
 * topics 为空时返回 +∞ 哨兵 1e9（表示全空间皆为正距离）。
 */
export function levelField(p: THREE.Vector3, topics: TopicRingSource[], rL: number, k: number): number {
  let f = 1e9;
  for (const t of topics) {
    const ang = Math.acos(Math.max(-1, Math.min(1, p.dot(t.pos))));
    f = smin(f, ang - t.w * rL, k);
  }
  return f;
}
