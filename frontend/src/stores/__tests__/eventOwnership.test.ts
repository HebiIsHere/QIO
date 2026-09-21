/**
 * 事件归属：主 turn / Subagent / 系统各自的事件不能串。
 *
 * 真实缺陷：Subagent 的内部循环用的是 `turn_id = subagent:task_xxx`，
 * 但前端的 ASSISTANT / TOOL_* / USAGE 处理完全没有检查归属 ——
 * 子任务的中间文本会进主对话、子任务的用量会记到主 turn 上。
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
  return {
    session: useSessionStore(),
    events: useEventStore(),
    approvals: useApprovalsStore(),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("主 turn 与 Subagent 的事件归属", () => {
  it("subagent 的 ASSISTANT 不进主对话，主 turn 的照常进入", () => {
    const { session, events } = setup();
    events.route({
      type: "TURN_START",
      id: "s",
      ts: "",
      data: { turn_id: "turn_main", revision: 1, instance_id: "i" },
    });
    const before = session.messages.length;

    events.route({
      type: "ASSISTANT",
      id: "a_sub",
      ts: "",
      data: { turn_id: "subagent:task_1", content: "子任务内部推理" },
    });
    expect(session.messages.length).toBe(before);

    events.route({
      type: "ASSISTANT",
      id: "a_main",
      ts: "",
      data: { turn_id: "turn_main", content: "主回答" },
    });
    expect(session.messages.length).toBe(before + 1);
    expect(session.messages[session.messages.length - 1].content).toBe("主回答");
  });

  it("没有主 turn 在跑时，带 turn_id 的 ASSISTANT 也不进主对话", () => {
    const { session, events } = setup();
    const before = session.messages.length;
    events.route({
      type: "ASSISTANT",
      id: "a_orphan",
      ts: "",
      data: { turn_id: "subagent:task_9", content: "孤儿文本" },
    });
    expect(session.messages.length).toBe(before);
  });

  it("subagent 的工具事件不在主对话里出卡片", () => {
    const { session, events } = setup();
    events.route({
      type: "TURN_START",
      id: "s",
      ts: "",
      data: { turn_id: "turn_main", revision: 1, instance_id: "i" },
    });
    const before = session.messages.length;

    events.route({
      type: "TOOL_START",
      id: "t_sub",
      ts: "",
      data: { turn_id: "subagent:task_1", call_id: "c1", tool: "fs_read" },
    });
    expect(session.messages.length).toBe(before);

    events.route({
      type: "TOOL_START",
      id: "t_main",
      ts: "",
      data: { turn_id: "turn_main", call_id: "c2", tool: "fs_read" },
    });
    expect(session.messages.length).toBe(before + 1);
  });

  it("USAGE 按事件真实 turn_id 归属，不记到主 turn 上", () => {
    const { session, events } = setup();
    events.route({
      type: "TURN_START",
      id: "s",
      ts: "",
      data: { turn_id: "turn_main", revision: 1, instance_id: "i" },
    });

    events.route({
      type: "USAGE",
      id: "u_sub",
      ts: "",
      data: { turn_id: "subagent:task_1", tokens: 999, iterations: 5 },
    });
    events.route({
      type: "USAGE",
      id: "u_main",
      ts: "",
      data: { turn_id: "turn_main", tokens: 7, iterations: 1 },
    });

    expect(events.turnUsageFor("turn_main")?.tokens).toBe(7);
    expect(events.turnUsageFor("subagent:task_1")?.tokens).toBe(999);
  });
});

describe("RESYNC 恢复完整运行状态", () => {
  it("subagent 的 WARNING / ERROR 不污染主 Session", () => {
    const { session, events } = setup();
    events.route({
      type: "TURN_START",
      id: "s",
      ts: "",
      data: { turn_id: "turn_main", revision: 1, instance_id: "i" },
    });

    events.route({
      type: "WARNING",
      id: "w_sub",
      ts: "",
      data: { turn_id: "subagent:task_1", message: "子任务的小警告" },
    });
    events.route({
      type: "ERROR",
      id: "e_sub",
      ts: "",
      data: { turn_id: "subagent:task_1", message: "子任务内部报错" },
    });
    expect(session.warning).toBeNull();
    expect(session.lastError).toBeNull();

    // 主 turn 自己的警告 / 错误照常显示
    events.route({
      type: "WARNING",
      id: "w_main",
      ts: "",
      data: { turn_id: "turn_main", message: "主循环的提示" },
    });
    expect(session.warning).toBe("主循环的提示");

    events.route({
      type: "ERROR",
      id: "e_main",
      ts: "",
      data: { turn_id: "turn_main", message: "主循环的错误" },
    });
    expect(session.lastError).toBe("主循环的错误");
  });

  it("恢复 turn queue + 待审批 + 独立任务，并清掉「正在同步」提示", async () => {
    const { session, events, approvals } = setup();
    vi.mocked(api.getRuntimeState).mockResolvedValueOnce({
      instance_id: "inst_new",
      revision: 42,
      turn_queue: {
        instance_id: "inst_new",
        revision: 42,
        running: { turn_id: "turn_a", message: "一" },
        queued: [{ turn_id: "turn_b", message: "二" }],
        cancelled: [],
      },
      approvals: [
        {
          approval_id: "appr_1",
          kind: "computer",
          payload: { action: "read" },
          turn_id: "turn_a",
          session_id: null,
          request_digest: "d1",
        },
      ],
      tasks: [
        {
          task_id: "task_1",
          tool: "research",
          status: "running",
          content_preview: "",
          error: null,
        },
      ],
    } as never);

    events.route({ type: "RESYNC", id: "rs", ts: "", data: { reason: "subscriber_backlog_overflow" } });
    await flushPromises();

    expect(session.activeTurnId).toBe("turn_a");
    expect(session.queuedTurnIds).toEqual(["turn_b"]);
    expect(session.turnRunning).toBe(true);
    expect(approvals.queue.map((a) => a.approval_id)).toEqual(["appr_1"]);
    expect(session.messages.some((m) => m.role === "subagent" && m.taskId === "task_1")).toBe(true);
    expect(session.warning).toBeNull();
  });

  it("服务器已经没有在跑的 turn 时，还挂着「运行中」的工具卡要收口", async () => {
    const { session, events } = setup();
    events.route({
      type: "TURN_START",
      id: "s",
      ts: "",
      data: { turn_id: "turn_main", revision: 1, instance_id: "i" },
    });
    events.route({
      type: "TOOL_START",
      id: "t1",
      ts: "",
      data: { turn_id: "turn_main", call_id: "c1", tool: "fs_read" },
    });
    expect(session.messages.some((m) => m.role === "tool" && m.toolRunning)).toBe(true);

    vi.mocked(api.getRuntimeState).mockResolvedValueOnce({
      instance_id: "i",
      revision: 99,
      turn_queue: { instance_id: "i", revision: 99, running: null, queued: [], cancelled: [] },
      approvals: [],
      tasks: [],
    } as never);

    events.route({ type: "RESYNC", id: "rs", ts: "", data: { reason: "subscriber_backlog_overflow" } });
    await flushPromises();

    expect(session.messages.some((m) => m.role === "tool" && m.toolRunning)).toBe(false);
  });

  it("resync 失败时保留错误（不能假装已经同步完成）", async () => {
    const { session, events } = setup();
    vi.mocked(api.getRuntimeState).mockRejectedValueOnce(new Error("network down"));

    events.route({ type: "RESYNC", id: "rs", ts: "", data: { reason: "subscriber_backlog_overflow" } });
    await flushPromises();

    expect(session.lastError).toContain("network down");
  });
});
