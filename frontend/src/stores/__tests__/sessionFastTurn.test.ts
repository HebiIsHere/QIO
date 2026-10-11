/**
 * 快轮次的竞态：`POST /api/turns` 的回执晚于 `TURN_END` 时，界面不能卡在「运行中」。
 *
 * 真实场景（本机无凭据）：后端十几毫秒就走完 TURN_START → TURN_END，
 * 而发送请求的回执还没回来。修复前 `send()` 会在回执里无条件 `turnRunning = true`，
 * 之后再也没有 TURN_END 来熄灭它 —— 停止按钮一直在、发送一直被禁用。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { useEventStore } from "../events";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [],
    })),
    sendTurn: vi.fn(async () => ({ ok: true, accepted: true, turn_id: "turn_fast", status: "accepted" })),
    cancelTurn: vi.fn(async () => ({ ok: true, cancelled: true, turn_id: "turn_fast" })),
    cancelActiveTurn: vi.fn(async () => ({ ok: true, cancelled: true, turn_id: null })),
    getTurnQueue: vi.fn(async () => ({ running: null, queued: [], cancelled: [], revision: 1, instance_id: "i" })),
    getRuntimeState: vi.fn(async () => ({
      instance_id: "i",
      revision: 1,
      turn_queue: { instance_id: "i", revision: 1, running: null, queued: [], cancelled: [] },
      approvals: [],
      tasks: [],
    })),
  },
}));

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { session: useSessionStore(), events: useEventStore() };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("发送回执与 TURN_END 的先后", () => {
  it("TURN_END 先到：回执迟到也不能把已结束的一轮重新点亮", async () => {
    const { session, events } = setup();
    // 明确的「稍后再给回执」闸门：release 用 definite assignment 声明，
    // 避免 TS 把闭包里的赋值当成「永远是 null」而把调用点收窄成 never。
    let release!: (value: unknown) => void;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    vi.mocked(api.sendTurn).mockImplementation(() => gate as never);

    const sending = session.send("快问快答");
    // 回执还没回来，但这一轮已经跑完（没有凭据时的真实形状）
    events.route({ type: "TURN_START", id: "s1", ts: "", data: { turn_id: "turn_fast", revision: 2, instance_id: "i" } });
    events.route({ type: "TURN_END", id: "e1", ts: "", data: { turn_id: "turn_fast", revision: 3, status: "unavailable", final_content: "" } });
    expect(session.turnRunning).toBe(false);

    release({ ok: true, accepted: true, turn_id: "turn_fast", status: "accepted", topic_id: null });
    await sending;
    expect(session.turnRunning).toBe(false);
    expect(session.turnPhase).toBe("idle");
  });

  it("正常顺序（回执先到、TURN_END 后到）仍然按运行中呈现，然后被 TURN_END 熄灭", async () => {
    const { session, events } = setup();
    vi.mocked(api.sendTurn).mockResolvedValue({
      ok: true,
      accepted: true,
      turn_id: "turn_slow",
      status: "accepted",
      topic_id: null,
    });
    await session.send("正常一轮");
    expect(session.turnRunning).toBe(true);
    events.route({ type: "TURN_START", id: "s2", ts: "", data: { turn_id: "turn_slow", revision: 6, instance_id: "i" } });
    events.route({ type: "TURN_END", id: "e2", ts: "", data: { turn_id: "turn_slow", revision: 7, status: "completed", final_content: "好" } });
    expect(session.turnRunning).toBe(false);
  });
});
