/**
 * F 组独立验证：M12 —— 新前端连接（无历史游标）必须完整恢复权威状态。
 *
 * 场景：后端进程里已经有「等待用户确认的审批」「独立任务」「工具执行记录」「执行叙事」，
 * 一个全新的前端连上来（SSE 建连成功、没有任何历史事件、没有游标）。
 * 它必须把这些都恢复出来，而不是只回来一个 turn 队列。
 *
 * 断言只依据可观察结果：approvals store 的队列、session.messages 里的卡片、
 * 以及快照是否真的被交给工具核对入口（tools 覆盖）。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { useEventStore } from "../events";
import { useApprovalsStore } from "../approvals";
import { api } from "../../services/api";

const hoisted = vi.hoisted(() => {
  const source = {
    onopen: null as null | (() => void),
    onerror: null as null | (() => void),
    close: () => undefined,
  };
  return { source };
});

vi.mock("../../services/events", () => ({
  connectEvents: vi.fn(() => hoisted.source),
  publishTestEvent: vi.fn(async () => undefined),
}));

vi.mock("../../services/api", () => ({
  api: {
    getRuntimeState: vi.fn(),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
    listDevAuthorizations: vi.fn(async () => ({ authorizations: [] })),
  },
}));

const INSTANCE = "inst_fresh";

function runtimeState(overrides: Record<string, unknown> = {}) {
  return {
    instance_id: INSTANCE,
    revision: 3,
    turn_queue: {
      instance_id: INSTANCE,
      revision: 3,
      running: null,
      queued: [],
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

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return {
    session: useSessionStore(),
    events: useEventStore(),
    approvals: useApprovalsStore(),
  };
}

/** 模拟「新前端连上后端」：connect() 建流成功 → onopen 触发首次权威同步。 */
async function connectFresh(events: ReturnType<typeof useEventStore>) {
  events.connect();
  hoisted.source.onopen?.();
  await flushPromises();
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("M12：新连接（无历史游标）的完整恢复", () => {
  it("后端已有的待确认事项 / 独立任务 / 工具记录 / 叙事必须被恢复", async () => {
    const { session, events, approvals } = setup();
    vi.mocked(api.getRuntimeState).mockResolvedValue(
      runtimeState({
        approvals: [
          {
            approval_id: "appr_1",
            kind: "computer",
            payload: { action: "read", path: "/tmp/x" },
            turn_id: "turn_old",
          },
        ],
        tasks: [{ task_id: "task_1", tool: "research", status: "running" }],
        tools: [
          {
            turn_id: "turn_old",
            tool_call_id: "call_1",
            tool_name: "fs_read",
            status: "running",
          },
        ],
        narratives: [
          {
            narrative_id: "nar_1",
            turn_id: "turn_old",
            kind: "progress",
            text: "正在读取文件",
            created_at: new Date().toISOString(),
          },
        ],
      }) as never,
    );

    const reconcileTools = vi.spyOn(session, "reconcileTools");

    await connectFresh(events);

    expect(api.getRuntimeState).toHaveBeenCalledTimes(1);

    // 待确认事项：必须出现在审批队列里（否则界面永远不会让用户点允许/拒绝）
    expect(approvals.queue.map((a) => a.approval_id)).toContain("appr_1");

    // 独立任务：必须有卡片，并且状态就是服务器说的 running
    const taskCard = session.messages.find(
      (m) => m.role === "subagent" && m.taskId === "task_1",
    );
    expect(taskCard, "独立任务卡必须被恢复出来").toBeTruthy();
    expect(taskCard?.taskStatus).toBe("running");

    // 工具执行记录：要么快照被交给工具核对入口，要么直接建出了对应的工具卡
    // （两种实现形状都算「覆盖了 tools」，但都不能像基线那样整份丢掉）
    const toolCard = session.messages.find((m) => m.role === "tool" && m.callId === "call_1");
    const reconcileArgs = (reconcileTools.mock.calls.at(-1)?.[0] ?? []) as Array<{
      tool_call_id?: string;
    }>;
    const toolsCovered =
      Boolean(toolCard) ||
      reconcileArgs.some((record) => record?.tool_call_id === "call_1");
    expect(toolsCovered, "快照里的工具记录不得被整份丢掉").toBe(true);

    // 执行叙事：按 id 去重地补回来
    const narrative = session.messages.find((m) => m.id === "nar_1");
    expect(narrative, "执行叙事必须被恢复出来").toBeTruthy();
    expect(narrative?.content).toBe("正在读取文件");
  });

  it("空快照的下限行为：不报错、不伪造内容（对照）", async () => {
    const { session, events, approvals } = setup();
    vi.mocked(api.getRuntimeState).mockResolvedValue(runtimeState() as never);

    await connectFresh(events);

    expect(approvals.queue).toEqual([]);
    expect(session.messages.filter((m) => m.role === "subagent")).toEqual([]);
    expect(session.messages.filter((m) => m.role === "narrative")).toEqual([]);
  });
});
