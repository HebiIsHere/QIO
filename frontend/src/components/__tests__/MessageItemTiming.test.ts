import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import MessageItem from "../MessageItem.vue";
import { clearTurnTimingCache } from "../../composables/useTurnTiming";
import type { StreamMessage } from "../../stores/session";

const { getTrace } = vi.hoisted(() => ({ getTrace: vi.fn() }));

vi.mock("../../services/api", () => ({
  api: { getTrace: (...args: unknown[]) => getTrace(...args), getUISettings: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

function makeMessage(over: Partial<StreamMessage> & { role: StreamMessage["role"] }): StreamMessage {
  return {
    id: "m1",
    content: "回答",
    contentType: "text",
    createdAt: "2026-10-02T17:00:00+08:00",
    ...over,
  };
}

function mountItem(msg: StreamMessage) {
  const pinia = createPinia();
  return mount(MessageItem, {
    props: { message: msg },
    global: { plugins: [pinia], stubs: { MarkdownContent: true } },
  });
}

beforeEach(() => {
  clearTurnTimingCache();
  getTrace.mockReset();
  getTrace.mockResolvedValue({
    turn_id: "turn_1",
    duration_ms: 1699,
    phases: {
      residual_ms: 0,
      notes: { queue_wait_ms: 0 },
      spans: [{ name: "model_wait", ms: 1699, depth: 1, start_ms: 0 }],
    },
  });
});

describe("对话里的耗时入口", () => {
  it("助手消息有自己的 turn_id：出现「耗时」入口，且挂载时不发请求", async () => {
    const w = mountItem(makeMessage({ role: "assistant", turnId: "turn_1" }));
    await flushPromises();
    expect(w.find("[data-test='turn-timing']").exists()).toBe(true);
    expect(w.text()).toContain("耗时");
    expect(getTrace).not.toHaveBeenCalled();
    w.unmount();
  });

  it("用户消息与没有 turn_id 的旧消息都不出现入口", () => {
    const user = mountItem(makeMessage({ role: "user", turnId: "turn_1" }));
    expect(user.find("[data-test='turn-timing']").exists()).toBe(false);
    user.unmount();

    const orphan = mountItem(makeMessage({ role: "assistant", turnId: null }));
    expect(orphan.find("[data-test='turn-timing']").exists()).toBe(false);
    orphan.unmount();
  });

  it("还在流式生成中的消息不显示（Trace 要这一轮结束才有）", () => {
    const w = mountItem(makeMessage({ role: "assistant", turnId: "turn_1", streaming: true }));
    expect(w.find("[data-test='turn-timing']").exists()).toBe(false);
    w.unmount();
  });
});
