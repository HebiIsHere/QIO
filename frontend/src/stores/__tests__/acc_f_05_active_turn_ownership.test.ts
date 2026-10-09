/**
 * F 独立验证（阶段一 · F05 前端侧证据）：排队不改变活动 turn 归属。
 *
 * 说明：F05 的后端链路反例见 backend/tests/test_acc_f_05_turn_ownership.py（基线绿）。
 * 前端这一侧的历史缺陷（session.ts 2862 注释所指）：SEND 受理回执把**排队中的** turn
 * 写成 activeTurnId，导致 Stop 打到错的目标、真正在跑那一轮的 TURN_END 被丢弃。
 * 该缺陷已在 2b204d7 修复；本用例是**已有证据 / 回归守卫**（基线应当绿），
 * 不制造假红灯。
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
    getRuntimeState: vi.fn(async () => ({
      instance_id: "i",
      revision: 1,
      turn_queue: { instance_id: "i", revision: 1, running: null, queued: [], cancelled: [] },
      approvals: [],
      tasks: [],
      tools: [],
    })),
  },
}));

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

describe("F05 排队不改变活动 turn 归属（前端 store）", () => {
  it("A 运行中登记 B/C 排队：active 仍是 A，别的 turn 的正文不进主对话", () => {
    const { events, session } = setup();

    events.route({ type: "TURN_START", id: "1", ts: "", data: { turn_id: "turn_A", revision: 1, instance_id: "i" } });
    expect(session.activeTurnId).toBe("turn_A");

    // 受理回执只登记排队（真实修复后 sendMessage 走的就是这条），不得写 active
    session.markTurnQueued("turn_B");
    session.markTurnQueued("turn_C");
    expect(session.activeTurnId).toBe("turn_A");
    expect(session.queuedTurnIds).toEqual(["turn_B", "turn_C"]);

    events.route({ type: "ASSISTANT", id: "2", ts: "", data: { turn_id: "turn_A", content: "A 的第一段" } });
    const assistantCount = session.messages.filter((m) => m.role === "assistant").length;

    // 排队中的 B 即便有事件，也不得进主对话、不得改 active
    events.route({ type: "ASSISTANT", id: "3", ts: "", data: { turn_id: "turn_B", content: "B 的回答" } });
    expect(session.messages.filter((m) => m.role === "assistant").length).toBe(assistantCount);
    expect(session.activeTurnId).toBe("turn_A");

    // A 结束：active 清空，B/C 仍在排队
    events.route({
      type: "TURN_END",
      id: "4",
      ts: "",
      data: { turn_id: "turn_A", status: "completed", final_content: "A 的第一段", revision: 2 },
    });
    expect(session.activeTurnId).toBeNull();
    expect(session.queuedTurnIds).toEqual(["turn_B", "turn_C"]);

    // B 真正开始（TURN_START 是唯一允许设 active 的入口）
    events.route({ type: "TURN_START", id: "5", ts: "", data: { turn_id: "turn_B", revision: 3, instance_id: "i" } });
    expect(session.activeTurnId).toBe("turn_B");
    expect(session.queuedTurnIds).toEqual(["turn_C"]);
  });
});
