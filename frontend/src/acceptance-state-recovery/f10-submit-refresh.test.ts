/**
 * 【审计 8fa8452 · F10】成功提交被后续刷新失败改判成「提交失败」。
 *
 * 反例（基线 interactive.submit）：提交请求与提交之后的 GET 在同一个 try 里，
 * POST 已经明确成功后，紧跟着的 refreshBoardFromServer / loadIntents 一旦失败就被
 * 当成**提交失败**：成功事实被抹掉、基线依据丢失，界面还会诱导用户重新提交。
 *
 * 正确行为：
 * - 提交请求结果与后续读取结果分开记录；
 * - POST 成功后 GET 失败 → 显示「提交成功，页面刷新失败」，保留成功事实与基线依据；
 * - 提供**只触发读取**的重试，绝不自动重复提交；
 * - 没有拿到回执（超时 / 断线 / 无响应）单独表达「提交结果未知」。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../stores/interactive";
import * as api from "../services/interactive";
import type { BoardState, SubmissionResult } from "../interactive/types";
import { card, payload } from "./support/harness";

vi.mock("../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
}));

function succeededResult(seq: number): SubmissionResult {
  return {
    status: "succeeded",
    submission: { id: "s1", seq, status: "succeeded", createdAt: "t" },
    before: { cards: [], groups: [], links: [], selection: [], empty: true },
    after: { cards: [], groups: [], links: [], selection: [], empty: true },
    expressions: [],
    baseline: { updated: true, previousSeq: seq - 1, seq },
    delivery: { delivered: false, reason: "未接入", detail: "" },
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true },
    checkedCleared: [],
  } as unknown as SubmissionResult;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue(payload(3, [card("c1", "正文")]) as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t", rev: 1 } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: null } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({
    ok: true, affected: [], impactConfirmationRequired: false,
  } as never);
  vi.mocked(api.saveBoardState).mockImplementation(async (_boardId, putState) => {
    const put = putState as BoardState;
    return { ok: true, seq: 4, savedAt: "t4", state: { ...put, seq: 4 } } as never;
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("F10 提交成功之后刷新失败", () => {
  it("POST 已成功、后续 GET 失败：保留成功事实，并只给「重试刷新」", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.submitBoard).mockResolvedValue(succeededResult(4) as never);

    // 提交之后的读取失败（板面 GET 挂掉）
    vi.mocked(api.fetchBoardState).mockRejectedValue(new Error("网络中断：读取板面失败"));

    const result = await store.submit();
    await flushPromises();

    expect(result, "提交请求本身已经成功，必须返回成功结果").not.toBeNull();
    expect(store.submitStatus, "提交成功被刷新失败改判成失败（F10）").toBe("succeeded");
    expect(store.submitError, "提交成功不该显示提交失败原因").toBeNull();
    expect(store.lastSubmission?.submission?.id, "成功事实（回执）必须保留").toBe("s1");
    expect(store.lastSubmission?.baseline?.seq, "基线依据必须保留").toBe(4);
    expect(store.submitRefreshState, "必须单独记录「页面刷新失败」").toBe("error");
    expect(store.submitRefreshError ?? "").toContain("网络中断");

    // 重试只触发读取：POST 次数不变
    const postsBefore = vi.mocked(api.submitBoard).mock.calls.length;
    vi.mocked(api.fetchBoardState).mockResolvedValue(payload(4, [card("c1", "正文")]) as never);
    const retried = await store.retrySubmitRefresh();
    expect(retried.ok).toBe(true);
    expect(store.submitRefreshState).toBe("idle");
    expect(store.submitRefreshError).toBeNull();
    expect(
      vi.mocked(api.submitBoard).mock.calls.length,
      "重试刷新绝不允许重复提交（F10）",
    ).toBe(postsBefore);
  });

  it("服务端明确拒绝（4xx）：仍然是提交失败，且不进入刷新失败状态", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.submitBoard).mockRejectedValue(
      Object.assign(new Error("409"), {
        status: 409,
        payload: { error: "stale_state", reason: "服务端已有更新的已保存版本（seq=9）" },
      }),
    );

    const result = await store.submit();
    expect(result).toBeNull();
    expect(store.submitStatus).toBe("failed");
    expect(store.submitError).toContain("板面版本已经变化");
    expect(store.submitResultUnknown, "明确拒绝不是「结果未知」").toBe(false);
    expect(store.submitRefreshState, "提交失败不属于「刷新失败」").toBe("idle");
  });

  it("没有回执（超时 / 断线）：单独表达「提交结果未知」，既不假定成功也不假定失败", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.submitBoard).mockRejectedValue(new Error("Failed to fetch"));

    const result = await store.submit();
    expect(result).toBeNull();
    expect(store.submitStatus, "没有回执不能当成明确失败").toBe("unknown");
    expect(store.submitResultUnknown).toBe(true);
    expect(store.submitError ?? "").toContain("提交结果未知");
    expect(store.lastSubmission, "没有成功事实可保留").toBeNull();
    // 不会自作主张去刷新，也不会有「已提交」的假象
    expect(store.submitRefreshState).toBe("idle");
  });
});
