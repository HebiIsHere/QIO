/**
 * PM-QIO032 第三轮 · #80 重复提交识别（i-state 数据层）
 *
 * E2E 缺陷：第一次提交落定后，不改内容再点提交 → 又 POST 一次并新增受理，界面没有
 * 「已提交过 / 没有新的改动」。
 *
 * 期望（Lead 任务书）：
 * 1) 已成功受理且当前候选相对「上次成功提交基准」没有有效改动 → **不发 POST**，
 *    状态 duplicate（界面「已提交过」），原受理记录 / 原进度入口保留；
 * 2) 有真实改动 → 正常提交；
 * 3) 上次失败 / 未知 → 仍允许重试；
 * 4) replayed=true 回执 → 按原记录处理，不新建。
 * 判据必须是精确事实（回执 after 快照 / 本机记住的受理快照 / 服务端按当前基准算的 pending），
 * 不能用时间或近似判断。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import { addCard, emptyState } from "../../interactive/board";
import { localVisibleRange } from "../../interactive/submission";
import type { BoardState, Snapshot, SubmissionResult } from "../../interactive/types";

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(),
    getSessionContext: vi.fn(async () => ({ topic_id: null })),
    stopTurn: vi.fn(),
    saveDrafts: vi.fn(async () => ({})),
  },
}));

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchAcceptance: vi.fn(),
  fetchUnderstanding: vi.fn(),
  retryUnderstanding: vi.fn(),
  fetchExecutions: vi.fn(),
  fetchIntentViews: vi.fn(),
  createIntents: vi.fn(),
  fetchIntents: vi.fn(),
}));

const NOW = new Date().toISOString();
const BOARD_ID = "board_default";

/** 一块有一张已勾选、有正文的文字卡的板面（可见、可提交；id 固定以便跨会话精确比较）。 */
function contentBoard(): BoardState {
  const state = addCard(emptyState(BOARD_ID), {
    kind: "text",
    content: "要提交的内容",
    checked: true,
  }) as BoardState;
  state.cards[0].id = "card_main";
  return state;
}

function boardState(cards: BoardState["cards"], seq = 3): BoardState {
  return {
    boardId: BOARD_ID,
    seq,
    updatedAt: NOW,
    cards,
    groups: [],
    links: [],
    selection: [],
  };
}

function boardPayload(
  state: BoardState,
  extra: {
    seq?: number;
    submissions?: { id: string; seq: number; status: string; createdAt: string }[];
    baseline?: { seq: number; submittedAt: string } | null;
    pending?: { baselineSeq: number | null; expressions: never[]; updatedAt: string | null };
  } = {},
) {
  const seq = extra.seq ?? state.seq ?? 3;
  return {
    board: { id: BOARD_ID, title: "默认板面" },
    state: { ...state, seq },
    seq,
    baseline: extra.baseline ?? null,
    submissions: extra.submissions ?? [],
    drafts: { drafts: {}, updatedAt: null },
    pending: extra.pending ?? { baselineSeq: null, expressions: [], updatedAt: null },
  };
}

function emptySnapshot(): Snapshot {
  return { cards: [], groups: [], links: [], selection: [], empty: true };
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
    visibleRange: { ...emptySnapshot() },
    checkedCleared: [],
    ...over,
  } as SubmissionResult;
}

function wireHappyPath(state: BoardState = contentBoard(), extra: Parameters<typeof boardPayload>[1] = {}) {
  vi.mocked(api.fetchBoardState).mockResolvedValue(boardPayload(state, extra) as never);
  vi.mocked(api.saveBoardState).mockImplementation((async (_id: string, candidate: BoardState) => ({
    ok: true,
    seq: 9,
    savedAt: NOW,
    state: { ...candidate, seq: 9 },
    pending: { baselineSeq: null, expressions: [], updatedAt: NOW },
  })) as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: NOW } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: emptySnapshot() } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } } as never);
  vi.mocked(api.fetchIntentViews).mockResolvedValue({ intents: [], source: "service" } as never);
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understandings: [] } as never);
}

