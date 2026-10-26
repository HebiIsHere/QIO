/**
 * 反馈宿主：全局唯一挂载点的接线（弹窗关闭后放行通知、卸载清理监听）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import GlobalFeedbackHost from "../GlobalFeedbackHost.vue";
import { useModalArbiterStore } from "../../../stores/modalArbiter";
import { useNotificationsStore } from "../../../stores/notifications";
import { feedbackGate } from "../../../stores/feedbackGate";

async function tick(): Promise<void> {
  await nextTick();
  await nextTick();
}

describe("GlobalFeedbackHost：只挂一次，自管监听与队列", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
    setActivePinia(createPinia());
  });

  it("中央弹窗打开期间通知排队；弹窗关闭后自动显示；卸载时清理定时器与监听", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const arbiter = useModalArbiterStore();
    const notifications = useNotificationsStore();
    const w = mount(GlobalFeedbackHost, {
      global: { plugins: [pinia] },
      attachTo: document.body,
    });

    arbiter.open({ id: "impact-1", kind: "impact" });
    notifications.enqueue({ key: "setup:1", kind: "setup_incomplete", text: "初始设置尚未完成" });
    await tick();
    expect(notifications.hasActive).toBe(false);

    arbiter.close("impact-1");
    await tick();
    expect(notifications.hasActive).toBe(true);

    expect(feedbackGate.installed).toBe(true);
    w.unmount();
    expect(feedbackGate.installed).toBe(false);
    // 卸载收掉通知计时器与排队内容：不会在宿主消失后继续改状态
    expect(notifications.notices).toHaveLength(0);
  });
});
