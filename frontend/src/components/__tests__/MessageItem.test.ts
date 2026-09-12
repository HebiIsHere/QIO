import { describe, expect, it, beforeEach, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import MessageItem from "../MessageItem.vue";
import { useEventStore } from "../../stores/events";
import { useUiStore } from "../../stores/ui";
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

describe("MessageItem 用量归属（问题3）", () => {
  it("正常模式不显示 token（宁可隐藏，也不显示错误数字）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    events.usageByTurn = { turn_1: { tokens: 100 } };
    const w = mountItem(makeMessage({ role: "assistant", content: "好的", turnId: "turn_1" }), pinia);
    expect(w.find(".meta").text()).not.toContain("tok");
    w.unmount();
  });

  it("开发者模式：助手消息显示自己 turn 的 token，而不是最近一次的累计值", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const ui = useUiStore();
    ui.developerMode = true;
    events.usageByTurn = { turn_1: { tokens: 100 }, turn_2: { tokens: 200 } };
    const w = mountItem(makeMessage({ role: "assistant", content: "A", turnId: "turn_1" }), pinia);
    expect(w.find(".meta").text()).toContain("token 100");
    w.unmount();
  });

  it("用户消息与无归属消息不带 token", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useUiStore().developerMode = true;
    useEventStore().usageByTurn = { turn_1: { tokens: 100 } };
    const user = mountItem(makeMessage({ role: "user", content: "问", turnId: "turn_1" }), pinia);
    expect(user.find(".meta").text()).not.toContain("tok");
    user.unmount();
    const orphan = mountItem(makeMessage({ role: "assistant", content: "A", turnId: null }), pinia);
    expect(orphan.find(".meta").text()).not.toContain("tok");
    orphan.unmount();
  });
});

describe("MessageItem 消息信息层级与状态", () => {
  it("等待中的消息只给一行轻量 metadata（等待中）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(makeMessage({ role: "user", content: "下一条", queued: true }), pinia);
    expect(w.find(".queued-tag").text()).toContain("等待中");
    w.unmount();
  });

  it("复制按钮：点击复制内容并给出短暂成功反馈", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const w = mountItem(makeMessage({ role: "assistant", content: "复制我" }), pinia);
    await w.find(".copy-btn").trigger("click");
    expect(writeText).toHaveBeenCalledWith("复制我");
    expect(w.find(".copy-btn").text()).toContain("已复制");
    w.unmount();
  });
});

describe("MessageItem 工具卡呈现", () => {
  it("工具卡标题用中文展示名，原始工具名只放在悬停提示里", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({
        role: "tool",
        toolName: "web_search",
        content: "…",
        toolOk: false,
        presentation: { title: "网络搜索", tool: "web_search", summary: "联网搜索暂不可用" },
      }),
      pinia,
    );
    expect(w.find(".tool-name").text()).toBe("网络搜索");
    expect(w.find(".tool-card").attributes("title")).toBe("web_search");
    expect(w.find(".tool-name").text()).not.toContain("web_search");
    w.unmount();
  });

  it("开发者模式下 token 单位用完整单词（中文界面不留 tok 缩写）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const ui = useUiStore();
    ui.developerMode = true;
    const events = useEventStore();
    events.usageByTurn = { turn_1: { tokens: 1204 } };
    const w = mountItem(makeMessage({ role: "assistant", content: "答", turnId: "turn_1" }), pinia);
    expect(w.find(".meta").text()).toContain("token 1,204");
    expect(w.find(".meta").text()).not.toContain("tok 1,204");
    w.unmount();
  });

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
