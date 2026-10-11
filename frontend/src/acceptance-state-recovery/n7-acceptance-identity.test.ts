/**
 * 契约 §1.3：未决提交的稳定请求身份 + 按身份的**精确查证**（子智能体 A）。
 *
 * 反例（整合基线的实际行为，已实测失败）：
 * - 基线把「按 createdAt 接近 + 排除上一次 submissionId」的理解记录当成受理证据；
 *   这条认领逻辑在冲突重组后既没被调用、又留着过时的测试期望。
 * 本文件锁死新契约：
 * - 身份在**发出提交之前**生成并落本机台账；同一次未决提交重启后仍是同一身份；
 * - 查证唯一路径是 fetchAcceptance 的精确匹配：命中才认受理（恢复 submissionId / understandingId
 *   并开始理解查询），查不到 / 查询失败继续「结果未知」，保留原内容与勾选，不盲目重发；
 * - 同身份但内容不同（409 request_identity_conflict）按冲突拒绝：明确失败、不认领受理、换新身份。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../stores/interactive";
import * as api from "../services/interactive";
import { cardDraftKey } from "../interactive/drafts";
import type { AcceptanceLookup, BoardState, SubmissionResult } from "../interactive/types";
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
  fetchAcceptance: vi.fn(),
  fetchUnderstanding: vi.fn(),
  fetchExecutions: vi.fn(),
}));

const DRAFT_KEY = cardDraftKey("c1");

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

/** 服务端的精确查证响应：身份与内容依据都取自客户端这次请求（证明确实带着身份去的）。 */
function acceptanceFor(submissionId = "s9"): (boardId: string, clientRequestId: string) => {
  acceptance: AcceptanceLookup;
} {
  return (_boardId, clientRequestId) => ({
    acceptance: {
      clientRequestId,
      submissionId,
      status: "succeeded",
      delivery: { delivered: true, reason: "", detail: "", understandingId: "u9", status: "accepted" },
      understandingId: "u9",
      understandingStatus: "accepted",
      baseStateVersion: 4,
      note: "",
      createdAt: "t",
      replayed: false,
    } as AcceptanceLookup,
  });
}

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
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understanding: null, understandings: [] } as never);
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("请求身份在发出之前生成并落本机台账（契约 §1.3）", () => {
  it("POST 带着身份发出；身份与内容依据都来自这次提交", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.submitBoard).mockResolvedValue(succeededResult(4) as never);

    await store.submit();

    const call = vi.mocked(api.submitBoard).mock.calls[0];
    const requestId = call[5];
    expect(typeof requestId, "提交请求必须带稳定请求身份").toBe("string");
    expect(String(requestId).startsWith("creq_")).toBe(true);
    expect(call[3], "身份要绑定这次提交的基准版本").toBe(4);
    expect(
      localStorage.getItem("qio.interactive.pending-submission.board_default"),
      "服务端已明确受理：未决台账收掉",
    ).toBeNull();
  });

  it("结果未知时台账必须保留：同一次未决提交重启后还是同一个身份", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.submitBoard).mockRejectedValue(new Error("网络中断，没有收到回执"));
    vi.mocked(api.fetchAcceptance).mockResolvedValue({ acceptance: null, reason: "没有记录" } as never);

    await store.submit();
    const first = store.pendingSubmission?.clientRequestId;
    expect(store.submitStatus).toBe("unknown");
    expect(first).toBeTruthy();
    const stored = localStorage.getItem("qio.interactive.pending-submission.board_default") ?? "";
    expect(stored, "台账在发出提交之前就写下了").toContain(String(first));

    // 「重启」：新的 pinia + 新的 store，只有本机台账还在
    setActivePinia(createPinia());
    const restarted = useInteractiveStore();
    vi.mocked(api.fetchAcceptance).mockImplementation(acceptanceFor() as never);
    await restarted.load();

    expect(restarted.submitStatus, "查证命中：恢复为已受理").toBe("succeeded");
    expect(restarted.lastAcceptance?.submissionId).toBe("s9");
    expect(restarted.acceptedUnderstandingId, "恢复受理回执给出的理解请求身份").toBe("u9");
    const lookupCalls = vi.mocked(api.fetchAcceptance).mock.calls;
    const lookupCall = lookupCalls[lookupCalls.length - 1];
    expect(lookupCall?.[1], "重启后查证用的仍是同一个身份").toBe(first);
  });
});

