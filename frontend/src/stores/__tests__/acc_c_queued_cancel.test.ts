/**
 * F12 反例（acc-c）：排队轮取消必须留下自己的结束事实（原因 / 动作 / 耗时），
 * 且绝不能结束或覆盖仍在运行的 A。
 */
import { describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({ topic_id: "", messages: [] })),
    sendTurn: vi.fn(async () => ({})),
  },
}));

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

/** A 正在跑，B 已被受理进入排队（返回 B 的用户消息，便于断言排队标记）。 */
function queueBAfterA(
  events: ReturnType<typeof useEventStore>,
  session: ReturnType<typeof useSessionStore>,
  opts: { withQueueEvent?: boolean } = {},
) {
  events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "turn_a", revision: 1 } });
  session.pushUser("A 的问题");
  const a = session.messages[session.messages.length - 1]!;
  a.turnId = "turn_a";
  events.route({
    type: "ASSISTANT",
    id: "2",
    ts: "",
    data: { turn_id: "turn_a", content: "A 正在处理", interim: true },
  });
  session.pushUser("B 的问题");
  const b = session.messages[session.messages.length - 1]!;
  b.turnId = "turn_b";
  b.queued = true;
  session.queuedMessageIds.push(b.id);
  session.markTurnQueued("turn_b");
  if (opts.withQueueEvent !== false) {
    events.route({
      type: "TURN_QUEUE",
      id: "3",
      ts: "",
      data: {
        running: { turn_id: "turn_a", message: "A 的问题" },
        queued: [{ turn_id: "turn_b", message: "B 的问题" }],
        cancelled: [],
      },
    });
  }
  return { a, b };
}

describe("F12：排队轮取消记录自己的结束事实", () => {
  it("B 未开始即取消：原因 / 动作 / 耗时落到 B；A 状态不变", () => {
    const { events, session } = setup();
    const { b } = queueBAfterA(events, session);

    events.route({
      type: "TURN_END",
      id: "4",
      ts: "",
      data: {
        turn_id: "turn_b",
        status: "cancelled",
        reason: "用户取消了排队中的这一轮",
        reason_code: "user_stopped",
        stopped_by: "user",
        actions: ["retry"],
        duration_ms: 0,
        queue_ms: 12345,
      },
    });

    // A 不受影响
    expect(session.activeTurnId).toBe("turn_a");
    expect(session.turnRunning).toBe(true);
    expect(session.turnPhase).not.toBe("idle");

    // B 的结束事实可靠落地
    const facts = session.factsFor("turn_b");
    expect(facts).not.toBeNull();
    expect(facts?.status).toBe("cancelled");
    expect(facts?.reason).toContain("取消");
    expect(facts?.reasonCode).toBe("user_stopped");
    expect(facts?.stoppedBy).toBe("user");
    expect(facts?.actions).toEqual(["retry"]);
    expect(facts?.queueMs).toBe(12345);

    // 排队标记先清理，但事实已经在了
    expect(b.queued).toBe(false);
    expect(session.isQueuedTurn("turn_b")).toBe(false);
    // B 作为已取消项可查看
    expect(session.turnQueue.cancelled.some((c) => c.turn_id === "turn_b")).toBe(true);
  });

  it("没有 TURN_QUEUE 事件（准备阶段失败）也要记录结束事实", () => {
    const { events, session } = setup();
    const { b } = queueBAfterA(events, session, { withQueueEvent: false });
    events.route({
      type: "TURN_END",
      id: "4",
      ts: "",
      data: {
        turn_id: "turn_b",
        status: "failed",
        reason: "附件准备失败",
        reason_code: "internal_error",
        actions: [],
        duration_ms: 0,
        queue_ms: 20,
      },
    });
    expect(session.factsFor("turn_b")?.reasonCode).toBe("internal_error");
    expect(b.queued).toBe(false);
    expect(session.activeTurnId).toBe("turn_a");
    expect(session.turnRunning).toBe(true);
  });

  it("重复的 B TURN_END：不覆盖 A、不重复落地、不改变 A 的运行态", () => {
    const { events, session } = setup();
    queueBAfterA(events, session);
    const end = {
      type: "TURN_END" as const,
      id: "4",
      ts: "",
      data: {
        turn_id: "turn_b",
        status: "cancelled",
        reason: "用户取消",
        reason_code: "user_stopped",
        duration_ms: 0,
        queue_ms: 5,
      },
    };
    events.route(end);
    events.route({ ...end, id: "5" });
    expect(session.activeTurnId).toBe("turn_a");
    expect(session.turnRunning).toBe(true);
    expect(session.factsFor("turn_b")?.reasonCode).toBe("user_stopped");
  });

  it("TURN_QUEUE 先把 B 摘出队列、B 的 TURN_END 随后才到：事实仍落地且不动 A", () => {
    const { events, session } = setup();
    const { b } = queueBAfterA(events, session);
    // 后端队列变化先到：B 已不在 queued 列表
    events.route({
      type: "TURN_QUEUE",
      id: "3b",
      ts: "",
      data: { running: { turn_id: "turn_a", message: "A 的问题" }, queued: [], cancelled: [] },
    });
    expect(session.isQueuedTurn("turn_b")).toBe(false);

    events.route({
      type: "TURN_END",
      id: "4",
      ts: "",
      data: {
        turn_id: "turn_b",
        status: "cancelled",
        reason: "用户取消了排队中的这一轮",
        reason_code: "user_stopped",
        stopped_by: "user",
        actions: [],
        duration_ms: 0,
        queue_ms: 321,
      },
    });
    expect(session.factsFor("turn_b")?.reasonCode).toBe("user_stopped");
    expect(session.factsFor("turn_b")?.queueMs).toBe(321);
    expect(session.activeTurnId).toBe("turn_a");
    expect(session.turnRunning).toBe(true);
    expect(b.queued).toBe(false);
  });

  it("倒序：B 的 END 先到、TURN_START 后到也不能把已取消的 B 重新点亮为运行中", () => {
    const { events, session } = setup();
    queueBAfterA(events, session);
    events.route({
      type: "TURN_END",
      id: "4",
      ts: "",
      data: {
        turn_id: "turn_b",
        status: "cancelled",
        reason: "用户取消",
        reason_code: "user_stopped",
      },
    });
    events.route({ type: "TURN_START", id: "5", ts: "", data: { turn_id: "turn_b", revision: 9 } });
    expect(session.activeTurnId).toBe("turn_a");
    expect(session.turnRunning).toBe(true);
  });
});
