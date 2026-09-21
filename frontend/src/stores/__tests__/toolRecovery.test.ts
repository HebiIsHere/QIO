/**
 * 工具卡的最终状态恢复（TOOL_END 丢失之后）。
 *
 * 事件流允许丢过程事件，但「服务器已经知道的最终结果」不能因此永久变成
 * 「结果未收到」。RESYNC 之后快照里的工具执行事实必须能覆盖本地过期的
 * 「运行中」，而 `unknown` 只在服务器也拿不出记录时才出现。
 *
 * 匹配身份是 `tool_call_id`（同一个工具名可能在一轮里被调用多次），
 * 并且要带上 `turn_id` 做一致性校验（跨 Turn 不得串状态）。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore, type StreamMessage } from "../session";
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
    sendTurn: vi.fn(async () => ({ ok: true, accepted: true, turn_id: "turn_x", status: "accepted" })),
    cancelTurn: vi.fn(async () => ({ ok: true, cancelled: true, turn_id: "turn_x" })),
    cancelActiveTurn: vi.fn(async () => ({ ok: true, cancelled: true, turn_id: null })),
    getTurnQueue: vi.fn(async () => ({ running: null, queued: [], cancelled: [], revision: 1 })),
    getRuntimeState: vi.fn(),
  },
}));

const INSTANCE = "inst_test";
const TURN = "turn_main";

function runtimeState(overrides: Record<string, unknown> = {}) {
  return {
    instance_id: INSTANCE,
    revision: 100,
    turn_queue: {
      instance_id: INSTANCE,
      revision: 100,
      running: { turn_id: TURN, message: "一" },
      queued: [],
      cancelled: [],
    },
    approvals: [],
    tasks: [],
    tools: [],
    ...overrides,
  };
}

/** 一条快照里的工具执行事实（字段与后端 /api/runtime/state.tools 一致）。 */
function toolRecord(over: Record<string, unknown> = {}) {
  return {
    turn_id: TURN,
    tool_call_id: "call_x",
    tool_name: "fs_read",
    status: "success",
    started_at: "2026-09-21T00:00:00+00:00",
    ended_at: "2026-09-21T00:00:01+00:00",
    error_summary: null,
    ...over,
  };
}

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
  return { session: useSessionStore(), events: useEventStore() };
}

function beginTurn(events: ReturnType<typeof useEventStore>, turnId = TURN) {
  events.route({
    type: "TURN_START",
    id: "s",
    ts: "",
    data: { turn_id: turnId, revision: 1, instance_id: INSTANCE },
  });
}

function startTool(
  events: ReturnType<typeof useEventStore>,
  callId: string,
  toolName = "fs_read",
  turnId = TURN,
) {
  events.route({
    type: "TOOL_START",
    id: `start_${callId}`,
    ts: "",
    data: { turn_id: turnId, call_id: callId, tool: toolName },
  });
}

function cardOf(session: ReturnType<typeof useSessionStore>, callId: string): StreamMessage | undefined {
  return session.messages.find((m) => m.role === "tool" && m.callId === callId);
}

