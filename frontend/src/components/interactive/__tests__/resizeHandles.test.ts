/**
 * 尺寸调整标记用例（互动模式契约 2026-10-13 · IRL §2 / 任务书 §3.2）。
 *
 * 锁定的行为：
 * - 八个方向的热区都在，角比边大（更好点）；
 * - 右下角一个克制的标记；
 * - − / ＋ 是真正的 <button>，键盘可达，点击只发事件、不改任何状态；
 * - disabled 时既不拖动也不给按钮。
 */
import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import ResizeHandles from "../ResizeHandles.vue";
import { RESIZE_HANDLES } from "../../../interactive/resize";

describe("ResizeHandles：热区", () => {
  it("八个方向的热区都在", () => {
    const wrapper = mount(ResizeHandles);
    const hits = wrapper.findAll("[data-resize-handle]");
    expect(hits).toHaveLength(8);
    expect(hits.map((hit) => hit.attributes("data-resize-handle"))).toEqual([...RESIZE_HANDLES]);
  });

  it("按下热区把方向与事件发出去", async () => {
    const wrapper = mount(ResizeHandles);
    await wrapper.find('[data-resize-handle="se"]').trigger("pointerdown");
    const events = wrapper.emitted("handle-down");
    expect(events).toHaveLength(1);
    expect(events?.[0]?.[0]).toBe("se");
  });

  it("disabled 时不渲染热区，也不发事件", () => {
    const wrapper = mount(ResizeHandles, { props: { disabled: true } });
    expect(wrapper.findAll("[data-resize-handle]")).toHaveLength(0);
    expect(wrapper.emitted("handle-down")).toBeUndefined();
  });

  it("自定义方向子集生效", () => {
    const wrapper = mount(ResizeHandles, { props: { handles: ["se"] as const } });
    expect(wrapper.findAll("[data-resize-handle]")).toHaveLength(1);
  });

  /*
    指针隔离（本轮滚动 / 手势规则）：同一个指针操作不得同时触发两种手势。
    宿主（卡片 / 对话窗口 / 板面）都在外层监听 pointerdown 做拖动 ——
    尺寸把手必须在本层就把 pointerdown 止住，否则「拉右下角」会同时开始拖卡片。
  */
  it("按在热区上时 pointerdown 不冒泡给宿主（不会同时触发拖动）", async () => {
    const host = document.createElement("div");
    const seen: string[] = [];
    host.addEventListener("pointerdown", () => seen.push("host"));
    document.body.appendChild(host);
    const wrapper = mount(ResizeHandles, { attachTo: host });
    await wrapper.find('[data-resize-handle="se"]').trigger("pointerdown");
    expect(wrapper.emitted("handle-down")).toHaveLength(1);
    expect(seen).toEqual([]);
    wrapper.unmount();
    host.remove();
  });

  it("按在 − / ＋ 按钮上同样不冒泡（键盘按钮不启动宿主拖动）", async () => {
    const host = document.createElement("div");
    const seen: string[] = [];
    host.addEventListener("pointerdown", () => seen.push("host"));
    document.body.appendChild(host);
    const wrapper = mount(ResizeHandles, { attachTo: host });
    await wrapper.find('[data-resize-key="w+"]').trigger("pointerdown");
    expect(seen).toEqual([]);
    wrapper.unmount();
    host.remove();
  });
});

describe("ResizeHandles：键盘 −/＋", () => {
  it("宽 / 高各有一对真实按钮，带可读的无障碍名称", () => {
    const wrapper = mount(ResizeHandles, { props: { label: "卡片尺寸" } });
    const keys = wrapper.findAll("[data-resize-key]");
    expect(keys.map((key) => key.attributes("data-resize-key"))).toEqual(["w-", "w+", "h-", "h+"]);
    expect(keys[0].attributes("aria-label")).toContain("减小宽度");
    expect(keys[3].attributes("aria-label")).toContain("增大高度");
    for (const key of keys) {
      expect(key.element.tagName).toBe("BUTTON");
      expect(key.attributes("type")).toBe("button");
    }
  });

  it("点击只发 nudge，不自己改尺寸", async () => {
    const wrapper = mount(ResizeHandles);
    await wrapper.find('[data-resize-key="w-"]').trigger("click");
    await wrapper.find('[data-resize-key="h+"]').trigger("click");
    expect(wrapper.emitted("nudge")).toEqual([
      ["w", -1],
      ["h", 1],
    ]);
  });

  it("disabled 时不渲染按钮", () => {
    const wrapper = mount(ResizeHandles, { props: { disabled: true } });
    expect(wrapper.findAll("[data-resize-key]")).toHaveLength(0);
  });
});

describe("ResizeHandles：克制的视觉", () => {
  it("右下角标记默认渲染，可以关掉", () => {
    expect(mount(ResizeHandles).find(".rz-mark").exists()).toBe(true);
    expect(mount(ResizeHandles, { props: { showMark: false } }).find(".rz-mark").exists()).toBe(false);
  });

  it("visible=false 只降视觉强调，不摘掉键盘可达性", () => {
    const wrapper = mount(ResizeHandles, { props: { visible: false } });
    expect(wrapper.find(".rz-root").classes()).toContain("rz-quiet");
    expect(wrapper.findAll("[data-resize-key]")).toHaveLength(4);
  });

  it("不使用硬编码色值（只走设计令牌）", () => {
    const wrapper = mount(ResizeHandles);
    const css = wrapper.html();
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
  });
});
