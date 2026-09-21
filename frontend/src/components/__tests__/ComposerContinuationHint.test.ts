/**
 * 接续提示要说清「哪一步生效」（阶段 1 的两步语义）。
 *
 * 用户点了「从这里继续」之后：**还没有**创建任何新片段，位置只是停在所选历史处。
 * 界面必须说「下一条消息将从…继续」，而不是让人以为已经建好了新片段；
 * 发送前取消是零成本的（不发消息就不留痕迹）。
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import Composer from "../Composer.vue";
import { useSessionStore } from "../../stores/session";

const cancelContinuation = vi.fn(async () => ({ ok: true, cancelled: true }));
vi.mock("../../services/api", () => ({
  api: {
    cancelContinuation: () => cancelContinuation(),
  },
}));

function mountComposer() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(Composer, { global: { plugins: [pinia] } });
}

beforeEach(() => {
  cancelContinuation.mockClear();
});

afterEach(() => {
  document.body.innerHTML = "";
});

describe("接续提示", () => {
  it("已登记但还没落实时，说明「下一条消息」才生效", () => {
    const w = mountComposer();
    const session = useSessionStore();

    session.setPendingContinuation({ intentId: "intent_1", sourceTitle: "早前讨论：滚动判定口径" });

    return w.vm.$nextTick().then(() => {
      const hint = w.find(".anchor.pending");
      expect(hint.exists()).toBe(true);
      expect(hint.text()).toContain("下一条消息");
      expect(hint.text()).toContain("早前讨论：滚动判定口径");
      // 不暴露 intent_id 这类内部实现术语
      expect(w.text()).not.toContain("intent_1");
      w.unmount();
    });
  });

  it("发送前可以取消，且取消按钮会调用后端（不改本地状态自行猜测）", async () => {
    const w = mountComposer();
    const session = useSessionStore();
    session.setPendingContinuation({ intentId: "intent_1", sourceTitle: "旧片段" });
    await w.vm.$nextTick();

    await w.find(".anchor-cancel").trigger("click");
    await flushPromises();

    expect(cancelContinuation).toHaveBeenCalledTimes(1);
    // 状态由服务端广播决定：本地提示在收到新 ANCHOR 前仍然在
    expect(session.pendingContinuation).not.toBeNull();
    w.unmount();
  });

  it("没有登记时不显示任何接续提示", () => {
    const w = mountComposer();
    return w.vm.$nextTick().then(() => {
      expect(w.find(".anchor.pending").exists()).toBe(false);
      expect(w.find(".anchor-cancel").exists()).toBe(false);
      w.unmount();
    });
  });
});
