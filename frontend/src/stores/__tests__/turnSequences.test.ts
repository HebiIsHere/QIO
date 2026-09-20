/**
 * Turn 状态序列测试：单个赋值正确 ≠ 组合起来正确。
 *
 * 这一组测试复现的核心缺陷：
 *   A 正在运行，用户发送 B，后端只是把 B 排队；
 *   但 SEND 返回 B 的 turn_id 后前端立刻 activeTurnId = B。
 * 后果有两层：Stop 会打到 B（真正在跑的是 A），且 TURN_END(A) 被
 * 「不属于当前 active」的过滤条件丢掉 → 界面永远停在运行中。
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
    sendTurn: vi.fn(async () => ({ ok: true, accepted: true, turn_id: "turn_b", status: "queued" })),
    cancelTurn: vi.fn(async () => ({ ok: true, cancelled: true, turn_id: "turn_b" })),
    cancelActiveTurn: vi.fn(async () => ({ ok: true, cancelled: true, turn_id: "turn_a" })),
  },
}));

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { session: useSessionStore(), events: useEventStore() };
}

function start(session: ReturnType<typeof useSessionStore>, events: ReturnType<typeof useEventStore>, id: string) {
  events.route({ type: "TURN_START", id: `s_${id}`, ts: "", data: { turn_id: id } });
  expect(session.activeTurnId).toBe(id);
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("连续发送：queued 不覆盖 active", () => {
  it("A START → B SEND：active 仍然是 A，B 只是排队", async () => {
    const { session, events } = setup();
    start(session, events, "turn_a");

    await session.send("第二条消息");

    expect(session.activeTurnId).toBe("turn_a");
    expect(session.queuedTurnIds).toContain("turn_b");
    expect(session.turnRunning).toBe(true);
  });

  it("A END → B START 之后 active 才变成 B", async () => {
    const { session, events } = setup();
    start(session, events, "turn_a");
    await session.send("第二条消息");

    events.route({ type: "TURN_END", id: "e_a_end", ts: "", data: { turn_id: "turn_a", status: "completed" } });
    expect(session.turnRunning).toBe(false);
    expect(session.activeTurnId).toBeNull();

    start(session, events, "turn_b");
    expect(session.activeTurnId).toBe("turn_b");
    expect(session.queuedTurnIds).not.toContain("turn_b");
  });

  it("正在运行那一轮的 TURN_END 不会被排队消息顶掉", async () => {
    const { session, events } = setup();
    start(session, events, "turn_a");
    await session.send("第二条消息");

    events.route({ type: "TURN_END", id: "e_a_end", ts: "", data: { turn_id: "turn_a", status: "completed" } });

    // 这是本次修复的关键回归点：以前 activeTurnId 已被 B 覆盖，
    // 真正的 TURN_END(A) 会被当成旧事件丢掉，界面永远停在运行中。
    expect(session.turnRunning).toBe(false);
    expect(session.activeTurnId).toBeNull();
  });

  it("TURN_QUEUE 快照是第二真源：重连后能恢复 active", () => {
    const { session, events } = setup();
    events.route({
      type: "TURN_QUEUE",
      id: "q1",
      ts: "",
      data: {
        running: { turn_id: "turn_a", message: "第一条" },
        queued: [{ turn_id: "turn_b", message: "第二条" }],
        cancelled: [],
      },
    });
    expect(session.activeTurnId).toBe("turn_a");
    expect(session.queuedTurnIds).toEqual(["turn_b"]);
    expect(session.turnRunning).toBe(true);
  });

  it("队列被取消后本地排队标记同步清空", () => {
    const { session, events } = setup();
    events.route({
      type: "TURN_QUEUE",
      id: "q1",
      ts: "",
      data: { running: { turn_id: "turn_a", message: "一" }, queued: [{ turn_id: "turn_b", message: "二" }], cancelled: [] },
    });
    events.route({
      type: "TURN_QUEUE",
      id: "q2",
      ts: "",
      data: { running: { turn_id: "turn_a", message: "一" }, queued: [], cancelled: [{ turn_id: "turn_b", message: "二" }] },
    });
    expect(session.queuedTurnIds).toEqual([]);
  });
});

describe("Stop 只针对真正在跑的 turn", () => {
  it("A START + B queued + Stop → 取消 A，不动 B", async () => {
    const { session, events } = setup();
    start(session, events, "turn_a");
    await session.send("第二条消息");

    await session.stopActiveTurn();

    expect(api.cancelTurn).toHaveBeenCalledWith("turn_a");
    expect(api.cancelTurn).not.toHaveBeenCalledWith("turn_b");
    expect(session.queuedTurnIds).toContain("turn_b");
  });

  it("还没收到 TURN_START 时 Stop 退化为「取消当前 active」（不会打到排队项）", async () => {
    const { session } = setup();
    session.turnRunning = true;
    session.activeTurnId = null;

    await session.stopActiveTurn();

    expect(api.cancelActiveTurn).toHaveBeenCalled();
    expect(api.cancelTurn).not.toHaveBeenCalled();
  });
});

describe("旧事件防护仍然有效", () => {
  it("属于更早 turn 的迟到 TURN_END 不会结束当前任务", () => {
    const { session, events } = setup();
    start(session, events, "turn_new");
    events.route({ type: "TURN_END", id: "late", ts: "", data: { turn_id: "turn_old" } });
    expect(session.turnRunning).toBe(true);
    expect(session.activeTurnId).toBe("turn_new");
  });

  it("activeTurnId 未知时（重连后首帧就是 END）仍然收敛", () => {
    const { session, events } = setup();
    session.turnRunning = true;
    events.route({ type: "TURN_END", id: "replay", ts: "", data: { turn_id: "turn_x" } });
    expect(session.turnRunning).toBe(false);
  });

  it("同一个 turn 的 TURN_END 重放只生效一次", () => {
    const { session, events } = setup();
    start(session, events, "turn_a");
    events.route({ type: "TURN_END", id: "e1", ts: "", data: { turn_id: "turn_a", status: "completed" } });
    start(session, events, "turn_b");
    // 重连后 replay 把 A 的 TURN_END 又送了一次
    events.route({ type: "TURN_END", id: "e2", ts: "", data: { turn_id: "turn_a", status: "completed" } });
    expect(session.activeTurnId).toBe("turn_b");
    expect(session.turnRunning).toBe(true);
  });
});
