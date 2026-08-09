/**
 * 星球 3D 场景组合式函数：透明球 + 球面 SDF 融合环 + 聚焦/环波交互。
 * 移植自参考原型 .superpowers/brainstorm/vs-1786250923/content/planet3d.html，
 * 数据源改为后端 API（按位置聚簇 → buildTopics 生成话题点）。
 * 对外接口（cameraState/selectedTopicId/fps/webglOK/markers 与 init/loadTopics/go/
 * focusTopic/handleClick/cancelAnimation/resize/setTopics）保持不变，新增 setTheme。
 */
import { onScopeDispose, ref, shallowRef } from "vue";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import type { TopicPosition } from "../services/api";
import { buildTopics, type TopicData } from "../planet/topicData";
import { MAX_TOPICS, RING_FRAG, RING_VERT, makeRingUniforms } from "../planet/planetShader";

export type CameraState = "overview" | "planet" | "focus";

const RADIUS = 1.0;
const DOT_RADIUS = 1.004;
/** 话题位置聚簇合并阈值（球面角距离，弧度） */
const CLUSTER_ANG = 0.5;
const RADII: Record<CameraState, number> = { overview: 5.5, planet: 2.6, focus: 2.25 };
const THEME_LINE: Record<"dark" | "light", number> = { dark: 0xe878bd, light: 0xb0136a };
/** 话题点兜底材质：Canvas 贴图不可用时复用（模块级单例，避免反复创建/泄漏） */
const DOT_FALLBACK_MATERIAL = new THREE.MeshBasicMaterial({ color: 0xc51b7d });