async function resync(events: ReturnType<typeof useEventStore>, state: unknown) {
  vi.mocked(api.getRuntimeState).mockResolvedValueOnce(state as never);
  events.route({ type: "RESYNC", id: "rs", ts: "", data: { reason: "overflow" } });
  await flushPromises();
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("服务器知道结果时：TOOL_END 丢了也要显示真实终态", () => {
  it("测试 1：快照说 success → 卡片恢复成成功，而不是「结果未收到」", async () => {
    const { session, events } = setup();
    beginTurn(events);
    startTool(events, "call_x");
    expect(cardOf(session, "call_x")?.toolStatus).toBe("running");

    await resync(events, runtimeState({ tools: [toolRecord()] }));

    const card = cardOf(session, "call_x");
    expect(card?.toolRunning).toBe(false);
    expect(card?.toolStatus).toBe("success");
    expect(card?.toolOk).toBe(true);
    expect(card?.toolError ?? null).toBeNull();
  });

  it("测试 2：快照说 failed → 恢复成失败并带上服务器给的原因", async () => {
    const { session, events } = setup();
    beginTurn(events);
    startTool(events, "call_x", "run_cmd");

    await resync(
      events,
      runtimeState({
        tools: [toolRecord({ tool_name: "run_cmd", status: "failed", error_summary: "命令退出码 1" })],
      }),
    );

    const card = cardOf(session, "call_x");
    expect(card?.toolStatus).toBe("failed");
    expect(card?.toolOk).toBe(false);
    expect(card?.toolError).toContain("命令退出码 1");
  });

  it("测试 3：快照说 cancelled → 恢复成「已取消」，不混成失败", async () => {
    const { session, events } = setup();
    beginTurn(events);
    startTool(events, "call_x", "run_cmd");

    await resync(
      events,
      runtimeState({ tools: [toolRecord({ tool_name: "run_cmd", status: "cancelled" })] }),
    );

    const card = cardOf(session, "call_x");
    expect(card?.toolStatus).toBe("cancelled");
    expect(card?.toolRunning).toBe(false);
  });

  it("测试 4：快照说还在跑 → 卡片保持运行中", async () => {
    const { session, events } = setup();
    beginTurn(events);
    startTool(events, "call_x");

    await resync(
      events,
      runtimeState({
        tools: [
          toolRecord({
            status: "running",
            ended_at: null,
          }),
        ],
      }),
    );

    const card = cardOf(session, "call_x");
    expect(card?.toolStatus).toBe("running");
    expect(card?.toolRunning).toBe(true);
  });

  it("服务器已经给出终态之后，再来的「还在跑」不得把它降级回运行中", async () => {
    const { session, events } = setup();
    beginTurn(events);
    startTool(events, "call_x");
    events.route({
      type: "TOOL_END",
      id: "e1",
      ts: "",
      data: { turn_id: TURN, call_id: "call_x", tool: "fs_read", ok: true, status: "success" },
    });
    expect(cardOf(session, "call_x")?.toolStatus).toBe("success");

    await resync(events, runtimeState({ tools: [toolRecord({ status: "running", ended_at: null })] }));

    expect(cardOf(session, "call_x")?.toolStatus).toBe("success");
  });
});

describe("服务器也不知道时：才轮到 unknown", () => {
  it("测试 5：快照里没有这次调用 → 收口为「结果未收到」，不伪造终态", async () => {
    const { session, events } = setup();
    beginTurn(events);
    startTool(events, "call_x");

    await resync(events, runtimeState());

    const card = cardOf(session, "call_x");
    expect(card?.toolRunning).toBe(false);
    expect(card?.toolStatus).toBe("unknown");
    expect(card?.toolError).toBeTruthy();
  });

  it("快照里的 stale running（服务器自己也无法确认）同样收口为未知", async () => {
    const { session, events } = setup();
    beginTurn(events);
    startTool(events, "call_x");

    await resync(
      events,
      runtimeState({ tools: [toolRecord({ status: "unknown", ended_at: null })] }),
    );

    const card = cardOf(session, "call_x");
    expect(card?.toolStatus).toBe("unknown");
    expect(card?.toolRunning).toBe(false);
  });
});

describe("顺序与身份", () => {
  it("测试 6：snapshot 之后到达的 buffered TOOL_END 仍然生效", async () => {
    const { session, events } = setup();
    beginTurn(events);
    startTool(events, "call_x");
    const release = deferredState(
      runtimeState({ tools: [toolRecord({ status: "running", ended_at: null })] }),
    );

    events.route({ type: "RESYNC", id: "rs", ts: "", data: { reason: "overflow" } });
    await flushPromises();
    // 同步期间工具真的结束了：这条事件比快照新，必须最后生效
    events.route({
      type: "TOOL_END",
      id: "e1",
      ts: "",
      data: { turn_id: TURN, call_id: "call_x", tool: "fs_read", ok: true, status: "success" },
    });

    release();
    await flushPromises();

    expect(cardOf(session, "call_x")?.toolStatus).toBe("success");
    expect(cardOf(session, "call_x")?.toolRunning).toBe(false);
  });

  it("测试 7：同一个工具名在一轮里多次调用 → 按 call_id 分别恢复", async () => {
    const { session, events } = setup();
    beginTurn(events);
    startTool(events, "call_1", "read_file");
    startTool(events, "call_2", "read_file");

    await resync(
      events,
      runtimeState({
        tools: [
          toolRecord({ tool_call_id: "call_1", tool_name: "read_file", status: "success" }),
          toolRecord({
            tool_call_id: "call_2",
            tool_name: "read_file",
            status: "failed",
            error_summary: "文件不存在",
          }),
        ],
      }),
    );

    expect(cardOf(session, "call_1")?.toolStatus).toBe("success");
    expect(cardOf(session, "call_2")?.toolStatus).toBe("failed");
  });

  it("测试 8：别的 Turn 的工具记录不得改到本轮的卡片上", async () => {
    const { session, events } = setup();
    beginTurn(events);
    startTool(events, "call_x");

    await resync(
      events,
      runtimeState({
        tools: [toolRecord({ turn_id: "turn_other", status: "success" })],
      }),
    );

    const card = cardOf(session, "call_x");
    expect(card?.toolStatus).not.toBe("success");
    expect(card?.toolStatus).toBe("unknown");
  });

  it("Subagent 内部工具不进入主对话的卡片核对", async () => {
    const { session, events } = setup();
    beginTurn(events);
    startTool(events, "call_x");

    await resync(
      events,
      runtimeState({
        tools: [toolRecord({ turn_id: "subagent:task_1", status: "success" })],
      }),
    );

    expect(cardOf(session, "call_x")?.toolStatus).toBe("unknown");
  });
});

describe("Turn 终态收敛", () => {
  it("测试 9：TURN_END 时还挂着「运行中」的工具卡必须收口，不能一直转圈", async () => {
    const { session, events } = setup();
    beginTurn(events);
    startTool(events, "call_x", "fs_read");
    startTool(events, "call_y", "run_cmd");
    events.route({
      type: "TOOL_END",
      id: "e1",
      ts: "",
      data: { turn_id: TURN, call_id: "call_x", tool: "fs_read", ok: true, status: "success" },
    });

    events.route({
      type: "TURN_END",
      id: "end",
      ts: "",
      data: { turn_id: TURN, status: "completed", revision: 2, final_content: "好" },
    });

    expect(cardOf(session, "call_x")?.toolStatus).toBe("success");
    const leftover = cardOf(session, "call_y");
    expect(leftover?.toolRunning).toBe(false);
    expect(leftover?.toolStatus).toBe("unknown");
  });

  it("TURN_END 之后仍然可以被服务器的事实改写成真实终态", async () => {
    const { session, events } = setup();
    beginTurn(events);
    startTool(events, "call_y", "run_cmd");
    events.route({
      type: "TURN_END",
      id: "end",
      ts: "",
      data: { turn_id: TURN, status: "completed", revision: 2, final_content: "好" },
    });
    expect(cardOf(session, "call_y")?.toolStatus).toBe("unknown");

    await resync(
      events,
      runtimeState({
        turn_queue: { instance_id: INSTANCE, revision: 3, running: null, queued: [], cancelled: [] },
        tools: [toolRecord({ tool_call_id: "call_y", tool_name: "run_cmd", status: "failed", error_summary: "超时" })],
      }),
    );

    const card = cardOf(session, "call_y");
    expect(card?.toolStatus).toBe("failed");
    expect(card?.toolError).toContain("超时");
  });
});
