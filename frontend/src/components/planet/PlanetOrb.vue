<script setup lang="ts">
/**
 * PlanetOrb —— 全屏 Planet 的**抽象压缩态**（第四阶段核心）。
 *
 * 它不是一个「好看的球图标」，而是 Planet 本体在小尺度、低信息密度下的样子：
 * - 同一套几何：透明球轮廓 + 3 层融合环（内粗外细）；
 * - 同一套数学：`planet/ringField.ts` 的 `levelFieldDir` / `smin`
 *   —— 与全屏 Planet 的 GPU 距离场是同一套等值线定义（不是另画几个椭圆）；
 * - 同一套颜色：只读 CSS 令牌（`--link` / `--accent` / `--accent-soft`），随主题切换；
 * - 同一套生命感：低频、小幅度呼吸与内部流动。
 *
 * 它**不承担信息展示**：不显示数量、不显示标签、不做进度指示。
 *
 * 为什么自己算像素而不是画 SVG：正交投影下，球面上某点的环是否可见取决于它与锚点的
 * **球面角距离**。用 2D 椭圆近似会在球边缘明显露馅（环直接变成被压扁的椭圆），
 * 而屏幕小球恰恰是用户最常看到的那一帧。
 *
 * 性能与降级：
 * - 离屏 canvas 以 2× 超采样绘制后缩放，边缘平滑；
 * - 重绘频率约 3–4fps（生命感要「不被注意到」才成立），页面隐藏或元素不可见时停止；
 * - `prefers-reduced-motion`：只画一帧静态图，不再呼吸；
 * - jsdom 等环境里 `getContext("2d")` 可能返回 null：跳过绘制但保留 canvas 结构。
 */
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import { levelFieldDir, spreadAnchors, type DirSource } from "../../planet/ringField";
import { LEVEL_A, LEVEL_HW, LEVEL_R } from "../../planet/topicData";
import { prefersReducedMotion } from "../../utils/motion";

const props = withDefaults(
  defineProps<{
    /** CSS 尺寸（px）。渲染分辨率会按 SS 超采样，与视觉尺寸解耦 */
    size?: number;
    /** 内部细节强度 0–1：转场阶段 A 会把它调高，表示「这个对象即将展开」 */
    detail?: number;
    /** 真实话题方向（可选）。缺省时用确定性锚点，保证压缩态可复现 */
    topics?: DirSource[];
    /** 运行中：仅用一个极轻的活动点表达状态，不做加载动画 */
    activity?: boolean;
  }>(),
  { size: 84, detail: 1, topics: undefined, activity: false },
);

/** 超采样倍数：2× 足够平滑，4× 只在静态帧里更细腻但会拖慢呼吸重绘 */
const SS = 2;
/** 融合环的 smooth-min 系数：与 GPU 版保持一致（planetShader.ts 的 uK） */
const K = 0.16;
/** 锚点数量：够读出「球面结构」，又不至于在小球上糊成一团 */
const ANCHOR_COUNT = 11;

const canvasRef = ref<HTMLCanvasElement | null>(null);
/** 呼吸相位：由 rAF 累加，驱动锚点的缓慢旋转与透明度微变 */
let phase = 0;
let raf = 0;
let lastDraw = 0;
let observer: MutationObserver | null = null;
let visibilityBound = false;
/** 离屏画布：只在需要时创建（jsdom 下不创建） */
let off: HTMLCanvasElement | null = null;
let offCtx: CanvasRenderingContext2D | null = null;

/** 固定种子的锚点：同一份代码永远得到同一个球 */
const anchors = spreadAnchors(ANCHOR_COUNT, 7);

/**
 * 当前环境是否真的有 2D canvas。
 * jsdom（vitest 默认环境）里 `getContext("2d")` 未实现，会返回 null 并往控制台刷
 * "Not implemented" 噪音 —— 与其每帧调用一次再判空，不如一开始就认出来。
 */
function canUseCanvas2D(): boolean {
  if (typeof navigator === "undefined") return false;
  return !/jsdom/i.test(String(navigator.userAgent || ""));
}

/** 缓存的主题色（RGB 分量）。主题变化时失效重算 —— 颜色只能来自令牌，不能写死在代码里 */
let cachedColors: { line: [number, number, number]; accent: [number, number, number]; lineCss: string; accentCss: string } | null = null;

