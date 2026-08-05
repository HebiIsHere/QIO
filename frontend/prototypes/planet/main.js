// 星球原型装配：场景 / 球体 / 话题标记 / 相机 / 交互 / 性能

import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { generateTopics } from "./data.js";
import { layoutTopics, RADIUS } from "./layout.js";
import { CameraRig } from "./camera.js";

const canvas = document.getElementById("planet-canvas");
const statusEl = document.getElementById("status");
const detailEl = document.getElementById("detail");
const listEl = document.getElementById("topic-list");

// --- WebGL 可用性与渲染器 ---
let renderer;
let webglOK = false;
try {
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  webglOK = true;
} catch (e) {
  statusEl.textContent = "WebGL 不可用（将走 2D 降级，原型阶段仅提示）";
  statusEl.classList.add("warn");
}
if (webglOK) {
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(window.innerWidth, window.innerHeight);
  const glInfo = renderer.getContext().getParameter(renderer.getContext().RENDERER);
  statusEl.textContent = `WebGL OK · ${glInfo} · 降级档：原型监控中`;
}

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b1020);
const camera = new THREE.PerspectiveCamera(45, window.innerWidth / window.innerHeight, 0.01, 100);
camera.position.set(0, 0, 5.5);

const rig = new CameraRig(camera);
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.enablePan = false;
controls.minDistance = 0.9;
controls.maxDistance = 8;

// --- 光照 ---
scene.add(new THREE.AmbientLight(0x8899cc, 0.6));
const dirLight = new THREE.DirectionalLight(0xffffff, 1.1);
dirLight.position.set(3, 2, 4);
scene.add(dirLight);
const backLight = new THREE.DirectionalLight(0x4466aa, 0.4);
backLight.position.set(-3, -2, -3);
scene.add(backLight);

// --- 星球球体（半透明 + 线框） ---
const sphereMat = new THREE.MeshPhongMaterial({
  color: 0x223a6e,
  transparent: true,
  opacity: 0.35,
  side: THREE.DoubleSide,
});
const sphere = new THREE.Mesh(new THREE.SphereGeometry(RADIUS, 48, 48), sphereMat);
scene.add(sphere);

const wireMat = new THREE.LineBasicMaterial({ color: 0x3a5a9e, transparent: true, opacity: 0.35 });
const wire = new THREE.LineSegments(
  new THREE.WireframeGeometry(new THREE.SphereGeometry(RADIUS * 1.001, 24, 24)),
  wireMat
);
scene.add(wire);

// 星星背景
const stars = new THREE.Points(
  new THREE.BufferGeometry().setFromPoints(
    Array.from({ length: 600 }, () => {
      const v = new THREE.Vector3(
        (Math.random() - 0.5) * 40,
        (Math.random() - 0.5) * 40,
        (Math.random() - 0.5) * 40
      );
      return v;
    })
  ),
  new THREE.PointsMaterial({ color: 0x8899cc, size: 0.03 })
);
scene.add(stars);

// --- 话题标记 ---
function makeLabel(text, color) {
  const c = document.createElement("canvas");
  c.width = 256; c.height = 64;
  const ctx = c.getContext("2d");
  ctx.font = "bold 26px system-ui, sans-serif";
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillStyle = color;
  ctx.fillText(text, 128, 32);
  const tex = new THREE.CanvasTexture(c);
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false });
  const sprite = new THREE.Sprite(mat);
  sprite.scale.set(0.5, 0.125, 1);
  return sprite;
}

const topics = generateTopics();
const positions = layoutTopics(topics);
const markers = [];
const markerMeshes = [];
const groupColors = {};

const PALETTE = [0x5b8def, 0x48c78e, 0xe0a06a, 0xb07ae0, 0x5fc9d6, 0xd96a8a, 0xa8c85b, 0x7a9ec2];
topics.forEach((topic, i) => {
  const pos = positions[i];
  const color = PALETTE[topic.groupIndex % PALETTE.length];
  groupColors[topic.group] = color;

  const mat = new THREE.MeshPhongMaterial({
    color,
    emissive: new THREE.Color(color).multiplyScalar(topic.activity > 0.5 ? 0.25 : 0.05),
  });
  const marker = new THREE.Mesh(new THREE.SphereGeometry(0.035, 12, 12), mat);
  marker.position.copy(pos);
  marker.userData = { topic, index: i };
  scene.add(marker);
  markerMeshes.push(marker);

  const label = makeLabel(topic.name, "#cfe0ff");
  label.position.copy(pos.clone().multiplyScalar(1.08));
  label.userData = { topic, index: i, isLabel: true };
  scene.add(label);
  markers.push(label);
});

