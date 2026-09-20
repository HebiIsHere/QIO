/**
 * 顶部状态行的真实性：
 * * 历史读不到时必须说出来并给重试，而不是显示成「没有历史」；
 * * 取消是安静的正常结局（不是错误横幅）。
 */
import { describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ConversationView from "../ConversationView.vue";
import { useSessionStore } from "../../stores/session";
import { useEventStore } from "../../stores/events";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [],
    })),
    getUISettings: vi.fn(async () => ({ typewriter_cps: 50 })),
  },
}));

function mountView() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(ConversationView, {
    global: {
      plugins: [pinia],
      stubs: { MessageStream: true, Composer: true, SettingsFloat: true },
    },
  });
}

describe("历史读取失败的状态表达", () => {
  it("失败时显示「历史记录暂时无法读取」+ 重试，重试成功后消失", async () => {
    vi.mocked(api.getSessionContext).mockRejectedValueOnce(new Error("boom"));
    const w = mountView();
    await flushPromises();

    const notice = w.find(".notice.quiet");
    expect(notice.exists()).toBe(true);
    expect(notice.text()).toContain("历史记录暂时无法读取");
    expect(notice.text()).toContain("重试");
    // 低干扰：不是错误横幅、也不是弹窗
    expect(w.find(".notice.err").exists()).toBe(false);

    await notice.find("button").trigger("click");
    await flushPromises();
    expect(w.find(".notice.quiet").exists()).toBe(false);
    expect(useSessionStore().history.status).toBe("ready");
    w.unmount();
  });
});

describe("取消的结局表达", () => {
  it("TURN_END(cancelled) 后显示安静的「已停止」，不是错误", async () => {
    const w = mountView();
    await flushPromises();
    const session = useSessionStore();
    const events = useEventStore();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "turn_1" } });
    events.route({
      type: "TURN_END",
      id: "2",
      ts: "",
      data: { turn_id: "turn_1", status: "cancelled", final_content: null },
    });
    await flushPromises();

    expect(session.turnRunning).toBe(false);
    const notice = w.find(".notice.quiet");
    expect(notice.exists()).toBe(true);
    expect(notice.text()).toContain("已停止");
    expect(w.find(".notice.err").exists()).toBe(false);
    w.unmount();
  });
});
