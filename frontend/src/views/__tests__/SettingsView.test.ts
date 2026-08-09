import { describe, expect, it, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import SettingsView from "../SettingsView.vue";
import { getTheme, setTheme } from "../../utils/theme";

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});

describe("SettingsView", () => {
  it("渲染凭据/偏好两个 tab 与表单控件（QInput/QSelect）", async () => {
    const w = mount(SettingsView, { global: { stubs: { RouterLink: true } } });
    await nextTick();
    expect(w.findAll(".tab").length).toBe(2);
    expect(w.find(".tab.active").text()).toBe("凭据");
    expect(w.find(".qio-input").exists()).toBe(true);
    expect(w.find(".qio-select").exists()).toBe(true);
    expect(w.find(".create").text()).toBe("创建凭据");
    w.unmount();
  });
  it("点击偏好 tab 显示偏好分区", async () => {
    const w = mount(SettingsView, { global: { stubs: { RouterLink: true } } });
    await w.findAll(".tab")[1].trigger("click");
    expect(w.find(".tab.active").text()).toBe("偏好");
    expect(w.find(".qio-switch").exists()).toBe(true);
    w.unmount();
  });
  it("空密钥创建被拦截并提示", async () => {
    const w = mount(SettingsView, { global: { stubs: { RouterLink: true } } });
    await nextTick();
    await w.find(".create").trigger("click");
    await nextTick();
    expect(w.find(".msg.err").text()).toContain("请先粘贴 API Key");
    w.unmount();
  });
  it("偏好页主题开关切换主题并持久化 data-theme/qio-theme", async () => {
    // 模拟 main.ts 启动时应用持久化主题（组件不再兜底写 data-theme）
    setTheme(getTheme());
    const w = mount(SettingsView, { global: { stubs: { RouterLink: true } } });
    await w.findAll(".tab")[1].trigger("click");
    const sw = w.find(".theme-switch");
    expect(sw.exists()).toBe(true);
    // 默认暗色
    expect(document.documentElement.dataset.theme).toBe("dark");
    await sw.trigger("click");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem("qio-theme")).toBe("light");
    await sw.trigger("click");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("qio-theme")).toBe("dark");
    w.unmount();
  });
});
