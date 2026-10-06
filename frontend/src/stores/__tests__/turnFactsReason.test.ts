/**
 * 问题 7（前端）：TURN_END 的 reason / reason_code / actions 必须按 turn_id 记进 TurnFacts。
 *
 * 修复前红：TurnFacts 只有耗时字段，后端给的失败原因与可用操作无处落地 ——
 * 失败/停止的过程区只有一个状态词，既说不出「为什么」，也没有可用的出口。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/turnFactsReason.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import type { AgentEvent } from "../../services/events";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";

const { getSessionContext, resendInterruptedTurn, sendTurn } = vi.hoisted(() => ({
  getSessionContext: vi.fn(async () => ({
    topic_id: "topic_1",
    topic_name: null,
    anchor_fragment: null,
    messages: [],
    turn_facts: [] as Record<string, unknown>[],
  })),
  resendInterruptedTurn: vi.fn(async () => ({
    ok: true,
    recovered_turn_id: "turn_1",
    turn_id: "turn_new",
    status: "queued",
  })),
  sendTurn: vi.fn(async () => ({
    ok: true,
    accepted: true,
    turn_id: "turn_new",
    status: "accepted",
    topic_id: "topic_1",
  })),
}));

vi.mock("../../services/api", () => ({
  api: { getSessionContext, resendInterruptedTurn, sendTurn },
  ApiError: class ApiError extends Error {},
}));

let seq = 0;
function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: "evt_" + seq, ts: new Date().toISOString(), data };
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

beforeEach(() => {
  seq = 0;
  getSessionContext.mockClear();
  resendInterruptedTurn.mockClear();
  sendTurn.mockClear();
});

describe("TURN_END 事实：原因与可用操作按 turn_id 落地", () => {
  it("reason / reason_code / stopped_by / actions 都记进这一轮的事实", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_1", revision: 1 }));
    events.route(
      ev("TURN_END", {
        turn_id: "turn_1",
        status: "failed",
        reason_code: "provider_error",
        reason: "模型服务没有响应（连续 2 次）",
        stopped_by: "system",
        actions: ["retry", "resend"],
        error: "upstream 502",
        duration_ms: 1500,
      }),
    );

    const facts = session.factsFor("turn_1");
    expect(facts).not.toBeNull();
    expect(facts?.reason).toBe("模型服务没有响应（连续 2 次）");
    expect(facts?.reasonCode).toBe("provider_error");
    expect(facts?.stoppedBy).toBe("system");
    expect(facts?.actions).toEqual(["retry", "resend"]);
    expect(facts?.durationMs).toBe(1500);
  });

  it("actions 只认白名单里的动作：未知 / 重复 / 非字符串一律丢弃", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_2", revision: 1 }));
    events.route(
      ev("TURN_END", {
        turn_id: "turn_2",
        status: "cancelled",
        reason_code: "user_stopped",
        reason: "你停止了这一轮",
        stopped_by: "user",
        actions: ["retry", "retry", "bogus", "continue", 42, null],
      }),
    );

    expect(session.factsFor("turn_2")?.actions).toEqual(["retry", "continue"]);
  });

  it("旧记录（事件里没有这些字段）不伪造原因：reason 为 null、actions 为空", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_3", revision: 1 }));
    events.route(ev("TURN_END", { turn_id: "turn_3", status: "completed", duration_ms: 900 }));

    const facts = session.factsFor("turn_3");
    expect(facts?.status).toBe("completed");
    expect(facts?.reason).toBeNull();
    expect(facts?.reasonCode).toBeNull();
    expect(facts?.stoppedBy).toBeNull();
    expect(facts?.actions).toEqual([]);
  });

  it("历史 / 重连快照带回同一份事实：turn_facts 按 turn_id 合并（没有的行不编造）", async () => {
    const { session } = setup();
    getSessionContext.mockResolvedValueOnce({
      topic_id: "topic_1",
      topic_name: null,
      anchor_fragment: null,
      messages: [],
      turn_facts: [
        {
          turn_id: "turn_old",
          status: "cancelled",
          reason_code: "user_stopped",
          reason: "你停止了这一轮",
          stopped_by: "user",
          actions: ["retry"],
        },
      ],
    });
    await session.loadHistory();

    const facts = session.factsFor("turn_old");
    expect(facts?.reason).toBe("你停止了这一轮");
    expect(facts?.reasonCode).toBe("user_stopped");
    expect(facts?.actions).toEqual(["retry"]);
    // 快照里没有的轮次仍然是 null：历史分页读不到就不显示原因
    expect(session.factsFor("turn_missing")).toBeNull();
  });
});

describe("问题 7：可用操作真的能执行（retry 重发该轮用户消息 / resend 走既有接口）", () => {
  it("retryTurn：用现有发送接口重发该轮的用户消息（新开一轮）", async () => {
    const { session } = setup();
    session.pushUser("帮我核对实现");
    const mine = session.messages[session.messages.length - 1];
    if (mine) mine.turnId = "turn_1";

    const ok = await session.retryTurn("turn_1");
    expect(ok).toBe(true);
    // 附件显式绑定（契约 §1.4）：没有附件也要把空数组传给发送接口
    expect(sendTurn).toHaveBeenCalledWith("帮我核对实现", null, []);
    const last = session.messages[session.messages.length - 1];
    expect(last?.role).toBe("user");
    expect(last?.content).toBe("帮我核对实现");
    expect(session.userMessageFor("turn_1")?.content).toBe("帮我核对实现");
  });

  it("retryTurn：找不到该轮的用户消息时如实反馈，不发请求", async () => {
    const { session } = setup();
    const ok = await session.retryTurn("turn_no_user");
    expect(ok).toBe(false);
    expect(sendTurn).not.toHaveBeenCalled();
    expect(session.turnActionFeedback["turn_no_user"]).toContain("找不到");
  });

  it("resendTurn：走既有 /api/turns/{id}/resend；失败留在界面上可重试", async () => {
    const { session } = setup();
    const ok = await session.resendTurn("turn_1");
    expect(ok).toBe(true);
    expect(resendInterruptedTurn).toHaveBeenCalledWith("turn_1");

    resendInterruptedTurn.mockRejectedValueOnce(
      Object.assign(new Error("POST /api/turns/turn_1/resend -> 409: already claimed"), { status: 409 }),
    );
    const failed = await session.resendTurn("turn_1");
    expect(failed).toBe(false);
    const feedback = session.turnActionFeedback["turn_1"] ?? "";
    expect(feedback).toContain("没有成功");
    expect(feedback).toContain("可以重试");
    expect(session.turnActionBusy).toBeNull();
  });

  it("sendTurn 被拒绝时 retry 不静默：反馈写在这一轮上", async () => {
    const { session } = setup();
    session.pushUser("再试一次");
    const mine = session.messages[session.messages.length - 1];
    if (mine) mine.turnId = "turn_9";
    sendTurn.mockRejectedValueOnce(new Error("POST /api/turns -> 500: boom"));

    const ok = await session.retryTurn("turn_9");
    expect(ok).toBe(false);
    expect(session.turnActionFeedback["turn_9"]).toContain("没有发出");
  });
});
