/**
 * 重启后「已经收下、但没有执行」的消息（`interrupted_turns`）。
 *
 * 守的是三件事：
 * 1. 界面只渲染后端给的形状 —— 后端说哪些还没被执行，就是哪些；
 * 2. **不自动重发**：只有用户点了「继续」才会调 resend，而且同一条只能有一个请求在飞；
 * 3. 后端用一次性 claim 挡住重复重发（409）时，界面必须说清楚并重新对齐权威状态，
 *    不能静默失败、也不能自作主张地删掉本地那一条。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { api } from "../../services/api";

const TURN_A = {
  turn_id: "turn_a",
  message: "帮我把发布闸门跑一遍",
  topic_id: "topic_1",
  status: "interrupted",
  reason: "queued_at_restart",
  reason_text: "这条消息当时还在排队，进程退出后没有开始执行",
  created_at: "2026-10-02T10:00:00+00:00",
  ended_at: "2026-10-02T10:05:00+00:00",
};
const TURN_B = {
  turn_id: "turn_b",
  message: "再看一下 cross_topic 的召回",
  topic_id: "topic_1",
  status: "interrupted",
  reason: "running_at_restart",
  reason_text: "这条消息执行到一半，进程退出后没有完成",
  created_at: "2026-10-02T10:01:00+00:00",
  updated_at: "2026-10-02T10:06:00+00:00",
};

function runtimeState(turns: unknown[]) {
  return {
    instance_id: "inst",
    revision: 1,
    turn_queue: { instance_id: "inst", revision: 1, running: null, queued: [], cancelled: [] },
    approvals: [],
    interrupted_approvals: [],
    interrupted_turns: turns,
    tasks: [],
    tools: [],
  };
}

vi.mock("../../services/api", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly path: string,
      detail: string,
    ) {
      super(detail);
      this.name = "ApiError";
    }
  },
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "",
      topic_name: null,
      anchor_fragment: null,
      messages: [],
    })),
    getRuntimeState: vi.fn(async () => runtimeState([])),
    resendInterruptedTurn: vi.fn(),
    dismissInterruptedTurn: vi.fn(),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
  },
}));

const getRuntimeState = vi.mocked(api.getRuntimeState);
const resend = vi.mocked(api.resendInterruptedTurn);
const dismiss = vi.mocked(api.dismissInterruptedTurn);

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return useSessionStore();
}

beforeEach(() => {
  vi.clearAllMocks();
  getRuntimeState.mockResolvedValue(runtimeState([]) as never);
  resend.mockResolvedValue({ ok: true, recovered_turn_id: "turn_a", turn_id: "turn_new", status: "queued" } as never);
  dismiss.mockResolvedValue({ ok: true, dismissed: "turn_a" } as never);
});

describe("中断消息从权威状态恢复", () => {
  it("resync 之后按后端给的列表渲染（含原文、原因、时间）", async () => {
    getRuntimeState.mockResolvedValue(runtimeState([TURN_A, TURN_B]) as never);
    const session = setup();

    await session.resyncTurnState();

    expect(session.interruptedTurns.map((t) => t.turn_id)).toEqual(["turn_a", "turn_b"]);
    expect(session.interruptedTurns[0].message).toBe("帮我把发布闸门跑一遍");
    expect(session.interruptedTurns[0].reason_text).toContain("还在排队");
  });

  it("终态（已完成 / 已取消）不会出现在入口里：后端不给，界面也不会自己造", async () => {
    // 后端只下发 interrupted 且未被处理过的行；这里模拟"只给了这一条"
    getRuntimeState.mockResolvedValue(runtimeState([TURN_A]) as never);
    const session = setup();

    await session.resyncTurnState();

    expect(session.interruptedTurns).toHaveLength(1);
    expect(session.interruptedTurns.some((t) => t.status !== "interrupted")).toBe(false);
  });

  it("拿不到权威状态时**不清空**已有列表（不把「拉不到」说成「没有」）", async () => {
    getRuntimeState.mockResolvedValue(runtimeState([TURN_A]) as never);
    const session = setup();
    await session.resyncTurnState();

    getRuntimeState.mockRejectedValueOnce(new Error("network down") as never);
    await session.resyncTurnState();

    expect(session.interruptedTurns.map((t) => t.turn_id)).toEqual(["turn_a"]);
    expect(session.lastError).toContain("状态同步失败");
  });
});

describe("继续发送：只有用户点了才发，且同一条只发一次", () => {
  it("点一次 → 调 resend，成功后从入口消失并给出说明", async () => {
    getRuntimeState.mockResolvedValue(runtimeState([TURN_A]) as never);
    const session = setup();
    await session.resyncTurnState();

    const res = await session.resumeInterruptedTurn("turn_a");

    expect(resend).toHaveBeenCalledTimes(1);
    expect(resend).toHaveBeenCalledWith("turn_a");
    expect(res.ok).toBe(true);
    expect(session.interruptedTurns).toHaveLength(0);
    expect(session.interruptedNotice).toContain("重新排队");
  });

  it("重复点击不会执行两次：第二次请求在飞的时候被挡下，且不静默", async () => {
    getRuntimeState.mockResolvedValue(runtimeState([TURN_A, TURN_B]) as never);
    const session = setup();
    await session.resyncTurnState();

    let release: (v: unknown) => void = () => {};
    resend.mockImplementationOnce(
      () => new Promise((resolve) => { release = resolve; }) as never,
    );
    const first = session.resumeInterruptedTurn("turn_a");
    const second = await session.resumeInterruptedTurn("turn_b");

    expect(resend).toHaveBeenCalledTimes(1);
    expect(second.ok).toBe(false);
    expect(second.message).toContain("还在提交中");

    release({ ok: true, recovered_turn_id: "turn_a", turn_id: "turn_new", status: "queued" });
    await first;
    expect(session.interruptedTurns.map((t) => t.turn_id)).toEqual(["turn_b"]);
  });

  it("后端返回 409（已经被处理过）→ 重新问服务端要真相，并给出可读反馈", async () => {
    getRuntimeState.mockResolvedValue(runtimeState([TURN_A]) as never);
    const session = setup();
    await session.resyncTurnState();
    expect(getRuntimeState).toHaveBeenCalledTimes(1);

    resend.mockRejectedValueOnce(Object.assign(new Error("409"), { status: 409 }) as never);
    // 服务端已经把它处理掉了：重新对齐后列表是空的
    getRuntimeState.mockResolvedValue(runtimeState([]) as never);
    const res = await session.resumeInterruptedTurn("turn_a");

    expect(getRuntimeState).toHaveBeenCalledTimes(2);
    expect(res.ok).toBe(false);
    expect(res.message).toContain("已经被处理过");
    expect(session.interruptedTurns).toHaveLength(0);
    expect(session.interruptedNotice).toContain("已经被处理过");
  });

  it("网络失败 → 入口保留（用户还能重试），并说明失败原因", async () => {
    getRuntimeState.mockResolvedValue(runtimeState([TURN_A]) as never);
    const session = setup();
    await session.resyncTurnState();

    resend.mockRejectedValueOnce(new Error("fetch failed") as never);
    const res = await session.resumeInterruptedTurn("turn_a");

    expect(res.ok).toBe(false);
    expect(session.interruptedTurns.map((t) => t.turn_id)).toEqual(["turn_a"]);
    expect(session.interruptedNotice).toContain("可以重试");
  });
});

describe("忽略与全部忽略", () => {
  it("忽略成功 → 从入口消失，记录由后端保留（前端不删数据）", async () => {
    getRuntimeState.mockResolvedValue(runtimeState([TURN_A, TURN_B]) as never);
    const session = setup();
    await session.resyncTurnState();

    const res = await session.dismissInterruptedTurn("turn_a");

    expect(dismiss).toHaveBeenCalledWith("turn_a");
    expect(res.ok).toBe(true);
    expect(session.interruptedTurns.map((t) => t.turn_id)).toEqual(["turn_b"]);
    expect(session.interruptedNotice).toContain("原文仍然保留");
  });

  it("全部忽略：逐条提交；有 409 的不算失败，但要对齐权威状态并说明", async () => {
    getRuntimeState.mockResolvedValue(runtimeState([TURN_A, TURN_B]) as never);
    const session = setup();
    await session.resyncTurnState();

    dismiss
      .mockResolvedValueOnce({ ok: true, dismissed: "turn_a" } as never)
      .mockRejectedValueOnce(Object.assign(new Error("409"), { status: 409 }) as never);
    getRuntimeState.mockResolvedValue(runtimeState([]) as never);

    const res = await session.dismissAllInterruptedTurns();

    expect(dismiss).toHaveBeenCalledTimes(2);
    expect(session.interruptedTurns).toHaveLength(0);
    expect(res.message).toContain("已经被处理过");
  });

  it("全部忽略里有真失败 → 不假装全成功，失败的仍留在入口", async () => {
    getRuntimeState.mockResolvedValue(runtimeState([TURN_A, TURN_B]) as never);
    const session = setup();
    await session.resyncTurnState();

    dismiss
      .mockResolvedValueOnce({ ok: true, dismissed: "turn_a" } as never)
      .mockRejectedValueOnce(new Error("fetch failed") as never);

    const res = await session.dismissAllInterruptedTurns();

    expect(res.ok).toBe(false);
    expect(res.message).toContain("1 条没有忽略成功");
    expect(session.interruptedTurns.map((t) => t.turn_id)).toEqual(["turn_b"]);
  });
});
