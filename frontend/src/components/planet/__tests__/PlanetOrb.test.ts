/**
 * 入口小球 = 全屏 Planet 的压缩态。
 *
 * 这里不验证像素（jsdom 没有 2D context），验证的是**契约**：
 * 它是 canvas 渲染的压缩态（不是图标）、尺寸受控、活动状态只是一枚安静的点、
 * 并且在拿不到 2D context 的环境里不会崩（否则整个对话页首屏会被拖垮）。
 */
import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import PlanetOrb from "../PlanetOrb.vue";

describe("PlanetOrb（Planet 压缩态）", () => {
  it("用 canvas 渲染，并带压缩态标记（不是图标、不是装饰球）", () => {
    const w = mount(PlanetOrb);
    expect(w.find("[data-compressed-planet]").exists()).toBe(true);
    expect(w.find("canvas.orb-canvas").exists()).toBe(true);
    // 没有 SVG 假环：环来自 canvas 上的距离场
    expect(w.find("svg").exists()).toBe(false);
  });

  it("尺寸由 props 控制（CSS 与画布分辨率分离）", () => {
    const w = mount(PlanetOrb, { props: { size: 104 } });
    const root = w.find("[data-compressed-planet]");
    expect(root.attributes("style")).toContain("104px");
    const canvas = w.find("canvas");
    expect(canvas.attributes("width")).toBe("208");
  });

  it("jsdom 下没有 2D context 也不崩（首屏不能因为画不出球而报错）", () => {
    const w = mount(PlanetOrb);
    expect(w.find("canvas").exists()).toBe(true);
    expect(w.html()).not.toContain("undefined");
  });

  it("运行中只是一枚安静的小点，不是加载动画", () => {
    const idle = mount(PlanetOrb);
    expect(idle.find(".orb-activity").exists()).toBe(false);
    const running = mount(PlanetOrb, { props: { activity: true } });
    expect(running.find(".orb-activity").exists()).toBe(true);
  });

  it("detail 变化不报错（转场阶段 A 会调高它）", async () => {
    const w = mount(PlanetOrb, { props: { detail: 0.2 } });
    await w.setProps({ detail: 1 });
    expect(w.find("canvas").exists()).toBe(true);
  });

  it("接受真实话题方向（压缩态反映当前这个球）", () => {
    const w = mount(PlanetOrb, {
      props: { topics: [{ x: 0, y: 0, z: 1, w: 1 }, { x: 1, y: 0, z: 0, w: 0.9 }] },
    });
    expect(w.find("canvas").exists()).toBe(true);
  });
});
