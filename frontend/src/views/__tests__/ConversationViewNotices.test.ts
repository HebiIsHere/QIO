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
      stubs: { MessageStream: true, Composer: true },
    },
  });
}

describe("历史读取失败的状态表达", () => {
  it("「还没设置完」提示不再生产挂载在对话列（改由顶部一次性轻提醒承接）", async () => {
    const w = mountView();
    const { useOnboardingStore } = await import("../../stores/onboarding");
    const store = useOnboardingStore();
    store.status = { done: false, hint_dismissed: false } as never;
    await flushPromises();

    // 提醒呈现换了位置，但「未完成/已关闭」的事实仍由 onboarding store 与后端字段表达
    expect(w.find(".setup-hint").exists()).toBe(false);
    expect(store.hintVisible).toBe(true);
    // v2：审批/任务入口移入左栏与输入区上方入口，顶部不再有需要让位的悬浮条，
    // 因此对话页不再写 --qio-top-notes-offset（其他页面仍由 App.vue 使用该变量）。
    expect(
      document.documentElement.style.getPropertyValue("--qio-top-notes-offset"),
    ).toBe("");

    store.status = { done: false, hint_dismissed: true } as never;
    await flushPromises();

    expect(w.find(".setup-hint").exists()).toBe(false);
    expect(store.hintVisible).toBe(false);
    w.unmount();
  });

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

describe("整体异常不再在页面里重复常驻（统一走顶部全局异常入口）", () => {
  it("凭据暂停 / 撤销不再渲染成页内常驻警告；store 字段保留给全局入口", async () => {
    const w = mountView();
    await flushPromises();
    const events = useEventStore();

    events.credentialNotice = "有一项凭据已暂停：依赖它的能力暂时不可用（可在「设置 → 凭据」恢复）";
    await flushPromises();

    // 同一条事实只在一个地方说：页面状态区不再复制一条 .notice.warn
    expect(w.find(".notice.warn").exists()).toBe(false);
    expect(events.credentialNotice, "数据不删除，交给全局异常入口使用").toContain("已暂停");
    w.unmount();
  });

  it("单轮失败仍然留在原位（err），不被整体异常入口吞掉", async () => {
    const w = mountView();
    await flushPromises();
    const session = useSessionStore();
    const events = useEventStore();
    session.lastError = "这一轮执行失败：模型调用被拒绝（401）";
    events.credentialNotice = "有一项凭据已失效：依赖它的能力暂时不可用";
    await flushPromises();

    expect(w.find(".notice.err").exists()).toBe(true);
    expect(w.find(".notice.err").text()).toContain("这一轮执行失败");
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
