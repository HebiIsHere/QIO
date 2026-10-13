/**
 * IRL-E 独立复核（第一阶段）· ResizeHandles.vue 的对抗式用例。
 *
 * 角度差异：不看「能不能发事件」，而是
 * 1) 键盘区与拖动热区互不抢事件；
 * 2) 无障碍边界（Tab 可达 / aria-hidden / role）；
 * 3) 用源码与样式表核对令牌、热区尺寸、方向光标；
 * 4) 两个静态发现：visible 是空操作、−/＋ 面板压住东边热区（后者 jsdom 无法命中测试，标为未验证）。
 */
import { mount } from "@vue/test-utils";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import ResizeHandles from "../ResizeHandles.vue";

// vitest 里 import.meta.url 是 http 的模块地址，读源码要用仓库内的相对路径。
const source = readFileSync(resolve(process.cwd(), "src/components/interactive/ResizeHandles.vue"), "utf8");
const tokens = readFileSync(resolve(process.cwd(), "src/styles/tokens.css"), "utf8");
const style = source.slice(source.indexOf("<style"));

function block(pattern: RegExp): string {
  const match = pattern.exec(style);
  return match ? match[1] : "";
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^$\{\}()|[\]\\]/g, "\\$&");
}

describe("事件归属", () => {
  it("按在 −/＋ 按钮上不会当成拖动（两块热区不抢事件）", async () => {
    const wrapper = mount(ResizeHandles);
    await wrapper.find('[data-resize-key="w+"]').trigger("pointerdown");
    expect(wrapper.emitted("handle-down")).toBeUndefined();
  });

  it("按下角时把事件对象一起交给父组件（父组件要读 clientX / pointerId）", async () => {
    const wrapper = mount(ResizeHandles);
    await wrapper.find('[data-resize-handle="nw"]').trigger("pointerdown");
    const events = wrapper.emitted("handle-down");
    expect(events).toHaveLength(1);
    expect(events?.[0]?.[0]).toBe("nw");
    expect(events?.[0]?.[1]).toBeInstanceOf(Event);
  });

  it("点击 ＋ 只发 nudge，不自己改任何状态", async () => {
    const wrapper = mount(ResizeHandles);
    await wrapper.find('[data-resize-key="h+"]').trigger("click");
    expect(wrapper.emitted("nudge")).toEqual([["h", 1]]);
    expect(wrapper.emitted("handle-down")).toBeUndefined();
  });
});

describe("无障碍边界", () => {
  it("−/＋ 是 Tab 可达的真实按钮，热区不进无障碍树", () => {
    const wrapper = mount(ResizeHandles);
    const keys = wrapper.findAll("[data-resize-key]");
    expect(keys).toHaveLength(4);
    for (const key of keys) {
      expect(key.element.tagName).toBe("BUTTON");
      expect(key.attributes("tabindex")).toBeUndefined();
      expect(key.attributes("aria-label")).toBeTruthy();
    }
    const hits = wrapper.findAll("[data-resize-handle]");
    expect(hits).toHaveLength(8);
    for (const hit of hits) {
      expect(hit.element.tagName).toBe("SPAN");
      expect(hit.attributes("aria-hidden")).toBe("true");
      expect(hit.attributes("tabindex")).toBeUndefined();
    }
    expect(wrapper.find("[data-resize-keys]").attributes("role")).toBe("group");
  });

  it("showKeys=false 只摘掉 −/＋，八向热区照旧", () => {
    const wrapper = mount(ResizeHandles, { props: { showKeys: false } });
    expect(wrapper.findAll("[data-resize-key]")).toHaveLength(0);
    expect(wrapper.findAll("[data-resize-handle]")).toHaveLength(8);
  });

  it("自定义方向子集保持传入顺序（角的压边顺序由调用方决定）", () => {
    const wrapper = mount(ResizeHandles, { props: { handles: ["e", "se", "n"] as const } });
    const order = wrapper.findAll("[data-resize-handle]").map((hit) => hit.attributes("data-resize-handle"));
    expect(order).toEqual(["e", "se", "n"]);
  });

  it("disabled 时既没有热区也没有按钮，且不接受任何事件", async () => {
    const wrapper = mount(ResizeHandles, { props: { disabled: true } });
    expect(wrapper.findAll("[data-resize-handle]")).toHaveLength(0);
    expect(wrapper.findAll("[data-resize-key]")).toHaveLength(0);
    expect(wrapper.find(".rz-root").classes()).toContain("rz-off");
    await wrapper.find(".rz-root").trigger("pointerdown");
    expect(wrapper.emitted("handle-down")).toBeUndefined();
  });
});

