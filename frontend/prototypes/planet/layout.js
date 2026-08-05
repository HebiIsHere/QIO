// 球面布局：
// 1) 斐波那契球面均匀散布（初始位置稳定）
// 2) 相似话题松弛聚类：同组话题互相拉近（限制在球面上），
//    迭代数次后收敛，位置固定（位置惰性，永不重排）

import * as THREE from "three";

const RADIUS = 1.0;

function fibonacciSphere(count) {
  const points = [];
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < count; i++) {
    const y = 1 - (i / (count - 1)) * 2;
    const radiusAtY = Math.sqrt(1 - y * y);
    const theta = golden * i;
    points.push(new THREE.Vector3(
      Math.cos(theta) * radiusAtY,
      y,
      Math.sin(theta) * radiusAtY
    ));
  }
  return points;
}

function clampToSphere(v, radius = RADIUS) {
  return v.normalize().multiplyScalar(radius);
}

function similarity(a, b) {
  if (a.groupIndex !== b.groupIndex) return 0;
  return (a.relevance + b.relevance) / 2 * 0.8 + 0.2;
}

function relax(points, topics, iterations = 60, pull = 0.012) {
  const n = topics.length;
  for (let it = 0; it < iterations; it++) {
    const next = points.map((p) => p.clone());
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const sim = similarity(topics[i], topics[j]);
        if (sim <= 0) continue;
        const delta = points[j].clone().sub(points[i]);
        const dist = delta.length();
        if (dist < 1e-6) continue;
        const dir = delta.normalize();
        // 相似节点拉近；距离越远拉力越强（按比例）
        const force = sim * pull * Math.min(1, dist);
        next[i].add(dir.clone().multiplyScalar(force * 0.5));
        next[j].add(dir.clone().multiplyScalar(-force * 0.5));
      }
    }
    points = next.map((p, i) => clampToSphere(p));
  }
  return points;
}

// 将话题按活跃度翻到正面（z > 0 为正面）
function orientActiveFront(points, topics) {
  const n = topics.length;
  // 活跃话题的平均朝向
  const center = new THREE.Vector3();
  let count = 0;
  for (let i = 0; i < n; i++) {
    if (topics[i].activity > 0.5) {
      center.add(points[i]);
      count++;
    }
  }
  if (count > 0) {
    center.normalize();
    // 旋转使 center 指向 +z（正面）
    const quat = new THREE.Quaternion().setFromUnitVectors(center, new THREE.Vector3(0, 0, 1));
    return points.map((p) => p.clone().applyQuaternion(quat));
  }
  return points;
}

function layoutTopics(topics) {
  const points = fibonacciSphere(topics.length);
  const relaxed = relax(points, topics);
  return orientActiveFront(relaxed, topics);
}

export { layoutTopics, RADIUS };