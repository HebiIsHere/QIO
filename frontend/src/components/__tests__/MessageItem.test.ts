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

describe("MessageItem 工具卡呈现", () => {
  it("无 presentation 时回退默认模板（工具名 + 原始内容）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({ role: "tool", toolName: "shell", content: "{}", toolOk: true }),
      pinia,
    );
    expect(w.find(".tool-name").text()).toBe("shell");
    expect(w.find(".tool-status").exists()).toBe(false);
    w.find(".tool-head").trigger("click");
    expect(w.find(".tool-detail pre").text()).toBe("{}");
    w.unmount();
  });

  it("有 presentation 时优先渲染 title/status/summary", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({
        role: "tool",
        toolName: "memory_search",
        content: "raw preview",
        toolOk: true,
        presentation: { title: "检索记忆", status: "ok", summary: "命中 3 条" },
      }),
      pinia,
    );
    expect(w.find(".tool-name").text()).toBe("检索记忆");
    expect(w.find(".tool-status").text()).toBe("ok");
    w.find(".tool-head").trigger("click");
    expect(w.find(".tool-detail pre").text()).toBe("命中 3 条");
    w.unmount();
  });

  it("失败工具卡呈现 status 标记为 fail 并显示错误", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({
        role: "tool",
        toolName: "ghost",
        content: "",
        toolOk: false,
        toolError: "unknown tool",
        presentation: { title: "失败工具", status: "fail" },
      }),
      pinia,
    );
    expect(w.find(".tool-name").text()).toBe("失败工具");
    expect(w.find(".tool-status.fail").exists()).toBe(true);
    w.find(".tool-head").trigger("click");
    expect(w.find(".tool-error").text()).toBe("unknown tool");
    w.unmount();
  });
});
