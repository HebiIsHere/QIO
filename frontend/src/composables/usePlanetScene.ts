/**
 * 星球 3D 场景组合式函数：场景创建、话题标记、相机状态、交互。
 * 移植自 prototypes/planet，数据源改为后端 API。
 */
import { onScopeDispose, ref, shallowRef } from "vue";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import type { TopicPosition } from "../services/api";

export type CameraState = "overview" | "planet" | "focus";

const RADIUS = 1.0;
const PALETTE = [0x5b8def, 0x48c78e, 0xe0a06a, 0xb07ae0, 0x5fc9d6, 0xd96a8a, 0xa8c85b, 0x7a9ec2];

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
  let markerMeshes: THREE.Mesh[] = [];
  let labelSprites: THREE.Sprite[] = [];
  let raf = 0;
  let frameCount = 0;
  let lastFpsTime = performance.now();
  let animatingCamera = false;

  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();

  function makeLabel(text: string): THREE.Sprite {
    const c = document.createElement("canvas");
    c.width = 256; c.height = 64;
    const ctx = c.getContext("2d");
    if (ctx) {
      ctx.font = "bold 26px system-ui, sans-serif";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillStyle = "#efe2ee";
      ctx.fillText(text, 128, 32);
    }
    const tex = new THREE.CanvasTexture(c);
    const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false });
    const sprite = new THREE.Sprite(mat);
    sprite.scale.set(0.5, 0.125, 1);
    return sprite;
  }

  function init() {
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

    scene.add(new THREE.AmbientLight(0xcc99bb, 0.6));
    const dir = new THREE.DirectionalLight(0xffffff, 1.1);
    dir.position.set(3, 2, 4);
    scene.add(dir);

    const sphere = new THREE.Mesh(
      new THREE.SphereGeometry(RADIUS, 48, 48),
      new THREE.MeshPhongMaterial({ color: 0x521f45, transparent: true, opacity: 0.35, side: THREE.DoubleSide }),
    );
    scene.add(sphere);
    const wire = new THREE.LineSegments(
      new THREE.WireframeGeometry(new THREE.SphereGeometry(RADIUS * 1.001, 24, 24)),
      new THREE.LineBasicMaterial({ color: 0x8a2f6b, transparent: true, opacity: 0.35 }),
    );
    scene.add(wire);

    animate();
  }

  function loadTopics(topics: TopicPosition[]) {
    const sc = scene;
    if (!sc) return;
    markerMeshes.forEach((m) => sc.remove(m));
    markerMeshes = [];
    labelSprites.forEach((s) => sc.remove(s));
    labelSprites = [];
    const labels: THREE.Sprite[] = [];
    topics.forEach((topic, i) => {
      const pos = new THREE.Vector3(...topic.position);
      const color = PALETTE[i % PALETTE.length];
      const mat = new THREE.MeshPhongMaterial({ color, emissive: new THREE.Color(color).multiplyScalar(topic.activity > 0.5 ? 0.25 : 0.05) });
      const marker = new THREE.Mesh(new THREE.SphereGeometry(0.035, 12, 12), mat);
      marker.position.copy(pos);
      marker.userData = { topicId: topic.topic_id, index: i };
      sc.add(marker);
      markerMeshes.push(marker);
      const label = makeLabel(topic.name);
      label.position.copy(pos.clone().multiplyScalar(1.08));
      label.userData = { topicId: topic.topic_id, index: i };
      sc.add(label);
      labels.push(label);
      labelSprites.push(label);
    });
    markers.value = markerMeshes;
  }

  async function go(state: CameraState, target?: THREE.Vector3 | null, duration = 650) {
    if (!camera) return;
    animatingCamera = true;
    const radii: Record<CameraState, number> = { overview: 5.5, planet: 2.6, focus: 1.6 };
    const endTarget = target?.clone().normalize() ?? new THREE.Vector3(0, 0, 1);
    const endPos = endTarget.clone().multiplyScalar(radii[state]);
    const startPos = camera.position.clone();
    const t0 = performance.now();
    await new Promise<void>((resolve) => {
      const step = (now: number) => {
        if (!camera) { resolve(); return; }
        let t = (now - t0) / duration;
        if (t >= 1) t = 1;
        const e = 1 - Math.pow(1 - t, 3);
        camera.position.lerpVectors(startPos, endPos, e);
        camera.lookAt(endTarget);
        if (t < 1) {
          requestAnimationFrame(step);
        } else {
          animatingCamera = false;
          cameraState.value = state;
          resolve();
        }
      };
      requestAnimationFrame(step);
    });
  }

  function cancelAnimation() {
    animatingCamera = false;
  }

  function focusTopic(topicId: string, topics: TopicPosition[]) {
    const topic = topics.find((t) => t.topic_id === topicId);
    if (!topic || !camera) return;
    selectedTopicId.value = topicId;
    markerMeshes.forEach((m, i) => {
      const isSelected = topics[i]?.topic_id === topicId;
      const mat = m.material as THREE.MeshPhongMaterial;
      // 选中标记用自身颜色发光，避免白色自发光糊成白球
      mat.emissive.copy(isSelected ? mat.color : new THREE.Color(0x000000));
      mat.emissiveIntensity = isSelected ? 0.8 : 0;
    });
    go("focus", new THREE.Vector3(...topic.position));
  }

  function handleClick(clientX: number, clientY: number) {
    if (!camera || !renderer || !canvas.value) return;
    const rect = canvas.value.getBoundingClientRect();
    pointer.x = ((clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObjects(markerMeshes);
    if (hits.length > 0) {
      const topicId = hits[0].object.userData.topicId as string;
      focusTopic(topicId, topicsRef.value);
    }
  }

  const topicsRef = ref<TopicPosition[]>([]);

  function animate() {
    raf = requestAnimationFrame(animate);
    if (renderer && scene && camera && controls) {
      controls.update();
      // 标签保持恒定屏幕尺寸：按相机距离缩放
      for (const label of labelSprites) {
        const d = camera.position.distanceTo(label.position);
        const s = Math.max(d * 0.09, 0.02);
        label.scale.set(s, s * 0.25, 1);
      }
      if (!animatingCamera && cameraState.value === "focus") {
        // keep looking at selected target while idle in focus mode
      }
      renderer.render(scene, camera);
      frameCount++;
      const now = performance.now();
      if (now - lastFpsTime >= 2000) {
        fps.value = Math.round((frameCount * 1000) / (now - lastFpsTime));
        frameCount = 0;
        lastFpsTime = now;
      }
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
    renderer?.dispose();
    renderer = null;
    scene = null;
    camera = null;
    controls = null;
  });

  return {
    webglOK, fps, cameraState, selectedTopicId,
    init, loadTopics, go, focusTopic, handleClick, cancelAnimation, resize,
    setTopics: (list: TopicPosition[]) => { topicsRef.value = list; },
  };
}