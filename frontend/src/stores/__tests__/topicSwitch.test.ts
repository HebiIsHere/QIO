/**
 * 待确认切换的状态与动作（spec 第 29~30 / 74 条）。
 *
 * 关键：收到建议时 Anchor 一动不动；只有用户点「转到这里」才真的切。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

import { useEventStore } from "../events";
import { useSessionStore } from "../session";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    // 与真实后端一致：会话上下文返回当前 Anchor 所在的话题
    getSessionContext: vi.fn(async () => ({
      topic_id: useSessionStore().currentTopicId ?? "t1",
      topic_name: useSessionStore().topicName ?? "QIO 前端",
      anchor_fragment: null,
      messages: [],
    })),
    sendTurn: vi.fn(async () => ({})),
    confirmTopicSwitch: vi.fn(async () => ({ ok: true, topic_id: "t2", fragment_id: null, fragment_title: null, historic: false })),
    rejectTopicSwitch: vi.fn(async () => ({ ok: true })),
  },
}));

afterEach(() => {
  vi.mocked(api.confirmTopicSwitch).mockClear();
  vi.mocked(api.rejectTopicSwitch).mockClear();
  vi.mocked(api.getSessionContext).mockClear();
});

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  const events = useEventStore();
  session.currentTopicId = "t1";
  session.topicName = "QIO 前端";
  return { session, events };
}

describe("待确认切换", () => {
  it("收到 TOPIC_SWITCH_SUGGESTED：只登记建议，Anchor 不变", () => {
    const { session, events } = setup();

    events.route({
      type: "TOPIC_SWITCH_SUGGESTED",
      id: "e1",
      ts: "",
      data: { from_topic_id: "t1", topic_id: "t2", topic_name: "顺丁橡胶降解", reason: "推测" },
    });

    expect(session.pendingSwitch?.topicId).toBe("t2");
    expect(session.pendingSwitch?.topicName).toBe("顺丁橡胶降解");
    expect(session.currentTopicId).toBe("t1");
    expect(api.confirmTopicSwitch).not.toHaveBeenCalled();
  });

  it("点「转到这里」→ 调用后端确认并把起点切过去", async () => {
    const { session, events } = setup();
    events.route({
      type: "TOPIC_SWITCH_SUGGESTED",
      id: "e1",
      ts: "",
      data: { topic_id: "t2", topic_name: "顺丁橡胶降解" },
    });

    await session.confirmPendingSwitch();

    expect(api.confirmTopicSwitch).toHaveBeenCalledTimes(1);
    expect(session.currentTopicId).toBe("t2");
    expect(session.pendingSwitch).toBeNull();
  });

  it("点「保留当前」→ 调用后端拒绝，起点保持原话题", async () => {
    const { session, events } = setup();
    events.route({
      type: "TOPIC_SWITCH_SUGGESTED",
      id: "e1",
      ts: "",
      data: { topic_id: "t2", topic_name: "顺丁橡胶降解" },
    });

    await session.rejectPendingSwitch();

    expect(api.rejectTopicSwitch).toHaveBeenCalledTimes(1);
    expect(session.currentTopicId).toBe("t1");
    expect(session.pendingSwitch).toBeNull();
  });

  it("确认失败时如实报错，不假装已经切过去", async () => {
    const { session, events } = setup();
    events.route({
      type: "TOPIC_SWITCH_SUGGESTED",
      id: "e1",
      ts: "",
      data: { topic_id: "t2", topic_name: "顺丁橡胶降解" },
    });
    vi.mocked(api.confirmTopicSwitch).mockRejectedValueOnce(new Error("后端不可用"));

    await session.confirmPendingSwitch();

    expect(session.currentTopicId).toBe("t1");
    expect(session.lastError).toContain("未切换");
  });

  it("新一轮用户消息开始时旧建议不再显示（不跨轮堆积）", () => {
    const { session, events } = setup();
    events.route({
      type: "TOPIC_SWITCH_SUGGESTED",
      id: "e1",
      ts: "",
      data: { topic_id: "t2", topic_name: "顺丁橡胶降解" },
    });
    expect(session.pendingSwitch).not.toBeNull();

    events.route({ type: "TURN_START", id: "e2", ts: "", data: { turn_id: "turn_1" } });

    expect(session.pendingSwitch).toBeNull();
  });
});
