import { describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] })),
    sendTurn: vi.fn(async () => ({})),
  },
}));

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

describe("迟到事件不得影响当前任务（任务01 D）", () => {
  it("属于旧 turn 的 TURN_END 不会把正在运行的新任务标记成结束", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "e1", ts: "", data: { turn_id: "turn_new" } });
    expect(session.turnRunning).toBe(true);
    expect(session.activeTurnId).toBe("turn_new");

    // 早先被取消的 turn 此时才把收尾事件送到
    events.route({ type: "TURN_END", id: "e2", ts: "", data: { turn_id: "turn_old" } });

    expect(session.turnRunning).toBe(true);
    expect(session.activeTurnId).toBe("turn_new");
  });

  it("属于当前 turn 的 TURN_END 正常结束运行态", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "e1", ts: "", data: { turn_id: "turn_a" } });
    events.route({ type: "TURN_END", id: "e2", ts: "", data: { turn_id: "turn_a" } });
    expect(session.turnRunning).toBe(false);
    expect(session.activeTurnId).toBeNull();
  });
});
