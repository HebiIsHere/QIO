/**
 * D 独立验证：流式增量在「事件 → store」这一层的契约（契约 §2.1）。
 *
 * 契约来源：docs/plans/2026-10-06-unified-process-attachments-streaming.md §2。
 * 这里不渲染 DOM，只吃冻结的 SSE 载荷并断言对话状态：
 *   * 正式回答（interim=false）在**任何** TURN_END 之前就已经以非空、streaming 状态出现；
 *   * 累计快照语义：就地更新、不回退、不追加第二个气泡；
 *   * 回退/重复 seq 必须丢弃；
 *   * 不同 delta_id 不得互相覆盖（总线合并键要细化到 delta_id）；
 *   * 答案放行后才发现是工具轮：同一段文字移到过程区，标识不变、绝不重复；
 *   * TURN_END.final_content 只做校准（替换），不追加；
 *   * 取消/失败保留已确认文本并如实给状态。
 *
 * 基线（ee6bbff）现状：ASSISTANT 一律被当成 interim 气泡（events.ts 里
 * pushAssistant(content, true, true)），没有 delta_id/seq 去重 ——
 * 因此本文件在实现合并前应当是**红的**。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/streamingDeltas.verify.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";
import type { AgentEvent } from "../../services/events";

vi.mock("../../services/api", () => {
  const payload = () => ({
    tasks: [],
    messages: [],
    approvals: [],
    tools: [],
    narratives: [],
    instance_id: "inst_verify",
    turn_queue: { running: null, queued: [], cancelled: [], revision: 0, instance_id: "inst_verify" },
  });
  return {
    api: new Proxy({}, { get: () => vi.fn(async () => payload()) }),
    ApiError: class ApiError extends Error {},
  };
});

let seq = 0;

function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: "evt_" + seq, ts: new Date().toISOString(), data };
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const events = useEventStore();
  const session = useSessionStore();
  events.dispatch(ev("TURN_START", { turn_id: "turn_1", revision: 1 }));
  session.pushUser("帮我看看这个项目");
  return { events, session };
}

function assistantMessages(session: ReturnType<typeof useSessionStore>) {
  return session.messages.filter((message) => message.role === "assistant");
}

function answerMessages(session: ReturnType<typeof useSessionStore>) {
  return assistantMessages(session).filter((message) => !message.interim);
}

function answerDelta(content: string, deltaSeq: number, deltaId = "dl_turn1_1") {
  return ev("ASSISTANT", {
    turn_id: "turn_1",
    content,
    interim: false,
    streaming: true,
    delta_id: deltaId,
    seq: deltaSeq,
  });
}

function turnEnd(data: Record<string, unknown>): AgentEvent {
  return ev("TURN_END", {
    turn_id: "turn_1",
    status: "completed",
    final_content: null,
    duration_ms: 1500,
    queue_ms: 0,
    started_at: "2026-10-06T08:00:00+00:00",
    ended_at: "2026-10-06T08:00:01.500+00:00",
    ...data,
  });
}

beforeEach(() => {
  seq = 0;
});

describe("契约 §2.1：正式回答真流式", () => {
  it("provider 还没结束：正式回答已非空、处于 streaming、且不是「过程」", () => {
    const { events, session } = setup();

    events.dispatch(answerDelta("前半段回答", 1));

    const answer = answerMessages(session);
    expect(answer.length).toBe(1);
    expect(answer[0].content).toBe("前半段回答");
    expect(answer[0].streaming).toBe(true);
    expect(answer[0].interim ?? false).toBe(false);
    expect(session.turnRunning || session.activeTurnId === "turn_1").toBe(true);
  });

  it("累计快照就地更新：不追加第二个气泡、不回退", () => {
    const { events, session } = setup();
    events.dispatch(answerDelta("前半段", 1));
    events.dispatch(answerDelta("前半段，后半段", 2));

    expect(assistantMessages(session).length).toBe(1);
    expect(assistantMessages(session)[0].content).toBe("前半段，后半段");
  });

  it("回退 / 重复 seq 必须丢弃（重连补发不得让文字倒退或重复）", () => {
    const { events, session } = setup();
    events.dispatch(answerDelta("已经确认的一段话", 5));
    events.dispatch(answerDelta("已经确认", 3)); // 回退：必须整条丢弃
    expect(assistantMessages(session).map((message) => message.content)).toEqual([
      "已经确认的一段话",
    ]);

    events.dispatch(answerDelta("已经确认的一段话", 5)); // 重复：不得产生第二条
    expect(assistantMessages(session).length).toBe(1);
  });

  it("不同 delta_id 互不覆盖：seq 去重必须按 delta_id 各算各的", () => {
    const { events, session } = setup();
    // 两次模型调用，第二个 delta 的 seq 从 1 重新开始（每条流式消息各自计数）
    events.dispatch(answerDelta("第一次模型说的话", 3, "dl_turn1_1"));
    events.dispatch(answerDelta("第二次模型的正式回答", 1, "dl_turn1_2"));

    const texts = assistantMessages(session).map((message) => message.content);
    expect(texts, "seq 不是全局的：新 delta_id 的 seq=1 不得被旧的 seq=3 丢掉").toContain(
      "第二次模型的正式回答",
    );
    expect(texts, "不同的 delta_id 不得互相覆盖").toContain("第一次模型说的话");
  });
});

describe("契约 §2.1：唯一允许的改判 —— 不放行之后才发现是工具轮", () => {
  it("同一段文字从答案区移到过程区：只出现一次、标识不变", () => {
    const { events, session } = setup();
    const text = "这段话说完了我才决定调工具";

    events.dispatch(answerDelta(text, 1, "dl_turn1_9"));
    expect(
      answerMessages(session).some((message) => message.content === text),
      "守卫放行之后、改判之前，这段文字属于正式回答",
    ).toBe(true);

    // 同一个 delta_id 后续被判为工具轮（守卫放行后的改判）
    events.dispatch(
      ev("ASSISTANT", {
        turn_id: "turn_1",
        content: text,
        interim: true,
        streaming: true,
        delta_id: "dl_turn1_9",
        seq: 2,
      }),
    );

    const occurrences = session.messages.filter((message) => message.content.includes(text)).length;
    expect(occurrences).toBe(1);
    expect(
      answerMessages(session).some((message) => message.content.includes(text)),
      "改判之后答案区不得再显示这段文字",
    ).toBe(false);
  });
});

describe("契约 §2.1 规则 6、7：校准与失败", () => {
  it("TURN_END.final_content 只做校准：替换不追加、全文只出现一次", () => {
    const { events, session } = setup();
    events.dispatch(answerDelta("前半段，后半段", 1));
    events.dispatch(turnEnd({ final_content: "前半段，后半段" }));

    const texts = assistantMessages(session).map((message) => message.content);
    expect(texts.filter((text) => text.includes("前半段，后半段")).length).toBe(1);
    expect(texts.some((text) => text === "后半段")).toBe(false);
    expect(session.turnRunning).toBe(false);
  });

  it("取消：已确认文本保留、不当成最终答案、状态如实", () => {
    const { events, session } = setup();
    events.dispatch(answerDelta("已经生成的一段", 1));
    events.dispatch(turnEnd({ status: "cancelled", final_content: null }));

    const texts = assistantMessages(session).map((message) => message.content);
    expect(texts).toContain("已经生成的一段");
    expect(session.lastTurnOutcome?.status).toBe("cancelled");
    expect(session.turnRunning).toBe(false);
  });
});
