/**
 * 星球 3D 场景组合式函数：透明球 + 球面 SDF 融合环 + 聚焦/环波交互。
 * 移植自参考原型 .superpowers/brainstorm/vs-1786250923/content/planet3d.html。
 *
 * 第二阶段职责划分（见 docs/architecture.md 的 Planet 一节）：
 *   话题数据 → 浏览调度（PlanetBrowseSession）→ 展示窗口 → 临时布局（layoutSlots）
 *   → Three.js 表现（本文件）。
 * 渲染循环只负责「怎么画」：谁应该在窗口里由浏览会话决定，本文件不自己挑话题。
 * 话题点用固定容量的对象池承载，进出只换数据，不 new / dispose。
 */
import { onScopeDispose, ref, shallowRef } from "vue";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import type { TopicPosition } from "../services/api";
import type { TopicData } from "../planet/topicData";
import { RING_FRAG, RING_VERT, makeRingUniforms } from "../planet/planetShader";
import { LEVEL_HW } from "../planet/topicData";
import {
  backSlotOrder,
  clampPolar,
  POLAR_LIMIT,
  randomBackPosition,
} from "../planet/layoutSlots";
import { DotPool } from "../planet/dotPool";
import { BrowseFlowDriver } from "../planet/browseFlow";
import type { BrowseTopic, Dir } from "../planet/browseSession";
import { PlanetBrowseSession } from "../planet/browseSession";

export type CameraState = "overview" | "planet" | "focus";

const RADIUS = 1.0;
const DOT_RADIUS = 1.004;
/** 话题位置聚簇合并阈值（球面角距离，弧度） */
const CLUSTER_ANG = 0.5;
/**
 * 相机距离。
 *
 * 第二阶段把 planet / focus 各后撤一点：原来 focus=2.25 时球体（半径 1）
 * 的角半径约 24°，已经超过 45° 视角的一半，星球边缘被画面裁掉，
 * 与「始终保持足够留白」冲突。后撤到 2.6 / 2.9 后球体完整落在画面内。
 */
const RADII: Record<CameraState, number> = { overview: 5.5, planet: 2.9, focus: 2.6 };
const THEME_LINE: Record<"dark" | "light", number> = { dark: 0xe878bd, light: 0xb0136a };
/** 话题点兜底材质：Canvas 贴图不可用时复用（模块级单例，避免反复创建/泄漏） */
const DOT_FALLBACK_MATERIAL = new THREE.MeshBasicMaterial({ color: 0xc51b7d });

/** 新话题进入的淡入时长（毫秒） */
const ENTER_FADE_MS = 420;

/** 话题点的稳定大小档位：由 visual_seed 决定，同一个话题每次出现一样大 */
function dotSizeOf(topic: BrowseTopic): number {
  const seed = topic.visual_seed ?? 0;
  return 0.85 + ((seed % 100) / 100) * 0.45;
}

