import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import PlanetDock from "../PlanetDock.vue";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] })),
    sendTurn: vi.fn(async () => ({})),
  },
}));

async function mountDock() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mount(PlanetDock, { global: { plugins: [pinia] } });
  await nextTick(); // 等浮动系统初始化（flush: post）
  return w;
}

beforeEach(() => {
  localStorage.clear();
});

describe("PlanetDock 话题星球入口", () => {
  it("渲染悬浮球按钮（可访问标签）", async () => {
    const w = await mountDock();
    const dock = w.find(".dock");
    expect(dock.exists()).toBe(true);
    expect(dock.attributes("aria-label")).toBe("打开话题星球");
    w.unmount();
  });

  it("点击展开话题星球（open 事件）", async () => {
    const w = await mountDock();
    await w.find(".dock").trigger("click");
    expect(w.emitted("open")).toBeTruthy();
    w.unmount();
  });

  it("拖动后点击不触发 open（不误触发）", async () => {
    const w = await mountDock();
    await w.find(".dock").trigger("mousedown", { clientX: 40, clientY: 40 });
    document.dispatchEvent(new MouseEvent("mousemove", { clientX: 160, clientY: 120, bubbles: true }));
    document.dispatchEvent(new MouseEvent("mouseup", { clientX: 160, clientY: 120, bubbles: true }));
    await w.find(".dock").trigger("click");
    expect(w.emitted("open")).toBeFalsy();
    w.unmount();
  });

  it("贴边：初始位置为右侧居中（像素 left/top，position fixed）", async () => {
    const w = await mountDock();
    const dock = w.find(".dock").element as HTMLElement;
    expect(dock.style.position).toBe("fixed");
    expect(dock.style.left).toMatch(/^\d+px$/);
    expect(dock.style.top).toMatch(/^\d+px$/);
    w.unmount();
  });
});