describe("查证只认精确匹配（契约 §1.1.4）", () => {
  it("命中：恢复 submissionId / understandingId，并按本次身份开始理解查询", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.submitBoard).mockRejectedValue(new Error("网络中断，没有收到回执"));
    vi.mocked(api.fetchAcceptance).mockImplementation(acceptanceFor("s9") as never);

    const result = await store.submit();
    await flushPromises();

    expect(result, "本次没有回执对象可返回").toBeNull();
    expect(store.submitStatus, "精确命中 = 已受理").toBe("succeeded");
    expect(store.submitResultUnknown).toBe(false);
    expect(store.submitError).toBeNull();
    expect(store.submitNote ?? "").toContain("同一个请求身份");
    expect(store.submitAcceptedByLookup, "界面必须知道这份成功来自查证（不许借用上次的改动条数）").toBe(true);
    expect(store.lastAcceptance?.submissionId).toBe("s9");
    expect(
      vi.mocked(api.submitBoard).mock.calls.length,
      "查证绝不允许自动重发提交",
    ).toBe(1);
    expect(vi.mocked(api.fetchAcceptance).mock.calls[0][1]).toBe(
      vi.mocked(api.submitBoard).mock.calls[0][5],
    );
    // 理解查询按**本次**提交身份发起（不是上一次的 submissionId）：
    // 轮询由定时器触发，这里直接刷新一次，把绑定关系确定性地量出来
    expect(store.understandingPolling, "命中受理后必须开始查询本次理解").toBe(true);
    await store.refreshUnderstanding();
    const understandingCalls = vi.mocked(api.fetchUnderstanding).mock.calls;
    const lookup = understandingCalls[understandingCalls.length - 1];
    expect(lookup?.[1], "查询必须按本次受理的 submissionId").toBe("s9");
  });

  it("查不到（404）：继续「结果未知」，保留原内容与勾选，不盲目重发、不改判失败", async () => {
    const store = useInteractiveStore();
    await store.load();
    store.setDraft(DRAFT_KEY, "还没提交的文字");
    vi.mocked(api.submitBoard).mockRejectedValue(new Error("网络中断，没有收到回执"));
    vi.mocked(api.fetchAcceptance).mockResolvedValue({
      acceptance: null,
      reason: "服务端没有这个请求身份的记录",
    } as never);

    const before = {
      draft: store.draftFor(DRAFT_KEY),
      checked: store.board?.cards.find((item) => item.id === "c1")?.checked,
    };
    await store.submit();

    expect(store.submitStatus, "查不到不是失败结论").toBe("unknown");
    expect(store.submitResultUnknown).toBe(true);
    expect(store.submitNote, "没有命中就不许有「已受理」的说明").toBeNull();
    expect(store.acceptanceLookupNote ?? "", "要说清查到了什么").toContain("结果仍然未知");
    expect(store.draftFor(DRAFT_KEY), "原内容必须保留").toBe(before.draft);
    expect(store.board?.cards.find((item) => item.id === "c1")?.checked, "勾选必须保留").toBe(
      before.checked,
    );
    expect(vi.mocked(api.submitBoard).mock.calls.length, "绝不自动重复提交").toBe(1);
    expect(store.pendingSubmission?.clientRequestId, "台账保留：供同身份重试").toBeTruthy();
  });

  it("查询失败（断网 / 5xx）：同样继续「结果未知」，不许当成失败结论", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.submitBoard).mockRejectedValue(new Error("网络中断，没有收到回执"));
    vi.mocked(api.fetchAcceptance).mockRejectedValue(new Error("查证也断网了"));

    await store.submit();

    expect(store.submitStatus).toBe("unknown");
    expect(store.submitError ?? "").toContain("提交结果未知");
    expect(store.acceptanceLookupNote ?? "").toContain("查证没有完成");
    expect(store.acceptanceLookupNote ?? "").toContain("结果仍然未知");
  });

  it("同身份重试：内容没变就沿用同一身份，服务端回放原事实", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.submitBoard).mockRejectedValue(new Error("网络中断，没有收到回执"));
    vi.mocked(api.fetchAcceptance).mockResolvedValue({ acceptance: null, reason: "没有记录" } as never);
    await store.submit();
    const first = vi.mocked(api.submitBoard).mock.calls[0][5];

    // 用户直接再点一次（内容没变）：必须沿用同一个身份，服务端据此返回原事实
    vi.mocked(api.submitBoard).mockResolvedValue({
      ...succeededResult(4),
      replayed: true,
    } as never);
    await store.submit();

    const second = vi.mocked(api.submitBoard).mock.calls[1][5];
    expect(second, "同一次未决提交的重试必须复用同一身份").toBe(first);
    expect(store.submitStatus).toBe("succeeded");
    expect(store.lastSubmission?.replayed, "服务端说是原记录就是原记录").toBe(true);
    expect(store.submitAcceptedByLookup, "这次成功来自回执，不是查证").toBe(false);
  });

  it("内容已变（基准版本前进）：那是另一次提交，必须换新身份", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.submitBoard).mockRejectedValue(new Error("网络中断，没有收到回执"));
    vi.mocked(api.fetchAcceptance).mockResolvedValue({ acceptance: null, reason: "没有记录" } as never);
    await store.submit();
    const first = vi.mocked(api.submitBoard).mock.calls[0][5];

    // 用户改了板面并保存成功：基准版本前进，这不是同一次提交
    vi.mocked(api.saveBoardState).mockImplementation(async (_boardId, putState) => {
      const put = putState as never as Record<string, unknown>;
      return { ok: true, seq: 5, savedAt: "t5", state: { ...put, seq: 5 } } as never;
    });
    store.commit(
      { ...store.board!, cards: [card("c1", "改过的正文", { checked: true })] } as BoardState,
      "改正文",
    );
    await store.saveNow();
    vi.mocked(api.submitBoard).mockResolvedValue(succeededResult(5) as never);
    await store.submit();

    const second = vi.mocked(api.submitBoard).mock.calls[1][5];
    expect(second, "内容不同就不是同一次提交：必须换身份").not.toBe(first);
  });
});