// 地区视觉：同组话题之间画淡连接线（呈现聚类）
const lineMat = new THREE.LineBasicMaterial({ color: 0x3a5a9e, transparent: true, opacity: 0.12 });
for (let i = 0; i < topics.length; i++) {
  for (let j = i + 1; j < topics.length; j++) {
    if (topics[i].groupIndex !== topics[j].groupIndex) continue;
    const g = new THREE.BufferGeometry().setFromPoints([positions[i], positions[j]]);
    scene.add(new THREE.Line(g, lineMat));
  }
}

// --- 交互：点击地点 / 标签 → 聚焦 ---
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
let selectedIndex = -1;

function selectTopic(index) {
  selectedIndex = index;
  markers.forEach((m, i) => {
    m.material.color.set(i === index ? 0xffffff : 0xcfe0ff);
  });
  markerMeshes.forEach((m, i) => {
    const base = PALETTE[topics[i].groupIndex % PALETTE.length];
    m.material.emissive.setHex(i === index ? 0xffffff : 0x000000);
    m.material.emissiveIntensity = i === index ? 0.6 : 0;
  });
  // 列表高亮
  document.querySelectorAll("#topic-list li").forEach((li, i) => {
    li.classList.toggle("active", i === index);
  });
  // 详情
  const t = topics[index];
  document.getElementById("d-title").textContent = t.name;
  document.getElementById("d-desc").textContent = t.desc;
  document.getElementById("d-related").textContent =
    `地区：${t.group} · 活跃度 ${t.activity.toFixed(2)}`;
  detailEl.style.display = "block";
}

function focusTopic(index) {
  selectTopic(index);
  rig.cancel();
  rig.go("focus", positions[index], 650);
}

canvas.addEventListener("pointerdown", () => { rig.cancel(); });

canvas.addEventListener("dblclick", (e) => {
  rig.cancel();
  rig.go("planet", null, 500);
  detailEl.style.display = "none";
});

canvas.addEventListener("pointerup", (e) => {
  const rect = canvas.getBoundingClientRect();
  pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObjects(markers);
  if (hits.length > 0) {
    const idx = hits[0].object.userData.index;
    focusTopic(idx);
  }
});

// --- 右侧列表 ---
topics.forEach((topic, i) => {
  const li = document.createElement("li");
  li.innerHTML = `<span>${topic.name}</span><span class="meta">${topic.group}</span>`;
  li.addEventListener("click", () => focusTopic(i));
  listEl.appendChild(li);
});

// --- 视图按钮 ---
document.getElementById("btn-overview").addEventListener("click", () => {
  rig.cancel();
  rig.go("overview", new THREE.Vector3(0, 0, 1), 600);
});
document.getElementById("btn-planet").addEventListener("click", () => {
  rig.cancel();
  rig.go("planet", null, 600);
});
document.getElementById("btn-reset").addEventListener("click", () => {
  rig.cancel();
  controls.reset();
  rig.go("overview", new THREE.Vector3(0, 0, 1), 600);
});

// --- 性能监控 ---
let frames = 0;
let fpsAccum = 0;
let lastStamp = performance.now();
setInterval(() => {
  const now = performance.now();
  const dt = (now - lastStamp) / 1000;
  lastStamp = now;
  const fps = frames / dt;
  frames = 0;
  const state = rig.getState();
  statusEl.textContent =
    `WebGL OK · ${fps.toFixed(0)} fps · 视角: ${state} · ` +
    `话题 ${topics.length} 个（正面=活跃层，旋转可探索背面）`;
}, 2000);

// --- 主循环 ---
const clock = new THREE.Clock();
function animate() {
  requestAnimationFrame(animate);
  const delta = clock.getDelta();
  controls.update();
  rig.update(delta);
  renderer.render(scene, camera);
  frames++;
}
animate();

// --- 窗口缩放 ---
window.addEventListener("resize", () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});