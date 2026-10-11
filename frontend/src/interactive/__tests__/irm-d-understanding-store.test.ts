/**
 * 子智能体 D · 真实理解的状态分离（契约 §14.1 / §14.4 / §14.6）。
 *
 * 这一轮要分开的四件事：提交已可靠受理 / 模型开始理解 / 理解完成（三态）/ 任务结束。
 * 这里验证前两件与三态理解结果，并验证三条不许绕过的边界：
 *   1. 受理成功只表示「已可靠收下」，绝不冒充「模型理解完成」；
 *   2. 受理情况不明（回执丢失 / 请求失败）先查询确认，不盲目重发；
 *   3. 页面刷新只恢复事实，不重复创建意图、不重复执行。
 * 服务层全部 mock，不依赖后端。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useInteractiveStore } from "../../stores/interactive";
import * as api from "../../services/interactive";
import { understandingOutcome, understandingText } from "../approval";
import type { BoardState, Intent, SubmissionResult, Understanding } from "../types";

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  fetchIntents: vi.fn(),
  submitBoard: vi.fn(),
  fetchAcceptance: vi.fn(),
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
  return {
    boardId: "board_t",
    seq,
    updatedAt: NOW,
    cards: [],
    groups: [],
    links: [],
    selection: [],
  };
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

function understandingFixture(over: Partial<Understanding> = {}): Understanding {
  return {
    id: "u1",
    boardId: "board_t",
    submissionId: "s1",
    requestId: "req-1",
    status: "accepted",
    stage: "",
    kind: null,
    steps: [],
    materials: [],
    summary: "",
    clarification: null,
    validation: { ok: true, reasons: [] },
    error: null,
    attempt: 1,
    adapter: "fake",
    intentIds: [],
    memory: [],
    createdAt: NOW,
    updatedAt: NOW,
    ...over,
  };
}

function intentFixture(id: string, understandingId: string, status: Intent["status"] = "pending"): Intent {
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
    understandingId,
    createdAt: NOW,
    updatedAt: NOW,
  };
}

function submitResult(over: Partial<SubmissionResult> = {}): SubmissionResult {
  return {
    status: "succeeded",
    submission: { id: "s1", seq: 4, status: "succeeded", createdAt: NOW },
    before: emptySnapshot(),
    after: emptySnapshot(),
    expressions: [],
    baseline: { updated: true, previousSeq: 3, seq: 4 },
    delivery: { delivered: true, reason: "", detail: "", understandingId: "u1", status: "accepted" },
    visibleRange: emptySnapshot(),
    checkedCleared: [],
    ...over,
  } as SubmissionResult;
}

/** 推进挂钟：既跑定时器，也把微任务排空 */
async function tick(ms = 1100): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms);
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
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({
    understanding: null,
    understandings: [],
  } as never);
  vi.mocked(api.retryUnderstanding).mockResolvedValue({ ok: true } as never);
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