describe("同身份但内容不同：服务端冲突（契约 §1.1.3）", () => {
  it("409 request_identity_conflict：明确失败、不认领受理、下一次提交换新身份", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.submitBoard).mockRejectedValue(
      Object.assign(new Error("409"), {
        status: 409,
        payload: { error: "request_identity_conflict", reason: "同一个请求身份的内容不一致" },
      }),
    );

    await store.submit();

    expect(store.submitStatus, "冲突是明确失败，不是未知").toBe("failed");
    expect(store.submitResultUnknown).toBe(false);
    expect(store.submitError ?? "").toContain("同一个请求身份");
    expect(store.submitError ?? "").toContain("没有创建提交");
    expect(store.submitNote, "冲突绝不允许认领受理").toBeNull();
    expect(store.submitAcceptedByLookup).toBe(false);
    expect(store.pendingSubmission, "被拒绝的身份不能再继续用").toBeNull();
    expect(
      localStorage.getItem("qio.interactive.pending-submission.board_default"),
      "被拒绝的身份也要从台账里收掉",
    ).toBeNull();

    vi.mocked(api.submitBoard).mockResolvedValue(succeededResult(4) as never);
    await store.submit();
    const ids = vi.mocked(api.submitBoard).mock.calls.map((call) => call[5]);
    expect(ids[1]).not.toBe(ids[0]);
  });
});
