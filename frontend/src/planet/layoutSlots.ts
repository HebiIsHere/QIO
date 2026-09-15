/**
 * 临时球面布局（PlanetLayout）。
 *
 * 第二阶段明确：**话题不需要永久球面坐标**。位置只属于「当前展示窗口」——
 * 同一个话题下次出现可以在别的槽位，用户不需要记住「橡胶实验在东北侧」。
 *
 * 但当前展示中的话题必须稳定：这里给出的槽位是**确定性**的 —— 种子来自
 * 话题编号 + 浏览会话，同一个 seed 每次算出来一模一样，所以刷新、重开、
 * 重新布局都不会让已经在窗口里的话题跳位。
 *
 * 布局规则：环形带内分布（避开极区，不会挤在天顶/天底），带上一点点
 * 确定性抖动，避免看起来像数学采样。不重叠、有留白、点击不会重合。
 */
import * as THREE from "three";

/** 环形带内缘 / 外缘的极角（弧度）：50°~130°，两端各留 50° 空出来。 */
const POLAR_MIN = THREE.MathUtils.degToRad(55);
const POLAR_MAX = THREE.MathUtils.degToRad(125);
/** 极角抖动幅度（弧度，约 4°）。 */
const POLAR_JITTER = THREE.MathUtils.degToRad(4);
/** 黄金角：相邻槽位的方位角间隔天然分散。 */
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));

/** 32 位确定性哈希：同一个 (seed, index) 永远得到同一个 [0,1) 值。 */
function unitHash(seed: number, index: number): number {
  let h = (seed | 0) ^ Math.imul(index + 1, 0x9e3779b1);
  h = Math.imul(h ^ (h >>> 16), 0x85ebca6b);
  h = Math.imul(h ^ (h >>> 13), 0xc2b2ae35);
  h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}

/** 把 [0,1) 的哈希映射到 [-amount, amount]。 */
function jitter(seed: number, index: number, amount: number): number {
  return (unitHash(seed, index) - 0.5) * 2 * amount;
}

/**
 * 生成 capacity 个展示槽位（单位球面）。
 *
 * @param capacity 槽位数量（= 星球可见容量）
 * @param sessionSeed 浏览会话种子：同一次打开期间保持不变
 */
export function slotPositions(capacity: number, sessionSeed: number): THREE.Vector3[] {
  const count = Math.max(1, Math.floor(capacity));
  const out: THREE.Vector3[] = [];
  for (let i = 0; i < count; i++) {
    const t = (i + 0.5) / count;
    const polar = THREE.MathUtils.lerp(POLAR_MIN, POLAR_MAX, t) + jitter(sessionSeed, i, POLAR_JITTER);
    const bounded = Math.min(POLAR_MAX + POLAR_JITTER, Math.max(POLAR_MIN - POLAR_JITTER, polar));
    const azimuth = i * GOLDEN_ANGLE + jitter(sessionSeed, i + 1000, 0.06);
    out.push(
      new THREE.Vector3(
        Math.sin(bounded) * Math.cos(azimuth),
        Math.cos(bounded),
        Math.sin(bounded) * Math.sin(azimuth),
      ).normalize(),
    );
  }
  return out;
}

/**
 * 背面槽位顺序：把最背对相机（用户最看不见）的槽位排在前面。
 *
 * 数据替换只允许发生在低感知区域，渲染层每帧用这个顺序告诉浏览会话
 * 「现在可以回收哪些槽位」。
 */
export function backSlotOrder(dirs: THREE.Vector3[], camDir: THREE.Vector3): number[] {
  const normalizedCam = camDir.clone().normalize();
  return dirs
    .map((dir, index) => ({ index, dot: dir.clone().normalize().dot(normalizedCam) }))
    .sort((a, b) => (a.dot === b.dot ? a.index - b.index : a.dot - b.dot))
    .map((entry) => entry.index);
}
