/**
 * E 组 · M12 受控验收：`stores/restore.ts` 是运行时状态的唯一恢复入口。
 *
 * 用受控快照 + 事件闸门（手动放行的 getRuntimeState）验证：
 * 1. 首次连接 / 页面刷新 / 重连 / RESYNC 全部走 `restoreRuntimeState(reason)`，
 *    一条快照覆盖 queue / approvals / tasks / tools / narratives / interrupted /
 *    未完成用户消息；
 * 2. 同步期间到达的实时事件先缓冲、快照后按序补放 —— 同步期间结束的事项不复活；
 * 3. 旧 generation 与旧实例的迟到结果被丢弃，不覆盖新状态；
 * 4. 单飞：重复请求不并发第二个快照；失败如实进入 failed 并保留原因。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { useApprovalsStore } from "../approvals";
import { useSessionStore } from "../session";
import { useEventStore } from "../events";
import { invalidateRestore, restoreGeneration, restoreRuntimeState } from "../restore";
import { api } from "../../services/api";

const captured = vi.hoisted(() => ({
  onEvent: null as ((event: unknown) => void) | null,
  handle: null as { onopen: (() => void) | null; onerror: ((e?: unknown) => void) | null; close: () => void } | null,
}));

vi.mock("../../services/events", () => ({
  connectEvents: (onEvent: (event: unknown) => void) => {
    captured.onEvent = onEvent;
    captured.handle = { onopen: null, onerror: null, close: () => {} };
    return captured.handle;
  },
  publishTestEvent: vi.fn(async () => undefined),
}));

vi.mock("../../services/api", () => ({
  api: {
    getRuntimeState: vi.fn(),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
    listDevAuthorizations: vi.fn(async () => ({ authorizations: [] })),
    dismissInterruptedTurn: vi.fn(async () => ({ ok: true, dismissed: "turn_a" })),
    resendInterruptedTurn: vi.fn(async () => ({ ok: true })),
  },
}));

const TURN_A = {
  turn_id: "turn_a",
  message: "帮我把发布闸门跑一遍",
  topic_id: "topic_1",
  status: "interrupted",
  reason: "running_at_restart",
  reason_text: "这条消息执行到一半，进程退出后没有完成",
  created_at: "2026-10-02T10:01:00+00:00",
};

function runtimeState(overrides: Record<string, unknown> = {}) {
  return {
    instance_id: "inst_new",
    revision: 42,
    turn_queue: {
      instance_id: "inst_new",
      revision: 42,
      running: { turn_id: "turn_a", message: "一" },
      queued: [{ turn_id: "turn_b", message: "二" }],
      cancelled: [],
    },
    approvals: [],
    tasks: [],
    tools: [],
    narratives: [],
    interrupted_approvals: [],
    interrupted_turns: [],
    ...overrides,
  };
}

/** 一次可以被测试手动放行的权威状态读取（受控闸门）。 */
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

function feed(event: Record<string, unknown> & { type: string }) {
  captured.onEvent?.({ id: `${event.type}_${Math.random()}`, ts: "", data: {}, ...event });
}

beforeEach(() => {
  vi.clearAllMocks();
  captured.onEvent = null;
  captured.handle = null;
});

