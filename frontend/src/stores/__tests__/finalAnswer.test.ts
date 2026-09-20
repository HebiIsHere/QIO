/**
 * 最终回答的唯一权威来源是 TURN_END.final_content。
 * 回归的是这个真实缺陷：模型先说话、再调工具、再说一次，最后给最终回答时，
 * 界面把「工具前的中间话」当成最终答案，真正的 final_content 被丢掉。
 */
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

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

function assistantTexts(session: ReturnType<typeof useSessionStore>) {
  return session.messages.filter((m) => m.role === "assistant");
}

describe("最终回答不会被中间话覆盖", () => {
  it("interim → tool → interim → TURN_END(final) 显示 final", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "t1" } });
    events.route({
      type: "ASSISTANT",
      id: "2",
      ts: "",
      data: { content: "我先看看仓库", interim: true },
    });
    events.route({ type: "TOOL_END", id: "3", ts: "", data: { tool: "fs_list", ok: true } });
    events.route({
      type: "ASSISTANT",
      id: "4",
      ts: "",
      data: { content: "再看一眼配置", interim: true },
    });
    events.route({
      type: "TURN_END",
      id: "5",
      ts: "",
      data: { turn_id: "t1", status: "completed", final_content: "检查完成。真正的问题是端口冲突。" },
    });

    const assistants = assistantTexts(session);
    expect(assistants[assistants.length - 1]?.content).toBe("检查完成。真正的问题是端口冲突。");
    expect(assistants[assistants.length - 1]?.interim).not.toBe(true);
    // 中间话保留为「过程」，不能被当成最终答案
    expect(assistants.some((m) => m.content === "我先看看仓库" && m.interim === true)).toBe(true);
    expect(assistants.some((m) => m.content === "再看一眼配置" && m.interim === true)).toBe(true);
    expect(session.turnRunning).toBe(false);
  });

  it("final_content 与流式文本相同时不重复追加", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "t2" } });
    events.route({ type: "ASSISTANT", id: "2", ts: "", data: { content: "就一句回答", interim: true } });
    events.route({
      type: "TURN_END",
      id: "3",
      ts: "",
      data: { turn_id: "t2", status: "completed", final_content: "就一句回答" },
    });
    const assistants = assistantTexts(session);
    expect(assistants).toHaveLength(1);
    expect(assistants[0]?.interim).not.toBe(true);
  });

  it("失败且没有 final_content 时，中间话不得变成最终回答", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "t3" } });
    events.route({ type: "ASSISTANT", id: "2", ts: "", data: { content: "我先查一下", interim: true } });
    events.route({
      type: "TURN_END",
      id: "3",
      ts: "",
      data: { turn_id: "t3", status: "failed", final_content: null, error: "provider exploded" },
    });
    const assistants = assistantTexts(session);
    expect(assistants).toHaveLength(1);
    expect(assistants[0]?.interim).toBe(true);
    expect(session.lastError).toBe("provider exploded");
    expect(session.turnRunning).toBe(false);
  });

  it("无凭据（unavailable）结束 turn 并给出提示", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "t4" } });
    events.route({
      type: "TURN_END",
      id: "2",
      ts: "",
      data: { turn_id: "t4", status: "unavailable", final_content: null, error: "no_credential" },
    });
    expect(session.turnRunning).toBe(false);
    expect(session.activeTurnId).toBeNull();
    expect(session.warning).toContain("凭据");
  });

  it("取消是安静的正常结局，不当错误", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "t5" } });
    events.route({
      type: "TURN_END",
      id: "2",
      ts: "",
      data: { turn_id: "t5", status: "cancelled", final_content: null },
    });
    expect(session.turnRunning).toBe(false);
    expect(session.lastError).toBeNull();
    expect(session.lastTurnOutcome?.status).toBe("cancelled");
  });

  it("同一 turn 的重复 TURN_END（重连重放）只生效一次", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "t6" } });
    events.route({
      type: "TURN_END",
      id: "2",
      ts: "",
      data: { turn_id: "t6", status: "completed", final_content: "答案" },
    });
    events.route({
      type: "TURN_END",
      id: "3",
      ts: "",
      data: { turn_id: "t6", status: "completed", final_content: "答案" },
    });
    expect(assistantTexts(session)).toHaveLength(1);
  });
});
