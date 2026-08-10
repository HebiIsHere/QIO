import { describe, expect, it, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import MessageItem from "../MessageItem.vue";
import { useEventStore } from "../../stores/events";
import type { StreamMessage } from "../../stores/session";

function makeMessage(over: Partial<StreamMessage> & { role: StreamMessage["role"] }): StreamMessage {
  return {
    id: "m1",
    content: "hi",
    contentType: "text",
    createdAt: "2026-08-09T09:42:11+08:00",
    ...over,
  };
}

function mountItem(msg: StreamMessage, pinia: Pinia) {
  return mount(MessageItem, {
    props: { message: msg },
    global: { plugins: [pinia], stubs: { MarkdownContent: true } },
  });
}

beforeEach(() => {
  document.documentElement.removeAttribute("data-theme");
  localStorage.clear();
});

describe("MessageItem token 下缀", () => {
  it("用户消息下缀与时间戳同一行显示累计 tok", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    events.usageTokens = 1204;
    const w = mountItem(makeMessage({ role: "user" }), pinia);
    expect(w.find(".ts").text()).toMatch(/^\d{2}:\d{2}:\d{2} · tok 1,204$/);
    w.unmount();
  });

  it("助手消息下缀显示当前累计 tok", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    events.usageTokens = 2340;
    const w = mountItem(makeMessage({ role: "assistant", content: "好的" }), pinia);
    expect(w.find(".ts").text()).toMatch(/^\d{2}:\d{2}:\d{2} · tok 2,340$/);
    w.unmount();
  });

  it("无 USAGE 事件时仍保留下缀（tok 0）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(makeMessage({ role: "assistant", content: "好的" }), pinia);
    expect(w.find(".ts").text()).toContain("tok 0");
    w.unmount();
  });

  it("工具卡时间行附带 tok（与时间戳同一行）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    events.usageTokens = 999;
    const w = mountItem(
      makeMessage({ role: "tool", toolName: "shell", content: "{}", toolOk: true }),
      pinia,
    );
    expect(w.find(".tool-time").text()).toMatch(/^\d{2}:\d{2}:\d{2} · tok 999$/);
    w.unmount();
  });
});

  it("interim 助手消息渲染「过程」标签与弱化气泡", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({ role: "assistant", content: "我先查一下仓库", interim: true }),
      pinia,
    );
    expect(w.find(".interim-tag").exists()).toBe(true);
    expect(w.find(".assist-bubble.interim").exists()).toBe(true);
    w.unmount();
  });
