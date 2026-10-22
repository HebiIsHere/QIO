/**
 * 接续提示要说清「哪一步生效」（阶段 1 的两步语义）。
 *
 * v2 迁移：这条提示从输入区内部移到输入区上方的「从历史继续」入口（契约 §6 入口②）。
 * 用户点了「从这里继续」之后：**还没有**创建任何新片段，位置只是停在所选历史处；
 * 界面必须说「下一条消息将从…继续」，而不是让人以为已经建好了新片段；
 * 发送前取消是零成本的（不发消息就不留痕迹），收起入口不会取消已选位置。
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import ComposerDock from "../conversation/ComposerDock.vue";
import { useSessionStore } from "../../stores/session";

const cancelContinuation = vi.fn(async () => ({ ok: true, cancelled: true }));
vi.mock("../../services/api", () => ({
  api: {
    cancelContinuation: () => cancelContinuation(),
  },
}));

function mountDock() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(ComposerDock, { global: { plugins: [pinia] } });
}

async function openContinuation(w: ReturnType<typeof mountDock>) {
  await w.find('[data-entry="continuation"]').trigger("click");
  await nextTick();
  return w.find(".panel");
}

beforeEach(() => {
  localStorage.clear();
  cancelContinuation.mockClear();
});

afterEach(() => {
  document.body.innerHTML = "";
});

describe("接续提示（入口②：从历史继续）", () => {
  it("已登记但还没落实时，说明「下一条消息」才生效", async () => {
    const w = mountDock();
    const session = useSessionStore();
    session.setPendingContinuation({ intentId: "intent_1", sourceTitle: "早前讨论：滚动判定口径" });
    await nextTick();
    expect(w.find('[data-entry="continuation"]').exists()).toBe(true);
    const panel = await openContinuation(w);
    expect(panel.text()).toContain("下一条消息将从「早前讨论：滚动判定口径」继续");
    // 不暴露 intent_id 这类内部实现术语
    expect(w.text()).not.toContain("intent_1");
    w.unmount();
  });

  it("发送前可以取消，且取消按钮会调用后端（不改本地状态自行猜测）", async () => {
    const w = mountDock();
    const session = useSessionStore();
    cancelContinuation.mockResolvedValueOnce({ ok: false, cancelled: false } as never);
    session.setPendingContinuation({ intentId: "intent_1", sourceTitle: "旧片段" });
    await nextTick();
    const panel = await openContinuation(w);
    await panel.find(".qio-btn").trigger("click");
    await flushPromises();
    expect(cancelContinuation).toHaveBeenCalledTimes(1);
    // 后端没有确认取消：本地提示仍然在（不能自行猜成已取消）
    expect(session.pendingContinuation).not.toBeNull();
    w.unmount();
  });

  it("收起入口不会取消已选位置（收起 ≠ 取消）", async () => {
    const w = mountDock();
    const session = useSessionStore();
    session.setPendingContinuation({ intentId: "intent_1", sourceTitle: "旧片段" });
    await nextTick();
    await openContinuation(w);
    await w.find('[data-entry="continuation"]').trigger("click");
    expect(w.find(".panel").exists()).toBe(false);
    expect(session.pendingContinuation).not.toBeNull();
    expect(cancelContinuation).not.toHaveBeenCalled();
    w.unmount();
  });

  it("没有登记、也不在历史位置时不显示任何接续入口", async () => {
    const w = mountDock();
    await nextTick();
    expect(w.find('[data-entry="continuation"]').exists()).toBe(false);
    w.unmount();
  });
});
