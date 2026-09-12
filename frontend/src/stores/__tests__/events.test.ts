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

  it("ANCHOR 事件区分「历史位置」与当前位置（historic）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({
      type: "ANCHOR",
      id: "e7",
      ts: "2026-09-12T00:00:00Z",
      data: {
        topic_id: "t1",
        topic_name: "话题A",
        fragment_id: "f13",
        fragment_title: "Anchor 生命周期",
        historic: true,
      },
    });
    expect(session.anchorHistoric).toBe(true);
    expect(session.anchorFragmentId).toBe("f13");

    // 成功一轮后位置推进到当前片段：历史提示必须消失
    events.route({
      type: "ANCHOR",
      id: "e8",
      ts: "2026-09-12T00:01:00Z",
      data: { topic_id: "t1", topic_name: "话题A", fragment_id: "f20", fragment_title: null, historic: false },
    });
    expect(session.anchorHistoric).toBe(false);
    expect(session.anchorFragmentId).toBe("f20");
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

  it("后端队列为空时清除本地「等待中」标记（排队项被取消不残留）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    session.pushUser("排队的消息");
    session.messages[0].queued = true;
    session.queuedMessageIds.push(session.messages[0].id);
    events.route({
      type: "TURN_QUEUE",
      id: "q2",
      ts: "2026-09-10T00:00:00Z",
      data: { running: { turn_id: "turn_a", message: "A" }, queued: [], cancelled: [] },
    });
    expect(session.messages[0].queued).toBe(false);
    expect(session.queuedMessageIds.length).toBe(0);
  });
});

describe("events store turn 用量归属（问题3）", () => {
  it("TURN 1 = 100 / TURN 2 = 200：历史 turn 不被后续累计覆盖", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();

    events.route({ type: "TURN_START", id: "t1", ts: "x", data: { turn_id: "turn_1" } });
    events.route({ type: "TURN_END", id: "e1", ts: "x", data: { turn_id: "turn_1", final_content: "A", tokens: 100, iterations: 2, tool_calls: 1 } });
    events.route({ type: "USAGE", id: "u1", ts: "x", data: { tokens: 100, iterations: 2, tool_calls: 1 } });

    events.route({ type: "TURN_START", id: "t2", ts: "x", data: { turn_id: "turn_2" } });
    events.route({ type: "TURN_END", id: "e2", ts: "x", data: { turn_id: "turn_2", final_content: "B", tokens: 200 } });
    events.route({ type: "USAGE", id: "u2", ts: "x", data: { tokens: 200 } });

    expect(events.usageByTurn.turn_1.tokens).toBe(100);
    expect(events.usageByTurn.turn_2.tokens).toBe(200);
    // 不是把两条都写成 300
    expect(events.usageByTurn.turn_1.tokens).not.toBe(300);
  });

  it("消息按 turnId 关联到自己的用量", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({ type: "TURN_START", id: "t1", ts: "x", data: { turn_id: "turn_1" } });
    events.route({ type: "TURN_END", id: "e1", ts: "x", data: { turn_id: "turn_1", final_content: "A", tokens: 42 } });
    expect(session.messages[0]?.turnId).toBe("turn_1");
    expect(events.turnUsageFor("turn_1")?.tokens).toBe(42);
    expect(events.turnUsageFor(null)).toBeUndefined();
  });
});

describe("events store WARNING 语义（问题13）", () => {
  it("WARNING 不代表 turn 结束：running 保持，直到 TURN_END", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();

    events.route({ type: "TURN_START", id: "t1", ts: "x", data: { turn_id: "turn_1" } });
    events.route({ type: "WARNING", id: "w1", ts: "x", data: { code: "tool_failed", message: "工具失败了一次", recoverable: true } });

    expect(session.turnRunning).toBe(true);
    expect(session.warning).toContain("工具失败了一次");
    // 非致命警告不是错误
    expect(session.lastError).toBeNull();

    events.route({ type: "TOOL_END", id: "x1", ts: "x", data: { tool: "shell", ok: true, error: null, content_preview: "" } });
    expect(session.turnRunning).toBe(true);

    events.route({ type: "TURN_END", id: "e1", ts: "x", data: { turn_id: "turn_1", final_content: "A" } });
    expect(session.turnRunning).toBe(false);
  });

  it("TURN_START 清空上一轮的 warning（警告不跨轮残留）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({ type: "WARNING", id: "w1", ts: "x", data: { message: "旧警告" } });
    expect(session.warning).toBe("旧警告");
    events.route({ type: "TURN_START", id: "t1", ts: "x", data: { turn_id: "turn_2" } });
    expect(session.warning).toBeNull();
  });

  it("ERROR 才是终止事件：结束 running 并记录错误", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const events = useEventStore();
    const session = useSessionStore();
    events.route({ type: "TURN_START", id: "t1", ts: "x", data: { turn_id: "turn_1" } });
    events.route({ type: "ERROR", id: "err1", ts: "x", data: { message: "planning failed" } });
    expect(session.turnRunning).toBe(false);
    expect(session.lastError).toBe("planning failed");
  });
});
