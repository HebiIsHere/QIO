import { describe, expect, it } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createRouter, createMemoryHistory, type Router } from "vue-router";
import SettingsFloat from "../SettingsFloat.vue";

const routes = [
  { path: "/", component: { template: "<div>conversation</div>" } },
  { path: "/settings", name: "settings", component: { template: "<div>settings</div>" } },
];

async function mountFloat(): Promise<{ w: ReturnType<typeof mount>; router: Router }> {
  const router = createRouter({ history: createMemoryHistory(), routes });
  await router.push("/");
  await router.isReady();
  const w = mount(SettingsFloat, { global: { plugins: [router] } });
  return { w, router };
}

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
});
