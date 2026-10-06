/**
 * 耗时入口的位置契约（契约 §3）：
 * 每轮**只保留一个**入口，它在过程区（TurnProcess）里，不在每条消息上；
 * 折叠态直接显示 TURN_END 的 duration_ms，未展开不发请求；
 * 明细失败也不抹掉已知总耗时。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import MessageItem from "../MessageItem.vue";
import TurnProcess from "../TurnProcess.vue";
import { clearTurnTimingCache } from "../../composables/useTurnTiming";
import { resetProcessState } from "../../stores/turnProcess";
import type { StreamMessage, TurnFacts } from "../../stores/session";

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

const FACTS: TurnFacts = {
  turnId: "turn_1",
  status: "completed",
  durationMs: 1699,
  queueMs: 0,
  startedAt: null,
  endedAt: null,
};

function mountProcess() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(TurnProcess, {
    props: { turnId: "turn_1", items: [], stages: [], facts: FACTS, running: false },
    global: { plugins: [pinia], stubs: { MarkdownContent: true } },
  });
}

async function open(w: ReturnType<typeof mountProcess>) {
  const details = w.find("[data-test='turn-timing']");
  (details.element as HTMLDetailsElement).open = true;
  await details.trigger("toggle");
  await flushPromises();
}

beforeEach(() => {
  clearTurnTimingCache();
  resetProcessState();
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

describe("耗时入口的位置（每轮一个，在过程区）", () => {
  it("助手消息本身不再挂耗时入口", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mount(MessageItem, {
      props: { message: makeMessage({ role: "assistant", turnId: "turn_1" }) },
      global: { plugins: [pinia], stubs: { MarkdownContent: true } },
    });
    expect(w.find("[data-test='turn-timing']").exists()).toBe(false);
    w.unmount();
  });

  it("过程区折叠态直接显示「已完成 · 耗时」，未展开不发请求", async () => {
    const w = mountProcess();
    await flushPromises();
    const entry = w.find("[data-test='turn-timing']");
    expect(entry.exists()).toBe(true);
    expect(entry.text()).toContain("已完成");
    expect(entry.text()).toContain("耗时");
    expect(entry.text()).toContain("1.7 秒");
    expect(getTrace).not.toHaveBeenCalled();
    w.unmount();
  });

  it("展开才拉明细；明细失败时已知总耗时仍在，且不显示「读取中」", async () => {
    // 全部调用都失败：展开时可能因 <details> 的 toggle 语义触发两次读取，
    // 一次成功就会把「失败」状态盖掉，这里要的是稳定的失败路径
    getTrace.mockRejectedValue(new Error("boom"));
    const w = mountProcess();
    await open(w);
    expect(getTrace).toHaveBeenCalledWith("turn_1");
    const total = w.find(".tt-total").text();
    expect(total).toContain("1.7 秒");
    expect(total).not.toContain("读取中");
    expect(w.text()).toContain("没读到耗时明细");
    expect(w.find(".tt-retry").exists()).toBe(true);
    w.unmount();
  });
});
