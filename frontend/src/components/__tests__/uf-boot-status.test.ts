/**
 * D · 启动状态（BootStatus）：简洁标识 + 状态；>10 秒才显示「已等待 N 秒」；
 * 技术细节默认折叠；失败显示真实原因与重试，不一直冒充启动中。
 */
import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import BootStatus from "../BootStatus.vue";

describe("BootStatus", () => {
  it("刚开始不显示等待秒数（不到 10 秒不打扰）", () => {
    const w = mount(BootStatus, { props: { state: "starting", waitedSeconds: 3 } });
    expect(w.text()).toContain("正在启动 QIO");
    expect(w.text()).not.toContain("已等待");
    expect(w.find(".boot-retry").exists()).toBe(false);
    w.unmount();
  });

  it("超过 10 秒才显示「已等待 N 秒」", () => {
    const w = mount(BootStatus, { props: { state: "starting", waitedSeconds: 12 } });
    expect(w.text()).toContain("已等待 12 秒");
    w.unmount();
  });

  it("技术细节默认折叠，点开才显示日志路径与原因", async () => {
    const w = mount(BootStatus, {
      props: { state: "failed", waitedSeconds: 61, detail: "后端没有应答（最后一条错误：ECONNREFUSED）" },
    });
    expect(w.find(".boot-details").exists()).toBe(false);
    await w.find(".boot-details-toggle").trigger("click");
    expect(w.find(".boot-details").exists()).toBe(true);
    expect(w.find(".boot-details").text()).toContain("QIO.log");
    w.unmount();
  });

  it("失败：显示真实原因与重试，不显示成「正在启动」", async () => {
    const w = mount(BootStatus, {
      props: { state: "failed", waitedSeconds: 60, detail: "后端没有应答" },
    });
    expect(w.text()).toContain("没有启动完成");
    expect(w.text()).not.toContain("正在启动");
    expect(w.text()).toContain("后端没有应答");
    await w.find(".boot-retry").trigger("click");
    expect(w.emitted("retry")).toBeTruthy();
    w.unmount();
  });
});
