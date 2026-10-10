/**
 * fb-E / R6 反例：最终正文校准仍按文字相似度判断，生成两份回答 / 忽略缩短。
 *
 * 用户可见规则（冻结契约 K2.2）：
 *  * 有 answer_id：只更新该 turn 内 deltaId === answer_id 的那条回答，**不新建**第二条；
 *  * 无 answer_id（旧事件）：校准该 turn 内**最后一条已发布的正式回答**（interim !== true）；
 *    不得比较全文 / 前缀；turn 内没有任何正式回答且 final_content 非空时才新建；
 *  * 缩短、非前缀改写都必须生效（12→13、长→短）。
 *
 * 基线：applyFinalAnswer 走 mergeFinalBody（全文/前缀比较），
 *   —— 12→13：既不相等也不互为前缀 → pushAssistant 追加第二条；
 *   —— 长→短：existing.startsWith(body) → 返回旧长文，缩短被静默忽略。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/fb_e_r6_answer_identity.test.ts
 */
import { describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore, type StreamMessage } from "../session";

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

function assistants(session: ReturnType<typeof useSessionStore>): StreamMessage[] {
  return session.messages.filter((m) => m.role === "assistant");
}

function start(events: ReturnType<typeof useEventStore>, turn: string) {
  events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: turn } });
}

function answer(
  events: ReturnType<typeof useEventStore>,
  turn: string,
  content: string,
  deltaId: string,
) {
  events.route({
    type: "ASSISTANT",
    id: "2",
    ts: "",
    data: { turn_id: turn, content, interim: false, streaming: true, delta_id: deltaId, seq: 1 },
  });
}

describe("R6 回答身份校准（不是文字相似度）", () => {
  it("同一身份 12→13：只更新那一条，不得出现两条正式回答", () => {
    const { events, session } = setup();
    start(events, "turn_r6a");
    answer(events, "turn_r6a", "12", "dl_r6a");
    events.route({
      type: "TURN_END",
      id: "3",
      ts: "",
      data: { turn_id: "turn_r6a", status: "completed", final_content: "13", answer_id: "dl_r6a" },
    });

    const list = assistants(session);
    expect(list, "同一回答身份被校准成了两条正式回答").toHaveLength(1);
    expect(list[0]!.content).toBe("13");
  });

  it("同一身份长→短：缩短必须生效（不得因前缀比较被忽略）", () => {
    const { events, session } = setup();
    start(events, "turn_r6b");
    answer(events, "turn_r6b", "这是一段很长的回答，需要被最终校准成更短的版本。", "dl_r6b");
    events.route({
      type: "TURN_END",
      id: "3",
      ts: "",
      data: { turn_id: "turn_r6b", status: "completed", final_content: "短版", answer_id: "dl_r6b" },
    });

    const list = assistants(session);
    expect(list).toHaveLength(1);
    expect(list[0]!.content, "长→短的最终校对被前缀相似度逻辑忽略").toBe("短版");
  });

  it("旧事件没有 answer_id：校准 turn 内最后一条已发布的正式回答，不新建", () => {
    const { events, session } = setup();
    start(events, "turn_r6c");
    answer(events, "turn_r6c", "12", "dl_r6c");
    events.route({
      type: "TURN_END",
      id: "3",
      ts: "",
      data: { turn_id: "turn_r6c", status: "completed", final_content: "13" },
    });

    const list = assistants(session);
    expect(list, "旧事件（无 answer_id）也必须就地校准，不得复制成两条").toHaveLength(1);
    expect(list[0]!.content).toBe("13");
  });

  it("旧事件没有 answer_id 且正文完全无关：仍是同一条回答（不得按相似度再建一条）", () => {
    const { events, session } = setup();
    start(events, "turn_r6d");
    answer(events, "turn_r6d", "第一段回答。", "dl_r6d");
    events.route({
      type: "TURN_END",
      id: "3",
      ts: "",
      data: { turn_id: "turn_r6d", status: "completed", final_content: "这是完全不同的一段回答。" },
    });
    expect(assistants(session)).toHaveLength(1);
    expect(assistants(session)[0]!.content).toBe("这是完全不同的一段回答。");
  });
});