describe("唯一入口：首次连接 / 页面刷新 / 重连都走它", () => {
  it("连接建立（onopen）触发一次完整恢复，一条快照覆盖全部权威状态", async () => {
    const { session, events, approvals } = setup();
    vi.mocked(api.getRuntimeState).mockResolvedValue(
      runtimeState({
        approvals: [
          {
            approval_id: "appr_1",
            kind: "computer",
            payload: { action: "read" },
            turn_id: "turn_a",
            session_id: null,
            request_digest: "d1",
          },
          {
            approval_id: "appr_cont",
            kind: "continue",
            payload: { reason: "budget", used_iterations: 3, max_iterations: 10 },
            turn_id: "turn_a",
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
        // 一条本地还在「运行中」的工具卡：服务器知道它是 success
        tools: [
          {
            turn_id: "turn_a",
            tool_call_id: "call_x",
            tool_name: "fs_read",
            status: "success",
          },
        ],
        narratives: [
          {
            narrative_id: "n1",
            turn_id: "turn_a",
            kind: "progress",
            text: "正在汇总",
            created_at: "2026-10-02T10:02:00+00:00",
          },
        ],
        interrupted_approvals: [
          { approval_id: "ia_1", kind: "computer", what: "读取文件", created_at: "2026-10-02T10:00:00+00:00" },
        ],
        interrupted_turns: [TURN_A],
      }) as never,
    );

    events.connect();
    await flushPromises();
    // 本地先有一张「运行中」的工具卡（重连后收到的旧状态）
    feed({ type: "TURN_START", data: { turn_id: "turn_a", revision: 40, instance_id: "inst_new" } });
    feed({ type: "TOOL_START", data: { turn_id: "turn_a", call_id: "call_x", tool: "fs_read" } });
    expect(session.messages.some((m) => m.role === "tool" && m.toolRunning)).toBe(true);

    captured.handle?.onopen?.();
    await flushPromises();

    expect(api.getRuntimeState).toHaveBeenCalledTimes(1);
    // queue
    expect(session.activeTurnId).toBe("turn_a");
    expect(session.queuedTurnIds).toEqual(["turn_b"]);
    expect(session.turnRunning).toBe(true);
    // approvals（普通待办 + 继续/停止操作条）
    expect(approvals.queue.map((a) => a.approval_id)).toContain("appr_1");
    expect(session.pendingContinue?.id).toBe("appr_cont");
    // tasks / tools / narratives
    expect(session.messages.some((m) => m.role === "subagent" && m.taskId === "task_1")).toBe(true);
    const toolCard = session.messages.find((m) => m.role === "tool" && m.callId === "call_x");
    expect(toolCard?.toolStatus).toBe("success");
    expect(toolCard?.toolRunning).toBe(false);
    expect(session.messages.some((m) => m.role === "narrative" && m.id === "n1")).toBe(true);
    // 上次没执行的事项
    expect(session.interruptedOperations.map((o) => o.approval_id)).toEqual(["ia_1"]);
    expect(session.interruptedTurns.map((t) => t.turn_id)).toEqual(["turn_a"]);
    // 恢复提示安静、状态诚实
    expect(session.resyncState).toBe("normal");
    expect(session.warning).toBeNull();
  });

  it("RESYNC 也走同一个入口（不是第二套恢复协议）", async () => {
    const { session, events } = setup();
    vi.mocked(api.getRuntimeState).mockResolvedValue(runtimeState() as never);
    events.connect();
    await flushPromises();

    feed({ type: "RESYNC", data: { reason: "subscriber_backlog_overflow" } });
    await flushPromises();

    expect(api.getRuntimeState).toHaveBeenCalledTimes(1);
    expect(session.activeTurnId).toBe("turn_a");
    expect(session.resyncState).toBe("normal");
  });
});

describe("同步期间事项结束不复活", () => {
  it("同步期间被批准的审批不复活；同步期间结束的未执行消息不被旧快照带回", async () => {
    const { session, events, approvals } = setup();
    events.connect();
    const release = deferredState(
      runtimeState({
        approvals: [
          { approval_id: "appr_1", kind: "computer", payload: {}, turn_id: "turn_a" },
        ],
        interrupted_turns: [TURN_A],
      }) as never,
    );

    void restoreRuntimeState("resync");
    await flushPromises();
    expect(events.resyncing).toBe(true);

    // 同步还在飞：审批被（别处）批准、未执行消息被本地忽略 → 都进了缓冲区 / 本地已收口
    feed({ type: "APPROVAL_RESULT", data: { approval_id: "appr_1", decision: "approved" } });
    session.applyInterruptedState([], [TURN_A as never]);
    await session.dismissInterruptedTurn("turn_a");
    // 快照之前本地还有一个「继续/停止」操作条，而这份快照里没有它
    session.pendingContinue = {
      id: "cont_stale",
      used: 1,
      max: 2,
      reason: "budget",
      budgetKind: "iterations",
      message: "",
    };

    release();
    await flushPromises();

    expect(approvals.queue.map((a) => a.approval_id)).toEqual([]); // 不复活
    expect(session.interruptedTurns).toEqual([]); // 不被旧快照带回
    expect(session.pendingContinue).toBeNull(); // 假待办本地收口
    expect(session.resyncState).toBe("normal");
  });
});

describe("旧快照 / 旧实例迟到不覆盖新状态", () => {
  it("同步期间学到更新的实例：这份旧实例快照整段丢弃，重新拉一次新的", async () => {
    const { session, events, approvals } = setup();
    events.connect();
    session.adoptInstance("inst_A");
    session.activeTurnId = "turn_old";
    session.turnRunning = true;
    const releaseOld = deferredState(
      runtimeState({
        instance_id: "inst_A",
        revision: 1,
        approvals: [{ approval_id: "appr_old", kind: "computer", payload: {} }],
        turn_queue: {
          instance_id: "inst_A",
          revision: 1,
          running: { turn_id: "turn_old", message: "旧" },
          queued: [],
          cancelled: [],
        },
      }) as never,
    );
    vi.mocked(api.getRuntimeState).mockResolvedValue(
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

    const pending = restoreRuntimeState("resync");
    await flushPromises();
    // 同步期间本地学到了更新的实例（旧实例的快照还在路上）
    session.adoptInstance("inst_B");

    releaseOld();
    const outcome = await pending;
    await flushPromises();

    expect(outcome.discarded).toBe("stale-instance");
    expect(api.getRuntimeState).toHaveBeenCalledTimes(2);
    // 旧实例快照里的审批 / 「还在跑」都没有被应用
    expect(approvals.queue).toEqual([]);
    expect(session.instanceId).toBe("inst_B");
    expect(session.activeTurnId).toBeNull();
    expect(session.turnRunning).toBe(false);
    expect(events.resyncBuffer).toEqual([]);
  });

  it("同步期间收到新实例的队列快照：旧快照数据被后续权威状态取代", async () => {
    const { session, events, approvals } = setup();
    events.connect();
    session.adoptInstance("inst_A");
    const releaseOld = deferredState(
      runtimeState({
        instance_id: "inst_A",
        revision: 1,
        approvals: [{ approval_id: "appr_old", kind: "computer", payload: {} }],
        turn_queue: {
          instance_id: "inst_A",
          revision: 1,
          running: { turn_id: "turn_old", message: "旧" },
          queued: [],
          cancelled: [],
        },
      }) as never,
    );
    // 学到新实例之后那次权威快照：新实例干净、没有那条旧审批
    vi.mocked(api.getRuntimeState).mockResolvedValue(
      runtimeState({
        instance_id: "inst_B",
        revision: 1,
        approvals: [],
        turn_queue: {
          instance_id: "inst_B",
          revision: 1,
          running: null,
          queued: [],
          cancelled: [],
        },
      }) as never,
    );

    void restoreRuntimeState("resync");
    await flushPromises();
    expect(events.resyncing).toBe(true);

    // 同步期间后端重启：新实例的队列快照到达（先缓冲，绝不与旧快照竞争）
    feed({
      type: "TURN_QUEUE",
      data: { running: null, queued: [], cancelled: [], revision: 1, instance_id: "inst_B" },
    });

    releaseOld();
    await flushPromises();

    // 最终状态必须是新实例的：旧实例的「还在跑」与旧审批都不复活
    expect(session.instanceId).toBe("inst_B");
    expect(session.activeTurnId).toBeNull();
    expect(session.turnRunning).toBe(false);
    expect(approvals.queue.map((a) => a.approval_id)).toEqual([]);
    expect(events.resyncBuffer).toEqual([]);
    expect(session.resyncState).toBe("normal");
  });

  it("旧 generation 的结果被丢弃，并自动重来一轮拿到新状态", async () => {
    const { session, approvals } = setup();
    const releaseOld = deferredState(
      runtimeState({
        instance_id: "inst_old",
        revision: 1,
        approvals: [{ approval_id: "appr_old", kind: "computer", payload: {} }],
        turn_queue: {
          instance_id: "inst_old",
          revision: 1,
          running: { turn_id: "turn_old", message: "旧" },
          queued: [],
          cancelled: [],
        },
      }) as never,
    );
    vi.mocked(api.getRuntimeState).mockResolvedValue(
      runtimeState({
        instance_id: "inst_fresh",
        revision: 9,
        turn_queue: {
          instance_id: "inst_fresh",
          revision: 9,
          running: null,
          queued: [],
          cancelled: [],
        },
        approvals: [{ approval_id: "appr_fresh", kind: "computer", payload: {} }],
      }) as never,
    );

    const before = restoreGeneration();
    const pending = restoreRuntimeState("resync");
    await flushPromises();

    // 连接被重建（外部失效）：在飞的那份结果作废
    invalidateRestore();
    releaseOld();
    const outcome = await pending;
    await flushPromises();

    expect(outcome.discarded).toBe("stale-generation");
    expect(restoreGeneration()).toBeGreaterThan(before + 1);
    expect(api.getRuntimeState).toHaveBeenCalledTimes(2);
    // 只有新状态生效：旧实例的审批与「还在跑」都不在
    expect(session.instanceId).toBe("inst_fresh");
    expect(session.activeTurnId).toBeNull();
    expect(approvals.queue.map((a) => a.approval_id)).toEqual(["appr_fresh"]);
  });
});

describe("单飞与失败语义", () => {
  it("重复 RESYNC 不并发第二个快照；完成后再补一轮", async () => {
    const { events, session } = setup();
    const release = deferredState(runtimeState() as never);
    vi.mocked(api.getRuntimeState).mockResolvedValue(runtimeState() as never);

    const first = restoreRuntimeState("resync");
    await flushPromises();
    const second = restoreRuntimeState("resync");
    const third = restoreRuntimeState("reconnect");
    await flushPromises();

    expect(api.getRuntimeState).toHaveBeenCalledTimes(1);

    release();
    await Promise.all([first, second, third]);
    await flushPromises();

    // 单飞期间来的请求只登记「完成后再来一轮」
    expect(api.getRuntimeState).toHaveBeenCalledTimes(2);
    expect(session.resyncState).toBe("normal");
    expect(events.resyncBuffer).toEqual([]);
  });

  it("快照失败：进入 failed 并保留原因，绝不假装已同步", async () => {
    const { session } = setup();
    vi.mocked(api.getRuntimeState).mockRejectedValueOnce(new Error("network down"));

    const outcome = await restoreRuntimeState("resync");

    expect(outcome.ok).toBe(false);
    expect(session.resyncState).toBe("failed");
    expect(session.lastError).toContain("network down");
    expect(session.warning).toBeNull();
  });
});
