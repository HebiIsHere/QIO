/**
 * F06 前端消费（acc-c2）：TURN_END 终态 `incomplete` 必须**如实**消费。
 *
 * 契约：docs/plans/2026-10-09-process-attachment-audit-consolidation.md
 *   §二 C2（有效流式结束 / 不完整结束）+ §七 C2（acc-b2 追加裁定：status=incomplete、
 *   reason_code=incomplete_stream、stopped_by=system、actions 含 retry、已确认正文保留）、
 *   §七 C1/C5（排队轮取消：reason_code=user_stopped、actions=retry、只落自己的轮）。
 *
 * 这一组断言守四件事：
 * 1) 不当作正常完成：facts.status 就是 incomplete，lastTurnOutcome 也如实；
 * 2) 不吞掉事实：reason / reason_code / error 全部留在这一轮的 facts 里（详情可看）；
 * 3) 不写误导性的全局错误：lastError 不得变成「本轮执行失败」；
 * 4) 已确认正文只出现一次（F11 的既有修复不得回归）。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/acc_c2_incomplete_turn.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import type { AgentEvent } from "../../services/events";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";
import type { StreamMessage } from "../session";

const { getSessionContext, getSessionMessagesBefore, sendTurn, getRuntimeState } = vi.hoisted(() => ({
  getSessionContext: vi.fn(),
  getSessionMessagesBefore: vi.fn(),
  sendTurn: vi.fn(async () => ({
    ok: true,
    accepted: true,
    turn_id: "turn_new",
    status: "accepted",
    topic_id: "topic_1",
  })),
  getRuntimeState: vi.fn(),
}));

vi.mock("../../services/api", () => ({
  api: { getSessionContext, getSessionMessagesBefore, sendTurn, getRuntimeState },
  ApiError: class ApiError extends Error {},
}));

const BODY = "已经确认的这一段正文只应该出现一次。";
const REASON = "模型在这一轮结束标记之前就断流了，这段正文可能不完整。";

let seq = 0;
function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: "c2_" + seq, ts: new Date().toISOString(), data };
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

function assistants(session: ReturnType<typeof useSessionStore>): StreamMessage[] {
  return session.messages.filter((m) => m.role === "assistant");
}

function countOf(text: string, needle: string): number {
  return text.split(needle).length - 1;
}

/** 后端的不完整结束事实（acc-b2 契约）：正文保留 + 人话原因 + retry。 */
function incompleteEnd(overrides: Record<string, unknown> = {}) {
  return {
    turn_id: "turn_i",
    status: "incomplete",
    reason_code: "incomplete_stream",
    reason: REASON,
    stopped_by: "system",
    actions: ["retry"],
    final_content: BODY,
    duration_ms: 2600,
    queue_ms: 40,
    error: "upstream eof before finish_reason",
    revision: 2,
    ...overrides,
  };
}

/** 一轮已经流式显示出来的正式回答（interim:false = 用户已经看到它了）。 */
function streamAnswers(events: ReturnType<typeof useEventStore>, turnId: string, body: string) {
  events.route(ev("TURN_START", { turn_id: turnId, revision: 1 }));
  events.route(
    ev("ASSISTANT", {
      turn_id: turnId,
      content: body,
      interim: false,
      streaming: true,
      delta_id: "dl_" + turnId,
      seq: 1,
    }),
  );
}

beforeEach(() => {
  seq = 0;
  localStorage.clear();
  getSessionContext.mockReset();
  sendTurn.mockClear();
  getRuntimeState.mockClear();
  getSessionContext.mockResolvedValue({
    topic_id: "topic_1",
    topic_name: "默认话题",
    anchor_fragment: null,
    messages: [],
    tool_records: [],
    turn_facts: [],
    has_more: false,
    next_before: null,
  });
});