/** 把 CSS 颜色解析成 RGB 分量：令牌值是 #rrggbb 或 rgba(...)，两种都要认 */
function parseColor(css: string, fallback: [number, number, number]): [number, number, number] {
  const s = css.trim();
  const hex = s.match(/^#([0-9a-f]{6})$/i);
  if (hex) {
    const n = parseInt(hex[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  const rgb = s.match(/^rgba?\(([^)]+)\)$/i);
  if (rgb) {
    const parts = rgb[1].split(",").map((p) => parseFloat(p.trim()));
    if (parts.length >= 3 && parts.every((v) => Number.isFinite(v))) {
      return [parts[0], parts[1], parts[2]];
    }
  }
  return fallback;
}

function readColors() {
  if (cachedColors) return cachedColors;
  const cs = typeof getComputedStyle === "function" ? getComputedStyle(document.documentElement) : null;
  const get = (name: string, fallback: string) => (cs?.getPropertyValue(name).trim() || fallback);
  const lineCss = get("--link", "#e878bd");
  const accentCss = get("--accent", "#c51b7d");
  cachedColors = {
    lineCss,
    accentCss,
    line: parseColor(lineCss, [232, 120, 189]),
    accent: parseColor(accentCss, [197, 27, 125]),
  };
  return cachedColors;
}

/** 环带在像素上的可见度：|f| 是角距离（弧度），乘以球半径 R 换算成像素距离 */
function bandAlpha(f: number, halfWidthPx: number, radiusPx: number): number {
  const distPx = Math.abs(f) * radiusPx;
  const edge = 1.15; // ~1px 抗锯齿
  return 1 - smoothstep(halfWidthPx - edge, halfWidthPx, distPx);
}

function smoothstep(a: number, b: number, x: number): number {
  if (b <= a) return x < a ? 0 : 1;
  const t = Math.max(0, Math.min(1, (x - a) / (b - a)));
  return t * t * (3 - 2 * t);
}

/**
 * 画一帧压缩态。
 *
 * 环的像素半宽比 GPU 版更细：小球上照搬 5.0/3.25/2.0 会把三层糊成一块，
 * 但**保持内粗外细的比例**（这是 Planet 环的识别特征，不能变）。
 */
/**
 * 画一帧压缩态。
 *
 * `list` 是这一帧要用的锚点（呼吸帧会传入缓慢旋转后的锚点）；
 * 调用方给了真实话题方向时优先用真实方向 —— 压缩态因此真的反映「当前这个球」。
 */
function draw(list?: DirSource[]) {
  const el = canvasRef.value;
  if (!el) return;
  if (!canUseCanvas2D()) return;
  const size = props.size;
  const w = Math.max(1, Math.round(size * SS));
  const h = w;
  if (!off) {
    off = typeof document.createElement === "function" ? document.createElement("canvas") : null;
    if (!off) return;
    off.width = w;
    off.height = h;
    // jsdom 里 getContext 返回 null：不抛错，只是没有像素
    offCtx = off.getContext("2d");
  }
  const ctx = offCtx;
  const main = el.getContext ? el.getContext("2d") : null;
  if (!ctx || !main || !off) return;
  if (off.width !== w || off.height !== h) {
    off.width = w;
    off.height = h;
  }
  el.width = w;
  el.height = h;

  const cx = w / 2;
  const cy = h / 2;
  // 留 1px 给外圈描边，避免贴边被切
  const R = w / 2 - 1.5 * SS;
  const colors = readColors();
  const detail = Math.max(0, Math.min(1, props.detail));
  const scale = 0.72 + 0.28 * detail; // 细节增强时整体亮一点，但不刺眼
  const src = props.topics && props.topics.length ? props.topics : (list ?? anchors);
  const [lr, lg, lb] = colors.line;
  const [ar, ag, ab] = colors.accent;

  const img = ctx.createImageData(w, h);
  const data = img.data;

  for (let y = 0; y < h; y++) {
    const v = (cy - y) / R; // 向上为正
    for (let x = 0; x < w; x++) {
      const u = (x - cx) / R;
      const d2 = u * u + v * v;
      const idx = (y * w + x) * 4;
      if (d2 > 1) {
        data[idx + 3] = 0;
        continue;
      }
      const nz = Math.sqrt(1 - d2);
      const p = { x: u, y: v, z: nz };
      // 三层融合环（与全屏 Planet 同一套 rL / 权重 / smin）
      const f0 = levelFieldDir(p, src, LEVEL_R[0], K);
      const f1 = levelFieldDir(p, src, LEVEL_R[1], K);
      const f2 = levelFieldDir(p, src, LEVEL_R[2], K);
      // 内粗外细：小球尺度取 2.1 / 1.4 / 0.9 px
      const hw0 = Math.max(1.2, LEVEL_HW[0] * 0.42);
      const hw1 = Math.max(0.9, LEVEL_HW[1] * 0.42);
      const hw2 = Math.max(0.7, LEVEL_HW[2] * 0.45);
      let a =
        LEVEL_A[0] * bandAlpha(f0, hw0, R) +
        LEVEL_A[1] * bandAlpha(f1, hw1, R) +
        LEVEL_A[2] * bandAlpha(f2, hw2, R);
      a = Math.min(1, a * scale);
      // 球体内部极淡的填充：让轮廓之内不是全透明，读起来是「一个球」而不是「一张网」
      const fill = 0.06 * (1 - d2) + 0.02;
      data[idx] = lr;
      data[idx + 1] = lg;
      data[idx + 2] = lb;
      data[idx + 3] = Math.round(255 * Math.min(1, a + fill));
    }
  }
  ctx.putImageData(img, 0, 0);

  // 锚点点阵：真实话题点在压缩态里退化为「内部结构暗示」，不做信息展示
  if (detail > 0.35) {
    ctx.save();
    ctx.globalAlpha = Math.min(1, (detail - 0.35) / 0.65) * 0.9;
    for (const t of src) {
      if (t.z <= 0.05) continue; // 背面点不画
      const px = cx + t.x * R;
      const py = cy - t.y * R;
      ctx.beginPath();
      ctx.arc(px, py, 1.15 * SS, 0, Math.PI * 2);
      ctx.fillStyle = colors.accentCss;
      ctx.fill();
    }
    ctx.restore();
  }

  // 球体轮廓：QIO 的「球」靠这一圈成立，不能省
  ctx.save();
  ctx.beginPath();
  ctx.arc(cx, cy, R, 0, Math.PI * 2);
  ctx.strokeStyle = colors.lineCss;
  ctx.globalAlpha = 0.85 * (0.75 + 0.25 * detail);
  ctx.lineWidth = 1.6 * SS;
  ctx.stroke();
  ctx.restore();

  // 缩放到 CSS 尺寸
  main.clearRect(0, 0, w, h);
  main.drawImage(off, 0, 0);
}

function step(now: number) {
  raf = requestAnimationFrame(step);
  // 约 3.5fps：生命感必须「不被注意到」才成立；再快就变成加载动画了
  if (now - lastDraw < 280) return;
  lastDraw = now;
  phase += 0.018;
  // 缓慢内部流动：把锚点绕 y 轴轻微旋转（幅度很小，肉眼只感到「活着」）
  const ang = Math.sin(phase) * 0.05;
  const cos = Math.cos(ang);
  const sin = Math.sin(ang);
  const rotated = anchors.map((t) => ({ x: t.x * cos + t.z * sin, y: t.y, z: -t.x * sin + t.z * cos, w: t.w }));
  draw(rotated);
}

function startLoop() {
  if (raf || prefersReducedMotion()) return;
  lastDraw = 0;
  raf = requestAnimationFrame(step);
}

function stopLoop() {
  if (raf) cancelAnimationFrame(raf);
  raf = 0;
}

function onVisibility() {
  if (document.hidden) stopLoop();
  else startLoop();
}

onMounted(() => {
  draw();
  if (!prefersReducedMotion()) startLoop();
  if (!visibilityBound && typeof document.addEventListener === "function") {
    document.addEventListener("visibilitychange", onVisibility);
    visibilityBound = true;
  }
  // 主题切换：颜色来自 CSS 令牌，重画一次即可跟随
  if (typeof MutationObserver !== "undefined") {
    observer = new MutationObserver(() => {
      cachedColors = null;
      draw();
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  }
});

onBeforeUnmount(() => {
  stopLoop();
  observer?.disconnect();
  observer = null;
  if (visibilityBound && typeof document.removeEventListener === "function") {
    document.removeEventListener("visibilitychange", onVisibility);
    visibilityBound = false;
  }
  off = null;
  offCtx = null;
});

watch(
  () => [props.size, props.detail, props.activity, props.topics] as const,
  () => draw(),
);
</script>

<template>
  <span
    class="orb"
    data-compressed-planet
    :style="{ width: `${size}px`, height: `${size}px` }"
    aria-hidden="true"
  >
    <canvas ref="canvasRef" class="orb-canvas" :width="size * 2" :height="size * 2"></canvas>
    <span v-if="activity" class="orb-activity"></span>
  </span>
</template>

<style scoped>
.orb {
  position: relative;
  display: inline-block;
  flex: none;
  /* 生命感永远不该是「发光」：这里只给极轻的落影，让球从背景里浮出来 */
  filter: drop-shadow(0 2px 10px var(--accent-soft));
}
.orb-canvas {
  display: block;
  width: 100%;
  height: 100%;
}
/* 运行中：一枚安静的小点，不做加载动画、不做提醒徽章 */
.orb-activity {
  position: absolute;
  top: 8%;
  right: 8%;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--warning);
  border: 1.5px solid var(--bg-base);
  box-sizing: content-box;
}
</style>
