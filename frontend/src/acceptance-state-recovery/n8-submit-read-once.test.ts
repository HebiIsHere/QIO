/**
 * F10 / 契约 §3：提交之后的读取**只有一条路径**（子智能体 A）。
 *
 * 反例（整合基线的实际行为，已实测失败）：submit() 成功路径在 POST 的 try 内又做了一遍
 * 板面 / 可见范围 / 清单读取与批次记录，随后 refreshAfterSubmit() 再做一遍：
 * - 读取失败（refreshBoardFromServer 抛错）被 catch 当成「没有回执」→ 成功被改判成未知；
 * - recordNewIntents 执行两次（同一批工作项被记两次）。
 *
 * 本文件锁死：提交成功后板面 / 可见范围 / 清单各**只读一次**，批次只记一次；
 * 读取失败只影响「页面事实新不新」，绝不改变提交事实（F10）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useInteractiveStore } from "../stores/interactive";
import * as api from "../services/interactive";
import * as approval from "../interactive/approval";
import type { SubmissionResult } from "../interactive/types";
import { card, intent, payload } from "./support/harness";

vi.mock("../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
  fetchExecutions: vi.fn(),
}));

// 只把批次记录换成可观测的包装：其它行为仍是真实现（不改变被测逻辑）
vi.mock("../interactive/approval", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../interactive/approval")>();
  return { ...actual, recordIntentBatch: vi.fn(actual.recordIntentBatch) };
});

function succeededResult(seq: number): SubmissionResult {
  return {
    status: "succeeded",
    submission: { id: "s1", seq, status: "succeeded", createdAt: "t" },
    before: { cards: [], groups: [], links: [], selection: [], empty: true },
    after: { cards: [], groups: [], links: [], selection: [], empty: true },
    expressions: [],
    baseline: { updated: true, previousSeq: seq - 1, seq },
    delivery: { delivered: true, reason: "", detail: "", understandingId: "u1", status: "accepted" },
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true },
    checkedCleared: [],
  } as unknown as SubmissionResult;
}

const calls = () => ({
  board: vi.mocked(api.fetchBoardState).mock.calls.length,
  visible: vi.mocked(api.fetchVisibleRange).mock.calls.length,
  intents: vi.mocked(api.fetchIntents).mock.calls.length,
});

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue(payload(4, [card("c1", "正文", { checked: true })]) as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t", rev: 1 } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: null } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({
    ok: true, affected: [], impactConfirmationRequired: false,
  } as never);
  vi.mocked(api.saveBoardState).mockImplementation(async (_boardId, putState) => {
    const put = putState as never as Record<string, unknown>;
    return { ok: true, seq: 4, savedAt: "t4", state: { ...put, seq: 4 } } as never;
  });
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("提交之后的读取只有一条路径", () => {
  it("提交成功后板面 / 可见范围 / 清单各只读一次，批次只记一次", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(approval.recordIntentBatch).mockClear();

    // 提交之后清单里出现一项新意图：这一批必须只被记录一次
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [intent("i9", "pending")], conflicts: [], batchAvailable: false, recovery: { paused: [] },
    } as never);
    /**
     * 以 POST 发出的那一刻为基线：保存本身会顺手刷新一次可见范围（既有行为，
     * 与「提交之后的读取」无关），所以这一段必须从 POST 之后开始量。
     */
    let atPost = calls();
    vi.mocked(api.submitBoard).mockImplementation(async () => {
      atPost = calls();
      return succeededResult(4) as never;
    });

    const result = await store.submit();

    expect(result, "提交成功必须返回结果").not.toBeNull();
    const after = calls();
    expect(after.board - atPost.board, "提交后的板面读取必须只发生一次").toBe(1);
    expect(after.visible - atPost.visible, "提交后的可见范围读取必须只发生一次").toBe(1);
    expect(after.intents - atPost.intents, "提交后的清单读取必须只发生一次").toBe(1);
    expect(
      vi.mocked(approval.recordIntentBatch).mock.calls.length,
      "recordNewIntents 只允许发生一次（重复记录会把同一批工作项记两次）",
    ).toBe(1);
  });

  it("提交后的读取失败：提交事实不变，返回结果非空，且不会再补一次读取", async () => {
    const store = useInteractiveStore();
    await store.load();
    const before = calls();
    vi.mocked(api.submitBoard).mockResolvedValue(succeededResult(4) as never);
    // POST 之后的板面读取失败
    vi.mocked(api.fetchBoardState).mockRejectedValue(new Error("网络中断：读取板面失败"));

    const result = await store.submit();

    expect(result, "POST 已经明确成功，提交请求本身必须算成功（F10）").not.toBeNull();
    expect(store.submitStatus).toBe("succeeded");
    expect(store.submitError).toBeNull();
    expect(store.submitResultUnknown, "读取失败不是「没有回执」").toBe(false);
    expect(store.lastSubmission?.submission?.id, "成功事实必须保留").toBe("s1");
    expect(store.submitRefreshState, "单独记录「页面刷新失败」").toBe("error");
    expect(store.submitRefreshError ?? "").toContain("网络中断");
    expect(
      calls().board - before.board,
      "读取失败也只是一次尝试：不许在别处再补一次读取",
    ).toBe(1);
  });
});

describe("任务清单读取（契约 §3 第 7 条）", () => {
  it("成功读取时同时接收真实执行记录，界面读到的就是服务端事实", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    await store.load();
    expect(store.latestExecution("t1"), "还没有执行记录").toBeNull();

    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [intent("t1", "running")],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
      executions: [
        {
          id: "e1",
          intentId: "t1",
          boardId: "board_default",
          attempt: 1,
          status: "running",
          steps: [],
          result: { cardIds: [], groupIds: [], linkIds: [] },
          error: null,
          createdAt: "t",
          updatedAt: "t",
        },
      ],
    } as never);

    await store.loadIntents();
    expect(store.intentsLoadState).toBe("ok");
    expect(store.latestExecution("t1")?.id, "清单里带的执行记录必须被接收").toBe("e1");
    expect(store.executionStatus.t1).toBe("running");
  });
});
