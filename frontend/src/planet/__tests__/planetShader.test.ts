import { describe, expect, it } from "vitest";
import * as THREE from "three";
import { MAX_TOPICS, RING_FRAG, RING_VERT, makeRingUniforms } from "../planetShader";
import { buildTopics } from "../topicData";

describe("planetShader", () => {
  it("RING_FRAG 包含 smin/levelField/ringAlpha 与 3 层环/环波参数", () => {
    expect(RING_FRAG).toContain("float smin");
    expect(RING_FRAG).toContain("levelField");
    expect(RING_FRAG).toContain("ringAlpha");
    expect(RING_FRAG).toContain("acos(clamp(dot(p, uTopics[i]), -1.0, 1.0))");
    expect(RING_FRAG).toContain("uR0");
    expect(RING_FRAG).toContain("uHW2");
    expect(RING_FRAG).toContain("uDim0");
    expect(RING_FRAG).toContain("fwidth(f)");
  });

  it("RING_VERT 透传 vPos", () => {
    expect(RING_VERT).toContain("vPos = position");
    expect(RING_VERT).toContain("gl_Position");
  });

  it("makeRingUniforms 填充到 MAX_TOPICS，占位权重 -100", () => {
    const topics = buildTopics([{ center: new THREE.Vector3(0, 0, 1), n: 3 }]);
    const u = makeRingUniforms(topics, "dark");
    expect(u.uTopics.value).toHaveLength(MAX_TOPICS);
    expect(u.uWeights.value).toHaveLength(MAX_TOPICS);
    expect(u.uCount.value).toBe(3);
    for (let i = 0; i < 3; i++) expect(u.uWeights.value[i]).toBeCloseTo(topics[i].w, 6);
    expect(u.uWeights.value[3]).toBe(-100);
    expect(u.uWeights.value[MAX_TOPICS - 1]).toBe(-100);
    expect(u.uTopics.value[3].length()).toBeCloseTo(1, 5);
  });

  it("主题切换换色：暗 0xe878bd / 亮 0xb0136a", () => {
    const dark = makeRingUniforms([], "dark");
    const light = makeRingUniforms([], "light");
    expect(dark.uRingColor.value.getHex()).toBe(0xe878bd);
    expect(light.uRingColor.value.getHex()).toBe(0xb0136a);
  });

  it("空话题 uniforms 仍合法（uCount=0，无 NaN 占位）", () => {
    const u = makeRingUniforms([], "dark");
    expect(u.uCount.value).toBe(0);
    expect(u.uWeights.value.every((v) => v === -100)).toBe(true);
    expect(u.uDim0.value).toBe(1);
    expect(u.uDim2.value).toBe(1);
    expect(u.uK.value).toBe(0.16);
  });
});