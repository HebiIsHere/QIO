/**
 * R4 问题一（store 级钉子）：同一 delta_id 的流式增量 → 一条消息、内容单调增长、
 * 收尾快照就地更新；失败/断流不得把已发布的正式回答移回过程区。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/answerStreaming.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import type { AgentEvent } from "../../services/events";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";

vi.mock("../../services/api", () => ({
  api: { getSessionContext: vi.fn(async () => ({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] })) },
  ApiError: class ApiError extends Error {},
}));

let seq = 0;
function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: "as_" + seq, ts: new Date().toISOString(), data };
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

function assistants(session: ReturnType<typeof useSessionStore>) {
  return session.messages.filter((m) => m.role === "assistant");
}

beforeEach(() => {
  seq = 0;
});

describe("回答调用的流式增量（契约 §1.1）", () => {
  it("同一 delta_id：只产生一条消息，内容按累计快照单调增长", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "t1", revision: 1 }));
    events.route(ev("ASSISTANT", { turn_id: "t1", content: "第一段", interim: false, streaming: true, delta_id: "dl_a", seq: 1 }));
    events.route(ev("ASSISTANT", { turn_id: "t1", content: "第一段，第二段", interim: false, streaming: true, delta_id: "dl_a", seq: 2 }));

    const list = assistants(session);
    expect(list).toHaveLength(1);
    expect(list[0]?.content).toBe("第一段，第二段");
    expect(list[0]?.interim).toBe(false);
    expect(list[0]?.streaming).toBe(true);
    expect(list[0]?.assistantSeq).toBe(2);
    // 增量已到达过：DOM 侧据此不再对这段文字做打字机（见 MessageItem）
    expect(list[0]?.assistantGrew).toBe(true);
  });

  it("seq 回退 / 重复的事件被丢弃：内容不回退、不重复", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "t2", revision: 1 }));
    events.route(ev("ASSISTANT", { turn_id: "t2", content: "一段", interim: false, streaming: true, delta_id: "dl_b", seq: 1 }));
    events.route(ev("ASSISTANT", { turn_id: "t2", content: "一段加长", interim: false, streaming: true, delta_id: "dl_b", seq: 2 }));
    events.route(ev("ASSISTANT", { turn_id: "t2", content: "旧内容", interim: false, streaming: true, delta_id: "dl_b", seq: 2 }));

    expect(assistants(session)).toHaveLength(1);
    expect(assistants(session)[0]?.content).toBe("一段加长");
  });

  it("收尾快照（streaming=false）就地校准：不新建、内容取累计全文、streaming 清除", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "t3", revision: 1 }));
    events.route(ev("ASSISTANT", { turn_id: "t3", content: "半句", interim: false, streaming: true, delta_id: "dl_c", seq: 1 }));
    events.route(ev("ASSISTANT", { turn_id: "t3", content: "半句，完整了。", interim: false, streaming: false, delta_id: "dl_c", seq: 2 }));

    const list = assistants(session);
    expect(list).toHaveLength(1);
    expect(list[0]?.content).toBe("半句，完整了。");
    expect(list[0]?.streaming).toBeFalsy();
    expect(list[0]?.interim).toBe(false);
  });

  it("失败且没有 final_content：已发布的正式回答不得被移回过程区", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "t4", revision: 1 }));
    events.route(ev("ASSISTANT", { turn_id: "t4", content: "已经流出来的正式回答。", interim: false, streaming: true, delta_id: "dl_d", seq: 1 }));
    events.route(ev("TURN_END", { turn_id: "t4", status: "failed", reason_code: "provider_error", reason: "连接中断", final_content: "" }));

    const list = assistants(session);
    expect(list).toHaveLength(1);
    expect(list[0]?.content).toBe("已经流出来的正式回答。");
    expect(list[0]?.interim, "正式回答 → 过程区永远不允许").toBe(false);
  });

  it("工作调用的 interim 正文仍只进过程区（不被回答阶段影响）", () => {
    const { events, session } = setup();
    events.route(ev("TURN_START", { turn_id: "t5", revision: 1 }));
    events.route(ev("ASSISTANT", { turn_id: "t5", content: "过程说明", interim: true, streaming: true, delta_id: "dl_p", seq: 1, stage_id: "st_1" }));

    const list = assistants(session);
    expect(list).toHaveLength(1);
    expect(list[0]?.interim).toBe(true);
    expect(list[0]?.stageId).toBe("st_1");
  });
});
