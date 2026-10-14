import * as THREE from "three";

export interface TopicData {
  id: string;
  ci: number;
  pos: THREE.Vector3;
  w: number;
}

/** 三层融合环半径（弧度角距离） */
export const LEVEL_R = [0.14, 0.22, 0.34];
/** 三层环不透明度权重 */
export const LEVEL_A = [0.62, 0.44, 0.28];
/** 像素半宽：内粗外细 */
export const LEVEL_HW = [5.0, 3.25, 2.0];
/** 同簇话题最小角间距（弧度） */
export const MIN_SEP = 0.095;

/**
 * 空槽位的环权重哨兵：与 `makeRingUniforms` 给「未使用 uniform 槽」的取值一致。
 *
 * 场项是 `ang − w·rL`：w = −100 时变成 `ang + 100·rL`，比三层环半径（0.14 / 0.22 / 0.34）
 * 大两个数量级，任何方向都不会与环层相交 —— 空槽因此不画环，也不会推开真实话题的环
 * （smin 取的是更小的那一项）。
 */
export const EMPTY_SLOT_WEIGHT = -100;

/**
 * 展示窗口 → 融合环锚点：**只有真的有话题的槽位才是锚点**。
 *
 * 2026-10-14 修：原来直接按槽位生成（空槽也带权重 1），空槽既没有圆点也没有标签，
 * 却在球面上画出一圈圈「里面什么都没有」的环 —— 用户报告「星球这里多了三圈圆环」。
 * 空槽保留占位（数组下标仍然对应槽位，applySwap 可以原地替换），只是权重被中和。
 */
export function ringAnchorsForSlots(
  dirs: readonly { x: number; y: number; z: number }[],
  occupied: readonly boolean[],
): TopicData[] {
  return dirs.map((dir, i) => ({
    id: `slot-${i}`,
    ci: i % 3,
    pos: new THREE.Vector3(dir.x, dir.y, dir.z),
    w: occupied[i] ? 1 : EMPTY_SLOT_WEIGHT,
  }));
}

/**
 * 由簇中心 + 数量生成话题点（单位球面，簇内最小角间距 MIN_SEP）。
 * 使用固定种子 LCG，保证每次生成结果一致（快照/测试可复现）。
 */
export function buildTopics(clusters: { center: THREE.Vector3; n: number }[]): TopicData[] {
  const out: TopicData[] = [];
  let seed = 20260809;
  const rnd = () => (seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296;
  const u = new THREE.Vector3(), v = new THREE.Vector3();
  const basis = (c: THREE.Vector3) => {
    u.crossVectors(c, new THREE.Vector3(0, 1, 0));
    if (u.lengthSq() < 1e-6) u.set(1, 0, 0);
    u.normalize();
    v.crossVectors(c, u).normalize();
  };
  clusters.forEach((c, ci) => {
    for (let k = 0; k < c.n; k++) {
      let pos: THREE.Vector3 | null = null;
      for (let tries = 0; tries < 40 && !pos; tries++) {
        const ang = 0.12 + rnd() * 0.10, ph = rnd() * Math.PI * 2;
        basis(c.center);
        const p = new THREE.Vector3().addScaledVector(c.center, Math.cos(ang))
          .addScaledVector(u, Math.sin(ang) * Math.cos(ph))
          .addScaledVector(v, Math.sin(ang) * Math.sin(ph)).normalize();
        let ok = true;
        for (const t of out) if (t.ci === ci && Math.acos(Math.max(-1, Math.min(1, p.dot(t.pos)))) < MIN_SEP) { ok = false; break; }
        if (ok) pos = p;
      }
      out.push({ id: `${ci}-${k}`, ci, pos: pos ?? c.center.clone(), w: 0.85 + rnd() * 0.45 });
    }
  });
  return out;
}