describe("受理与理解分开（契约 §14.1）", () => {
  it("受理成功只显示「已受理」，理解状态随查询结果更新，理解完成才产出待审批意图", async () => {
    const store = useInteractiveStore();
    await seed(store);
    vi.mocked(api.submitBoard).mockResolvedValue(submitResult() as never);

    const result = await store.submit();
    expect(result?.status).toBe("succeeded");
    // 受理是回执说的事实：立刻可显示「已受理」，但绝不冒充「理解完成」
    expect(store.submitStatus).toBe("succeeded");
    expect(store.understandingStatus).toBe("accepted");
    expect(store.submissionStage).toBe("accepted");
    expect(store.submitNote ?? "").toContain("已受理");
    expect(store.understanding).toBeNull();

    // 第一次查询：正在理解（说明来自服务端 stage，不是前端编的）
    const understandingRecord = understandingFixture({
      status: "understanding",
      stage: "正在读取这次提交的材料",
    });
    vi.mocked(api.fetchUnderstanding).mockResolvedValue({
      understanding: understandingRecord,
      understandings: [understandingRecord],
    } as never);
    await tick();
    expect(store.understandingStatus).toBe("understanding");
    expect(store.submissionStage).toBe("understanding");
    expect(understandingText(store.understanding, store.understandingStatus).detail).toContain(
      "正在读取这次提交的材料",
    );

    // 第二次查询：理解完成，产出两项等待审批的意图
    const understood = understandingFixture({
      status: "understood",
      kind: "intent",
      summary: "整理两份材料",
      intentIds: ["i1", "i2"],
    });
    vi.mocked(api.fetchUnderstanding).mockResolvedValue({
      understanding: understood,
      understandings: [understood],
    } as never);
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [intentFixture("i1", "u1"), intentFixture("i2", "u1")],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    } as never);
    await tick();
    await tick(0);

    expect(store.understandingStatus).toBe("understood");
    expect(store.submissionStage).toBe("understood");
    expect(store.understandingIntents.map((item) => item.id)).toEqual(["i1", "i2"]);
    // 同一个理解请求只拉一次意图：不重复创建
    expect(api.createIntents).toHaveBeenCalledTimes(1);
    await store.syncIntentsFromUnderstanding();
    expect(api.createIntents).toHaveBeenCalledTimes(1);
  });

  it("需要补充：给出缺失项，不生成任何任务", () => {
    const record = understandingFixture({
      status: "needs_clarification",
      kind: "clarification",
      clarification: { question: "还需要哪些材料？", missing: ["材料版本", "时间范围"] },
    });
    expect(understandingOutcome("needs_clarification")).toBe("clarification");
    const text = understandingText(record, "needs_clarification");
    expect(text.label).toBe("需要补充");
    expect(text.detail).toContain("材料版本");
    expect(text.detail).toContain("时间范围");
    expect(text.detail).toContain("没有生成任何任务");
    expect(text.needsUser).toBe(true);
  });

  it("无需行动：明确说明本次没有需要开展的任务", () => {
    const text = understandingText(
      understandingFixture({ status: "no_action", kind: "no_action", summary: "" }),
      "no_action",
    );
    expect(understandingOutcome("no_action")).toBe("no_action");
    expect(text.label).toBe("本次无需开展任务");
    expect(text.detail).toContain("没有生成任何任务");
  });

  it("理解失败：重试前先查询确认；理解已经不在失败状态时拒绝重发", async () => {
    const store = useInteractiveStore();
    await seed(store);

    // 受理情况不明 + 服务端说这次理解失败了 → 先查询确认，再重试一次
    const failed = understandingFixture({ status: "failed", error: "模型没有返回有效结果" });
    vi.mocked(api.fetchUnderstanding).mockResolvedValue({
      understanding: failed,
      understandings: [failed],
    } as never);
    const retried = await store.retryUnderstanding("u1");
    expect(api.fetchUnderstanding).toHaveBeenCalled();
    expect(api.retryUnderstanding).toHaveBeenCalledTimes(1);
    expect(api.retryUnderstanding).toHaveBeenCalledWith("u1");
    expect(retried.ok).toBe(true);

    // 查询确认它已经完成：不再重试（不盲目重发）
    vi.mocked(api.retryUnderstanding).mockClear();
    const done = understandingFixture({ status: "understood", kind: "intent" });
    vi.mocked(api.fetchUnderstanding).mockResolvedValue({
      understanding: done,
      understandings: [done],
    } as never);
    const refused = await store.retryUnderstanding("u1");
    expect(refused.ok).toBe(false);
    expect(refused.reason ?? "").toContain("不需要重试");
    expect(api.retryUnderstanding).not.toHaveBeenCalled();
  });
});

/**
 * 契约 §1.3 改写了这一段的口径：受理证据**只有**一条路径 ——
 * 同一个 clientRequestId 在服务端的精确记录（fetchAcceptance）。
 * 旧口径（按 createdAt 接近 + 排除上一次 submissionId 去「认领」一条理解记录）
 * 已经删除；查不到 / 查询失败一律继续「结果未知」，不许改判失败，也不许拿
 * 上一次提交留下的理解记录冒充本次（§1.1.3 / §1.1.4 / §1.1.5）。
 */
