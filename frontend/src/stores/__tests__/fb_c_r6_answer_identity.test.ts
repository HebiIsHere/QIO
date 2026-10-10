/**
 * R6 反例（fb-c）：最终正文校正仍按文字相似度判断 -> 「12 -> 13」出两条正式回答、长 -> 短被忽略。
 *
 * 契约 K2.2（docs/plans/2026-10-10-final-boundaries-r1-r7.md，禁止文字相似度）：
 * * 有 answer_id（TURN_END 新字段 = 目标回答的 delta_id）：只更新该 turn 内
 *   deltaId === answer_id 的正文，绝不新建；找不到该身份 = 按缺失处理（不猜）。
 * * 无 answer_id（旧事件）：校准该 turn 内**最后一条** interim === false 的正式回答；
 *   该 turn 没有任何正式回答且 final_content 非空时才新建。
 * * final_content 缺省（undefined/null）= 不校准；显式空串 = 清空目标正文（保留消息与注记）。
 * * 重复 delta / 重复 TURN_END 幂等；旧 turn / 错误身份晚到不得污染当前回答；
 *   正常校准不重启动画、不把正式回答移进过程区。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/fb_c_r6_answer_identity.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import type { AgentEvent } from "../../services/events";
import { useEventStore } from "../events";
import { useSessionStore, splitTurnItems, SYSTEM_ANNOTATION_HEADER } from "../session";
import type { StreamMessage } from "../session";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({ topic_id: "", messages: [] })),
    sendTurn: vi.fn(async () => ({})),
  },
  ApiError: class ApiError extends Error {},
}));

const NOTE =
  SYSTEM_ANNOTATION_HEADER + "\n· 任务 ws_x 还没有测试证据\n不能当作「已完成」。";

let seq = 0;
function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: "r6_" + seq, ts: new Date().toISOString(), data };
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

function assistants(session: ReturnType<typeof useSessionStore>): StreamMessage[] {
  return session.messages.filter((m) => m.role === "assistant");
}

function formal(session: ReturnType<typeof useSessionStore>): StreamMessage[] {
  return assistants(session).filter((m) => m.interim !== true);
}

function pushAnswer(
  events: ReturnType<typeof useEventStore>,
  turnId: string,
  deltaId: string,
  text: string,
  seqNo = 1,
) {
  events.route(
    ev("ASSISTANT", {
      turn_id: turnId,
      content: text,
      interim: false,
      streaming: true,
      delta_id: deltaId,
      seq: seqNo,
    }),
  );
}

beforeEach(() => {
  seq = 0;
  localStorage.clear();
});

describe("R6：校准按回答身份，禁止文字相似度", () => {
  it("同身份 12 -> 13：只留一条正式回答，正文就地更新", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    pushAnswer(events, "turn_x", "dl_1", "12");
    events.route(ev("TURN_END", { turn_id: "turn_x", status: "completed", final_content: "13" }));

    const list = formal(session);
    expect(list, "不得因为正文不同再补一条").toHaveLength(1);
    expect(list[0]!.content).toBe("13");
  });

  it("长 -> 短：覆盖为目标正文，不能被相似度判定忽略", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    pushAnswer(events, "turn_x", "dl_1", "这是一段很长很长的正式回答内容，用于验证缩短场景。");
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    events.route(ev("TURN_END", { turn_id: "turn_x", status: "completed", final_content: "短" }));

    const list = formal(session);
    expect(list).toHaveLength(1);
    expect(list[0]!.content).toBe("短");
  });

  it("有 answer_id：只改目标那一份，同 turn 的其它回答身份各自保留", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    pushAnswer(events, "turn_x", "dl_1", "第一段回答。");
    pushAnswer(events, "turn_x", "dl_2", "第二段回答。");
    events.route(
      ev("TURN_END", {
        turn_id: "turn_x",
        status: "completed",
        final_content: "修订后的第一段回答。",
        answer_id: "dl_1",
      }),
    );

    const list = formal(session);
    expect(list, "两条不同身份都要保留").toHaveLength(2);
    expect(list[0]!.content).toBe("修订后的第一段回答。");
    expect(list[1]!.content, "非目标身份绝不能被改").toBe("第二段回答。");
  });

  it("answer_id 找不到该身份：按缺失处理，不新建也不改任何正文", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    pushAnswer(events, "turn_x", "dl_1", "原文。");
    events.route(
      ev("TURN_END", {
        turn_id: "turn_x",
        status: "completed",
        final_content: "不该出现的正文",
        answer_id: "dl_missing",
      }),
    );

    const list = formal(session);
    expect(list).toHaveLength(1);
    expect(list[0]!.content).toBe("原文。");
  });

  it("显式空串 = 清空目标正文（消息与注记保留）", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    pushAnswer(events, "turn_x", "dl_1", "将要被清空的正文。");
    events.route(
      ev("TURN_END", {
        turn_id: "turn_x",
        status: "completed",
        final_content: "",
        annotation: NOTE,
      }),
    );

    const list = formal(session);
    expect(list, "消息不能因为清空而消失").toHaveLength(1);
    expect(list[0]!.content, "正文清空后只剩注记").toBe(NOTE);
    expect(list[0]!.interim).not.toBe(true);
  });

  it("final_content 缺省（没有该字段）= 不校准正文，注记仍可挂上", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    pushAnswer(events, "turn_x", "dl_1", "已经流式显示的正文。");
    events.route(ev("TURN_END", { turn_id: "turn_x", status: "completed", annotation: NOTE }));

    const list = formal(session);
    expect(list).toHaveLength(1);
    expect(list[0]!.content).toContain("已经流式显示的正文。");
    expect(list[0]!.content).toContain("系统核对");
  });

  it("final_content 与 answer_id 都缺省：正文一个字都不动", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    pushAnswer(events, "turn_x", "dl_1", "保持原样。");
    events.route(ev("TURN_END", { turn_id: "turn_x", status: "completed" }));

    expect(formal(session)[0]!.content).toBe("保持原样。");
  });

  it("无 answer_id（旧事件）：只校准该 turn 最后一条正式回答，前面的身份保留", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    pushAnswer(events, "turn_x", "dl_1", "第一段回答。");
    pushAnswer(events, "turn_x", "dl_2", "第二段回答。");
    events.route(
      ev("TURN_END", { turn_id: "turn_x", status: "completed", final_content: "最后一段的校正版。" }),
    );

    const list = formal(session);
    expect(list).toHaveLength(2);
    expect(list[0]!.content).toBe("第一段回答。");
    expect(list[1]!.content).toBe("最后一段的校正版。");
  });

  it("该 turn 没有任何正式回答：final_content 非空才新建一条正式回答", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    events.route(
      ev("ASSISTANT", {
        turn_id: "turn_x",
        content: "我先查一下",
        interim: true,
        streaming: true,
        delta_id: "dl_mid",
        seq: 1,
      }),
    );
    events.route(ev("TURN_END", { turn_id: "turn_x", status: "completed", final_content: "正式回答。" }));

    const list = formal(session);
    expect(list).toHaveLength(1);
    expect(list[0]!.content).toBe("正式回答。");
    // 中间话仍然是中间话，不被当成回答
    expect(assistants(session).filter((m) => m.interim === true).map((m) => m.content)).toEqual([
      "我先查一下",
    ]);
  });

  it("重复 TURN_END / 相同正文重放：幂等，不产生第二条", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    pushAnswer(events, "turn_x", "dl_1", "同一条回答。");
    const end = ev("TURN_END", { turn_id: "turn_x", status: "completed", final_content: "同一条回答。" });
    events.route(end);
    events.route({ ...end, id: "r6_replay" });

    expect(formal(session)).toHaveLength(1);
    expect(formal(session)[0]!.content).toBe("同一条回答。");
  });

  it("delta 重放乱序（旧 seq 后到）不回退已到达的正文", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    pushAnswer(events, "turn_x", "dl_1", "13", 2);
    pushAnswer(events, "turn_x", "dl_1", "12", 1);
    events.route(ev("TURN_END", { turn_id: "turn_x", status: "completed", final_content: "14" }));

    const list = formal(session);
    expect(list).toHaveLength(1);
    expect(list[0]!.content).toBe("14");
  });

  it("旧协议（没有 delta_id 的整段推送）：校准仍只留一条", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    events.route(ev("ASSISTANT", { turn_id: "turn_x", content: "旧后端整段回答。", interim: false }));
    events.route(ev("TURN_END", { turn_id: "turn_x", status: "completed", final_content: "旧后端校正版。" }));

    const list = formal(session);
    expect(list).toHaveLength(1);
    expect(list[0]!.content).toBe("旧后端校正版。");
  });

  it("错误身份 / 旧 turn 晚到不得污染当前回答", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_a", revision: 1 }));
    pushAnswer(events, "turn_a", "dl_a", "A 的回答。");
    events.route(ev("TURN_END", { turn_id: "turn_a", status: "completed", final_content: "A 的回答。", revision: 2 }));
    events.route(ev("TURN_START", { turn_id: "turn_b", revision: 3 }));
    pushAnswer(events, "turn_b", "dl_b", "B 的回答。");

    // B 结束，但身份指向 A 的回答：按缺失处理，B 的正文不动
    events.route(
      ev("TURN_END", {
        turn_id: "turn_b",
        status: "completed",
        final_content: "不该出现的内容",
        answer_id: "dl_a",
        revision: 4,
      }),
    );

    const bAnswer = session.messages.find((m) => m.role === "assistant" && m.turnId === "turn_b");
    expect(bAnswer?.content).toBe("B 的回答。");
    const aAnswer = session.messages.find((m) => m.role === "assistant" && m.turnId === "turn_a");
    expect(aAnswer?.content).toBe("A 的回答。");
  });

  it("正常校准不重启动画、不把正式回答移进过程区", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    pushAnswer(events, "turn_x", "dl_1", "12", 1);
    pushAnswer(events, "turn_x", "dl_1", "123", 2);
    const before = formal(session)[0]!;
    const idBefore = before.id;
    expect(before.assistantGrew).toBe(true);

    events.route(
      ev("TURN_END", { turn_id: "turn_x", status: "completed", final_content: "1234", answer_id: "dl_1" }),
    );

    const after = formal(session);
    expect(after).toHaveLength(1);
    expect(after[0]!.id).toBe(idBefore);
    expect(after[0]!.assistantGrew, "增量标记保留：逐字动画不重播").toBe(true);
    expect(after[0]!.interim).not.toBe(true);
    // 正式回答仍然在正文区（不在过程区）
    const view = splitTurnItems(session.messages);
    expect(view.answers.map((m) => m.id)).toContain(idBefore);
  });

  it("缺省 final_content 但给了 answer_id 与注记：只挂注记，正文不动", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    pushAnswer(events, "turn_x", "dl_1", "保持不变的正文。");
    session.applyFinalAnswer(null, undefined, { turnId: "turn_x", answerId: "dl_1", annotation: NOTE });

    expect(formal(session)).toHaveLength(1);
    expect(formal(session)[0]!.content).toContain("保持不变的正文。");
    expect(formal(session)[0]!.content).toContain("系统核对");
  });

  it("显式空串 + answer_id 只清空目标那一份", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    pushAnswer(events, "turn_x", "dl_1", "第一段。");
    pushAnswer(events, "turn_x", "dl_2", "第二段。");
    session.applyFinalAnswer("", undefined, { turnId: "turn_x", answerId: "dl_1" });

    const list = formal(session);
    expect(list).toHaveLength(2);
    expect(list[0]!.content).toBe("");
    expect(list[1]!.content).toBe("第二段。");
  });
});
