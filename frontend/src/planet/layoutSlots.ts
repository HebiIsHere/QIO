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

/** 极冠留白：|y| 不超过这个值（约 18°），避免话题堆在正南北极。 */
const POLAR_CAP = 0.95;
/**
 * 相机纵向限位（弧度）：离极点至少 35°。
 * 太靠近极点时横向拖动会退化（OrbitControls 的 azimuth 在那里没有意义），
 * 拖起来会「怎么拖都不动」。
 */
export const POLAR_LIMIT = THREE.MathUtils.degToRad(35);
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
/** 确定性随机源（同一个 seed 同一串数）。 */
export function seededRng(seed: number): () => number {
  let state = (seed >>> 0) || 1;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * 打开星球时把展示窗口**随机铺开在整个球面上**。
 *
 * 之前用的是环形带（所有话题挤在赤道附近），视觉上更像一条项链而不是星球。
 * 现在按球面均匀随机取点，同时保持最小角间距（不重叠、点得中），
 * 并且不把话题堆到正南北极（`POLAR_CAP`）。
 */
export function spreadPositions(capacity: number, seed: number): THREE.Vector3[] {
  const count = Math.max(1, Math.floor(capacity));
  const rng = seededRng(seed);
  return spreadWithRng(count, rng);
}

function spreadWithRng(count: number, rng: () => number): THREE.Vector3[] {
  const out: THREE.Vector3[] = [];
  let relax = 1;
  for (let i = 0; i < count; i++) {
    let best: THREE.Vector3 | null = null;
    for (let attempt = 0; attempt < 120; attempt++) {
      const dir = unitVector(rng);
      if (Math.abs(dir.y) > POLAR_CAP) continue;
      best = best ?? dir;
      const minSep = MIN_SEP * relax;
      const ok = out.every(
        (other) => Math.acos(Math.min(1, Math.max(-1, dir.dot(other)))) >= minSep,
      );
      if (ok) {
        best = dir;
        break;
      }
      // 后半程逐步放宽：宁可稍近一点，也不能死循环或返回极点
      if (attempt > 80) relax = Math.max(0.6, relax * 0.995);
    }
    out.push(best ?? unitVector(rng));
  }
  return out;
}

/** 球面均匀方向（先均匀取 z，再取方位角）。 */
function unitVector(rng: () => number): THREE.Vector3 {
  const z = rng() * 2 - 1;
  const phi = rng() * Math.PI * 2;
  const r = Math.sqrt(Math.max(0, 1 - z * z));
  return new THREE.Vector3(r * Math.cos(phi), z, r * Math.sin(phi)).normalize();
}

/**
 * 保留兼容：旧调用方（与部分测试）用「环形带槽位」的名字。
 * 第二阶段位置不再是固定槽位，这里直接给出球面随机铺开的结果。
 */
export function slotPositions(capacity: number, sessionSeed: number): THREE.Vector3[] {
  return spreadPositions(capacity, sessionSeed);
}

/** 把方向压进相机的纵向限位范围内（程序性移动也要遵守同一条限位）。 */
export function clampPolar(dir: THREE.Vector3, limit: number = POLAR_LIMIT): THREE.Vector3 {
  const v = dir.clone().normalize();
  const polar = Math.acos(Math.min(1, Math.max(-1, v.y)));
  if (polar <= Math.PI / 2 && polar < limit) {
    const phi = Math.atan2(v.z, v.x);
    return new THREE.Vector3(
      Math.sin(limit) * Math.cos(phi),
      Math.cos(limit),
      Math.sin(limit) * Math.sin(phi),
    ).normalize();
  }
  if (polar > Math.PI / 2 && polar > Math.PI - limit) {
    const phi = Math.atan2(v.z, v.x);
    return new THREE.Vector3(
      Math.sin(limit) * Math.cos(phi),
      -Math.cos(limit),
      Math.sin(limit) * Math.sin(phi),
    ).normalize();
  }
  return v;
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
