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
import { flushPromises } from "@vue/test-utils";
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
    getTurnQueue: vi.fn(async () => ({
      running: null,
      queued: [],
      cancelled: [],
      revision: 100,
      instance_id: "inst_test",
    })),
    getRuntimeState: vi.fn(async () => ({
      instance_id: "inst_test",
      revision: 100,
      turn_queue: { instance_id: "inst_test", revision: 100, running: null, queued: [], cancelled: [] },
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

function start(
  session: ReturnType<typeof useSessionStore>,
  events: ReturnType<typeof useEventStore>,
  id: string,
  revision = 1,
) {
  events.route({ type: "TURN_START", id: `s_${id}`, ts: "", data: { turn_id: id, revision } });
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

/**
 * TURN_QUEUE 是服务器状态的权威快照：既能**恢复**缺失状态，
 * 也必须能**清除**本地已经过期的状态。
 */
describe("TURN_QUEUE 权威快照", () => {
  it("服务器已空闲时必须清掉本地的 stale active", () => {
    const { session, events } = setup();
    start(session, events, "turn_a", 3);
    expect(session.turnRunning).toBe(true);

    events.route({
      type: "TURN_QUEUE",
      id: "q1",
      ts: "",
      data: { running: null, queued: [], cancelled: [], revision: 9 },
    });

    expect(session.activeTurnId).toBeNull();
    expect(session.turnRunning).toBe(false);
    expect(session.turnPhase).toBe("idle");
  });

  it("服务器有 running 时恢复 active 与排队列表", () => {
    const { session, events } = setup();
    events.route({
      type: "TURN_QUEUE",
      id: "q1",
      ts: "",
      data: {
        running: { turn_id: "turn_a", message: "一" },
        queued: [{ turn_id: "turn_b", message: "二" }],
        cancelled: [],
        revision: 12,
      },
    });
    expect(session.activeTurnId).toBe("turn_a");
    expect(session.queuedTurnIds).toEqual(["turn_b"]);
    expect(session.turnRunning).toBe(true);
  });

  it("旧快照不得覆盖更新的 Turn 状态（TURN_START 之后再晚到的旧快照）", () => {
    const { session, events } = setup();
    start(session, events, "turn_a", 20);

    // 这是重连/重放里最危险的时序：一份「服务器当时空闲」的旧快照晚到了
    events.route({
      type: "TURN_QUEUE",
      id: "stale",
      ts: "",
      data: { running: null, queued: [], cancelled: [], revision: 18 },
    });

    expect(session.activeTurnId).toBe("turn_a");
    expect(session.turnRunning).toBe(true);
  });

  it("漏掉 TURN_END 后靠服务器快照恢复为空闲", () => {
    const { session, events } = setup();
    start(session, events, "turn_a", 30);
    // 客户端漏掉了 TURN_END(A)：本地仍以为 A 在跑
    assertRunning(session);

    // 重连后收到服务器快照：已经空闲
    events.route({
      type: "TURN_QUEUE",
      id: "q_after_reconnect",
      ts: "",
      data: { running: null, queued: [], cancelled: [{ turn_id: "turn_a", message: "一" }], revision: 31 },
    });

    expect(session.activeTurnId).toBeNull();
    expect(session.turnRunning).toBe(false);
  });

  it("收到 RESYNC 时重新拉取权威快照并据此对齐状态", async () => {
    const { session, events } = setup();
    start(session, events, "turn_a", 40);
    vi.mocked(api.getRuntimeState).mockResolvedValueOnce({
      instance_id: "inst_test",
      revision: 41,
      turn_queue: {
        instance_id: "inst_test",
        revision: 41,
        running: null,
        queued: [],
        cancelled: [],
      },
      approvals: [],
      tasks: [],
    } as never);

    events.route({ type: "RESYNC", id: "resync_1", ts: "", data: { reason: "overflow" } });
    await flushPromises();

    expect(api.getRuntimeState).toHaveBeenCalled();
    expect(session.activeTurnId).toBeNull();
    expect(session.turnRunning).toBe(false);
    expect(session.warning).toBeNull();
  });

  it("resync 能把真实的 running 与 queued 一起恢复回来", async () => {
    const { session, events } = setup();
    // 本地状态已经被事件丢失搞乱：以为空闲，其实是 A 在跑、B 在排队
    expect(session.activeTurnId).toBeNull();
    vi.mocked(api.getRuntimeState).mockResolvedValueOnce({
      instance_id: "inst_test",
      revision: 50,
      turn_queue: {
        instance_id: "inst_test",
        revision: 50,
        running: { turn_id: "turn_a", message: "一" },
        queued: [{ turn_id: "turn_b", message: "二" }],
        cancelled: [],
      },
      approvals: [],
      tasks: [],
    } as never);

    events.route({ type: "RESYNC", id: "resync_2", ts: "", data: { reason: "overflow" } });
    await flushPromises();

    expect(session.activeTurnId).toBe("turn_a");
    expect(session.queuedTurnIds).toEqual(["turn_b"]);
    expect(session.turnRunning).toBe(true);
  });

  it("RESYNC 之后补发的陈旧 TURN_END 不得污染已经对齐的状态", async () => {
    const { session, events } = setup();
    // 服务器已经空闲（权威快照），本地也据此对齐
    events.route({
      type: "TURN_QUEUE",
      id: "q_idle",
      ts: "",
      data: { running: null, queued: [], cancelled: [], revision: 60 },
    });
    session.pushAssistant("现在的回答");
    const before = session.messages.length;

    // 溢出前缓冲下来的一条旧 TURN_END，如今才补发
    events.route({
      type: "TURN_END",
      id: "stale_end",
      ts: "",
      data: { turn_id: "turn_old", status: "completed", final_content: "很久以前的回答", revision: 55 },
    });

    expect(session.messages.length).toBe(before);
    expect(session.lastTurnOutcome).toBeNull();
  });

  it("stale TURN_START（revision 更旧）不得修改任何状态", () => {
    const { session, events } = setup();
    events.route({
      type: "TURN_QUEUE",
      id: "q1",
      ts: "",
      data: { running: null, queued: [], cancelled: [], revision: 60, instance_id: "inst_test" },
    });
    const snapshot = { ...session.turnQueue };

    events.route({
      type: "TURN_START",
      id: "stale_start",
      ts: "",
      data: { turn_id: "turn_old", revision: 55, instance_id: "inst_test" },
    });

    expect(session.activeTurnId).toBeNull();
    expect(session.turnRunning).toBe(false);
    expect(session.turnQueue).toEqual(snapshot);
  });

  it("stale TURN_QUEUE 不得部分应用（turnQueue 也必须原样）", () => {
    const { session, events } = setup();
    events.route({
      type: "TURN_QUEUE",
      id: "q_new",
      ts: "",
      data: {
        running: { turn_id: "turn_a", message: "a" },
        queued: [{ turn_id: "turn_b", message: "b" }],
        cancelled: [],
        revision: 60,
        instance_id: "inst_test",
      },
    });
    expect(session.turnQueue.running?.turn_id).toBe("turn_a");

    // 旧快照（revision 55，说服务器空闲）晚到
    events.route({
      type: "TURN_QUEUE",
      id: "q_stale",
      ts: "",
      data: { running: null, queued: [], cancelled: [], revision: 55, instance_id: "inst_test" },
    });

    expect(session.turnQueue.running?.turn_id).toBe("turn_a");
    expect(session.activeTurnId).toBe("turn_a");
    expect(session.queuedTurnIds).toEqual(["turn_b"]);
    expect(session.turnRunning).toBe(true);
  });

  it("后端重启（instance_id 变化）后必须接受新实例的低 revision", () => {
    const { session, events } = setup();
    events.route({
      type: "TURN_QUEUE",
      id: "q_old_instance",
      ts: "",
      data: {
        running: { turn_id: "turn_a", message: "a" },
        queued: [],
        cancelled: [],
        revision: 135,
        instance_id: "inst_A",
      },
    });
    expect(session.activeTurnId).toBe("turn_a");

    // 后端重启：新实例从 revision=1 重新计数，且不再有正在跑的 turn
    events.route({
      type: "TURN_QUEUE",
      id: "q_new_instance",
      ts: "",
      data: { running: null, queued: [], cancelled: [], revision: 1, instance_id: "inst_B" },
    });

    expect(session.activeTurnId).toBeNull();
    expect(session.turnRunning).toBe(false);
  });
});

function assertRunning(session: ReturnType<typeof useSessionStore>) {
  expect(session.activeTurnId).toBe("turn_a");
  expect(session.turnRunning).toBe(true);
}
