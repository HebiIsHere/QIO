/**
 * 子智能体 D · 真实执行状态与暂停 / 继续 / 取消（契约 §14.1 / §14.4 / §14.6）。
 *
 * 关键边界：不允许「已批准就一直运行中」——
 *   - 排队 / 执行中 / 暂停 / 结束（成功、失败、取消）都是服务端执行记录里的状态；
 *   - 意图说 running 但没有执行记录时，如实说「已批准，等待执行记录」，不画读条；
 *   - 成功 / 失败 / 取消都结束读条；刷新只恢复事实，不重复执行。
 * 服务层全部 mock，不依赖后端。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useInteractiveStore } from "../../stores/interactive";
import * as api from "../../services/interactive";
import { executionIsLive, executionProgress, executionText } from "../approval";
import type { BoardState, Execution, ExecutionStep, Intent } from "../types";

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  fetchIntents: vi.fn(),
  submitBoard: vi.fn(),
  fetchUnderstanding: vi.fn(),
  retryUnderstanding: vi.fn(),
  fetchExecutions: vi.fn(),
  createIntents: vi.fn(),
  approveIntent: vi.fn(),
  rejectIntent: vi.fn(),
  batchDecide: vi.fn(),
  pauseIntent: vi.fn(),
  resumeIntent: vi.fn(),
  cancelIntent: vi.fn(),
}));

const NOW = new Date().toISOString();

function boardState(seq = 3): BoardState {
  return { boardId: "board_t", seq, updatedAt: NOW, cards: [], groups: [], links: [], selection: [] };
}

function boardPayload(seq = 3) {
  return {
    board: { id: "board_t", title: "板面" },
    state: boardState(seq),
    seq,
    baseline: null,
    submissions: [],
    drafts: { drafts: {}, updatedAt: null },
    pending: { expressions: [] },
  };
}

function emptySnapshot() {
  return { cards: [], groups: [], links: [], selection: [], empty: true };
}

function step(index: number, status: ExecutionStep["status"]): ExecutionStep {
  return { index, op: "add_reply_card", status };
}

function exec(
  id: string,
  intentId: string,
  status: Execution["status"],
  steps: ExecutionStep[] = [],
  error: string | null = null,
): Execution {
  return {
    id,
    intentId,
    boardId: "board_t",
    attempt: 1,
    status,
    steps,
    result: { cardIds: [], groupIds: [], linkIds: [] },
    error,
    createdAt: NOW,
    updatedAt: NOW,
  };
}

function intentFixture(id: string, status: Intent["status"], execution?: Execution | null): Intent {
  return {
    id,
    boardId: "board_t",
    submissionId: "s1",
    title: "整理材料 " + id,
    summary: "",
    status,
    preview: { cards: [], groups: [], links: [], note: "" },
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 0, text: "" },
    reason: "",
    demo: false,
    execution: execution ?? null,
    createdAt: NOW,
    updatedAt: NOW,
  };
}

async function seed(store: ReturnType<typeof useInteractiveStore>): Promise<void> {
  vi.mocked(api.fetchBoardState).mockResolvedValue(boardPayload() as never);
  vi.mocked(api.saveBoardState).mockImplementation(
    async (_boardId, putState) => ({ ok: true, seq: 4, savedAt: NOW, state: putState }) as never,
  );
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: NOW } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({
    visibleRange: { ...emptySnapshot(), notVisibleCount: 0 },
  } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({
    ok: true,
    checkId: "chk",
    stateVersion: 3,
    affected: [],
    impactConfirmationRequired: false,
  } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  } as never);
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understanding: null, understandings: [] } as never);
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
  vi.mocked(api.createIntents).mockResolvedValue({ created: [], demo: false } as never);
  await store.load();
}

beforeEach(() => {
  vi.useFakeTimers();
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("执行状态真实显示（契约 §14.1）", () => {
  it("排队 / 执行中 / 暂停 / 结束各自显示，结束就停止查询", async () => {
    const store = useInteractiveStore();
    await seed(store);
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [intentFixture("i1", "running"), intentFixture("i2", "paused")],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    } as never);
    vi.mocked(api.fetchExecutions).mockResolvedValue({
      executions: [
        exec("e1", "i1", "queued", [step(0, "pending")]),
        exec("e2", "i2", "paused", [step(0, "done"), step(1, "pending")]),
      ],
    } as never);
    await store.load();
    expect(store.executionStatus.i1).toBe("queued");
    expect(store.executionStatus.i2).toBe("paused");
    expect(store.liveExecutions.map((item) => item.id)).toEqual(["e1"]);
    expect(executionText(store.latestExecution("i1"), intentFixture("i1", "running")).label).toBe("排队中");
    expect(executionText(store.latestExecution("i2"), intentFixture("i2", "paused")).label).toBe("已暂停");
    expect(store.executionPolling).toBe(true);

    // 结束（成功 / 取消）：读条必须停下，状态说结束
    vi.mocked(api.fetchExecutions).mockResolvedValue({
      executions: [
        exec("e1", "i1", "done", [step(0, "done"), step(1, "done")]),
        exec("e2", "i2", "cancelled", [step(0, "done")]),
      ],
    } as never);
    await vi.advanceTimersByTimeAsync(1300);
    expect(store.executionStatus.i1).toBe("done");
    expect(store.executionStatus.i2).toBe("cancelled");
    expect(store.executionPolling).toBe(false);
    const done = store.latestExecution("i1");
    expect(executionIsLive(done)).toBe(false);
    expect(executionText(done, intentFixture("i1", "done")).settled).toBe(true);
    expect(executionProgress(done).text).toContain("已完成 2/2 步");
  });

  it("已批准但没有执行记录：如实说「等待执行记录」，不显示成运行中", async () => {
    const store = useInteractiveStore();
    await seed(store);
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [intentFixture("i1", "running")],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    } as never);
    await store.load();
    expect(store.latestExecution("i1")).toBeNull();
    expect(store.executionStatus.i1).toBeUndefined();
    const text = executionText(null, intentFixture("i1", "running"));
    expect(text.running).toBe(false);
    expect(text.label).toBe("已批准，等待执行记录");
    expect(text.detail).toContain("不会把它显示成正在执行");
    expect(store.executionPolling).toBe(false);
  });

  it("批准后立刻显示服务端同步返回的执行记录；execution 缺失时不造假", async () => {
    const store = useInteractiveStore();
    await seed(store);
    vi.mocked(api.approveIntent).mockResolvedValue({
      ok: true,
      execution: exec("e1", "i1", "running", [step(0, "running")]),
    } as never);
    await store.approve("i1");
    // 不等下一次查询：批准回执里的真实执行立刻生效
    expect(store.executionStatus.i1).toBe("running");
    expect(store.latestExecution("i1")?.id).toBe("e1");
    expect(store.executionPolling).toBe(true);

    // 演示意图 / 没有执行：approve 返回 execution=null，界面不许自己造一条运行中的记录
    vi.mocked(api.approveIntent).mockResolvedValue({ ok: true, execution: null } as never);
    await store.approve("i2");
    expect(store.latestExecution("i2")).toBeNull();
    expect(store.executionStatus.i2).toBeUndefined();
  });

  it("失败保留真实原因并结束读条", () => {
    const execution = exec("e1", "i1", "failed", [step(0, "done"), step(1, "failed")], "第二步没有成功：材料版本已变化");
    const text = executionText(execution, intentFixture("i1", "failed"));
    expect(text.label).toBe("失败");
    expect(text.detail).toContain("材料版本已变化");
    expect(text.running).toBe(false);
    expect(text.settled).toBe(true);
    expect(executionIsLive(execution)).toBe(false);
  });
});

describe("暂停 / 继续 / 取消都有真实入口（契约 §14.4）", () => {
  it("三个操作各自调用对应接口；重复点击不重复发请求", async () => {
    const store = useInteractiveStore();
    await seed(store);
    vi.mocked(api.pauseIntent).mockResolvedValue({ ok: true } as never);
    vi.mocked(api.resumeIntent).mockResolvedValue({ ok: true } as never);
    vi.mocked(api.cancelIntent).mockResolvedValue({ ok: true } as never);

    await store.pauseExecution("i1");
    expect(api.pauseIntent).toHaveBeenCalledWith("i1");
    await store.resumeExecution("i1");
    expect(api.resumeIntent).toHaveBeenCalledWith("i1");
    await store.cancelExecution("i1");
    expect(api.cancelIntent).toHaveBeenCalledWith("i1");

    // 同一意图同一时间只发一次：第二次如实返回「正在处理中」，不发请求
    vi.mocked(api.pauseIntent).mockClear();
    let release: (value: unknown) => void = () => {};
    vi.mocked(api.pauseIntent).mockImplementation(
      () => new Promise((resolve) => { release = resolve; }) as never,
    );
    const first = store.pauseExecution("i2");
    const second = await store.pauseExecution("i2");
    expect(second.ok).toBe(false);
    expect(second.reason ?? "").toContain("正在处理中");
    expect(api.pauseIntent).toHaveBeenCalledTimes(1);
    release({ ok: true });
    await first;
  });

  it("重复批准不会重复创建或重复执行", async () => {
    const store = useInteractiveStore();
    await seed(store);
    let release: (value: unknown) => void = () => {};
    vi.mocked(api.approveIntent).mockImplementation(
      () => new Promise((resolve) => { release = resolve; }) as never,
    );
    const first = store.approve("i1");
    const second = await store.approve("i1");
    expect(second.ok).toBe(false);
    expect(api.approveIntent).toHaveBeenCalledTimes(1);
    release({ ok: true });
    await first;
  });
});
