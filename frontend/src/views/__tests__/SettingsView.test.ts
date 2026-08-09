import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import SettingsView from "../SettingsView.vue";

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
});