describe("样式与令牌（读源码，不靠肉眼）", () => {
  it("没有硬编码色值：十六进制 / rgb / hsl 都没有", () => {
    expect(source).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(source).not.toMatch(/\b(?:rgb|rgba|hsl|hsla)\(/);
  });

  it("引用到的 var(--*) 令牌都在 tokens.css 里定义", () => {
    const used = Array.from(source.matchAll(/var\((--[a-z0-9-]+)/g)).map((match) => match[1]);
    expect(used.length).toBeGreaterThan(0);
    const missing = Array.from(new Set(used)).filter(
      (name) => !new RegExp(escapeRegExp(name) + "\\s*:").test(tokens),
    );
    expect(missing).toEqual([]);
  });

  it("热区尺寸：边 10px、角 16px", () => {
    expect(block(/\.rz-hit-n,\s*\.rz-hit-s\s*\{([^}]*)\}/)).toMatch(/height:\s*10px/);
    expect(block(/\.rz-hit-e,\s*\.rz-hit-w\s*\{([^}]*)\}/)).toMatch(/width:\s*10px/);
    const corner = block(/\.rz-hit-ne,\s*\.rz-hit-nw,\s*\.rz-hit-se,\s*\.rz-hit-sw\s*\{([^}]*)\}/);
    expect(corner).toMatch(/width:\s*16px/);
    expect(corner).toMatch(/height:\s*16px/);
  });

  it("八个方向各有可辨识的光标", () => {
    expect(block(/\.rz-hit-n,\s*\.rz-hit-s\s*\{([^}]*)\}/)).toMatch(/cursor:\s*ns-resize/);
    expect(block(/\.rz-hit-e,\s*\.rz-hit-w\s*\{([^}]*)\}/)).toMatch(/cursor:\s*ew-resize/);
    const corners = style.slice(style.indexOf(".rz-hit-ne"));
    expect(corners).toMatch(/\.rz-hit-ne\s*\{[^}]*cursor:\s*nesw-resize/);
    expect(corners).toMatch(/\.rz-hit-nw\s*\{[^}]*cursor:\s*nwse-resize/);
    expect(corners).toMatch(/\.rz-hit-se\s*\{[^}]*cursor:\s*nwse-resize/);
    expect(corners).toMatch(/\.rz-hit-sw\s*\{[^}]*cursor:\s*nesw-resize/);
  });

  it("prefers-reduced-motion 下不做过渡", () => {
    expect(style).toMatch(/prefers-reduced-motion: reduce/);
  });
});

describe("修复后的静态核对（jsdom 无布局；真实命中测试仍需浏览器，见报告 U1）", () => {
  it("修复后（F5）：.rz-quiet 有了视觉降级规则，visible 不再是空操作", () => {
    const quiet = mount(ResizeHandles, { props: { visible: false } });
    const loud = mount(ResizeHandles, { props: { visible: true } });
    expect(quiet.find(".rz-root").classes()).toContain("rz-quiet");
    expect(loud.find(".rz-root").classes()).not.toContain("rz-quiet");
    expect(style).toMatch(/\.rz-quiet\s+\.rz-mark\s*\{[^}]*opacity/);
    expect(style).toMatch(/\.rz-quiet:hover\s+\.rz-mark/);
  });

  it("修复后（F5）：降级只作用于视觉——八向热区与 −/＋ 一个不少，也没有 display:none", () => {
    const wrapper = mount(ResizeHandles, { props: { visible: false } });
    expect(wrapper.findAll("[data-resize-handle]")).toHaveLength(8);
    expect(wrapper.findAll("[data-resize-key]")).toHaveLength(4);
    expect(style).not.toMatch(/\.rz-quiet[^{]*\{[^}]*display:\s*none/);
    expect(style).not.toMatch(/\.rz-quiet[^{]*\{[^}]*pointer-events:\s*none/);
  });

  it("修复后（W1）：−/＋ 面板避开东 10px、南 10px 热区，也不压 16px 的东南角", () => {
    const keys = block(/\.rz-keys\s*\{([^}]*)\}/);
    const right = Number(/right:\s*(\d+)px/.exec(keys)?.[1] ?? -1);
    const bottom = Number(/bottom:\s*(\d+)px/.exec(keys)?.[1] ?? -1);
    expect(right).toBeGreaterThanOrEqual(10);
    expect(bottom).toBeGreaterThanOrEqual(10);
    // 东南角是 16×16：要完全避开，至少一个方向得让开 16px。
    expect(right >= 16 || bottom >= 16).toBe(true);
  });
});
