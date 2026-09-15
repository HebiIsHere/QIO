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
/** 同屏话题之间的最小角间距（弧度，约 10.3°）：不重叠、点得中。 */
export const MIN_SEP = 0.18;
/**
 * 新话题落点必须离「轮廓线」至少这么远（弧度）。
 * 太小的话用户会在数据替换的半路上看到它突然出现。
 */
const BACK_MARGIN = 0.12;

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

/**
 * 给一个**新进入窗口**的话题挑位置：在球体背面随机取点。
 *
 * 这是第二阶段的核心规则 —— 稳定性只要求「不拖动时窗口内的话题不乱动」，
 * 而不是「每个话题永远固定在某个槽位」。新话题出现在用户此刻看不见的
 * 背面任意位置，随着继续旋转自然转到正面。
 *
 * 约束：离相机方向至少 BACK_MARGIN（保证真的在看不见的那一侧）、
 * 与窗口里已有话题保持 MIN_SEP 角间距（不重叠）；实在挤不下时
 * 放宽间距也不返回正面的位置，最多重试若干次，绝不死循环。
 */
export function randomBackPosition(
  occupied: THREE.Vector3[],
  cameraDir: THREE.Vector3,
  rng: () => number,
): THREE.Vector3 {
  const cam = cameraDir.clone().normalize();
  const used = occupied.map((dir) => dir.clone().normalize());
  const threshold = -BACK_MARGIN;
  let fallback: THREE.Vector3 | null = null;
  const attempts = 160;
  for (let i = 0; i < attempts; i++) {
    // 先在整球均匀采样，再拒绝掉正面与太近的点
    const z = rng() * 2 - 1;
    const phi = rng() * Math.PI * 2;
    const r = Math.sqrt(Math.max(0, 1 - z * z));
    const dir = new THREE.Vector3(r * Math.cos(phi), z, r * Math.sin(phi));
    if (dir.dot(cam) > threshold) continue;
    fallback = fallback ?? dir;
    const tooClose = used.some(
      (other) => Math.acos(Math.min(1, Math.max(-1, dir.dot(other)))) < MIN_SEP,
    );
    if (!tooClose) return dir;
    if (i > attempts * 0.6) {
      // 后半程开始放宽间距要求：宁可稍近一点，也不返回正面位置
      const relaxed = MIN_SEP * 0.6;
      const stillClose = used.some(
        (other) => Math.acos(Math.min(1, Math.max(-1, dir.dot(other)))) < relaxed,
      );
      if (!stillClose) return dir;
    }
  }
  return fallback ?? cam.clone().multiplyScalar(-1);
}
