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

describe("凭据状态提示", () => {
  it("凭据被暂停 / 撤销时，对话页把话说出来并给出恢复入口", async () => {
    const w = mountView();
    await flushPromises();
    const events = useEventStore();

    // 后端在凭据不可用/暂停/撤销时都会送这条文案（store 里早就写了，界面从来没渲染过）
    events.credentialNotice = "有一项凭据已暂停：依赖它的能力暂时不可用（可在「设置 → 凭据」恢复）";
    await flushPromises();

    const notice = w.find(".notice.warn");
    expect(notice.exists()).toBe(true);
    expect(notice.text()).toContain("已暂停");
    expect(notice.text()).toContain("前往设置");

    // 恢复之后这条提示自己消失（不常驻）
    events.credentialNotice = null;
    await flushPromises();
    expect(w.find(".notice.warn").exists()).toBe(false);
    w.unmount();
  });

  it("错误优先于凭据提示：同一时刻只显示最需要处理的那一条", async () => {
    const w = mountView();
    await flushPromises();
    const session = useSessionStore();
    const events = useEventStore();
    session.lastError = "这一轮执行失败：模型调用被拒绝（401）";
    events.credentialNotice = "有一项凭据已失效：依赖它的能力暂时不可用";
    await flushPromises();

    expect(w.find(".notice.err").exists()).toBe(true);
    expect(w.find(".notice.warn").exists()).toBe(false);
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
