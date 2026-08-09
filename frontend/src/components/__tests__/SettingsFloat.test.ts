import { describe, expect, it, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createRouter, createMemoryHistory, type Router } from "vue-router";
import SettingsFloat from "../SettingsFloat.vue";

const routes = [
  { path: "/", name: "conversation", component: { template: "<div>conversation</div>" } },
  { path: "/settings", name: "settings", component: { template: "<div>settings</div>" } },
];

async function mountFloat(): Promise<{ w: ReturnType<typeof mount>; router: Router }> {
  const router = createRouter({ history: createMemoryHistory(), routes });
  await router.push("/");
  await router.isReady();
  const w = mount(SettingsFloat, { global: { plugins: [router] } });
  return { w, router };
}

/** 模拟一次拖动：mousedown + document mousemove（>3px）+ document mouseup */
function drag(w: ReturnType<typeof mount>) {
  return w.find(".settings-float").trigger("mousedown", { clientX: 20, clientY: 20 });
}

beforeEach(() => {
  localStorage.clear();
});

describe("SettingsFloat 浮动设置入口", () => {
  it("渲染右上角 ⚙ 按钮（可访问标签）", async () => {
    const { w } = await mountFloat();
    const btn = w.find(".settings-float");
    expect(btn.exists()).toBe(true);
    expect(btn.text()).toContain("⚙");
    expect(btn.attributes("aria-label")).toBe("打开设置");
    w.unmount();
  });

  it("点击 ⚙ 打开整页设置路由", async () => {
    const { w, router } = await mountFloat();
    await w.find(".settings-float").trigger("click");
    await flushPromises();
    expect(router.currentRoute.value.name).toBe("settings");
    w.unmount();
  });

  it("拖动后点击不打开设置（不误触发）", async () => {
    const { w, router } = await mountFloat();
    await drag(w);
    document.dispatchEvent(new MouseEvent("mousemove", { clientX: 120, clientY: 90, bubbles: true }));
    document.dispatchEvent(new MouseEvent("mouseup", { clientX: 120, clientY: 90, bubbles: true }));
    await w.find(".settings-float").trigger("click");
    await flushPromises();
    expect(router.currentRoute.value.name).toBe("conversation");
    w.unmount();
  });

  it("贴角：初始位置为右上角（像素 left/top，position fixed）", async () => {
    const { w } = await mountFloat();
    const btn = w.find(".settings-float").element as HTMLElement;
    expect(btn.style.position).toBe("fixed");
    expect(btn.style.left).toMatch(/^\d+px$/);
    expect(btn.style.top).toBe("18px");
    w.unmount();
  });
});
