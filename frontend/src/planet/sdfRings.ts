import * as THREE from "three";

export function smin(a: number, b: number, k: number): number {
  const h = Math.max(0, Math.min(1, 0.5 + 0.5 * (b - a) / k));
  return b * (1 - h) + a * h - k * h * (1 - h);
}

export interface TopicRingSource { pos: THREE.Vector3; w: number; }

export function levelField(p: THREE.Vector3, topics: TopicRingSource[], rL: number, k: number): number {
  let f = 1e9;
  for (const t of topics) {
    const ang = Math.acos(Math.max(-1, Math.min(1, p.dot(t.pos))));
    f = smin(f, ang - t.w * rL, k);
  }
  return f;
}