describe("F06 前端：incomplete 是「未完成」，不是完成", () => {
  it("事实完整落地：status=incomplete / reason_code=incomplete_stream / actions 含 retry", () => {
    const { events, session } = setup();
    streamAnswers(events, "turn_i", BODY);
    events.route(ev("TURN_END", incompleteEnd()));

    const facts = session.factsFor("turn_i");
    expect(facts, "不完整结束也必须留下这一轮的事实").not.toBeNull();
    expect(facts?.status).toBe("incomplete");
    expect(facts?.reasonCode).toBe("incomplete_stream");
    expect(facts?.stoppedBy).toBe("system");
    expect(facts?.actions).toEqual(["retry"]);
    expect(facts?.reason).toBe(REASON);
    // 结束事实里的原始错误不能因为「不是 failed」就被吞掉（详情里要能看到）
    expect(facts?.errorText).toContain("eof");
    // 总耗时口径不变（排队 + 执行都来自 TURN_END）
    expect(facts?.durationMs).toBe(2600);
    expect(facts?.queueMs).toBe(40);
  });

  it("lastError 不得变成误导性的「本轮执行失败」；结局仍如实登记为 incomplete", () => {
    const { events, session } = setup();
    streamAnswers(events, "turn_i", BODY);
    events.route(ev("TURN_END", incompleteEnd()));

    expect(session.lastError, "不完整结束不是「这一轮执行失败」").toBeNull();
    expect(session.lastTurnOutcome?.status).toBe("incomplete");
    expect(session.lastTurnOutcome?.turnId).toBe("turn_i");
    // 忽略掉中间话：不是失败，也不该把这一轮说成还在跑
    expect(session.turnRunning).toBe(false);
    expect(session.activeTurnId).toBeNull();
  });

  it("已流式显示的正文保留且只出现一次（F11 修复不回归）", () => {
    const { events, session } = setup();
    streamAnswers(events, "turn_i", BODY);
    events.route(ev("TURN_END", incompleteEnd()));

    const list = assistants(session);
    expect(list).toHaveLength(1);
    expect(countOf(list[0]!.content, BODY), "正文只出现一次").toBe(1);
    expect(list[0]!.interim, "已经发布过的正式回答不因为断流被撤回").not.toBe(true);
    expect(list[0]!.streaming, "落定为静态").toBeUndefined();
  });

  it("final_content 比已到达正文短（未确认后缀）：以后端确认的正文为准，仍然只有一条", () => {
    const { events, session } = setup();
    streamAnswers(events, "turn_i", BODY);
    // 后端确认的那部分比前端已经显示的短：未确认的后缀不得继续显示（F06 验收）
    const confirmed = BODY.slice(0, 8);
    events.route(ev("TURN_END", incompleteEnd({ final_content: confirmed })));

    const list = assistants(session);
    expect(list, "不得因为「全文不等」再补一条回答").toHaveLength(1);
    expect(list[0]!.content, "正文以确认过的那部分为准（K2.2 覆盖，不比较相似度）").toBe(confirmed);
  });

  it("重连重放（同一 turn 的 TURN_END 再来一次）不得追加第二条正文", () => {
    const { events, session } = setup();
    streamAnswers(events, "turn_i", BODY);
    const end = ev("TURN_END", incompleteEnd());
    events.route(end);
    events.route({ ...end, id: "c2_replay" });

    const list = assistants(session);
    expect(list).toHaveLength(1);
    expect(countOf(list[0]!.content, BODY)).toBe(1);
    expect(session.factsFor("turn_i")?.reasonCode).toBe("incomplete_stream");
  });

  it("final_content 为空（正文只在流里）也不把 incomplete 收成完成", () => {
    const { events, session } = setup();
    streamAnswers(events, "turn_i", BODY);
    events.route(ev("TURN_END", incompleteEnd({ final_content: null })));

    expect(session.factsFor("turn_i")?.status).toBe("incomplete");
    const list = assistants(session);
    expect(list).toHaveLength(1);
    expect(countOf(list[0]!.content, BODY)).toBe(1);
    expect(session.lastError).toBeNull();
  });
});

