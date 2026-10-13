/**
 * IRL-D · 提交 / 理解状态的身份归属（任务书 §5.2 / §5.6 / §5.7）。
 *
 * 三条事实必须成立：
 * - **阶段归属**：说明只属于写下它的那个阶段；理解有结论之后，受理时的「正在理解」只是历史；
 * - **迟到回执不覆盖**：按请求身份的精确查证返回时，如果已经换过一代提交（或换了板面），
 *   这次回执不落地 —— 更新的状态绝不被它改判；
 * - **板面身份**：板面一换，上一个板面的提交 / 理解事实整块归零；同一个板面重复读取不受影响。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useInteractiveStore } from "../interactive";
import * as board from "../../interactive/board";
import { submissionIdentityFor } from "../../interactive/submission";
import type { BoardStateResponse, Understanding } from "../../interactive/types";

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(async () => ({
    board: { id: "board_default", title: "互动板面" },
    state: { boardId: "board_default", seq: 1, updatedAt: "t", cards: [], groups: [], links: [], selection: [] },
    seq: 1,
    baseline: null,
    submissions: [],
    drafts: { drafts: {}, updatedAt: null },
    pending: [],
  })),
  fetchVisibleRange: vi.fn(async () => ({ visibleRange: null })),
  fetchIntents: vi.fn(async () => ({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } })),
  fetchUnderstanding: vi.fn(async () => ({ understanding: null, understandings: [] })),
  fetchExecutions: vi.fn(async () => ({ executions: [] })),
  fetchAcceptance: vi.fn(),
  createIntents: vi.fn(async () => ({ intents: [] })),
  saveBoardState: vi.fn(async () => ({ ok: true, seq: 1, savedAt: "t" })),
  submitBoard: vi.fn(),
}));

const NOW = "2026-10-13T00:00:00.000Z";
const REQUEST_ID = "creq_test_00000001";

function identity(boardId = "board_default") {
  return submissionIdentityFor({
    boardId,
    baseStateVersion: 1,
    note: "",
    makeId: () => REQUEST_ID,
  });
}

function acceptancePayload() {
  return {
    clientRequestId: REQUEST_ID,
    submissionId: "s_1",
    status: "succeeded",
    delivery: {
      delivered: true,
      reason: "",
      detail: "",
      understandingId: "u_1",
      status: "accepted",
    },
    understandingId: "u_1",
    understandingStatus: "accepted",
    baseStateVersion: 1,
    note: "",
  };
}

function understandingFixture(over: Partial<Understanding> = {}): Understanding {
  return {
    id: "u_late",
    boardId: "board_default",
    submissionId: "s_old",
    requestId: "req-1",
    status: "understood",
    stage: "",
    kind: "intent",
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

function boardPayload(id: string, seq = 1): BoardStateResponse {
  return {
    board: { id, title: "互动板面" },
    state: { boardId: id, seq, updatedAt: NOW, cards: [], groups: [], links: [], selection: [] },
    seq,
    baseline: null,
    submissions: [],
    drafts: { drafts: {}, updatedAt: null },
    pending: [],
  } as unknown as BoardStateResponse;
}

beforeEach(async () => {
  setActivePinia(createPinia());
  localStorage.clear();
  const api = await import("../../services/interactive");
  vi.mocked(api.fetchBoardState).mockReset();
  vi.mocked(api.fetchBoardState).mockImplementation(async () => boardPayload("board_default"));
  vi.mocked(api.fetchUnderstanding).mockReset();
  vi.mocked(api.fetchUnderstanding).mockImplementation(async () => ({ understanding: null, understandings: [] }));
  vi.mocked(api.fetchAcceptance).mockReset();
});

describe("阶段说明的归属（§5.2 / §5.3）", () => {
  it("理解还没结束时，受理 / 查询说明都是当前事实", () => {
    const store = useInteractiveStore();
    store.understandingStatus = "accepted";
    expect(store.submitNoteOwnsCurrentStage).toBe(true);
    expect(store.understandingNoteOwnsCurrentStage).toBe(true);
    store.understandingStatus = "understanding";
    expect(store.submitNoteOwnsCurrentStage).toBe(true);
  });

  it("理解一旦有结论，旧阶段说明就只是历史", () => {
    const store = useInteractiveStore();
    for (const status of ["understood", "needs_clarification", "no_action", "failed"] as const) {
      store.understandingStatus = status;
      expect(store.submitNoteOwnsCurrentStage, status).toBe(false);
      expect(store.understandingNoteOwnsCurrentStage, status).toBe(false);
    }
    // 还没有提交过（状态未知）时，说明仍然是当前事实
    store.understandingStatus = null;
    expect(store.submitNoteOwnsCurrentStage).toBe(true);
  });
});

describe("迟到的回执不覆盖更新的状态（§5.6）", () => {
  it("查证返回时已经开始新的提交：这次回执不落地", async () => {
    const api = await import("../../services/interactive");
    const store = useInteractiveStore();
    store.board = board.emptyState("board_default");
    store.submitStatus = "unknown";
    store.submitError = "提交结果未知：没有拿到服务端回执";
    store.pendingSubmission = identity();

    let release: (value: unknown) => void = () => {};
    vi.mocked(api.fetchAcceptance).mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve as (value: unknown) => void;
        }) as never,
    );

    const inflight = store.confirmAcceptanceAfterUnclear();
    // 用户点了「重新提交」：新的提交已经开始（界面真实路径就是先进入 submitting）
    store.submitStatus = "submitting";
    store.submitNote = null;
    release({ acceptance: acceptancePayload() });
    const result = await inflight;

    expect(result, "迟到的查证不许返回受理事实").toBeNull();
    expect(store.submitStatus, "不许把新提交改判成已受理").toBe("submitting");
    expect(store.submitNote).toBeNull();
    expect(store.understandingStatus).toBeNull();
    expect(store.submitAcceptedByLookup).toBe(false);
  });

  it("没有新提交时，同一份回执照常落地（守卫只挡迟到的那一次）", async () => {
    const api = await import("../../services/interactive");
    const store = useInteractiveStore();
    store.board = board.emptyState("board_default");
    store.submitStatus = "unknown";
    store.pendingSubmission = identity();
    vi.mocked(api.fetchAcceptance).mockResolvedValue({ acceptance: acceptancePayload() } as never);

    const result = await store.confirmAcceptanceAfterUnclear();

    expect(result?.submissionId).toBe("s_1");
    expect(store.submitStatus).toBe("succeeded");
    expect(store.understandingStatus).toBe("accepted");
    expect(store.submitNote ?? "").toContain("查证");
  });

  it("板面已经切走：这次精确查证不落地", async () => {
    const api = await import("../../services/interactive");
    const store = useInteractiveStore();
    store.board = board.emptyState("board_default");
    store.submitStatus = "unknown";
    store.pendingSubmission = identity();
    let release: (value: unknown) => void = () => {};
    vi.mocked(api.fetchAcceptance).mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve as (value: unknown) => void;
        }) as never,
    );
    const inflight = store.confirmAcceptanceAfterUnclear();
    store.boardId = "board_other";
    release({ acceptance: acceptancePayload() });
    expect(await inflight).toBeNull();
    expect(store.submitStatus).toBe("unknown");
  });

  it("切换板面期间返回的理解查询结果不落地（epoch 已经前进）", async () => {
    const api = await import("../../services/interactive");
    const store = useInteractiveStore();
    store.board = board.emptyState("board_default");

    let release: (value: unknown) => void = () => {};
    let calls = 0;
    vi.mocked(api.fetchUnderstanding).mockImplementation(async () => {
      calls += 1;
      if (calls === 1) {
        return new Promise((resolve, reject) => {
          release = resolve as (value: unknown) => void;
          void reject;
        }) as never;
      }
      return { understanding: null, understandings: [] };
    });

    const inflight = store.loadUnderstanding("s_old");
    vi.mocked(api.fetchBoardState).mockImplementation(async () => boardPayload("board_other"));
    store.boardId = "board_other";
    await store.load();
    release({
      understanding: understandingFixture(),
      understandings: [understandingFixture()],
    });

    expect(await inflight, "迟到的理解查询返回 null").toBeNull();
    expect(store.understandingStatus).toBeNull();
    expect(store.understanding).toBeNull();
  });
});

describe("状态按板面身份归属（§5.7）", () => {
  it("切换板面：上一个板面的提交 / 理解事实整块归零", async () => {
    const api = await import("../../services/interactive");
    const store = useInteractiveStore();
    store.board = board.emptyState("board_default");
    store.submitStatus = "succeeded";
    store.understandingStatus = "understood";
    store.understanding = understandingFixture({ id: "u_old", submissionId: "s_old" });
    store.understandings = [store.understanding];
    store.submitNote = "已受理：QIO 已经可靠收下这次提交，正在理解。";
    store.understandingNote = "页面不在前台：已暂停自动查询，回到页面会继续。";
    store.lastSubmission = { status: "succeeded" } as never;

    store.boardId = "board_other";
    vi.mocked(api.fetchBoardState).mockImplementation(async () => boardPayload("board_other", 4));
    await store.load();

    expect(store.submitStatus).toBe("idle");
    expect(store.understandingStatus).toBeNull();
    expect(store.understanding).toBeNull();
    expect(store.understandings).toEqual([]);
    expect(store.submitNote).toBeNull();
    expect(store.understandingNote).toBeNull();
    expect(store.lastSubmission).toBeNull();
    expect(store.submitAcceptedByLookup).toBe(false);
  });

  it("同一个板面重复读取：本次提交事实保留（不是每次刷新都清）", async () => {
    const store = useInteractiveStore();
    store.board = board.emptyState("board_default");
    store.submitStatus = "succeeded";
    store.submitNote = "已受理：QIO 已经可靠收下这次提交，正在理解。";
    store.lastSubmission = { status: "succeeded" } as never;

    await store.load();
    await store.load();

    expect(store.submitStatus).toBe("succeeded");
    expect(store.submitNote ?? "").toContain("已受理");
    expect(store.lastSubmission).not.toBeNull();
  });
});