export function usePlanetScene(canvas: { value: HTMLCanvasElement | null }) {
  const webglOK = ref(false);
  const fps = ref(0);
  const cameraState = ref<CameraState>("overview");
  const selectedTopicId = ref<string | null>(null);
  const markers = shallowRef<THREE.Mesh[]>([]);

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
  let currentData: TopicData[] = [];
  let currentTheme: "dark" | "light" = "dark";

  let dotMeshes: THREE.Mesh[] = [];
  let dotByTopicId = new Map<string, THREE.Mesh>();
  let raf = 0;
  let frameCount = 0;
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
  let pointerHandlers: { down: (e: PointerEvent) => void; up: (e: PointerEvent) => void; cancel: () => void } | null = null;

  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  const _q = new THREE.Quaternion();
  const _worldDir = new THREE.Vector3();
  const _centerV = new THREE.Vector3();
  const _camDirV = new THREE.Vector3();
  const tmpV = new THREE.Vector3();

  const topicsRef = ref<TopicPosition[]>([]);

  const easeInOutCubic = (x: number) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);

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

  /** 将真实话题位置按球面角距离贪心聚簇，返回簇（中心 + 数量 + 成员 topic_id）。 */
  function buildClusters(topics: TopicPosition[]) {
    interface Cluster { center: THREE.Vector3; n: number; members: string[]; }
    const clusters: Cluster[] = [];
    for (const t of topics) {
      const p = new THREE.Vector3(...t.position).normalize();
      let best: Cluster | null = null;
      let bestAng = Infinity;
      for (const cl of clusters) {
        const ang = Math.acos(Math.max(-1, Math.min(1, p.dot(cl.center))));
        if (ang < bestAng) { bestAng = ang; best = cl; }
      }
      if (best && bestAng < CLUSTER_ANG) {
        best.members.push(t.topic_id);
        best.n += 1;
        best.center.add(p).normalize();
      } else {
        clusters.push({ center: p.clone(), n: 1, members: [t.topic_id] });
      }
    }
    return clusters;
  }

  function applyRingUniforms(data: TopicData[]) {
    if (!ringMat) return;
    ringUniforms = makeRingUniforms(data, currentTheme);
    ringMat.uniforms = ringUniforms;
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
    const gridMat = () => new THREE.LineBasicMaterial({ color: THEME_LINE[currentTheme], transparent: true, opacity: 0.2, depthWrite: false });
    for (let lat = -60; lat <= 60; lat += 30) {
      const r = Math.cos((lat * Math.PI) / 180), y = Math.sin((lat * Math.PI) / 180);
      const pts: THREE.Vector3[] = [];
      for (let i = 0; i <= 96; i++) { const a = (i / 96) * Math.PI * 2; pts.push(new THREE.Vector3(Math.cos(a) * r, y, Math.sin(a) * r)); }
      const m = gridMat();
      m.opacity = lat === 0 ? 0.35 : 0.18;
      gridMats.push(m);
      gridGroup.add(new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts), m));
    }
    for (let lon = 0; lon < 360; lon += 45) {
      const pts: THREE.Vector3[] = [];
      for (let i = 0; i <= 64; i++) { const a = (i / 64) * Math.PI; pts.push(new THREE.Vector3(Math.cos(a), Math.sin(a), 0)); }
      const m = gridMat();
      m.opacity = 0.13;
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
    pointerHandlers = { down: onPointerDown, up: onPointerUp, cancel: onPointerCancel };
    canvas.value.addEventListener("pointerdown", onPointerDown);
    canvas.value.addEventListener("pointerup", onPointerUp);
    canvas.value.addEventListener("pointercancel", onPointerCancel);

    animate();
  }

  /**
   * 加载话题位置并重建星球话题点（圆点 + 融合环 uniforms）。
   * v1 限制：星球仅渲染前 MAX_TOPICS(16) 个话题（球面融合环 uniform 数组上限），
   * 超出部分不生成圆点，但侧栏话题列表仍展示全部话题。
   */
  function loadTopics(topics: TopicPosition[]) {
    const pg = planetGroup;
    if (!pg) return;
    topicsRef.value = topics;
    dotMeshes.forEach((m) => { pg.remove(m); m.geometry.dispose(); });
    dotMeshes = [];
    dotByTopicId.clear();
    focusedDot = null;
    targetQuat = null;
    if (!topics.length) {
      currentData = [];
      applyRingUniforms(currentData);
      markers.value = [];
      return;
    }
    const clusters = buildClusters(topics);
    const orderedIds: string[] = [];
    clusters.forEach((cl) => cl.members.forEach((id) => orderedIds.push(id)));
    currentData = buildTopics(clusters.map((cl) => ({ center: cl.center, n: cl.n }))).slice(0, MAX_TOPICS);
    applyRingUniforms(currentData);
    currentData.forEach((td, i) => {
      const topicId = orderedIds[i];
      const base = 0.028 * td.w;
      const dot = new THREE.Mesh(new THREE.CircleGeometry(base, 48), dotMat ?? DOT_FALLBACK_MATERIAL);
      dot.position.copy(td.pos).multiplyScalar(DOT_RADIUS);
      dot.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), td.pos.clone().normalize());
      dot.userData = { topicId, ci: td.ci, base };
      dot.renderOrder = 5;
      pg.add(dot);
      dotMeshes.push(dot);
      if (topicId) dotByTopicId.set(topicId, dot);
    });
    markers.value = dotMeshes;
  }

  function beginTween(from: THREE.Vector3, to: THREE.Vector3, dur: number, done?: () => void) {
    if (controls) controls.enabled = false;
    // 覆盖旧补间时先完成旧 done 回调：go/focusTopic 被打断时其 promise 仍能 resolve，
    // 避免 close() 的 await planet.go(...) 因 tween 被覆盖而永不完成（界面卡死）。
    if (tween?.done) tween.done();
    tween = { from, to, t: 0, dur, done };
  }

  function go(state: CameraState, target?: THREE.Vector3 | null, duration = 700): Promise<void> {
    return new Promise((resolve) => {
      if (!camera) { resolve(); return; }
      const endTarget = target?.clone().normalize() ?? new THREE.Vector3(0, 0, 1);
      const endPos = endTarget.clone().multiplyScalar(RADII[state]);
      focusedDot = null;
      targetQuat = null;
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

  /** 聚焦话题：相机 tween 到 方向*2.25（easeInOutCubic）+ 星球 slerp 使点居中 + 环波。 */
  function focusTopic(topicId: string, topics: TopicPosition[]) {
    if (!camera || !planetGroup) return;
    const dot = dotByTopicId.get(topicId);
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
    beginTween(camera.position.clone(), _camDirV.clone().multiplyScalar(RADII.focus), 800);
    waveStart = performance.now(); // 环波
  }

  /**
   * 画布点击：仅接受 pointerup 判定为有效点击的命中（拖拽/长按/指针取消不触发聚焦）。
   * Raycaster 不检查 object.visible，背面（半球剔除后 visible=false）的话题点会
   * 在透明球上被误命中，因此这里只取 visible === true 的命中。
   */
  function handleClick(clientX: number, clientY: number) {
    if (!camera || !renderer || !canvas.value) return;
    if (!pendingClick) return;
    pendingClick = false;
    const rect = canvas.value.getBoundingClientRect();
    pointer.x = ((clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObjects(dotMeshes, true);
    const hit = hits.find((h) => h.object.visible === true);
    if (hit) {
      const topicId = hit.object.userData.topicId as string | undefined;
      if (topicId) focusTopic(topicId, topicsRef.value);
    }
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
    if (fillMat) fillMat.color.setHex(theme === "dark" ? 0x2a2130 : 0xffffff);
    if (ringMat) applyRingUniforms(currentData);
    if (scene) scene.background = new THREE.Color(theme === "dark" ? 0x0f0a10 : 0xf4f1ec);
  }

  function animate() {
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

    // 半球剔除：点在星球背面则隐藏
    planetGroup.updateMatrixWorld(true);
    planetGroup.getWorldPosition(_centerV);
    _camDirV.subVectors(camera.position, _centerV);
    for (const dot of dotMeshes) {
      dot.getWorldPosition(tmpV);
      dot.visible = tmpV.sub(_centerV).dot(_camDirV) >= 0;
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

  function resize() {
    if (!camera || !renderer || !canvas.value) return;
    camera.aspect = canvas.value.clientWidth / canvas.value.clientHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(canvas.value.clientWidth, canvas.value.clientHeight);
  }

  onScopeDispose(() => {
    cancelAnimationFrame(raf);
    if (pointerHandlers && canvas.value) {
      canvas.value.removeEventListener("pointerdown", pointerHandlers.down);
      canvas.value.removeEventListener("pointerup", pointerHandlers.up);
      canvas.value.removeEventListener("pointercancel", pointerHandlers.cancel);
    }
    renderer?.dispose();
    renderer = null;
    scene = null;
    camera = null;
    controls = null;
    planetGroup = null;
  });

  return {
    webglOK, fps, cameraState, selectedTopicId, markers,
    init, loadTopics, go, focusTopic, handleClick, cancelAnimation, resize, setTheme,
    setTopics: (list: TopicPosition[]) => {
      // 仅暂存列表供 handleClick/focusTopic 查找；星球渲染上限见 loadTopics（MAX_TOPICS=16）
      topicsRef.value = list;
    },
  };
}