describe("F06 前端：刷新 / 重连后仍然是「未完成 + 原因 + retry」", () => {
  it("历史台账 turn_facts 带 incomplete：恢复 status / reason_code / actions（换设备也一样）", async () => {
    const { session } = setup();
    getSessionContext.mockResolvedValueOnce({
      topic_id: "topic_1",
      topic_name: "默认话题",
      anchor_fragment: null,
      messages: [
        {
          id: "m_user",
          role: "user",
          content: "帮我看看这段流",
          content_type: "text",
          created_at: "2026-10-09T05:00:00+00:00",
          turn_id: "turn_i",
        },
        {
          id: "m_assist",
          role: "assistant",
          content: BODY,
          content_type: "text",
          created_at: "2026-10-09T05:00:01+00:00",
          turn_id: "turn_i",
        },
      ],
      tool_records: [],
      turn_facts: [
        {
          turn_id: "turn_i",
          status: "incomplete",
          reason_code: "incomplete_stream",
          reason: REASON,
          stopped_by: "system",
          actions: ["retry"],
        },
      ],
      has_more: false,
      next_before: null,
    });

    await session.loadHistory();

    const facts = session.factsFor("turn_i");
    expect(facts?.status).toBe("incomplete");
    expect(facts?.reasonCode).toBe("incomplete_stream");
    expect(facts?.reason).toBe(REASON);
    expect(facts?.actions).toEqual(["retry"]);
    expect(session.lastError).toBeNull();
  });

  it("RESYNC 快照的 turn_facts 同样带回 incomplete（断线期间结束的那一轮）", () => {
    const { events, session } = setup();
    events.applyRuntimeState({
      approvals: [],
      tasks: [],
      tools: [],
      narratives: [],
      turn_facts: [
        {
          turn_id: "turn_i",
          status: "incomplete",
          reason_code: "incomplete_stream",
          reason: REASON,
          stopped_by: "system",
          actions: ["retry"],
        },
      ],
    });
    expect(session.factsFor("turn_i")?.status).toBe("incomplete");
    expect(session.factsFor("turn_i")?.reasonCode).toBe("incomplete_stream");
    expect(session.factsFor("turn_i")?.actions).toEqual(["retry"]);
    expect(session.lastError).toBeNull();
  });

  it("翻更早的历史（分页）同样把 incomplete 事实带回来", async () => {
    const { session } = setup();
    session.historyCursor = "cursor_1";
    session.historyHasMore = true;
    getSessionMessagesBefore.mockResolvedValueOnce({
      topic_id: "topic_1",
      messages: [
        {
          id: "m_old_user",
          role: "user",
          content: "更早的那一轮",
          content_type: "text",
          created_at: "2026-10-08T05:00:00+00:00",
          turn_id: "turn_old",
        },
      ],
      tool_records: [],
      has_more: false,
      next_before: null,
      turn_facts: [
        {
          turn_id: "turn_old",
          status: "incomplete",
          reason_code: "incomplete_stream",
          reason: REASON,
          stopped_by: "system",
          actions: ["retry"],
        },
      ],
    });

    await session.loadOlderHistory();

    expect(session.factsFor("turn_old")?.status).toBe("incomplete");
    expect(session.factsFor("turn_old")?.actions).toEqual(["retry"]);
  });
});

