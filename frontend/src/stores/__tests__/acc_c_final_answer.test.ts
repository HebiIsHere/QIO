/**
 * F11 反例（acc-c）：TURN_END 的 final_content 附系统核对注释时，
 * 整个正式回答不得重复出现。
 *
 * 两种形态都要兼容：
 *  * 旧形态：注释内嵌在 final_content 末尾（core/turn_facts.py::ANNOTATION_HEADER）；
 *  * 新形态：注释走独立字段（字段名以后端/集成 diff 为准，这里覆盖常见几个）。
 */
import { describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";
import type { StreamMessage } from "../session";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({ topic_id: "", messages: [] })),
    sendTurn: vi.fn(async () => ({})),
  },
}));

const BODY = "文件暂时无法读取。";
const ANNOTATION =
  "—— 系统核对（后端事实，不是模型的说法）：\n· 任务 ws_x 还没有测试证据\n这几项没有通过验证，不能当作「已完成 / 可使用」。";

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

function assistantMessages(session: ReturnType<typeof useSessionStore>): StreamMessage[] {
  return session.messages.filter((m) => m.role === "assistant");
}

function countOf(text: string, needle: string): number {
  return text.split(needle).length - 1;
}

describe("F11：附注释不复制正文", () => {
  it("旧形态：注释内嵌 final_content —— 正文只出现一次，注释也在同一条里", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "turn_a" } });
    events.route({
      type: "ASSISTANT",
      id: "2",
      ts: "",
      data: { turn_id: "turn_a", content: BODY, interim: false, streaming: true, delta_id: "dl_a", seq: 1 },
    });
    events.route({
      type: "TURN_END",
      id: "3",
      ts: "",
      data: {
        turn_id: "turn_a",
        status: "completed",
        final_content: BODY + "\n\n" + ANNOTATION,
        duration_ms: 3000,
      },
    });

    const list = assistantMessages(session);
    expect(list).toHaveLength(1);
    expect(countOf(list[0]!.content, BODY), "正文只出现一次").toBe(1);
    expect(countOf(list[0]!.content, "系统核对"), "注释只出现一次").toBe(1);
    expect(list[0]!.interim).not.toBe(true);
  });

  it("新形态（独立 annotation 字段）：复用回答并就地补注释，不复制正文", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "turn_b" } });
    events.route({
      type: "ASSISTANT",
      id: "2",
      ts: "",
      data: { turn_id: "turn_b", content: BODY, interim: false, streaming: true, delta_id: "dl_b", seq: 1 },
    });
    events.route({
      type: "TURN_END",
      id: "3",
      ts: "",
      data: { turn_id: "turn_b", status: "completed", final_content: BODY, annotation: ANNOTATION },
    });

    const list = assistantMessages(session);
    expect(list).toHaveLength(1);
    expect(countOf(list[0]!.content, BODY)).toBe(1);
    expect(list[0]!.content).toContain("系统核对");
    expect(countOf(list[0]!.content, "系统核对")).toBe(1);
  });

  it("新形态（独立 final_annotation 字段）同样只保留一份正文", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "turn_c" } });
    events.route({
      type: "ASSISTANT",
      id: "2",
      ts: "",
      data: { turn_id: "turn_c", content: BODY, interim: false, streaming: false, delta_id: "dl_c", seq: 1 },
    });
    events.route({
      type: "TURN_END",
      id: "3",
      ts: "",
      data: {
        turn_id: "turn_c",
        status: "completed",
        final_content: BODY + "\n\n" + ANNOTATION,
        final_annotation: ANNOTATION,
      },
    });
    const list = assistantMessages(session);
    expect(list).toHaveLength(1);
    expect(countOf(list[0]!.content, BODY)).toBe(1);
    expect(countOf(list[0]!.content, "系统核对")).toBe(1);
  });

  it("不重启整段打字动画：复用同一条消息对象（id / 增量标记不变）", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "turn_d" } });
    events.route({
      type: "ASSISTANT",
      id: "2",
      ts: "",
      data: { turn_id: "turn_d", content: BODY, interim: false, streaming: true, delta_id: "dl_d", seq: 1 },
    });
    // 第二次增量：这条消息走「就地增长」，组件据此不再做逐字点亮
    events.route({
      type: "ASSISTANT",
      id: "2b",
      ts: "",
      data: { turn_id: "turn_d", content: BODY + "。", interim: false, streaming: true, delta_id: "dl_d", seq: 2 },
    });
    const before = assistantMessages(session)[0]!;
    const idBefore = before.id;
    expect(before.assistantGrew).toBe(true);
    events.route({
      type: "TURN_END",
      id: "3",
      ts: "",
      data: {
        turn_id: "turn_d",
        status: "completed",
        final_content: BODY + "。\n\n" + ANNOTATION,
      },
    });
    const after = assistantMessages(session);
    expect(after).toHaveLength(1);
    expect(after[0]!.id).toBe(idBefore);
    expect(after[0]!.assistantGrew, "增量标记保留，逐字动画不重播").toBe(true);
    expect(after[0]!.streaming, "落定为静态").toBeUndefined();
  });

  it("旧协议（没有 answer_id）：正文不同也不再补第二条 —— 校准该 turn 最后一条正式回答", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "turn_e" } });
    events.route({
      type: "ASSISTANT",
      id: "2",
      ts: "",
      data: { turn_id: "turn_e", content: "第一段回答。", interim: false, streaming: false, delta_id: "dl_e1", seq: 1 },
    });
    events.route({
      type: "TURN_END",
      id: "3",
      ts: "",
      data: { turn_id: "turn_e", status: "completed", final_content: "这是完全不同的一段回答。" },
    });
    /**
     * 契约 K2.2：禁止用「全文是否相等 / 前缀」判断同一次回答 —— 无 answer_id 的旧事件
     * 一律校准该 turn 最后一条正式回答（interim === false）。
     */
    const list = assistantMessages(session);
    expect(list, "不得因为正文不同再补一条").toHaveLength(1);
    expect(list.map((m) => m.content)).toEqual(["这是完全不同的一段回答。"]);
  });

  it("新协议（answer_id）：同 turn 的多个回答身份各自保留，只改目标那一条", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "turn_g" } });
    events.route({
      type: "ASSISTANT",
      id: "2",
      ts: "",
      data: { turn_id: "turn_g", content: "第一段回答。", interim: false, streaming: true, delta_id: "dl_g1", seq: 1 },
    });
    events.route({
      type: "ASSISTANT",
      id: "3",
      ts: "",
      data: { turn_id: "turn_g", content: "第二段回答。", interim: false, streaming: true, delta_id: "dl_g2", seq: 1 },
    });
    events.route({
      type: "TURN_END",
      id: "4",
      ts: "",
      data: {
        turn_id: "turn_g",
        status: "completed",
        final_content: "修订后的第一段回答。",
        answer_id: "dl_g1",
      },
    });
    const list = assistantMessages(session);
    expect(list, "两条不同身份都要保留").toHaveLength(2);
    expect(list.map((m) => m.content)).toEqual(["修订后的第一段回答。", "第二段回答。"]);
  });

  it("过程说明（interim）不会被注释复制成正式回答", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "turn_f" } });
    events.route({
      type: "ASSISTANT",
      id: "2",
      ts: "",
      data: { turn_id: "turn_f", content: "我先查一下", interim: true, streaming: true, delta_id: "dl_f1", seq: 1 },
    });
    events.route({
      type: "ASSISTANT",
      id: "3",
      ts: "",
      data: { turn_id: "turn_f", content: BODY, interim: false, streaming: true, delta_id: "dl_f2", seq: 1 },
    });
    events.route({
      type: "TURN_END",
      id: "4",
      ts: "",
      data: { turn_id: "turn_f", status: "completed", final_content: BODY + "\n\n" + ANNOTATION },
    });
    const interims = session.messages.filter((m) => m.role === "assistant" && m.interim === true);
    const answers = assistantMessages(session).filter((m) => m.interim !== true);
    expect(interims.map((m) => m.content)).toEqual(["我先查一下"]);
    expect(answers).toHaveLength(1);
    expect(countOf(answers[0]!.content, BODY)).toBe(1);
  });
});
