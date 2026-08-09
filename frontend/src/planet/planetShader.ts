import * as THREE from "three";
import type { TopicData } from "./topicData";
import { LEVEL_R, LEVEL_A, LEVEL_HW } from "./topicData";

/** 融合环 uniform 数组上限（与 GLSL uTopics[16] 对应） */
export const MAX_TOPICS = 16;

export const RING_VERT = `
  varying vec3 vPos;
  void main(){ vPos = position; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }
`;

/**
 * 球面 SDF 融合环片段着色器，移植自参考原型 planet3d.html（vs-1786250923）：
 * - acos(dot) 球面角距离场；
 * - smin 平滑融合（IQ 公式）；
 * - ringAlpha：fwidth 像素距离恒定线宽 + 抗锯齿；
 * - 3 层环（uR0-2 / uA0-2 / uDim0-2 / uHW0-2），内粗外细 5.0/3.25/2.0。
 */
export const RING_FRAG = `
  uniform vec3 uTopics[16];
  uniform float uWeights[16];
  uniform int uCount;
  uniform vec3 uRingColor;
  uniform float uR0, uR1, uR2;
  uniform float uA0, uA1, uA2;
  uniform float uDim0, uDim1, uDim2;
  uniform float uK;
  uniform float uHW0, uHW1, uHW2;
  varying vec3 vPos;

  float smin(float a, float b, float k){
    float h = clamp(0.5 + 0.5*(b - a)/k, 0.0, 1.0);
    return mix(b, a, h) - k*h*(1.0 - h);
  }
  float levelField(vec3 p, float rL){
    float f = 1e9;
    for (int i = 0; i < 16; i++) {
      if (i >= uCount) break;
      float ang = acos(clamp(dot(p, uTopics[i]), -1.0, 1.0));
      f = smin(f, ang - uWeights[i]*rL, uK);
    }
    return f;
  }
  float ringAlpha(float f, float halfW){
    float g = max(length(vec2(dFdx(f), dFdy(f))), 1e-5);
    float dp = abs(f) / g;          // distance to contour in pixels
    float aa = fwidth(f) / g;       // ~1px edge AA
    return 1.0 - smoothstep(halfW - aa, halfW, dp);
  }
  void main(){
    vec3 p = normalize(vPos);
    float f0 = levelField(p, uR0);
    float f1 = levelField(p, uR1);
    float f2 = levelField(p, uR2);
    float a = uA0*uDim0*ringAlpha(f0, uHW0)
            + uA1*uDim1*ringAlpha(f1, uHW1)
            + uA2*uDim2*ringAlpha(f2, uHW2);
    gl_FragColor = vec4(uRingColor, clamp(a, 0.0, 1.0));
  }
`;

/**
 * 组装融合环 uniforms：话题位置/权重（不足 MAX_TOPICS 用 (0,1,0) 与 -100 占位），
 * 3 层环半径/透明度/像素半宽 + 环波维度 uDim0-2（初始 1）。
 */
export function makeRingUniforms(topics: TopicData[], theme: "dark" | "light") {
  const colors = { dark: new THREE.Color(0xe878bd), light: new THREE.Color(0xb0136a) };
  const list: THREE.Vector3[] = [], w = new Float32Array(MAX_TOPICS);
  topics.forEach((t, i) => { list.push(t.pos); w[i] = t.w; });
  for (let i = topics.length; i < MAX_TOPICS; i++) { list.push(new THREE.Vector3(0, 1, 0)); w[i] = -100; }
  return {
    uTopics: { value: list },
    uWeights: { value: w },
    uCount: { value: topics.length },
    uRingColor: { value: colors[theme] },
    uR0: { value: LEVEL_R[0] }, uR1: { value: LEVEL_R[1] }, uR2: { value: LEVEL_R[2] },
    uA0: { value: LEVEL_A[0] }, uA1: { value: LEVEL_A[1] }, uA2: { value: LEVEL_A[2] },
    uDim0: { value: 1 }, uDim1: { value: 1 }, uDim2: { value: 1 },
    uK: { value: 0.16 },
    uHW0: { value: LEVEL_HW[0] }, uHW1: { value: LEVEL_HW[1] }, uHW2: { value: LEVEL_HW[2] },
  };
}