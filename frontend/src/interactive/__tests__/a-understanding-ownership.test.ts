/**
 * 任务书 3 / 契约 §3：理解状态必须归属**本次**提交（子智能体 A）。
 *
 * 两条反例（Lead 指定的验收点）：
 * a) 第一次提交的理解已经完成 → 第二次提交受理成功但查询失败：
 *    界面必须显示本次的「已受理（查询还没成功）」，不得显示上一次的「理解完成」；
 * b) 旧查询（属于上一次提交 / 代次已过期）迟到返回「理解完成」：
 *    新提交的「已受理」不许被覆盖。
 *
 * 另外锁死归属校验本身：查询返回的记录不属于本次提交时，不许被采纳成本次的理解
 * （整合基线的 adoptUnderstandings 会退回 list[0]，正好让上一次的记录冒充本次）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../../stores/interactive";
import * as api from "../../services/interactive";
import type { BoardState, SubmissionResult, Understanding } from "../types";

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

/** 第二次提交的受理回执：另一个 submissionId + 另一个 understandingId */
function secondReceipt(): SubmissionResult {
  return submitResult({
    submission: { id: "s2", seq: 5, status: "succeeded", createdAt: NOW },
    delivery: { delivered: true, reason: "", detail: "", understandingId: "u2", status: "accepted" },
  });
}

async function tick(ms = 1100): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms);
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

async function seed(): Promise<ReturnType<typeof useInteractiveStore>> {
  const store = useInteractiveStore();
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
    ok: true, checkId: "chk", stateVersion: 3, affected: [], impactConfirmationRequired: false,
  } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] },
  } as never);
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understanding: null, understandings: [] } as never);
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
  vi.mocked(api.createIntents).mockResolvedValue({ created: [], demo: false } as never);
  await store.load();
  return store;
}

beforeEach(() => {
  localStorage.clear();
  vi.useFakeTimers();
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("理解状态归属本次提交", () => {
  it("a) 上一次理解已完成、本次受理成功但查询失败：显示本次的「已受理」，不显示上一次的「理解完成」", async () => {
    const store = await seed();

    // 第一次提交：理解很快完成
    vi.mocked(api.submitBoard).mockResolvedValue(submitResult() as never);
    await store.submit();
    const understood = understandingFixture({ id: "u1", submissionId: "s1", status: "understood", kind: "intent" });
    vi.mocked(api.fetchUnderstanding).mockResolvedValue({
      understanding: understood,
      understandings: [understood],
    } as never);
    await tick();
    expect(store.understandingStatus).toBe("understood");

    // 第二次提交：受理成功（s2 / u2），但之后的每一次理解查询都失败
    vi.mocked(api.submitBoard).mockResolvedValue(secondReceipt() as never);
    vi.mocked(api.fetchUnderstanding).mockRejectedValue(new Error("查询理解状态失败"));
    await store.submit();

    expect(store.submitStatus).toBe("succeeded");
    expect(store.understandingStatus, "本次受理成功 → 本次的「已受理」").toBe("accepted");
    expect(store.understanding?.id ?? null, "上一次的 u1 不许留成本次的理解").not.toBe("u1");

    await tick();
    await flushPromises();
    expect(
      store.understandingStatus,
      "查询失败不改判成本次的失败，更不许退回上一次的 understood",
    ).toBe("accepted");
    expect(store.understandingError ?? "").toContain("查询理解状态失败");
    expect(store.understanding?.id ?? null).not.toBe("u1");
  });

  it("b) 旧查询（属于上一次提交）迟到返回「理解完成」：新提交的「已受理」不被覆盖", async () => {
    const store = await seed();
    vi.mocked(api.submitBoard).mockResolvedValue(submitResult() as never);
    await store.submit();
    expect(store.understandingStatus).toBe("accepted");

    // 第一次提交之后的查询挂在飞行中（真实窗口）
    const inflight = deferred<never>();
    vi.mocked(api.fetchUnderstanding).mockReturnValue(inflight.promise as never);
    await tick();

    // 用户又提交了一次：本次受理成功，理解状态归位到本次的「已受理」
    vi.mocked(api.submitBoard).mockResolvedValue(secondReceipt() as never);
    await store.submit();
    expect(store.understandingStatus).toBe("accepted");

    // 旧查询这时才返回「理解完成」（属于 s1 / u1）
    const stale = understandingFixture({ id: "u1", submissionId: "s1", status: "understood", kind: "intent" });
    inflight.resolve({ understanding: stale, understandings: [stale] } as never);
    await flushPromises();
    await tick(0);

    expect(store.understandingStatus, "迟到的旧查询不许覆盖新请求的状态").toBe("accepted");
    expect(store.understanding?.id ?? null, "旧记录不许落地成本次的理解").not.toBe("u1");
  });

  it("归属校验：查询只返回别的提交的记录时，本次 understanding 保持为空、状态保持本次的「已受理」", async () => {
    const store = await seed();
    vi.mocked(api.submitBoard).mockResolvedValue(secondReceipt() as never);
    await store.submit();
    expect(store.understandingStatus).toBe("accepted");

    const other = understandingFixture({ id: "u_other", submissionId: "s1", status: "understood", kind: "intent" });
    vi.mocked(api.fetchUnderstanding).mockResolvedValue({
      understanding: other,
      understandings: [other],
    } as never);
    await tick();
    await flushPromises();

    expect(store.understanding, "别的提交的理解记录不许冒充本次（不许退回 list[0]）").toBeNull();
    expect(store.understandingStatus).toBe("accepted");
    expect(store.understandings.length, "历史仍然列出").toBe(1);
  });
});
