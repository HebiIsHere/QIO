/**
 * RESYNC 恢复协议的完整语义：
 *
 * * 同步期间到达的实时事件必须**先缓存**，等 snapshot 应用完再按顺序应用 ——
 *   否则「snapshot 返回旧状态」会把刚发生的新事件覆盖掉；
 * * 同一时间只允许一个 resync（不并发多个 snapshot 请求）；
 * * snapshot 是**替换 / 核对**语义：服务器没有的审批、任务、工具都要收口；
 * * 同步成功回到 normal 并清掉提示；失败进入 failed，不假装已同步。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { useEventStore } from "../events";
import { useApprovalsStore } from "../approvals";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [],
    })),
    sendTurn: vi.fn(async () => ({ ok: true, accepted: true, turn_id: "turn_x", status: "accepted" })),
    cancelTurn: vi.fn(async () => ({ ok: true, cancelled: true, turn_id: "turn_x" })),
    cancelActiveTurn: vi.fn(async () => ({ ok: true, cancelled: true, turn_id: null })),
    getTurnQueue: vi.fn(async () => ({ running: null, queued: [], cancelled: [], revision: 1 })),
    getRuntimeState: vi.fn(),
  },
}));

const INSTANCE = "inst_test";

function runtimeState(overrides: Record<string, unknown> = {}) {
  return {
    instance_id: INSTANCE,
    revision: 100,
    turn_queue: {
      instance_id: INSTANCE,
      revision: 100,
      running: { turn_id: "turn_main", message: "一" },
      queued: [],
      cancelled: [],
    },
    approvals: [],
    tasks: [],
    tools: [],
    ...overrides,
  };
}

/** 一个可以被测试手动放行的 runtime state 请求。 */
function deferredState(state: unknown) {
  let release!: () => void;
  const promise = new Promise((resolve) => {
    release = () => resolve(state);
  });
  vi.mocked(api.getRuntimeState).mockImplementationOnce(() => promise as never);
  return release;
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return {
    session: useSessionStore(),
    events: useEventStore(),
    approvals: useApprovalsStore(),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("同步期间的实时事件不会被旧 snapshot 覆盖", () => {
  it("场景 B：snapshot 里有 pending A，同步期间 A 被批准 → A 不得复活", async () => {
    const { session, events, approvals } = setup();
    const release = deferredState(
      runtimeState({
        approvals: [
          { approval_id: "A", kind: "computer", payload: {}, turn_id: "turn_main" },
        ],
      }),
    );

    events.route({ type: "RESYNC", id: "rs", ts: "", data: { reason: "overflow" } });
    await flushPromises();

    // 同步还没返回：服务器/别的客户端批准了 A
    events.route({
      type: "APPROVAL_RESULT",
      id: "ar",
      ts: "",
      data: { approval_id: "A", decision: "approved", turn_id: "turn_main" },
    });

    release();
    await flushPromises();

    expect(approvals.queue.map((a) => a.approval_id)).toEqual([]);
    expect(session.warning).toBeNull();
    expect(session.resyncState).toBe("normal");
  });

  it("场景 D：snapshot 先应用，同步期间的事件随后按顺序应用", async () => {
    const { session, events } = setup();
    const release = deferredState(runtimeState());

    events.route({ type: "RESYNC", id: "rs", ts: "", data: { reason: "overflow" } });
    await flushPromises();

    // ASSISTANT 是累计语义：第二条包含第一条的全文（后端就是这么发的）
    events.route({
      type: "ASSISTANT",
      id: "a1",
      ts: "",
      data: { turn_id: "turn_main", content: "同步期间的第一段" },
    });
    events.route({
      type: "ASSISTANT",
      id: "a2",
      ts: "",
      data: { turn_id: "turn_main", content: "同步期间的第一段 + 第二段" },
    });

    // 同步还没结束：这些事件必须还在缓冲区里，不能被应用
    expect(session.messages.filter((m) => m.role === "assistant")).toEqual([]);

    release();
    await flushPromises();

    const assistants = session.messages.filter((m) => m.role === "assistant");
    expect(assistants.map((m) => m.content)).toEqual(["同步期间的第一段 + 第二段"]);
  });

  it("场景 E：连续多个 RESYNC 只发起一次 snapshot 请求", async () => {
    const { events } = setup();
    const release = deferredState(runtimeState());

    events.route({ type: "RESYNC", id: "rs1", ts: "", data: { reason: "overflow" } });
    await flushPromises();
    events.route({ type: "RESYNC", id: "rs2", ts: "", data: { reason: "overflow" } });
    events.route({ type: "RESYNC", id: "rs3", ts: "", data: { reason: "overflow" } });
    await flushPromises();

    expect(api.getRuntimeState).toHaveBeenCalledTimes(1);

    release();
    await flushPromises();
    // 同步期间又来过 RESYNC → 完成后还要再同步一次（但不能并发）
    expect(api.getRuntimeState).toHaveBeenCalledTimes(2);
  });
});

describe("snapshot 的替换 / 核对语义", () => {
  it("服务器不再 pending 的审批必须从本地移除", async () => {
    const { session, events, approvals } = setup();
    approvals.enqueue("stale_1", "computer", {});
    approvals.enqueue("stale_2", "computer", {});
    vi.mocked(api.getRuntimeState).mockResolvedValueOnce(runtimeState() as never);

    events.route({ type: "RESYNC", id: "rs", ts: "", data: { reason: "overflow" } });
    await flushPromises();

    expect(approvals.queue).toEqual([]);
    expect(session.warning).toBeNull();
  });

  it("场景 C：快照里没有的活动任务不能再显示成 running", async () => {
    const { events, session } = setup();
    events.route({
      type: "TURN_START",
      id: "s",
      ts: "",
      data: { turn_id: "turn_main", revision: 1, instance_id: INSTANCE },
    });
    events.route({
      type: "SUBAGENT_STATUS",
      id: "sb",
      ts: "",
      data: { task_id: "task_B", status: "running", tool: "research" },
    });
    vi.mocked(api.getRuntimeState).mockResolvedValueOnce(runtimeState() as never);

    events.route({ type: "RESYNC", id: "rs", ts: "", data: { reason: "overflow" } });
    await flushPromises();

    const card = session.messages.find((m) => m.role === "subagent" && m.taskId === "task_B");
    expect(card?.taskStatus).not.toBe("running");
    expect(card?.toolError).toBeTruthy();
  });

  it("场景 F：TOOL_END 丢失后，快照里没有的「运行中」工具卡要收口", async () => {
    const { session, events } = setup();
    events.route({
      type: "TURN_START",
      id: "s",
      ts: "",
      data: { turn_id: "turn_main", revision: 1, instance_id: INSTANCE },
    });
    events.route({
      type: "TOOL_START",
      id: "t1",
      ts: "",
      data: { turn_id: "turn_main", call_id: "call_x", tool: "fs_read" },
    });
    // 快照说：这一轮还在跑，但没有正在执行的工具
    vi.mocked(api.getRuntimeState).mockResolvedValueOnce(runtimeState() as never);

    events.route({ type: "RESYNC", id: "rs", ts: "", data: { reason: "overflow" } });
    await flushPromises();

    const card = session.messages.find((m) => m.role === "tool" && m.callId === "call_x");
    expect(card?.toolRunning).toBe(false);
    expect(card?.toolError).toBeTruthy();
  });

  it("快照里有 active tool 时，正在跑的工具卡保持运行中", async () => {
    const { session, events } = setup();
    events.route({
      type: "TURN_START",
      id: "s",
      ts: "",
      data: { turn_id: "turn_main", revision: 1, instance_id: INSTANCE },
    });
    events.route({
      type: "TOOL_START",
      id: "t1",
      ts: "",
      data: { turn_id: "turn_main", call_id: "call_live", tool: "fs_read" },
    });
    vi.mocked(api.getRuntimeState).mockResolvedValueOnce(
      runtimeState({
        // 快照里的工具事实带语义状态：running 才是「此刻真的在跑」
        // （终态见 `toolRecovery.test.ts`）。
        tools: [
          {
            turn_id: "turn_main",
            tool_call_id: "call_live",
            tool_name: "fs_read",
            status: "running",
          },
        ],
      }) as never,
    );

    events.route({ type: "RESYNC", id: "rs", ts: "", data: { reason: "overflow" } });
    await flushPromises();

    const card = session.messages.find((m) => m.role === "tool" && m.callId === "call_live");
    expect(card?.toolRunning).toBe(true);
  });
});

describe("同步状态与提示", () => {
  it("同步失败进入 failed 并保留错误（不假装已同步）", async () => {
    const { session, events } = setup();
    vi.mocked(api.getRuntimeState).mockRejectedValueOnce(new Error("network down"));

    events.route({ type: "RESYNC", id: "rs", ts: "", data: { reason: "overflow" } });
    await flushPromises();

    expect(session.resyncState).toBe("failed");
    expect(session.lastError).toContain("network down");
    expect(session.warning).toBeNull();
  });

  it("场景 G：instance 变化时自动做一次完整 resync", async () => {
    const { session, events } = setup();
    events.route({
      type: "TURN_START",
      id: "s1",
      ts: "",
      data: { turn_id: "turn_a", revision: 100, instance_id: "inst_A" },
    });
    vi.mocked(api.getRuntimeState).mockResolvedValueOnce(
      runtimeState({
        instance_id: "inst_B",
        revision: 1,
        turn_queue: {
          instance_id: "inst_B",
          revision: 1,
          running: null,
          queued: [],
          cancelled: [],
        },
      }) as never,
    );

    // 后端重启：新实例的第一条事件
    events.route({
      type: "TURN_QUEUE",
      id: "q_new",
      ts: "",
      data: { running: null, queued: [], cancelled: [], revision: 1, instance_id: "inst_B" },
    });
    await flushPromises();

    expect(session.instanceId).toBe("inst_B");
    expect(api.getRuntimeState).toHaveBeenCalled();
    expect(session.activeTurnId).toBeNull();
    expect(session.turnRunning).toBe(false);
  });
});