describe("受理情况不明时按请求身份精确查证（契约 §1.3）", () => {
  it("服务端按同一个请求身份确认受理：恢复受理与理解身份，且不重复提交", async () => {
    const store = useInteractiveStore();
    await seed(store);
    vi.mocked(api.submitBoard).mockRejectedValue(new Error("网络中断，没有收到回执"));
    // 服务端按**客户端发出的**身份与内容依据返回事实：身份与基准版本都对得上才算命中
    vi.mocked(api.fetchAcceptance).mockImplementation(async (_boardId, clientRequestId) => ({
      acceptance: {
        clientRequestId,
        submissionId: "s9",
        status: "succeeded",
        delivery: { delivered: true, reason: "", detail: "", understandingId: "u9", status: "accepted" },
        understandingId: "u9",
        understandingStatus: "accepted",
        baseStateVersion: vi.mocked(api.submitBoard).mock.calls[0][3],
        note: "",
        createdAt: new Date(Date.now() - 1000).toISOString(),
        replayed: false,
      },
    } as never));

    const result = await store.submit();
    expect(result).toBeNull();
    expect(api.submitBoard).toHaveBeenCalledTimes(1); // 绝不自动重发
    expect(store.submitStatus).toBe("succeeded");
    expect(store.submitNote ?? "").toContain("同一个请求身份");
    expect(store.understandingStatus).toBe("accepted");
    expect(store.submissionStage).toBe("accepted");
    expect(store.acceptedUnderstandingId, "恢复受理回执给出的理解请求身份").toBe("u9");
    expect(store.lastAcceptance?.submissionId).toBe("s9");
    // 查证用的必须是这次提交发出的那个身份
    expect(vi.mocked(api.fetchAcceptance).mock.calls[0][1]).toBe(
      vi.mocked(api.submitBoard).mock.calls[0][5],
    );
  });

  it("只有上一次提交留下的理解记录（这次身份查不到）：继续「结果未知」，不认领、不改判失败", async () => {
    const store = useInteractiveStore();
    await seed(store);
    // 先有一次成功提交，留下 lastSubmission(s1) 与它的理解记录
    vi.mocked(api.submitBoard).mockResolvedValue(submitResult() as never);
    await store.submit();
    expect(store.submitStatus).toBe("succeeded");

    // 第二次请求失败；这次身份在服务端**没有**记录，只有上一次 s1 的理解记录
    vi.mocked(api.submitBoard).mockRejectedValue(new Error("网络中断，没有收到回执"));
    vi.mocked(api.fetchAcceptance).mockResolvedValue({
      acceptance: null,
      reason: "没有这条请求身份的记录",
    } as never);
    const old = understandingFixture({
      id: "u_old",
      submissionId: "s1",
      status: "understood",
      kind: "intent",
      createdAt: new Date(Date.now() - 1000).toISOString(),
    });
    vi.mocked(api.fetchUnderstanding).mockResolvedValue({
      understanding: old,
      understandings: [old],
    } as never);

    await store.submit();
    expect(store.submitStatus, "查不到不是失败结论").toBe("unknown");
    expect(store.submitError ?? "").toContain("网络中断");
    expect(store.understandingStatus, "上一次的 understood 不许冒充本次理解").not.toBe("understood");
    expect(store.submissionStage).toBe("unknown");
    expect(store.acceptanceLookupNote ?? "").toContain("结果仍然未知");
  });

  it("查证也没有记录 / 查询失败：继续「结果未知」，保留改动，不盲目重发", async () => {
    const store = useInteractiveStore();
    await seed(store);
    vi.mocked(api.submitBoard).mockRejectedValue(new Error("网络中断，没有收到回执"));
    vi.mocked(api.fetchAcceptance).mockResolvedValue({
      acceptance: null,
      reason: "没有这条请求身份的记录",
    } as never);

    const result = await store.submit();
    expect(result).toBeNull();
    expect(api.submitBoard).toHaveBeenCalledTimes(1);
    expect(store.submitStatus, "没有精确证据就只能说「未知」").toBe("unknown");
    expect(store.submitResultUnknown).toBe(true);
    expect(store.submitError ?? "").toContain("网络中断");
    expect(store.submitNote, "没命中不许出现「已受理」的说明").toBeNull();

    // 查证本身失败（断网）：同样只说明「还没确认」
    const second = useInteractiveStore();
    await seed(second);
    vi.mocked(api.fetchAcceptance).mockRejectedValue(new Error("查证也断网了"));
    await second.submit();
    expect(second.submitStatus).toBe("unknown");
    expect(second.acceptanceLookupNote ?? "").toContain("查证没有完成");
  });
});

describe("页面刷新只恢复事实（契约 §14.6）", () => {
  it("刷新后恢复理解状态与执行状态，不重复创建意图、不重复执行", async () => {
    const store = useInteractiveStore();
    await seed(store);
    const understood = understandingFixture({
      status: "understood",
      kind: "intent",
      summary: "整理材料",
      intentIds: ["i1"],
    });
    vi.mocked(api.fetchUnderstanding).mockResolvedValue({
      understanding: understood,
      understandings: [understood],
    } as never);
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [intentFixture("i1", "u1", "running")],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    } as never);
    const runningExecution = {
      id: "e1",
      intentId: "i1",
      boardId: "board_t",
      attempt: 1,
      status: "running",
      steps: [],
      result: { cardIds: [], groupIds: [], linkIds: [] },
      error: null,
      createdAt: NOW,
      updatedAt: NOW,
    };
    vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [runningExecution] } as never);

    await store.load();
    expect(store.understandingStatus).toBe("understood");
    expect(store.submissionStage).toBe("understood");
    expect(store.executionStatus.i1).toBe("running");
    expect(api.submitBoard).not.toHaveBeenCalled();
    // 已结束的理解不需要再拉意图：刷新不会重复创建
    expect(api.createIntents).not.toHaveBeenCalled();
    // 还在执行的记录要继续查（真实进度），不是前端自己动
    expect(store.executionPolling).toBe(true);
    store.stopUnderstandingPolling();
    // 服务端说这次执行结束了：读条停下，不出现「已批准一直运行中」
    vi.mocked(api.fetchExecutions).mockResolvedValue({
      executions: [
        { ...runningExecution, status: "done", updatedAt: new Date(Date.now() + 1000).toISOString() },
      ],
    } as never);
    await tick(1300);
    expect(store.executionStatus.i1).toBe("done");
    expect(store.executionPolling).toBe(false);
  });
});
