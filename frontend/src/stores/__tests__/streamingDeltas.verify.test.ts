/**
 * D 独立验证：流式增量在「事件 → store」这一层的契约（plan §1.1 最终版）。
 *
 * 契约来源：docs/plans/2026-10-06-audit-seven-fixes.md §1.1（Lead 2026-10-06 最终口径）。
 * 这里不渲染 DOM，只吃冻结的 SSE 载荷并断言对话状态：
 *   * 正文增量**一开始就以 interim=true 在过程区实时可见**（边生成边显示）；
 *   * 该次调用结束且没有工具调用 → **同一 delta_id** 用 {interim:false, streaming:false}
 *     原样**提升**到正式回答区（同一条消息，不重打、不重复）；
 *   * 调用结束时有工具调用 → 该段留在过程区；
 *   * **永久废止「正式回答 → 过程区」这个方向**：答案区文字不因后来的工具增量而消失或转移；
 *   * 累计快照语义：就地更新、不回退、不追加第二个气泡；
 *   * 回退/重复 seq 必须丢弃；不同 delta_id 不得互相覆盖（合并键细化到 delta_id）；
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

describe("契约 §1.1 最终版：正文先进过程区，无工具调用时原样提升", () => {
  function interimDelta(content: string, deltaSeq: number, deltaId = "dl_turn1_7", stageId?: string) {
    return ev("ASSISTANT", {
      turn_id: "turn_1",
      content,
      interim: true,
      streaming: true,
      delta_id: deltaId,
      seq: deltaSeq,
      ...(stageId ? { stage_id: stageId } : {}),
    });
  }

  it("正文一开始就在过程区实时可见（不是等 provider 结束才出现）", () => {
    const { events, session } = setup();
    events.dispatch(interimDelta("第一句。", 1));

    const process = assistantMessages(session).filter((message) => message.interim);
    expect(process.length, "过程说明必须已经落地（实时可见）").toBe(1);
    expect(process[0].content).toBe("第一句。");
    expect(process[0].streaming, "还在生成中").toBe(true);
    expect(answerMessages(session), "角色还没确定前不得出现在答案区").toEqual([]);
  });

  it("无工具调用：同一 delta_id 用 {interim:false, streaming:false} 原样提升到答案区", () => {
    const { events, session } = setup();
    const full = "第一句。第二句。";
    events.dispatch(interimDelta("第一句。", 1, "dl_turn1_7"));
    events.dispatch(interimDelta(full, 2, "dl_turn1_7"));
    // 调用结束且没有工具调用 → 提升（同一 delta_id，同一份文字）
    events.dispatch(
      ev("ASSISTANT", {
        turn_id: "turn_1",
        content: full,
        interim: false,
        streaming: false,
        delta_id: "dl_turn1_7",
        seq: 3,
      }),
    );

    const all = assistantMessages(session).filter((message) => message.content.trim());
    expect(all.length, "同一条消息只能有一份（不得既留在过程区又出现在答案区）").toBe(1);
    expect(all[0].content).toBe(full);
    expect(all[0].interim ?? false, "提升之后必须是正式回答").toBe(false);
    expect(answerMessages(session).length).toBe(1);
  });

  it("工具轮：正文留在过程区；答案区文字不因后来的工具增量消失或转移", () => {
    const { events, session } = setup();
    const toolTurnText = "我先说明一下，马上要调用工具。";
    events.dispatch(interimDelta(toolTurnText, 1, "dl_turn1_8", "st_turn1_1"));

    // 调用结束时有工具调用 → 这段是过程说明；随后的工具增量不得把它搬进答案区
    events.dispatch(
      ev("TOOL_START", { turn_id: "turn_1", call_id: "c1", tool: "fs_read", stage_id: "st_turn1_1" }),
    );
    events.dispatch(
      ev("TOOL_END", { turn_id: "turn_1", call_id: "c1", tool: "fs_read", ok: true, stage_id: "st_turn1_1" }),
    );
    expect(
      answerMessages(session).some((message) => message.content.includes(toolTurnText)),
      "工具轮的正文不得出现在答案区",
    ).toBe(false);
    expect(
      session.messages.filter((message) => message.content.includes(toolTurnText)).length,
      "同一段文字全局只有一份",
    ).toBe(1);

    // 之后到来的正式回答进答案区；再来的工具增量不得让它消失或转移
    const answer = "工具跑完了，这是正式回答。";
    events.dispatch(
      ev("ASSISTANT", {
        turn_id: "turn_1",
        content: answer,
        interim: false,
        streaming: false,
        delta_id: "dl_turn1_9",
        seq: 1,
      }),
    );
    events.dispatch(
      ev("TOOL_START", { turn_id: "turn_1", call_id: "c2", tool: "grep_search", stage_id: "st_turn1_1" }),
    );

    const answers = answerMessages(session).map((message) => message.content);
    expect(answers, "答案区文字不得因为工具增量而消失").toContain(answer);
    expect(
      answers.some((text) => text.includes(toolTurnText)),
      "过程说明不得被搬进答案区",
    ).toBe(false);
    expect(
      session.messages.filter((message) => message.content.includes(answer)).length,
      "答案区文字不得重复",
    ).toBe(1);
  });

  it("§1.1 废止的方向：迟到的 interim=true 不得把答案区文字搬回过程区", () => {
    const { events, session } = setup();
    const text = "这段文字已经进入正式回答区。";
    events.dispatch(
      ev("ASSISTANT", {
        turn_id: "turn_1",
        content: text,
        interim: false,
        streaming: false,
        delta_id: "dl_turn1_10",
        seq: 1,
      }),
    );
    expect(answerMessages(session).some((message) => message.content === text)).toBe(true);

    // 旧规则允许、§1.1 最终版**永久废止**的改判方向：答案 → 过程
    events.dispatch(interimDelta(text, 2, "dl_turn1_10"));

    expect(
      session.messages.filter((message) => message.content.includes(text)).length,
      "全局只能有一份（不得因此多出重复气泡）",
    ).toBe(1);
    expect(
      answerMessages(session).some((message) => message.content.includes(text)),
      "文字必须留在答案区，不得被搬回过程区",
    ).toBe(true);
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
