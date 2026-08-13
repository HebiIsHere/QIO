import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { createRouter, createMemoryHistory, type Router } from "vue-router";
import ConversationView from "../ConversationView.vue";
import { useSessionStore } from "../../stores/session";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "",
      topic_name: null,
      anchor_fragment: null,
      messages: [],
    })),
    sendTurn: vi.fn(async () => ({})),
  },
}));

function makeRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/", component: { template: "<div />" } },
      { path: "/settings", component: { template: "<div />" } },
    ],
  });
}

function mountView(pinia: Pinia, router: Router) {
  return mount(ConversationView, {
    global: { plugins: [pinia, router] },
  });
}

beforeEach(() => {
  document.documentElement.removeAttribute("data-theme");
  localStorage.clear();
});

describe("ConversationView 状态条移除与异常提示", () => {
  it("不再渲染顶部状态条（StatusBar 已移除）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountView(pinia, makeRouter());
    await flushPromises();
    expect(w.find(".status-bar").exists()).toBe(false);
    expect(w.find(".settings-float").exists()).toBe(true);
    w.unmount();
  });

  it("有 lastError 时顶部显示异常提示条并可跳设置", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    session.lastError = "credential missing";
    const w = mountView(pinia, makeRouter());
    await flushPromises();
    const hint = w.find(".err-hint");
    expect(hint.exists()).toBe(true);
    expect(hint.text()).toContain("credential missing");
    expect(hint.text()).toContain("前往设置");
    w.unmount();
  });

  it("等待回复时用户消息下方显示三圆点跳动指示，收到助手消息后消失", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountView(pinia, makeRouter());
    await flushPromises();
    const session = useSessionStore();
    session.pushUser("hello");
    session.turnStarted();
    await nextTick();
    expect(w.find(".typing-bubble").exists()).toBe(true);
    expect(w.findAll(".typing-bubble .dot").length).toBe(3);
    session.pushAssistant("final answer");
    session.turnEnded();
    await nextTick();
    expect(w.find(".typing-bubble").exists()).toBe(false);
    w.unmount();
  });

  it("无异常时不显示提示条", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountView(pinia, makeRouter());
    await flushPromises();
    expect(w.find(".err-hint").exists()).toBe(false);
    w.unmount();
  });
});
