/**
 * 融合环距离场（无依赖版本）。
 *
 * 为什么单独抽出来：全屏 Planet 用 three.js 的 `ShaderMaterial` 在 GPU 上算这个场，
 * 而入口小球（压缩态）要画同一个场却**不能**引入 three.js —— 悬浮球在对话页首屏，
 * 一旦 import three，首屏 chunk 就会多出整个 three（既有约定是「three.js 不进首屏 chunk」，
 * 靠 PlanetView 的懒加载达成）。
 *
 * 所以：先把数学放这里（纯数组运算），再由两侧各自使用 ——
 * `sdfRings.ts` 用 three 的 Vector3 做 CPU 版（测试用），`PlanetOrb.vue` 用普通对象做压缩态渲染。
 * 两边用的是**同一个函数**，这是「小球与全屏 Planet 是同一个对象」在代码层的依据。
 */

/** Smooth minimum（IQ 公式）。k 必须 > 0：k=0 时 a===b 会产生 0/0。 */
export function smin(a: number, b: number, k: number): number {
  const h = Math.max(0, Math.min(1, 0.5 + (0.5 * (b - a)) / k));
  return b * (1 - h) + a * h - k * h * (1 - h);
}

/** 一个话题锚点：单位方向 + 环宽权重 */
export interface DirSource {
  x: number;
  y: number;
  z: number;
  w: number;
}

/**
 * 球面角距离场：对每个锚点算 p 到该环的角距离并做 smooth-min 融合。
 * 前置条件：p 与锚点方向都是单位向量（`acos(clamp(dot))` 只在单位向量下才是夹角）。
 * 锚点为空时返回 +∞ 哨兵 1e9。
 */
export function levelFieldDir(
  p: { x: number; y: number; z: number },
  topics: readonly DirSource[],
  rL: number,
  k: number,
): number {
  let f = 1e9;
  for (const t of topics) {
    const dot = p.x * t.x + p.y * t.y + p.z * t.z;
    const ang = Math.acos(dot > 1 ? 1 : dot < -1 ? -1 : dot);
    f = smin(f, ang - t.w * rL, k);
  }
  return f;
}

/**
 * 在球面上均匀撒 N 个确定性锚点（Fibonacci 球）。
 * 同一 seed 一定得到同一组方向 —— 压缩态因此是「可复现的同一个球」，
 * 不会每次打开都换个样子（那就不像同一个对象了）。
 */
export function spreadAnchors(count: number, seed = 1): DirSource[] {
  const out: DirSource[] = [];
  // 用一个小 LCG 把相位/权重打散，但保持完全确定性
  let s = (seed * 2654435761) >>> 0;
  const rnd = () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
  const offset = rnd() * Math.PI * 2;
  for (let i = 0; i < count; i++) {
    const y = 1 - (2 * (i + 0.5)) / count;
    const r = Math.sqrt(Math.max(0, 1 - y * y));
    const phi = offset + i * Math.PI * (3 - Math.sqrt(5)); // 黄金角
    out.push({
      x: Math.cos(phi) * r,
      y,
      z: Math.sin(phi) * r,
      // 权重 0.85–1.15：环宽略有差异，读起来像「有内容的球」而不是一张对称网
      w: 0.85 + rnd() * 0.3,
    });
  }
  return out;
}