/** 提交回执的 after 快照 = 当前板面投影到可见范围（模拟服务端按本次载荷返回）。 */
function currentSnapshot(store: ReturnType<typeof useInteractiveStore>): Snapshot {
  return localVisibleRange(store.board) as Snapshot;
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  setActivePinia(createPinia());
  wireHappyPath();
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe("#80 未改动重复提交：不 POST、已提交过、保留原受理记录", () => {
  it("第一次成功受理后不改内容再提交：submitBoard 只调用一次，状态 duplicate，原记录仍在", async () => {
    const store = useInteractiveStore();
    vi.mocked(api.submitBoard).mockImplementation((async () => submitResult({ after: currentSnapshot(store) })) as never);
    await store.load();
    await store.submit();
    expect(api.submitBoard).toHaveBeenCalledTimes(1);
    expect(store.lastSubmission?.submission.id).toBe("s1");
    const accepted = store.lastSubmission;

    // 不改内容，再点提交
    const second = await store.submit();
    expect(second).toBeNull();
    expect(api.submitBoard, "未改动重复提交不许再发 POST").toHaveBeenCalledTimes(1);
    expect(store.submitStatus).toBe("duplicate");
    expect(store.submitNote ?? "").toContain("已提交过");
    expect(store.roundStatusLabel).toBe("已提交过");
    // 原受理记录 / 原进度入口保留（不被没有新工作的回执覆盖）
    expect(store.lastSubmission).toBe(accepted);
    expect(store.latestAcceptedSubmission?.id).toBe("s1");
    expect(store.hasAcceptedSubmission).toBe(true);
  });

  it("有真实改动时正常提交（不会被误判重复）", async () => {
    const store = useInteractiveStore();
    let call = 0;
    vi.mocked(api.submitBoard).mockImplementation((async () => {
      call += 1;
      return submitResult({
        submission: { id: call === 1 ? "s1" : "s2", seq: call === 1 ? 4 : 5, status: "succeeded", createdAt: NOW },
        after: currentSnapshot(store),
      });
    }) as never);
    await store.load();
    await store.submit();
    expect(api.submitBoard).toHaveBeenCalledTimes(1);

    // 真实改动：新增一张有正文的卡
    store.commit(
      addCard(store.board!, { kind: "text", content: "第二段真实内容", checked: true }) as BoardState,
      "添加文字卡",
    );
    await store.submit();
    expect(api.submitBoard, "有真实改动时必须正常提交").toHaveBeenCalledTimes(2);
    expect(store.submitStatus).toBe("succeeded");
    expect(store.latestAcceptedSubmission?.id).toBe("s2");
  });

  it("上次明确失败后仍允许重试（不判重复）", async () => {
    const store = useInteractiveStore();
    const failure = Object.assign(new Error("服务端拒绝这次请求"), { status: 400 });
    vi.mocked(api.submitBoard).mockRejectedValueOnce(failure as never);
    await store.load();
    await store.submit();
    expect(store.submitStatus).toBe("failed");

    vi.mocked(api.submitBoard).mockImplementation((async () => submitResult({ after: currentSnapshot(store) })) as never);
    await store.submit();
    expect(api.submitBoard, "失败后重试必须真的再发一次提交").toHaveBeenCalledTimes(2);
    expect(store.submitStatus).toBe("succeeded");
  });

  it("replayed=true 回执按原记录处理：不显示成新的「已受理」，状态仍成功", async () => {
    const store = useInteractiveStore();
    vi.mocked(api.submitBoard).mockResolvedValue(
      submitResult({
        replayed: true,
        submission: { id: "s0", seq: 2, status: "succeeded", createdAt: NOW },
        delivery: { delivered: true, reason: "", detail: "", understandingId: "u0", status: "accepted" },
        after: currentSnapshot(store),
      }) as never,
    );
    await store.load();
    await store.submit();
    expect(store.submitStatus).toBe("succeeded");
    expect(store.lastSubmission?.submission.id).toBe("s0");
    expect(store.submitNote ?? "").toContain("原受理记录");
    expect(store.submitNote ?? "").not.toContain("已受理：QIO");
  });
});

describe("#80 刷新后的重复识别（精确基准事实）", () => {
  it("刷新后没有 lastSubmission：用本机记住的受理快照判重复，不发 POST", async () => {
    // 第一次会话：提交成功 → 本机记住受理快照
    const first = useInteractiveStore();
    vi.mocked(api.submitBoard).mockImplementation((async () => submitResult({ after: currentSnapshot(first) })) as never);
    await first.load();
    await first.submit();
    expect(api.submitBoard).toHaveBeenCalledTimes(1);

    // 模拟刷新：新 pinia + 服务器最新成功提交是 s1
    setActivePinia(createPinia());
    vi.mocked(api.submitBoard).mockClear();
    wireHappyPath(contentBoard(), {
      submissions: [{ id: "s1", seq: 4, status: "succeeded", createdAt: NOW }],
      baseline: { seq: 4, submittedAt: NOW },
    });
    const second = useInteractiveStore();
    await second.load();
    await second.submit();
    expect(api.submitBoard, "刷新后也知道没有新改动，不许再 POST").toHaveBeenCalledTimes(0);
    expect(second.submitStatus).toBe("duplicate");
    expect(second.latestAcceptedSubmission?.id).toBe("s1");
  });

  it("服务端按当前基准算出的 pending 为空时判重复（baselineSeq 与基准一致才可信）", async () => {
    wireHappyPath(contentBoard(), {
      submissions: [{ id: "s1", seq: 4, status: "succeeded", createdAt: NOW }],
      baseline: { seq: 3, submittedAt: NOW },
      pending: { baselineSeq: 3, expressions: [], updatedAt: NOW },
    });
    const store = useInteractiveStore();
    await store.load();
    await store.submit();
    expect(api.submitBoard).toHaveBeenCalledTimes(0);
    expect(store.submitStatus).toBe("duplicate");
  });

  it("pending 与当前基准对不上（过期基准）时不据此判重复：正常提交交给服务端权威", async () => {
    wireHappyPath(contentBoard(), {
      submissions: [{ id: "s1", seq: 4, status: "succeeded", createdAt: NOW }],
      baseline: { seq: 5, submittedAt: NOW },
      pending: { baselineSeq: 4, expressions: [], updatedAt: NOW },
    });
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.submitBoard).mockImplementation((async () => submitResult({ after: currentSnapshot(store) })) as never);
    await store.submit();
    expect(store.submitStatus).not.toBe("duplicate");
    expect(api.submitBoard).toHaveBeenCalledTimes(1);
  });
});
