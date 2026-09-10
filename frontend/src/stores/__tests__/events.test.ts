import { describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";

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

describe("events store 路由", () => {
  it("ASSISTANT 事件 → 追加 interim 助手消息", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({
      type: "ASSISTANT",
      id: "e1",
      ts: "2026-08-10T00:00:00Z",
      data: { content: "我先查一下仓库", interim: true },
    });
    const last = session.messages[session.messages.length - 1];
    expect(last?.role).toBe("assistant");
    expect(last?.content).toBe("我先查一下仓库");
    expect(last?.interim).toBe(true);
  });

  it("ASSISTANT 空内容不追加消息", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({
      type: "ASSISTANT",
      id: "e2",
      ts: "2026-08-10T00:00:00Z",
      data: { content: "  " },
    });
    expect(session.messages.length).toBe(0);
  });

  it("TOOL_END 携带 presentation → 工具消息保存呈现", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({
      type: "TOOL_END",
      id: "e3",
      ts: "2026-08-10T00:00:00Z",
      data: {
        tool: "memory_search",
        ok: true,
        error: null,
        content_preview: "raw",
        presentation: { title: "检索记忆", status: "ok", summary: "命中 3 条" },
      },
    });
    const last = session.messages[session.messages.length - 1];
    expect(last?.role).toBe("tool");
    expect(last?.presentation).toEqual({ title: "检索记忆", status: "ok", summary: "命中 3 条" });
  });

  it("ANCHOR 事件 → 实时更新会话锚点（话题名/片段）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({
      type: "ANCHOR",
      id: "e5",
      ts: "2026-08-10T00:00:00Z",
      data: {
        topic_id: "topic_new",
        topic_name: "养鹅",
        fragment_id: "frag_1",
        fragment_title: "鹅的日常",
      },
    });
    expect(session.currentTopicId).toBe("topic_new");
    expect(session.topicName).toBe("养鹅");
    expect(session.anchorFragmentId).toBe("frag_1");
    expect(session.anchorFragment?.title).toBe("鹅的日常");
  });

  it("ANCHOR 事件空 topic_id 不更新", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({
      type: "ANCHOR",
      id: "e6",
      ts: "2026-08-10T00:00:00Z",
      data: { topic_id: "", topic_name: "x" },
    });
    expect(session.currentTopicId).toBeNull();
  });

  it("TOOL_END 无 presentation → 工具消息 presentation 为 null", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({
      type: "TOOL_END",
      id: "e4",
      ts: "2026-08-10T00:00:00Z",
      data: { tool: "echo", ok: true, error: null, content_preview: "hi" },
    });
    const last = session.messages[session.messages.length - 1];
    expect(last?.presentation).toBeNull();
  });

  it("连续 ASSISTANT 事件就地更新同一条流式消息，不新建（问题1）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({ type: "TURN_START", id: "t1", ts: "2026-08-10T00:00:00Z", data: {} });
    events.route({ type: "ASSISTANT", id: "a1", ts: "2026-08-10T00:00:00Z", data: { content: "你好" } });
    events.route({ type: "ASSISTANT", id: "a2", ts: "2026-08-10T00:00:00Z", data: { content: "你好，世界" } });
    expect(session.messages.length).toBe(1);
    const last = session.messages[0];
    expect(last?.content).toBe("你好，世界");
    expect(last?.streaming).toBe(true);
  });

  it("TURN_END 落定流式消息：streaming 清除、interim 为 false（问题1）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({ type: "ASSISTANT", id: "a1", ts: "2026-08-10T00:00:00Z", data: { content: "回答" } });
    events.route({ type: "TURN_END", id: "t1", ts: "2026-08-10T00:00:00Z", data: { final_content: "回答" } });
    const last = session.messages[session.messages.length - 1];
    expect(last?.streaming).toBeUndefined();
    expect(last?.interim).toBe(false);
    // 不应再新建第二条 final（同一条落定）
    expect(session.messages.length).toBe(1);
  });

  it("消息快照产生时的话题名（问题3）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    session.setAnchor("topic_a", null, "话题A");
    session.pushUser("你好");
    expect(session.messages[0]?.topicName).toBe("话题A");
    // 切换话题后，旧消息快照不变
    session.setAnchor("topic_b", null, "话题B");
    expect(session.messages[0]?.topicName).toBe("话题A");
  });

  it("kind=continue 的审批事件 → 进入 pendingContinue 而非审批队列", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({
      type: "APPROVAL_REQUIRED",
      id: "c1",
      ts: "2026-09-10T00:00:00Z",
      data: {
        approval: {
          approval_id: "appr_1",
          kind: "continue",
          payload: { used_iterations: 128, max_iterations: 128 },
        },
      },
    });
    expect(session.pendingContinue?.id).toBe("appr_1");
    expect(session.pendingContinue?.used).toBe(128);
  });

  it("TURN_START 记录 activeTurnId，消息归属该 turn，TURN_END 清除", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({ type: "TURN_START", id: "t1", ts: "2026-09-10T00:00:00Z", data: { turn_id: "turn_x" } });
    expect(session.activeTurnId).toBe("turn_x");
    events.route({ type: "ASSISTANT", id: "a1", ts: "2026-09-10T00:00:00Z", data: { content: "回答", turn_id: "turn_x" } });
    const last = session.messages[session.messages.length - 1];
    expect(last?.turnId).toBe("turn_x");
    events.route({ type: "TURN_END", id: "t2", ts: "2026-09-10T00:00:00Z", data: { final_content: "回答", turn_id: "turn_x" } });
    expect(session.activeTurnId).toBeNull();
  });

  it("TURN_QUEUE 事件 → 更新 turnQueue 快照", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({
      type: "TURN_QUEUE",
      id: "q1",
      ts: "2026-09-10T00:00:00Z",
      data: {
        running: { turn_id: "turn_a", message: "A" },
        queued: [{ turn_id: "turn_b", message: "B" }],
      },
    });
    expect(session.turnQueue.running?.turn_id).toBe("turn_a");
    expect(session.turnQueue.queued.map((q) => q.turn_id)).toEqual(["turn_b"]);
  });
});
