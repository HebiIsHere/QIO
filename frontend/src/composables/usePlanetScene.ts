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
import { backSlotOrder, slotPositions } from "../planet/layoutSlots";
import { DotPool } from "../planet/dotPool";
import { BrowseFlowDriver } from "../planet/browseFlow";
import type { BrowseTopic } from "../planet/browseSession";
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
  let dotMat: THREE.MeshBasicMaterial | null = null;
  /** 话题点对象池：数量固定 = 可见容量，进出只换数据（见 planet/dotPool.ts） */
  let dotPool: DotPool | null = null;
  /** 当前槽位的球面方向（单位球面，由 layoutSlots 按会话种子确定） */
  let slotDirs: THREE.Vector3[] = [];
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
      renderer = new THREE.WebGLRenderer({ canvas: canvas.value, antialias: true });
    } catch {
      webglOK.value = false;
      return;
    }
    webglOK.value = true;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(canvas.value.clientWidth, canvas.value.clientHeight);

    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0f0a10);
    camera = new THREE.PerspectiveCamera(45, canvas.value.clientWidth / canvas.value.clientHeight, 0.01, 100);
    camera.position.set(0, 0, 5.5);

    controls = new OrbitControls(camera, canvas.value);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.enablePan = false;
    controls.minDistance = 0.9;
    controls.maxDistance = 8;

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
      gridGroup.add(new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts), m));
    }
    for (let lon = 0; lon < 360; lon += 60) {
      const pts: THREE.Vector3[] = [];
      for (let i = 0; i <= 64; i++) { const a = (i / 64) * Math.PI; pts.push(new THREE.Vector3(Math.cos(a), Math.sin(a), 0)); }
      const m = gridMat();
      m.opacity = 0.07;
      gridMats.push(m);
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
    slotDirs = slotPositions(session.capacity, options.seed ?? 1);
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
    const items = session.windowSlots();
    dotPool.applyWindow(items, slotDirs);
    dotPool.all().forEach((mesh, slot) => {
      // 已经在窗口里的话题不再重播淡入：只有真正换进来的才做进入动画
      const changed = (mesh.userData.windowTopicId as string | null) !== (items[slot]?.topic_id ?? null);
      if (changed) {
        mesh.userData.enterAt = now;
        mesh.userData.windowTopicId = items[slot]?.topic_id ?? null;
      }
    });
    applyRingUniforms(currentData);
    windowChanged?.();
  }

  /** 单个槽位替换（旋转推动话题流时使用）。 */
  function applySwap(slot: number, topic: BrowseTopic | null, now: number) {
    if (!dotPool) return;
    const mesh = dotPool.place(slot, topic, slotDirs[slot] ?? new THREE.Vector3(0, 0, 1));
    mesh.userData.enterAt = now;
    mesh.userData.windowTopicId = topic?.topic_id ?? null;
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
      const endPos = endTarget.clone().multiplyScalar(RADII[state]);
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
    if (controls) controls.enabled = true;
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
    if (scene) scene.background = new THREE.Color(theme === "dark" ? 0x0f0a10 : 0xf4f1ec);
  }

  function animate() {
    if (paused) {
      loopActive = false;
      return;
    }
    raf = requestAnimationFrame(animate);
    if (!renderer || !scene || !camera || !controls || !planetGroup) return;
    const now = performance.now();
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
    // 聚焦补间期间 slerp 使点居中；空闲自转
    if (focusedDot && targetQuat && tween) {
      planetGroup.quaternion.slerp(targetQuat, Math.min(1, dt * 7));
    } else if (!focusedDot) {
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
          const swap = browseSession.takeSwap(step > 0 ? 1 : -1, backSlots(), now);
          if (swap) applySwap(swap.slot, swap.topic, now);
        }
      }
    }

    // 半球剔除 + 进入淡入：新话题从「远处低透明」自然进入（spec 第 81 条）
    // 数据替换只发生在背面（不可见）槽位，用户看到的是连续世界。
    planetGroup.updateMatrixWorld(true);
    planetGroup.getWorldPosition(_centerV);
    _camDirV.subVectors(camera.position, _centerV);
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
      mat.opacity = k;
      const base = (dot.userData.base as number) ?? 0.028;
      dot.scale.setScalar((0.7 + 0.3 * k) * (base / 0.028));
    }

    // 选中标记跟随选中点（圆环始终正对相机；点转到背面时一并隐藏）
    if (selRing) {
      const dot = focusedDot;
      if (dot && dot.visible) {
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
      const sp = dot && dot.visible && title ? screenPosOf(dot) : null;
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
    renderer.setSize(w, h);
    // 立即重绘：ResizeObserver 回调在 rAF 渲染之后、paint 之前触发，setSize 会清空
    // WebGL 绘图缓冲；若不在同一回调内补一帧，过渡动画期间每一帧画布都是空帧（闪屏）。
    renderer.render(scene, camera);
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
    setPaused,
    primeCamera, pullBack,
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
      windowSize: dotPool?.windowTopicIds().filter(Boolean).length ?? 0,
      id: instanceId,
      hasSession: browseSession !== null,
    }),
    /** 把浏览会话的展示窗口同步到画布（窗口内容变化后调用）。 */
    refreshWindow,
    setTopics: (list: TopicPosition[]) => {
      // 仅暂存列表：星球的话题点由展示窗口决定（见 attachBrowse / PlanetBrowseSession）
      topicsRef.value = list;
    },
  };
}
