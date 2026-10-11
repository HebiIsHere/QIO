/**
 * 【E 独立验收探针 · 不进产品代码】
 *
 * 目的：用**计数**证明「提交成功之后的读取只发生一次、批次记录只落一次」，
 * 而不是只看界面文案。基线 c6a03b1 上这条断言会失败（读取与批次记录各发生两次），
 * 修复后必须通过。
 *
 * 运行方式（由 scripts/irm-int-verify/README.md 记录）：
 *   1. 复制本文件到 frontend/src/__irm_int_e_probe__/e-submit-read-once.probe.test.ts
 *   2. 在 frontend 目录执行：npm test -- src/__irm_int_e_probe__/e-submit-read-once.probe.test.ts
 *   3. 运行完删除 frontend/src/__irm_int_e_probe__/（不提交）
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";

vi.mock("../interactive/approval", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../interactive/approval")>();
  return { ...actual, recordIntentBatch: vi.fn(actual.recordIntentBatch) };
});

vi.mock("../services/interactive", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../services/interactive")>();
  return {
    ...actual,
    fetchBoardState: vi.fn(),
    saveBoardState: vi.fn(),
    saveDrafts: vi.fn(),
    fetchVisibleRange: vi.fn(),
    previewMaterialImpact: vi.fn(),
    checkMaterialImpact: vi.fn(),
    submitBoard: vi.fn(),
    fetchIntents: vi.fn(),
    fetchUnderstanding: vi.fn(),
    fetchExecutions: vi.fn(),
    fetchAcceptance: vi.fn(),
  };
});

import { useInteractiveStore } from "../stores/interactive";
import * as api from "../services/interactive";
import { recordIntentBatch } from "../interactive/approval";
import type { BoardState, SubmissionResult } from "../interactive/types";
import { card, intent, payload } from "../acceptance-state-recovery/support/harness";

function succeededResult(seq: number): SubmissionResult {
  return {
    status: "succeeded",
    submission: { id: "s1", seq, status: "succeeded", createdAt: "t" },
    before: { cards: [], groups: [], links: [], selection: [], empty: true },
    after: { cards: [], groups: [], links: [], selection: [], empty: true },
    expressions: [],
    baseline: { updated: true, previousSeq: seq - 1, seq },
    delivery: { delivered: true, status: "accepted", understandingId: "u1", reason: null, detail: null },
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true },
    checkedCleared: [],
  } as unknown as SubmissionResult;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.clearAllMocks();
  vi.mocked(api.fetchBoardState).mockResolvedValue(payload(3, [card("c1", "正文")]) as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t", rev: 1 } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: null } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({
    ok: true,
    affected: [],
    impactConfirmationRequired: false,
  } as never);
  vi.mocked(api.saveBoardState).mockImplementation(async (_boardId, putState) => {
    const put = putState as BoardState;
    return { ok: true, seq: 4, savedAt: "t4", state: { ...put, seq: 4 } } as never;
  });
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understandings: [] } as never);
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
  vi.mocked(api.fetchAcceptance).mockResolvedValue({ acceptance: null } as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("E 探针：提交成功路径不得重复读取 / 重复记批次", () => {
  it("POST 明确成功后：板面、可见范围、任务清单各只读一次，批次只记一次", async () => {
    const store = useInteractiveStore();
    await store.load();
    // 让 load 期间还在飞的请求先落地，避免把它们算进「提交之后的读取」
    await flushPromises();

    vi.mocked(api.submitBoard).mockResolvedValue(succeededResult(4) as never);
    // 提交之后清单里出现一条新意图：这样「批次记录」才有东西可记
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [intent("i-new")],
      conflicts: [],
      batchAvailable: true,
    } as never);

    const before = {
      board: vi.mocked(api.fetchBoardState).mock.calls.length,
      visible: vi.mocked(api.fetchVisibleRange).mock.calls.length,
      intents: vi.mocked(api.fetchIntents).mock.calls.length,
      batches: vi.mocked(recordIntentBatch).mock.calls.length,
    };

    const result = await store.submit();
    await flushPromises();

    expect(result, "提交请求本身必须成功返回；失败原因=" + String(store.submitError)).not.toBeNull();
    const after = {
      board: vi.mocked(api.fetchBoardState).mock.calls.length,
      visible: vi.mocked(api.fetchVisibleRange).mock.calls.length,
      intents: vi.mocked(api.fetchIntents).mock.calls.length,
      batches: vi.mocked(recordIntentBatch).mock.calls.length,
    };
    // 一次性把四个计数都报出来：失败时能直接看到「哪一项多了几次」
    const delta = {
      board: after.board - before.board,
      visible: after.visible - before.visible,
      intents: after.intents - before.intents,
      batches: after.batches - before.batches,
    };
    expect(
      { board: delta.board, intents: delta.intents, batches: delta.batches },
      "提交成功后的板面读取 / 清单读取 / 批次记录（期望各一次）",
    ).toEqual({ board: 1, intents: 1, batches: 1 });
    // 可见范围多一次是**合理**的：saveNow 成功路径里有一句 fire-and-forget 的
    // refreshVisibleRange()（stores/interactive.ts 保存成功分支），与「提交后刷新」无关。
    expect(delta.visible, "可见范围读取（saveNow 一次 + 提交后刷新一次）").toBe(2);
  });

  it("POST 明确成功、读取失败：提交事实不变，只标「页面刷新失败」", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.submitBoard).mockResolvedValue(succeededResult(4) as never);
    vi.mocked(api.fetchBoardState).mockRejectedValue(new Error("网络中断：读取板面失败"));

    const result = await store.submit();
    await flushPromises();

    expect(result, "读取失败不得把提交结果改判成未知；实际 submitStatus=" + store.submitStatus).not.toBeNull();
    expect(store.submitStatus).toBe("succeeded");
    expect(store.submitError).toBeNull();
    expect(store.submitRefreshState).toBe("error");
    expect(store.submitRefreshError ?? "").toContain("网络中断");
  });
});