describe("F12 前端：排队轮取消的结束事实只落它自己", () => {
  /** A 正在跑；B 已被受理进入排队（带用户消息与排队标记）。 */
  function queueB(events: ReturnType<typeof useEventStore>, session: ReturnType<typeof useSessionStore>) {
    events.route(ev("TURN_START", { turn_id: "turn_a", revision: 1 }));
    session.pushUser("A 的问题");
    const a = session.messages[session.messages.length - 1]!;
    a.turnId = "turn_a";
    session.pushUser("B 的问题");
    const b = session.messages[session.messages.length - 1]!;
    b.turnId = "turn_b";
    b.queued = true;
    session.queuedMessageIds.push(b.id);
    session.markTurnQueued("turn_b");
    events.route(
      ev("TURN_QUEUE", {
        running: { turn_id: "turn_a", message: "A 的问题" },
        queued: [{ turn_id: "turn_b", message: "B 的问题" }],
        cancelled: [],
        revision: 2,
      }),
    );
    return { a, b };
  }

  it("B 取消：facts 落到 B（actions=retry 可执行），A 的运行态与归属不变，不写 lastError", () => {
    const { events, session } = setup();
    queueB(events, session);
    events.route(
      ev("TURN_END", {
        turn_id: "turn_b",
        status: "cancelled",
        reason_code: "user_stopped",
        reason: "你取消了排队中的这一轮",
        stopped_by: "user",
        actions: ["retry"],
        duration_ms: 0,
        queue_ms: 12345,
        revision: 3,
      }),
    );

    // A 完全不受影响
    expect(session.activeTurnId).toBe("turn_a");
    expect(session.turnRunning).toBe(true);
    expect(session.turnPhase).not.toBe("idle");
    // B 的结束事实可靠落地，且可执行（找得到这一轮的用户消息）
    expect(session.factsFor("turn_b")?.status).toBe("cancelled");
    expect(session.factsFor("turn_b")?.reasonCode).toBe("user_stopped");
    expect(session.factsFor("turn_b")?.actions).toEqual(["retry"]);
    expect(session.userMessageFor("turn_b")?.content).toBe("B 的问题");
    // 排队标记先清理
    expect(session.isQueuedTurn("turn_b")).toBe(false);
    expect(session.turnQueue.cancelled.some((c) => c.turn_id === "turn_b")).toBe(true);
    // 排队取消不是「本轮执行失败」
    expect(session.lastError).toBeNull();
  });

  it("重复 / 迟到的 B 结束事件只生效一次：先到的原因不被后到的覆盖", () => {
    const { events, session } = setup();
    queueB(events, session);
    events.route(
      ev("TURN_END", {
        turn_id: "turn_b",
        status: "cancelled",
        reason_code: "user_stopped",
        reason: "先到的原因",
        stopped_by: "user",
        actions: ["retry"],
        revision: 3,
      }),
    );
    events.route(
      ev("TURN_END", {
        turn_id: "turn_b",
        status: "failed",
        reason_code: "internal_error",
        reason: "后到的原因（重放 / 竞争）",
        stopped_by: "system",
        actions: [],
        revision: 4,
      }),
    );

    expect(session.factsFor("turn_b")?.reasonCode).toBe("user_stopped");
    expect(session.factsFor("turn_b")?.reason).toBe("先到的原因");
    expect(session.activeTurnId).toBe("turn_a");
    expect(session.turnRunning).toBe(true);
  });

  it("竞争取消：队列快照（revision 更大）先摘掉 B，B 的 TURN_END（revision 更旧）随后补发 —— 事实仍要落地", () => {
    const { events, session } = setup();
    const { b } = queueB(events, session);
    // 权威队列快照先把 B 摘出队列（revision 更大 = 更新），B 的 END 反而更旧
    events.route(
      ev("TURN_QUEUE", {
        running: { turn_id: "turn_a", message: "A 的问题" },
        queued: [],
        cancelled: [],
        revision: 5,
      }),
    );
    expect(session.isQueuedTurn("turn_b")).toBe(false);

    events.route(
      ev("TURN_END", {
        turn_id: "turn_b",
        status: "cancelled",
        reason_code: "user_stopped",
        reason: "你取消了排队中的这一轮",
        stopped_by: "user",
        actions: ["retry"],
        queue_ms: 321,
        revision: 4,
      }),
    );

    expect(session.factsFor("turn_b")?.reasonCode, "迟到但确定的结束事实不得整条丢掉").toBe("user_stopped");
    expect(session.factsFor("turn_b")?.actions).toEqual(["retry"]);
    expect(session.factsFor("turn_b")?.queueMs).toBe(321);
    expect(b.queued).toBe(false);
    // 仍然绝不触碰正在跑的 A，也不写全局结局 / 错误
    expect(session.activeTurnId).toBe("turn_a");
    expect(session.turnRunning).toBe(true);
    expect(session.lastTurnOutcome).toBeNull();
    expect(session.lastError).toBeNull();
  });

  it("倒序：B 的结束事件先到、TURN_START 后到，不把已取消的 B 重新点亮", () => {
    const { events, session } = setup();
    queueB(events, session);
    events.route(
      ev("TURN_END", {
        turn_id: "turn_b",
        status: "cancelled",
        reason_code: "user_stopped",
        reason: "你取消了排队中的这一轮",
        stopped_by: "user",
        actions: ["retry"],
        revision: 3,
      }),
    );
    events.route(ev("TURN_START", { turn_id: "turn_b", revision: 9 }));

    expect(session.activeTurnId, "已取消的排队轮不能变成 active").toBe("turn_a");
    expect(session.turnRunning).toBe(true);
    expect(session.factsFor("turn_b")?.status).toBe("cancelled");
  });
});
