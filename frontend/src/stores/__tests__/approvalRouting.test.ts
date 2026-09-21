/**
 * 同一个审批，实时收到和断线恢复必须走**完全相同**的 UI 路径。
 *
 * 真实缺陷：`kind = continue`（预算耗尽后的「继续/停止」）实时走 ContinueBar，
 * 但 RESYNC 恢复 pending approval 时无条件丢进 approvalsStore，
 * 于是它会以普通审批弹窗的形式出现。
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

const INSTANCE = "i";

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

describe("continue 审批的实时与恢复路径一致", () => {
  it("实时的 kind=continue 走 ContinueBar，不进审批队列", () => {
    const { session, events, approvals } = setup();
    events.route({
      type: "APPROVAL_REQUIRED",
      id: "ap",
      ts: "",
      data: {
        approval: {
          approval_id: "appr_continue",
          kind: "continue",
          payload: { used_iterations: 128, max_iterations: 128 },
          turn_id: "turn_main",
        },
      },
    });

    expect(session.pendingContinue?.id).toBe("appr_continue");
    expect(session.pendingContinue?.used).toBe(128);
    expect(approvals.queue).toEqual([]);
  });

  it("RESYNC 恢复的 kind=continue 也走 ContinueBar，而不是普通审批弹窗", async () => {
    const { session, events, approvals } = setup();
    vi.mocked(api.getRuntimeState).mockResolvedValueOnce({
      instance_id: INSTANCE,
      revision: 5,
      turn_queue: {
        instance_id: INSTANCE,
        revision: 5,
        running: { turn_id: "turn_main", message: "一" },
        queued: [],
        cancelled: [],
      },
      approvals: [
        {
          approval_id: "appr_continue_2",
          kind: "continue",
          payload: { used_iterations: 64, max_iterations: 64 },
          turn_id: "turn_main",
        },
      ],
      tasks: [],
      tools: [],
    } as never);

    events.route({ type: "RESYNC", id: "rs", ts: "", data: { reason: "overflow" } });
    await flushPromises();

    expect(session.pendingContinue?.id).toBe("appr_continue_2");
    expect(session.pendingContinue?.max).toBe(64);
    expect(approvals.queue).toEqual([]);
  });

  it("恢复的普通审批仍然进审批队列（带上下文，不抢焦点）", async () => {
    const { events, approvals } = setup();
    vi.mocked(api.getRuntimeState).mockResolvedValueOnce({
      instance_id: INSTANCE,
      revision: 6,
      turn_queue: {
        instance_id: INSTANCE,
        revision: 6,
        running: null,
        queued: [],
        cancelled: [],
      },
      approvals: [
        {
          approval_id: "appr_tool",
          kind: "tool_create",
          payload: { name: "t" },
          turn_id: "turn_main",
          request_digest: "d1",
        },
      ],
      tasks: [],
      tools: [],
    } as never);

    events.route({ type: "RESYNC", id: "rs", ts: "", data: { reason: "overflow" } });
    await flushPromises();

    expect(approvals.queue.map((a) => a.approval_id)).toEqual(["appr_tool"]);
    expect(approvals.current?.requestDigest).toBe("d1");
    expect(approvals.visible).toBe(false); // 恢复时不抢焦点
  });
});