/** 确定性随机源：给「新话题落在背面哪个位置」用，同一个浏览会话可复现。 */
function mulberry32(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function usePlanetScene(canvas: { value: HTMLCanvasElement | null }) {
  const webglOK = ref(false);
  const fps = ref(0);
  const cameraState = ref<CameraState>("overview");
  const selectedTopicId = ref<string | null>(null);
  const markers = shallowRef<THREE.Mesh[]>([]);
  /** 指针当前指向的话题点 + 它旁边要显示的简短名称（不常驻，避免遮挡） */
  const hoverTopicId = ref<string | null>(null);
  const hoverLabel = ref<{ x: number; y: number; title: string } | null>(null);
  /** 当前选中话题的名称标签（跟着选中点走；只有位置有明显变化才更新，避免每帧触发重渲染） */
  const selectedLabel = ref<{ x: number; y: number; title: string } | null>(null);

  let renderer: THREE.WebGLRenderer | null = null;
  let scene: THREE.Scene | null = null;
  let camera: THREE.PerspectiveCamera | null = null;
  let controls: OrbitControls | null = null;
  let planetGroup: THREE.Group | null = null;
  let ringMat: THREE.ShaderMaterial | null = null;
  let ringUniforms: ReturnType<typeof makeRingUniforms> | null = null;
  let contour: THREE.LineLoop | null = null;
  let fillMat: THREE.MeshBasicMaterial | null = null;
  let gridMats: THREE.LineBasicMaterial[] = [];
  /** 网格基准透明度：reveal 只做比例缩放，保证「完整 Planet」时与既有取值完全一致 */
  let gridBase: number[] = [];
  let dotMat: THREE.MeshBasicMaterial | null = null;
  /** 话题点对象池：数量固定 = 可见容量，进出只换数据（见 planet/dotPool.ts） */
  let dotPool: DotPool | null = null;
  /** 当前槽位的球面方向（单位球面，由 layoutSlots 按会话种子确定） */
  let slotDirs: THREE.Vector3[] = [];
  /** 新话题落点用的确定性随机源（同一个浏览会话可复现） */
  let placeRng: () => number = Math.random;
  /** 浏览会话：序列 / 游标 / 展示窗口（见 planet/browseSession.ts） */
  let browseSession: PlanetBrowseSession | null = null;
  /** 旋转 → 话题流 的节流器 */
  let flowDriver = new BrowseFlowDriver();
  let lastAzimuth: number | null = null;
  /** 最近一次槽位替换的时间（仅用于诊断「旋转有没有真的推动话题流」） */
  let lastSwapAt = 0;
  let swapCount = 0;
  let feedCount = 0;
  let stepCount = 0;
  let lastDelta = 0;
  /** 诊断：这个闭包的实例号（用于确认「不是两个 usePlanetScene 实例在打架」） */
  const instanceId = Math.random().toString(36).slice(2, 8);
  /** 当前正面槽位数量（仅供开发构建的诊断钩子读取） */
  let frontFacing = 0;
  /** 窗口话题集合变化时的回调（调用方刷新标签 / 选中态） */
  let windowChanged: (() => void) | null = null;
  /**
   * 低功耗模式（对话页的入口小球）：帧率降到 ~15fps。
   *
   * 为什么需要：小球现在是**真实星球场景**渲染的（同一个场景、同一个相机，只是整体缩到入口尺度），
   * 它会在对话页常驻。全帧率跑一个 84px 的球没有意义，还白白耗电。
   */
  let lowPower = false;
  let lastLowPowerFrame = 0;
  /**
   * 空闲自转（入口小球「微微旋转」）。
   * 平时只有「没有聚焦话题」时才自转；小球状态是特例：它没有明确的焦点，但应当一直慢速转动。
   */
  let idleSpin = false;
  /**
   * 信息密度（第四阶段 Planet 连续体）：
   * 0 = 抽象态（只有球体轮廓与融合环，读起来就是入口小球的放大版），
   * 1 = 完整 Planet（话题点、经纬线、标签齐全）。
   * 它不是「透明度动画」的别名：转场期间话题点真的按密度出现/退场，
   * 所以「小球长大成 Planet」是同一个对象在增加信息密度，而不是两层交叉淡入。
   */
  let reveal = 1;
  /** 选中话题的持续标记：跟随选中点的圆环（不依赖短暂的环线动画） */
  let selRing: THREE.Mesh | null = null;
  let currentData: TopicData[] = [];
  let currentTheme: "dark" | "light" = "dark";

  let dotMeshes: THREE.Mesh[] = [];
  let raf = 0;
  /** 渲染循环是否在跑（隐藏/暂停时停掉，避免不可见时仍然绘帧） */
  let loopActive = false;
  let paused = false;
  /** 最近一次实际应用的画布尺寸：尺寸没变就不重设、不补帧 */
  let lastCanvasW = 0;
  let lastCanvasH = 0;
  let frameCount = 0;
  let lastSelectedLabel: { x: number; y: number; title: string } | null = null;
  let lastFpsTime = performance.now();
  let lastNow = performance.now();

  // 交互状态（计时一律 performance.now()）
  let tween: { from: THREE.Vector3; to: THREE.Vector3; t: number; dur: number; done?: () => void } | null = null;
  let targetQuat: THREE.Quaternion | null = null;
  /**
   * 「打开前的球体朝向」快照与「转回去」的补间（见 markReturnOrientation / rotateBack）。
   *
   * 为什么要单独一套而不是复用 targetQuat：`targetQuat` 是**聚焦**用的指数 slerp
   * （`slerp(targetQuat, dt*7)`，没有时长概念，还会被 `idleSpin` 的 `rotation.y +=` 抵消）；
   * 收起要的是「按时长走完一段固定朝向的旋转」，而且必须和球态的自转互斥。
   */
  let returnOrientation: THREE.Quaternion | null = null;
  let orientationTween: { from: THREE.Quaternion; to: THREE.Quaternion; t: number; dur: number } | null = null;
  let focusedDot: THREE.Mesh | null = null;
  let waveStart = -1e9;
  /** pointerdown 坐标/时间；pointerup 判定后立即消费清空，避免陈旧状态吞掉后续点击 */
  let lastDown: { x: number; y: number; t: number } | null = null;
  /** pointerup 判定为“有效点击”（位移小 + 时长短）后才允许 handleClick 触发聚焦 */
  let pendingClick = false;
  let pointerHandlers: {
    down: (e: PointerEvent) => void;
    up: (e: PointerEvent) => void;
    cancel: () => void;
    move: (e: PointerEvent) => void;
    leave: () => void;
  } | null = null;

  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  const _q = new THREE.Quaternion();
  const _ringQ = new THREE.Quaternion();
  const _worldDir = new THREE.Vector3();
  const _centerV = new THREE.Vector3();
  const _camDirV = new THREE.Vector3();
  const tmpV = new THREE.Vector3();
  const tmpV2 = new THREE.Vector3();

  /** 命中范围放大：可见圆点投影到屏幕后，指针距离 ≤ 该像素数即算命中 */
  const HOVER_PX = 14;
  const CLICK_PX = 16;

  const topicsRef = ref<TopicPosition[]>([]);

const easeInOutCubic = (x: number) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);

/**
 * 系统「减少动画」偏好：CSS 侧已由 base.css 处理，
 * 但相机补间是 JS 的 rAF 动画，必须自己读这个偏好，否则设了也照样动 700ms。
 */
function prefersReducedMotion(): boolean {
  // 与 CSS、设置页共用同一份偏好：html[data-motion] 先说话，
  // 没有属性时（未初始化/测试环境）再退回系统媒体查询。
  const attr = typeof document !== "undefined" ? document.documentElement.getAttribute("data-motion") : null;
  if (attr === "reduced") return true;
  if (attr === "standard") return false;
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/** 减少动画时把补间压到一帧：状态仍然正确落位，只是不再有位移过程 */
function motionDuration(ms: number): number {
  return prefersReducedMotion() ? 1 : ms;
}

  function makeDotTexture(): THREE.CanvasTexture | null {
    const c = document.createElement("canvas");
    c.width = 256; c.height = 256;
    const g = c.getContext("2d");
    if (!g) return null;
    g.clearRect(0, 0, 256, 256);
    g.fillStyle = "#c51b7d";
    g.beginPath(); g.arc(128, 128, 64, 0, Math.PI * 2); g.fill();
    const tex = new THREE.CanvasTexture(c);
    tex.magFilter = THREE.LinearFilter;
    tex.minFilter = THREE.LinearFilter;
    tex.generateMipmaps = false;
    tex.anisotropy = renderer ? renderer.capabilities.getMaxAnisotropy() : 1;
    return tex;
  }

  function applyRingUniforms(data: TopicData[]) {
    if (!ringMat || !ringUniforms) return;
    // 原地更新 uniform 值（不能整体替换 ringMat.uniforms：
    // three 在编译时缓存 uniform 绑定，替换对象后环不会随数据更新）。
    const u = makeRingUniforms(data, currentTheme);
    const d = ringUniforms;
    d.uTopics.value = u.uTopics.value;
    d.uWeights.value = u.uWeights.value;
    d.uCount.value = u.uCount.value;
    d.uRingColor.value.copy(u.uRingColor.value as THREE.Color);
    d.uR0.value = u.uR0.value; d.uR1.value = u.uR1.value; d.uR2.value = u.uR2.value;
    d.uA0.value = u.uA0.value; d.uA1.value = u.uA1.value; d.uA2.value = u.uA2.value;
    d.uK.value = u.uK.value;
    d.uHW0.value = u.uHW0.value; d.uHW1.value = u.uHW1.value; d.uHW2.value = u.uHW2.value;
  }

  function init() {
    if (renderer) return; // 幂等：已初始化则直接跳过
    if (!canvas.value) return;
    try {
      /**
       * alpha: true —— 画布本身**不画背景**，球体之外的像素是透明的。
       *
       * 为什么必须这样：入口小球长大成 Planet 时，整个星球层会被缩放到小球那个尺度。
       * 如果画布自己画一块不透明底色，缩放中的画布就会露出一块矩形（颜色只要与页面底色
       * 有一点差异就看得出来）。所以「虚空」一律交给星球页的铺底（`--planet-void`），
       * 画布只负责球体本体 —— 这样「同一个对象长大」在任何尺度上都成立。
       */
      renderer = new THREE.WebGLRenderer({ canvas: canvas.value, antialias: true, alpha: true });
    } catch {
      webglOK.value = false;
      return;
    }
    webglOK.value = true;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    /**
     * 第三个参数 false：**不要让 three 写 canvas 的内联 width/height style**。
     *
     * three 默认会把 CSS 尺寸写进元素内联样式，而内联样式优先级高于我们的类规则 ——
     * 结果「球态把画布设成球大小」这类规则永远不生效（实测：规则匹配、变量也解析，
     * 画布还是 1439px，因为内联 style 是 1439px）。尺寸统一交给 CSS 控制，
     * 渲染器只负责读 clientWidth/clientHeight 并设置绘图缓冲。
     */
    renderer.setSize(canvas.value.clientWidth, canvas.value.clientHeight, false);
    renderer.setClearAlpha(0);

    scene = new THREE.Scene();
    // 不设 scene.background：背景由星球页铺底提供（见上面 alpha 的说明）
    camera = new THREE.PerspectiveCamera(45, canvas.value.clientWidth / canvas.value.clientHeight, 0.01, 100);
    camera.position.set(0, 0, 5.5);

    controls = new OrbitControls(camera, canvas.value);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.enablePan = false;
    controls.minDistance = 0.9;
    controls.maxDistance = 8;
    // 纵向限位：离南北极至少 35°。太靠近极点时横向拖动会退化（方位角失去意义），
    // 用户会觉得「怎么拖都不动」；这条限位同时也是「不集中于极区」的相机侧保证。
    controls.minPolarAngle = POLAR_LIMIT;
    controls.maxPolarAngle = Math.PI - POLAR_LIMIT;

    planetGroup = new THREE.Group();
    scene.add(planetGroup);

    // 透明球
    fillMat = new THREE.MeshBasicMaterial({ color: 0x2a2130, transparent: true, opacity: 0.05, depthWrite: false });
    const fill = new THREE.Mesh(new THREE.SphereGeometry(RADIUS, 64, 48), fillMat);
    fill.renderOrder = 1;
    planetGroup.add(fill);

    // 轮廓圆：始终面向相机的圆环线（每帧 quaternion.copy(camera.quaternion)）
    const contourPts: THREE.Vector3[] = [];
    for (let i = 0; i <= 128; i++) { const a = (i / 128) * Math.PI * 2; contourPts.push(new THREE.Vector3(Math.cos(a), Math.sin(a), 0)); }
    contour = new THREE.LineLoop(
      new THREE.BufferGeometry().setFromPoints(contourPts),
      new THREE.LineBasicMaterial({ color: THEME_LINE[currentTheme], transparent: true, opacity: 0.8, depthWrite: false }),
    );
    contour.renderOrder = 4;
    scene.add(contour);

    // 极淡经纬线（lat -60..60 步 30；lon 0..330 步 45）
    const gridGroup = new THREE.Group();
    planetGroup.add(gridGroup);
    // 辅助网格刻意压得更淡（任务05 E「减弱辅助网格，优先突出可操作话题点」）：
    // 只影响网格与经线的可见强度，不改变球体结构、融合环与聚焦距离。
    const gridMat = () => new THREE.LineBasicMaterial({ color: THEME_LINE[currentTheme], transparent: true, opacity: 0.14, depthWrite: false });
    for (let lat = -60; lat <= 60; lat += 30) {
      const r = Math.cos((lat * Math.PI) / 180), y = Math.sin((lat * Math.PI) / 180);
      const pts: THREE.Vector3[] = [];
      for (let i = 0; i <= 96; i++) { const a = (i / 96) * Math.PI * 2; pts.push(new THREE.Vector3(Math.cos(a) * r, y, Math.sin(a) * r)); }
      const m = gridMat();
      m.opacity = lat === 0 ? 0.22 : 0.1;
      gridMats.push(m);
      // 记下基准透明度：信息密度（reveal）按比例缩放它，而不是覆盖它
      gridBase.push(m.opacity);
      gridGroup.add(new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts), m));
    }
    for (let lon = 0; lon < 360; lon += 60) {
      const pts: THREE.Vector3[] = [];
      for (let i = 0; i <= 64; i++) { const a = (i / 64) * Math.PI; pts.push(new THREE.Vector3(Math.cos(a), Math.sin(a), 0)); }
      const m = gridMat();
      m.opacity = 0.07;
      gridMats.push(m);
      gridBase.push(m.opacity);
      const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), m);
      line.rotation.y = (lon * Math.PI) / 180;
      gridGroup.add(line);
    }

    // 环球：球面 SDF 融合环
    dotMat = (() => {
      const tex = makeDotTexture();
      if (!tex) return null;
      return new THREE.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false, side: THREE.DoubleSide });
    })();
    ringUniforms = makeRingUniforms([], currentTheme);
    ringMat = new THREE.ShaderMaterial({
      uniforms: ringUniforms,
      vertexShader: RING_VERT,
      fragmentShader: RING_FRAG,
      transparent: true,
      depthWrite: false,
    });
    const ringSphere = new THREE.Mesh(new THREE.SphereGeometry(RADIUS * 1.006, 128, 96), ringMat);
    ringSphere.renderOrder = 2;
    planetGroup.add(ringSphere);

    // 选中标记：面向相机的圆环，位置跟随选中话题点（持续可见，不是一次性的环波）
    selRing = new THREE.Mesh(
      new THREE.RingGeometry(1, 1.22, 48),
      new THREE.MeshBasicMaterial({ color: THEME_LINE[currentTheme], transparent: true, opacity: 0.95, depthWrite: false, side: THREE.DoubleSide }),
    );
    selRing.renderOrder = 6;
    selRing.visible = false;
    planetGroup.add(selRing);

    // 点击 vs 拖拽判定：pointerup 判定并消费 lastDown（陈旧状态不吞点击），pointercancel 清空
    const onPointerDown = (e: PointerEvent) => { lastDown = { x: e.clientX, y: e.clientY, t: performance.now() }; pendingClick = false; };
    const onPointerUp = (e: PointerEvent) => {
      if (lastDown) {
        const dx = e.clientX - lastDown.x, dy = e.clientY - lastDown.y;
        const dt = performance.now() - lastDown.t;
        lastDown = null;
        pendingClick = dx * dx + dy * dy < 36 && dt < 450;
      }
    };
    const onPointerCancel = () => { lastDown = null; pendingClick = false; };
    const onPointerMove = (e: PointerEvent) => {
      // 拖动中不显示标签：否则一边转球一边弹名字会互相干扰
      if (lastDown) { clearHover(); return; }
      const hit = pickTopic(e.clientX, e.clientY, HOVER_PX);
      if (!hit) { clearHover(); return; }
      hoverTopicId.value = hit.topicId;
      hoverLabel.value = { x: hit.x, y: hit.y, title: hit.title };
    };
    const onPointerLeave = () => clearHover();
    pointerHandlers = { down: onPointerDown, up: onPointerUp, cancel: onPointerCancel, move: onPointerMove, leave: onPointerLeave };
    canvas.value.addEventListener("pointerdown", onPointerDown);
    canvas.value.addEventListener("pointerup", onPointerUp);
    canvas.value.addEventListener("pointercancel", onPointerCancel);
    canvas.value.addEventListener("pointermove", onPointerMove);
    canvas.value.addEventListener("pointerleave", onPointerLeave);

    document.addEventListener("visibilitychange", onVisibilityChange);
    startLoop();
  }

  /**
   * 渲染循环的启停。页面切到后台（或调用方显式暂停）时不再绘帧：
   * 装饰动画没有「必须继续跑」的理由，停下来可以省电与算力；
   * 恢复时重置时间基准，避免把暂停期间的时长当成一帧 dt 造成跳变。
   */
  function startLoop() {
    if (loopActive || paused) return;
    loopActive = true;
    lastNow = performance.now();
    raf = requestAnimationFrame(animate);
  }

  function stopLoop() {
    loopActive = false;
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
  }

  function setPaused(next: boolean) {
    if (paused === next) return;
    paused = next;
    if (next) stopLoop();
    else startLoop();
  }

  /** 低功耗（入口小球）：~15fps，够表达「活着」，不浪费 GPU/电 */
  function setLowPower(next: boolean) {
    lowPower = next;
  }

  /** 空闲自转（入口小球慢速旋转） */
  function setIdleSpin(next: boolean) {
    idleSpin = next;
  }

  /**
   * 环在**屏幕上**的目标宽度（像素，按「绘图缓冲像素 → 屏幕像素」的比值折算）。
   *
   * 融合环的宽度是按 buffer 像素算的（fwidth 恒定像素宽），整层被缩小时像素宽也跟着被缩掉 ——
   * 所以要按当前缩放铺回去。但**补偿倍数不能由调用方给**：调用方算它的时候未必拿到了当时
   * 的几何（实测踩过：尾段刚把画布换成球自己的 108px，补偿倍数还是整层缩放 0.13 时的 ≈2.0，
   * 于是那一帧的环被画成 12px 宽 —— 用户看到的就是「缩小途中闪现一下」）。
   * 所以只接受目标屏幕宽度，倍数在**渲染前**用当时的几何现算。
   */
  let ringScreenWidthPx = LEVEL_HW[0];

  function setRingScreenWidth(px: number) {
    ringScreenWidthPx = Math.max(0.5, Math.min(LEVEL_HW[0], Number.isFinite(px) ? px : LEVEL_HW[0]));
  }

  /**
   * 渲染前落实环宽补偿：`屏幕像素 / 绘图缓冲像素` 用**画布此刻的真实几何**现算。
   * 用 `getBoundingClientRect()` 在这里是**对的** —— 要的就是「含层变换之后画布有多宽」；
   * 布局值（`clientWidth`）与它相除，正好是「一个绘图缓冲像素等于多少屏幕像素」。
   */
  function applyRingWidthCompensation() {
    const el = canvas.value;
    if (!ringUniforms || !el || !el.clientWidth) return;
    const layerScale = el.getBoundingClientRect().width / el.clientWidth;
    const scale = ringScreenWidthPx / (LEVEL_HW[0] * Math.max(1e-4, layerScale));
    ringUniforms.uWidthScale.value = Math.max(0.2, Math.min(12, scale));
  }

  function onVisibilityChange() {
    setPaused(document.hidden);
  }

  function clearHover() {
    if (hoverTopicId.value === null && hoverLabel.value === null) return;
    hoverTopicId.value = null;
    hoverLabel.value = null;
  }

  /**
   * 建立本次浏览的展示窗口（第二阶段）：
   *
   * - 槽位来自 `layoutSlots`（当前展示布局，不写进数据库，也不是永久坐标）；
   * - 渲染对象来自对象池（数量固定 = 可见容量，进出只换数据）；
   * - 窗口内容由 `PlanetBrowseSession` 决定，渲染循环只负责怎么画。
   */
  function attachBrowse(
    session: PlanetBrowseSession,
    options: { seed?: number; onWindowChange?: () => void } = {},
  ): void {
    const pg = planetGroup;
    if (!pg) return;
    browseSession = session;
    windowChanged = options.onWindowChange ?? null;
    placeRng = mulberry32((options.seed ?? 1) >>> 0);
    slotDirs = session
      .windowMembers()
      .map((m) => (m ? new THREE.Vector3(...m.dir) : new THREE.Vector3(0, 1, 0)));
    const material = dotMat ?? DOT_FALLBACK_MATERIAL;

    if (!dotPool || dotPool.capacity !== session.capacity) {
      dotPool?.all().forEach((m) => pg.remove(m));
      dotPool = new DotPool({
        capacity: session.capacity,
        radius: DOT_RADIUS,
        sizeOf: dotSizeOf,
        createMesh: () => {
          const mesh = new THREE.Mesh(
            new THREE.CircleGeometry(0.028, 32),
            material.clone(),
          );
          mesh.renderOrder = 5;
          pg.add(mesh);
          return mesh;
        },
      });
    }
    dotMeshes = dotPool.all();
    currentData = slotDirs.map((dir, i) => ({ id: `slot-${i}`, ci: i % 3, pos: dir, w: 1 }));
    applyRingUniforms(currentData);
    markers.value = dotMeshes;
    refreshWindow(performance.now());
    flowDriver.reset();
    lastAzimuth = null;
  }

  /** 把浏览会话当前的展示窗口画出来（整窗同步一次）。 */
  function refreshWindow(now: number) {
    const session = browseSession;
    if (!session || !dotPool) return;
    const members = session.windowMembers();
    slotDirs = members.map((m) => (m ? new THREE.Vector3(...m.dir) : new THREE.Vector3(0, 1, 0)));
    dotPool.applyWindow(
      members.map((m) => m?.topic ?? null),
      slotDirs,
    );
    dotPool.all().forEach((mesh, slot) => {
      // 已经在窗口里的话题不再重播淡入：只有真正换进来的才做进入动画
      const changed =
        (mesh.userData.windowTopicId as string | null) !== (members[slot]?.topic.topic_id ?? null);
      if (changed) {
        mesh.userData.enterAt = now;
        mesh.userData.windowTopicId = members[slot]?.topic.topic_id ?? null;
      }
    });
    currentData = slotDirs.map((dir, i) => ({ id: `slot-${i}`, ci: i % 3, pos: dir, w: 1 }));
    applyRingUniforms(currentData);
    windowChanged?.();
  }

  /** 单个槽位替换（旋转推动话题流时使用）。 */
  function applySwap(slot: number, topic: BrowseTopic | null, dir: THREE.Vector3, now: number) {
    if (!dotPool) return;
    slotDirs[slot] = dir.clone().normalize();
    currentData[slot] = { id: `slot-${slot}`, ci: slot % 3, pos: slotDirs[slot], w: 1 };
    const mesh = dotPool.place(slot, topic, slotDirs[slot]);
    mesh.userData.enterAt = now;
    mesh.userData.windowTopicId = topic?.topic_id ?? null;
    applyRingUniforms(currentData);
    lastSwapAt = now;
    swapCount += 1;
    windowChanged?.();
  }

  /** 当前哪些槽位在球体背面（用户看不见），按最背面优先排序。 */
  function backSlots(): number[] {
    if (!camera || !planetGroup || !slotDirs.length) return [];
    planetGroup.getWorldPosition(_centerV);
    _camDirV.subVectors(camera.position, _centerV).normalize();
    const worldDirs = slotDirs.map((dir) =>
      dir.clone().applyQuaternion(planetGroup!.quaternion).normalize(),
    );
    return backSlotOrder(worldDirs, _camDirV);
  }

  function beginTween(from: THREE.Vector3, to: THREE.Vector3, dur: number, done?: () => void) {
    if (controls) controls.enabled = false;
    // 覆盖旧补间时先完成旧 done 回调：go/focusTopic 被打断时其 promise 仍能 resolve，
    // 避免 close() 的 await planet.go(...) 因 tween 被覆盖而永不完成（界面卡死）。
    if (tween?.done) tween.done();
    tween = { from, to, t: 0, dur: motionDuration(dur), done };
  }

  function go(state: CameraState, target?: THREE.Vector3 | null, duration = 300): Promise<void> {
    return new Promise((resolve) => {
      if (!camera) { resolve(); return; }
      const endTarget = target?.clone().normalize() ?? new THREE.Vector3(0, 0, 1);
      // 程序性移动也遵守同一条纵向限位，避免补间结束后被 OrbitControls 拽一下
      const endPos = clampPolar(endTarget).multiplyScalar(RADII[state]);
      focusedDot = null;
      targetQuat = null;
      // 已经在目标构图：直接落状态，不空跑一段「不动」的补间锁住拖动
      if (camera.position.distanceToSquared(endPos) < 1e-4) {
        cameraState.value = state;
        if (controls) controls.enabled = true;
        controls?.update();
        resolve();
        return;
      }
      beginTween(camera.position.clone(), endPos, duration, () => {
        cameraState.value = state;
        controls?.update();
        resolve();
      });
    });
  }

  function cancelAnimation() {
    tween = null;
    targetQuat = null;
    orientationTween = null;
    if (controls) controls.enabled = true;
  }

  /**
   * 记下「打开前的球体朝向」：收起时要转回它（用户要求：展开与收回都同时有旋转）。
   * 在展开**聚焦之前**调用 —— 记的是入口小球那一刻的朝向，而不是聚焦之后的。
   */
  function markReturnOrientation() {
    returnOrientation = planetGroup ? planetGroup.quaternion.clone() : null;
  }

  /**
   * 与体量收缩**同刻开始**、把球转回 `markReturnOrientation` 记下的朝向。返回实际夹角（度）。
   *
   * 时长按夹角缩放，形状与 `focusTopic` 的 `distanceFactor` 一致（完全对齐 0.6×、差 180° 1.4×），
   * 再**封顶在 maxMs**（= 收起窗口）：超出窗口就会拖到球态，和那里的 `rotation.y +=` 抢同一个四元数。
   * 曲线用 `easeInOutCubic` —— 它是对称曲线，所以「收起转回去」正好是「展开转过来」的时间倒放。
   */
  function rotateBack(maxMs: number): number {
    if (!planetGroup || !returnOrientation || !Number.isFinite(maxMs) || maxMs <= 0) return 0;
    const from = planetGroup.quaternion.clone();
    const to = returnOrientation.clone();
    const dot = Math.min(1, Math.abs(from.dot(to)));
    const angle = 2 * Math.acos(dot); // 0..π
    if (angle < 1e-3) return 0;
    const factor = 0.6 + 0.4 * (1 - Math.cos(angle));
    orientationTween = { from, to, t: 0, dur: Math.max(1, Math.min(maxMs, Math.round(maxMs * factor))) };
    return Math.round((angle * 180) / Math.PI);
  }

  /**
   * 把相机直接放到某个状态的构图（不走补间）。
   * 用途：展开星球时不再「先看到远景小球、再整体放大」——
   * 一开始就接近最终构图，入场只做小幅收敛，动作才连贯。
   */
  function primeCamera(state: CameraState) {
    if (!camera || !controls) return;
    camera.position.set(0, 0, RADII[state]);
    cameraState.value = state;
    controls.update();
  }

  /**
   * 轻微后撤（保持朝向与当前焦点，不重置星球旋转）。
   * 用途：收起时的「收势」——先退一点再淡出，避免一边缩回全景一边淡出。
   */
  function pullBack(amount = 0.4, duration = 180): Promise<void> {
    const cam = camera;
    if (!cam) return Promise.resolve();
    const dir = cam.position.clone().normalize();
    const to = dir.multiplyScalar(cam.position.length() + amount);
    return new Promise((resolve) => {
      beginTween(cam.position.clone(), to, duration, () => resolve());
    });
  }

  /** 聚焦话题：相机 tween 到 方向*2.25（easeInOutCubic）+ 星球 slerp 使点居中 + 环波。
   * opts.duration 覆盖补间时长（重对焦/边栏开合后微调用短时长）；opts.wave=false 抑制环波。 */
  function focusTopic(topicId: string, topics: TopicPosition[], opts?: { duration?: number; wave?: boolean }) {
    if (!camera || !planetGroup) return;
    const dot = dotPool?.meshOfTopic(topicId) ?? null;
    if (!dot) {
      const topic = topics.find((t) => t.topic_id === topicId);
      if (topic) go("focus", new THREE.Vector3(...topic.position));
      return;
    }
    selectedTopicId.value = topicId;
    focusedDot = dot;
    cameraState.value = "focus";
    planetGroup.updateMatrixWorld(true);
    planetGroup.getWorldPosition(_centerV);
    _worldDir.copy(dot.position).normalize().applyQuaternion(planetGroup.quaternion);
    _camDirV.subVectors(camera.position, _centerV).normalize();
    _q.setFromUnitVectors(_worldDir, _camDirV);
    targetQuat = _q.multiply(planetGroup.quaternion.clone());
    // 短距离用更短时间：已经接近目标朝向时不再跑完整时长
    const base = opts?.duration ?? 320;
    const alignment = Math.max(-1, Math.min(1, _worldDir.dot(_camDirV)));
    const distanceFactor = 0.6 + 0.4 * (1 - alignment); // 完全对齐 → 0.6×
    // 同一个话题、且构图已经合适：不重复全幅聚焦，也不重播环波
    const alreadyFramed = alignment > 0.9998 && Math.abs(camera.position.length() - RADII.focus) < 0.01;
    if (alreadyFramed) {
      planetGroup.quaternion.copy(targetQuat);
      targetQuat = null;
      if (controls) controls.enabled = true;
      controls?.update();
      return;
    }
    beginTween(camera.position.clone(), _camDirV.clone().multiplyScalar(RADII.focus), base * distanceFactor);
    if (opts?.wave !== false) waveStart = performance.now(); // 环波
  }

  /** 话题点在屏幕上的位置（相对视口），用于放大命中范围与悬停标签定位 */
  function screenPosOf(dot: THREE.Mesh): { x: number; y: number; z: number } | null {
    if (!camera || !canvas.value) return null;
    dot.getWorldPosition(tmpV2).project(camera);
    const rect = canvas.value.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    return {
      x: rect.left + ((tmpV2.x + 1) / 2) * rect.width,
      y: rect.top + ((1 - tmpV2.y) / 2) * rect.height,
      z: tmpV2.z,
    };
  }

  /**
   * 命中话题点：先精确 raycast（与旧行为一致），再退回「屏幕上距离 ≤ 半径像素」。
   * 后者让点击/悬停范围大于可见圆点 —— 不必精确点中像素中心，触屏也更容易点中。
   * 只考虑 visible 的点：背面点不会穿透命中。
   */
  function pickTopic(clientX: number, clientY: number, radiusPx: number): { topicId: string; title: string; x: number; y: number } | null {
    if (!camera || !renderer || !canvas.value) return null;
    const rect = canvas.value.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    pointer.x = ((clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObjects(dotMeshes, true);
    const direct = hits.find((h) => h.object.visible === true);
    if (direct) {
      const topicId = direct.object.userData.topicId as string | undefined;
      if (topicId) {
        return { topicId, title: (direct.object.userData.title as string) ?? "", x: clientX, y: clientY };
      }
    }
    let best: { topicId: string; title: string; x: number; y: number } | null = null;
    let bestD = radiusPx * radiusPx;
    for (const dot of dotMeshes) {
      if (!dot.visible) continue;
      const topicId = dot.userData.topicId as string | undefined;
      if (!topicId) continue;
      const sp = screenPosOf(dot);
      if (!sp || sp.z > 1) continue;
      const d = (sp.x - clientX) ** 2 + (sp.y - clientY) ** 2;
      if (d <= bestD) {
        bestD = d;
        best = { topicId, title: (dot.userData.title as string) ?? "", x: sp.x, y: sp.y };
      }
    }
    return best;
  }

  /**
   * 画布点击：仅接受 pointerup 判定为有效点击的命中（拖拽/长按/指针取消不触发聚焦）。
   * Raycaster 不检查 object.visible，背面（半球剔除后 visible=false）的话题点会
   * 在透明球上被误命中，因此这里只取 visible === true 的命中。
   */
  function handleClick(clientX: number, clientY: number): boolean {
    if (!camera || !renderer || !canvas.value) return false;
    if (!pendingClick) return false;
    pendingClick = false;
    const hit = pickTopic(clientX, clientY, CLICK_PX);
    if (hit) { focusTopic(hit.topicId, topicsRef.value); return true; }
    return false;
  }

  /** 环波：uDimL = 1 - 0.75*exp(-((dt - L*0.16)/0.10)^2)，dt<1.2s 后恢复 1。 */
  function applyWave(now: number) {
    if (!ringUniforms) return;
    const dt = (now - waveStart) / 1000;
    if (dt < 1.2) {
      ringUniforms.uDim0.value = 1 - 0.75 * Math.exp(-Math.pow((dt - 0 * 0.16) / 0.10, 2));
      ringUniforms.uDim1.value = 1 - 0.75 * Math.exp(-Math.pow((dt - 1 * 0.16) / 0.10, 2));
      ringUniforms.uDim2.value = 1 - 0.75 * Math.exp(-Math.pow((dt - 2 * 0.16) / 0.10, 2));
    } else {
      ringUniforms.uDim0.value = ringUniforms.uDim1.value = ringUniforms.uDim2.value = 1;
    }
  }

  /** 主题切换：融合环/轮廓/经纬线换色，透明球与背景随主题。 */
  function setTheme(theme: "dark" | "light") {
    currentTheme = theme;
    const line = THEME_LINE[theme];
    if (contour) (contour.material as THREE.LineBasicMaterial).color.setHex(line);
    gridMats.forEach((m) => m.color.setHex(line));
    if (selRing) (selRing.material as THREE.MeshBasicMaterial).color.setHex(line);
    if (fillMat) fillMat.color.setHex(theme === "dark" ? 0x2a2130 : 0xffffff);
    if (ringMat) applyRingUniforms(currentData);
    // 不在这里改背景：画布是透明的（alpha: true），「虚空」由星球页铺底用
    // `--planet-void` 绘制 —— 转场期间球体外围才不会出现矩形画布。
  }

  function animate() {
    if (paused) {
      loopActive = false;
      return;
    }
    raf = requestAnimationFrame(animate);
    if (!renderer || !scene || !camera || !controls || !planetGroup) return;
    const now = performance.now();
    // 低功耗：入口小球只需要「活着」，~15fps 足够（跳过的帧连逻辑都不跑）
    if (lowPower && now - lastLowPowerFrame < 66) return;
    lastLowPowerFrame = now;
    const dt = Math.min(0.1, (now - lastNow) / 1000);
    lastNow = now;

    if (tween) {
      tween.t += dt * 1000;
      const k = Math.min(1, tween.t / tween.dur);
      camera.position.lerpVectors(tween.from, tween.to, easeInOutCubic(k));
      if (k >= 1) {
        const done = tween.done;
        tween = null;
        targetQuat = null;
        controls.enabled = true;
        controls.update();
        done?.();
      }
    }
    /**
     * 朝向补间（收起时转回打开前的朝向）优先，且与下面两条互斥：
     * 它写的是同一个 `planetGroup.quaternion`，和聚焦 slerp、空闲自转的 `rotation.y +=`
     * 同时写会互相抵消。
     */
    if (orientationTween) {
      orientationTween.t += dt * 1000;
      const k = Math.min(1, orientationTween.t / orientationTween.dur);
      planetGroup.quaternion.slerpQuaternions(orientationTween.from, orientationTween.to, easeInOutCubic(k));
      if (k >= 1) {
        planetGroup.quaternion.copy(orientationTween.to);
        orientationTween = null;
      }
    } else if (focusedDot && targetQuat && tween) {
      planetGroup.quaternion.slerp(targetQuat, Math.min(1, dt * 7));
    } else if (!focusedDot || idleSpin) {
      // 入口小球状态（idleSpin）即使没有焦点也保持极慢自转 —— 这就是「微微旋转」
      planetGroup.rotation.y += dt * 0.1;
    }
    if (contour) contour.quaternion.copy(camera.quaternion);

    // 旋转推动话题流：方位角累计够一步就换掉一个背面槽位。
    // 只统计用户真实操作镜头产生的方位角变化（补间期间 controls 关闭，不参与流动）。
    {
      const azimuth = controls.getAzimuthalAngle();
      if (lastAzimuth === null) {
        lastAzimuth = azimuth;
      } else {
        let delta = azimuth - lastAzimuth;
        if (delta > Math.PI) delta -= Math.PI * 2;
        else if (delta < -Math.PI) delta += Math.PI * 2;
        lastAzimuth = azimuth;
        const interacting = controls.enabled && !tween;
        feedCount += 1;
        lastDelta = delta;
        const step = flowDriver.feed(delta, now, interacting);
        if (step !== 0) stepCount += 1;
        if (step !== 0 && browseSession) {
          planetGroup.getWorldPosition(_centerV);
          _camDirV.subVectors(camera.position, _centerV).normalize();
          const camDir = _camDirV.clone();
          const swap = browseSession.takeSwap(
            step > 0 ? 1 : -1,
            {
              backSlots: backSlots(),
              // 新话题落在球体背面任意位置：不是继承旧位置，也不是永久槽位
              place: (occupied: Dir[]) =>
                randomBackPosition(
                  occupied.map((d) => new THREE.Vector3(...d)),
                  camDir,
                  placeRng,
                ).toArray() as Dir,
            },
            now,
          );
          if (swap) applySwap(swap.slot, swap.topic, new THREE.Vector3(...swap.dir), now);
        }
      }
    }

    // 半球剔除 + 进入淡入：新话题从「远处低透明」自然进入（spec 第 81 条）
    // 数据替换只发生在背面（不可见）槽位，用户看到的是连续世界。
    planetGroup.updateMatrixWorld(true);
    planetGroup.getWorldPosition(_centerV);
    _camDirV.subVectors(camera.position, _centerV);
    /**
     * 信息密度曲线（第四阶段 Planet 连续体）：
     * - reveal <= 0.25：抽象态，话题点几乎不可见（只有球与融合环）
     * - 0.25 → 0.8：话题点淡入并长大到全尺寸
     * - >= 0.8：完整 Planet
     * 网格整体更晚、更克制地回来，避免小尺度上「一坨线」。
     */
    const dotDensity = Math.max(0, Math.min(1, (reveal - 0.25) / 0.55));
    const gridDensity = 0.15 + 0.85 * Math.max(0, Math.min(1, (reveal - 0.45) / 0.55));
    gridMats.forEach((m, i) => {
      m.opacity = (gridBase[i] ?? m.opacity) * gridDensity;
    });
    frontFacing = 0;
    for (const dot of dotMeshes) {
      dot.getWorldPosition(tmpV);
      const active = dot.userData.active === true;
      dot.visible = active && tmpV.sub(_centerV).dot(_camDirV) >= 0;
      if (dot.visible) frontFacing += 1;
      const mat = dot.material as THREE.MeshBasicMaterial;
      if (!mat.transparent) continue;
      const enterAt = (dot.userData.enterAt as number) ?? 0;
      const k = Math.min(1, Math.max(0, (now - enterAt) / ENTER_FADE_MS));
      // 进入淡入 × 信息密度：转场期间话题点真的按密度出现（不是整体淡入）
      mat.opacity = k * dotDensity;
      const base = (dot.userData.base as number) ?? 0.028;
      dot.scale.setScalar((0.7 + 0.3 * k) * (base / 0.028) * (0.55 + 0.45 * reveal));
    }

    // 选中标记跟随选中点（圆环始终正对相机；点转到背面时一并隐藏）
    if (selRing) {
      const dot = focusedDot;
      // 压缩态里不该出现「选中环」这种信息层：密度不够时先收起来
      if (dot && dot.visible && reveal > 0.7) {
        // 选中环与话题点同属 planetGroup：必须用「局部」坐标，
        // 写世界坐标会被父级旋转再变换一次，环就跑到球面别处去了。
        selRing.position.copy(dot.position);
        _ringQ.copy(planetGroup.quaternion).invert().multiply(camera.quaternion);
        selRing.quaternion.copy(_ringQ);
        selRing.scale.setScalar(((dot.userData.base as number) ?? 0.028) * 1.75);
        selRing.visible = true;
      } else if (selRing.visible) {
        selRing.visible = false;
      }
    }
    // 选中话题的名称：跟着点走，位置变化超过 1.5px 才写回响应式状态
    {
      const dot = focusedDot;
      const title = dot ? ((dot.userData.title as string) ?? "") : "";
      const sp = dot && dot.visible && title && reveal > 0.7 ? screenPosOf(dot) : null;
      if (sp && title) {
        const changed =
          !lastSelectedLabel ||
          lastSelectedLabel.title !== title ||
          Math.abs(lastSelectedLabel.x - sp.x) > 1.5 ||
          Math.abs(lastSelectedLabel.y - sp.y) > 1.5;
        if (changed) {
          lastSelectedLabel = { x: sp.x, y: sp.y, title };
          selectedLabel.value = lastSelectedLabel;
        }
      } else if (selectedLabel.value) {
        lastSelectedLabel = null;
        selectedLabel.value = null;
      }
    }

    applyWave(now);
    controls.update();
    applyRingWidthCompensation(); // 环宽按「此刻」的几何现算（见 setRingScreenWidth）
    renderer.render(scene, camera);

    frameCount++;
    if (now - lastFpsTime >= 2000) {
      fps.value = Math.round((frameCount * 1000) / (now - lastFpsTime));
      frameCount = 0;
      lastFpsTime = now;
    }
  }

  function resize(force = false) {
    if (!camera || !renderer || !scene || !canvas.value) return;
    const w = canvas.value.clientWidth;
    const h = canvas.value.clientHeight;
    // 隐藏（display:none）或尺寸没变化时不重设、不补帧：
    // 侧栏过渡期间 ResizeObserver 会连续回调，这一步能省掉重复的清缓冲 + 重绘。
    // force=true 用于「隐藏后重新显示」：尺寸可能没变，但绘图缓冲需要重建。
    if (w <= 0 || h <= 0) return;
    if (!force && w === lastCanvasW && h === lastCanvasH) return;
    lastCanvasW = w;
    lastCanvasH = h;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
    // 立即重绘：ResizeObserver 回调在 rAF 渲染之后、paint 之前触发，setSize 会清空
    // WebGL 绘图缓冲；若不在同一回调内补一帧，过渡动画期间每一帧画布都是空帧（闪屏）。
    applyRingWidthCompensation();
    renderer.render(scene, camera);
  }

  /**
   * 信息密度（第四阶段 Planet 连续体）：0 = 抽象态（球 + 融合环），1 = 完整 Planet。
   * 只影响话题点/网格/选中环/标签的呈现密度，**不改变**球体结构、融合环参数与聚焦距离。
   */
  function setReveal(t: number) {
    reveal = Math.max(0, Math.min(1, Number.isFinite(t) ? t : 1));
    // 暂停时（离屏）也补一帧，避免再次显示时停在旧密度上
    if (paused && renderer && scene && camera) {
      applyRingWidthCompensation();
      renderer.render(scene, camera);
    }
  }

  /**
   * 球体此刻在屏幕上的中心与半径（CSS 像素，含画布在页面中的偏移）。
   * 入口小球与全屏 Planet 的尺度对齐靠它：转场起点必须是「球体真实占多大」，
   * 而不是一个估算比例 —— 否则放大过程会有一个跳变。
   * 场景未就绪时返回 null，调用方自己退化成估算值。
   *
   * 关键：这里**不能**用 `getBoundingClientRect()`。星球层（`.planet-stage`）上带着
   * `scale()`，而 canvas 是它的子元素 —— 它的 client rect 已经被缩放过了。
   * 第二次打开星球时层上还残留着上一次的 `--stage-k`（≈0.14），量出来的画布高度只有真实高度的
   * 百分之十几，球体屏幕半径跟着变小，起始缩放被算成 ≈1，于是「星球不再从小球长大、
   * 而是直接出现」（用户实测反馈的回归）。所以尺寸与位置一律用**布局值**
   * （offsetWidth/offsetHeight + offsetParent 链），它们不受 transform 影响。
   */
  function sphereScreenRect(): { cx: number; cy: number; radius: number } | null {
    const el = canvas.value;
    if (!camera || !el || !planetGroup) return null;
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    if (!w || !h) return null;
    let left = 0;
    let top = 0;
    let node: HTMLElement | null = el;
    while (node) {
      left += node.offsetLeft;
      top += node.offsetTop;
      node = node.offsetParent as HTMLElement | null;
    }
    planetGroup.updateMatrixWorld(true);
    camera.updateMatrixWorld();
    const center = new THREE.Vector3();
    planetGroup.getWorldPosition(center);
    const dist = Math.max(RADIUS * 1.001, camera.position.distanceTo(center));
    // 透视投影下球体轮廓的角半径 = asin(R / d)，换算到屏幕像素用同样的半 FOV 比例
    const angular = Math.asin(Math.min(1, RADIUS / dist));
    const halfFov = (camera.fov * Math.PI) / 360;
    const radius = ((h / 2) * Math.tan(angular)) / Math.tan(halfFov);
    if (!Number.isFinite(radius) || radius <= 0) return null;
    const ndc = center.clone().project(camera);
    return { cx: left + ((ndc.x + 1) / 2) * w, cy: top + ((1 - ndc.y) / 2) * h, radius };
  }

  onScopeDispose(() => {
    stopLoop();
    document.removeEventListener("visibilitychange", onVisibilityChange);
    if (pointerHandlers && canvas.value) {
      canvas.value.removeEventListener("pointerdown", pointerHandlers.down);
      canvas.value.removeEventListener("pointerup", pointerHandlers.up);
      canvas.value.removeEventListener("pointercancel", pointerHandlers.cancel);
      canvas.value.removeEventListener("pointermove", pointerHandlers.move);
      canvas.value.removeEventListener("pointerleave", pointerHandlers.leave);
    }
    selRing?.geometry.dispose();
    (selRing?.material as THREE.Material | undefined)?.dispose();
    selRing = null;
    renderer?.dispose();
    renderer = null;
    scene = null;
    camera = null;
    controls = null;
    planetGroup = null;
  });

  return {
    webglOK, fps, cameraState, selectedTopicId, markers, hoverTopicId, hoverLabel, selectedLabel,
    init, attachBrowse, go, focusTopic, handleClick, cancelAnimation, resize, setTheme,
    setPaused, setLowPower, setIdleSpin,
    /** 环在屏幕上的目标宽度（像素）；补偿倍数由渲染器按当时几何现算，见 setRingScreenWidth */
    setRingScreenWidth,
    primeCamera, pullBack,
    /** 展开时记下「打开前的朝向」、收起时转回它（见 markReturnOrientation / rotateBack） */
    markReturnOrientation, rotateBack,
    /** 当前展示窗口里的 topic_id（按槽位顺序，空位为 null）。 */
    windowTopicIds: () => dotPool?.windowTopicIds() ?? [],
    /** 诊断信息（开发构建用：确认旋转是否真的在推动话题流）。 */
    debugState: () => ({
      azimuth: controls ? controls.getAzimuthalAngle() : 0,
      interacting: Boolean(controls?.enabled) && tween === null,
      frontFacing,
      backSlots: slotDirs.length ? backSlots().length : 0,
      swaps: swapCount,
      feeds: feedCount,
      steps: stepCount,
      lastDelta,
      lastSwapAgo: lastSwapAt ? Math.round(performance.now() - lastSwapAt) : -1,
      /** 星球自转角（入口小球的「微微旋转」用它确认动画真的在跑） */
      rotationY: planetGroup ? Math.round(planetGroup.rotation.y * 1000) / 1000 : 0,
      /**
       * 球体朝向（四元数，x/y/z/w）。
       * 给验收用：展开会转到「当前话题正对镜头」，收起要转回**打开前的朝向**——
       * 只看 rotationY 不够（聚焦用的是任意轴的四元数），要比就得比整段朝向。
       */
      quaternion: planetGroup
        ? [planetGroup.quaternion.x, planetGroup.quaternion.y, planetGroup.quaternion.z, planetGroup.quaternion.w].map(
            (v) => Math.round(v * 1000) / 1000,
          )
        : null,
      lowPower,
      /** 环宽补偿的当前值（入口小球上环是否可见，就看它有没有被铺回去） */
      ringWidthScale: ringUniforms ? Math.round(ringUniforms.uWidthScale.value * 100) / 100 : null,
      windowSize: dotPool?.windowTopicIds().filter(Boolean).length ?? 0,
      id: instanceId,
      hasSession: browseSession !== null,
      /**
       * 当前窗口每个槽位的方向，**已换算到世界坐标**（带着星球自身的朝向），
       * 开发构建的验收脚本据此算「此刻正面看得见的是谁」。
       */
      dirs: (browseSession?.windowMembers() ?? []).map((m) => {
        if (!m) return null;
        const v = new THREE.Vector3(...m.dir).applyQuaternion(planetGroup!.quaternion).normalize();
        return [v.x, v.y, v.z].map((n) => Math.round(n * 1000) / 1000);
      }),
      /** 话题在星球自身坐标系里的位置（判断「不拖动时位置有没有变」用这个） */
      localDirs: (browseSession?.windowMembers() ?? []).map((m) =>
        m ? m.dir.map((v) => Math.round(v * 1000) / 1000) : null,
      ),
      cameraDir: (() => {
        if (!camera || !planetGroup) return [0, 0, 1];
        planetGroup.getWorldPosition(_centerV);
        return _camDirV
          .subVectors(camera.position, _centerV)
          .normalize()
          .toArray()
          .map((v) => Math.round(v * 1000) / 1000);
      })(),
    }),
    /** 把浏览会话的展示窗口同步到画布（窗口内容变化后调用）。 */
    refreshWindow,
    /** 信息密度：0 = 抽象态（球 + 融合环），1 = 完整 Planet（Planet 连续体用） */
    setReveal,
    /** 球体此刻在屏幕上的中心与半径（入口小球与全屏 Planet 的尺度对齐用） */
    sphereScreenRect,
    setTopics: (list: TopicPosition[]) => {
      // 仅暂存列表：星球的话题点由展示窗口决定（见 attachBrowse / PlanetBrowseSession）
      topicsRef.value = list;
    },
  };
}
