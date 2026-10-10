/**
 * 【独立验收 D · closure-d】反例 R5 / R6（第①层：状态与单元）。
 *
 * R5：服务端兜底的影响确认必须能拿到有效 checkId；确认后不许再以同一条说明被拒。
 * R6：服务端说明里列出的任务不许被客户端漏掉（漏掉 = 用户看不到谁会被暂停）。
 *
 * 断言的都是正确行为：基线（冻结时）必须失败；两个用例的失败原因不同、都不许跳过断言。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import type { BoardState } from "../../interactive/types";

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
}));

function card(id: string, content: string): BoardState["cards"][number] {
  return {
    id, kind: "text", content, meta: {}, x: 0, y: 0, w: 1, h: 1,
    checked: false, hidden: false, folded: false, bookmarked: false, deleted: false,
    createdAt: "", updatedAt: "",
  };
}

function state(seq: number, cards: BoardState["cards"]): BoardState {
  return { boardId: "board_default", seq, updatedAt: "2026-10-10T00:00:00Z", cards, groups: [], links: [], selection: [] };
}

function payload(seq: number, cards: BoardState["cards"]): never {
  return {
    board: { id: "board_default", title: "默认板面" },
    state: state(seq, cards),
    seq, baseline: null, submissions: [], drafts: { drafts: {}, updatedAt: null },
  } as never;
}

function intent(id: string, status = "running"): never {
  return {
    id, boardId: "board_default", submissionId: null, title: "任务-" + id, summary: "", status,
    preview: { kind: "task" }, impact: { materials: ["c1"], consequence: "暂停并保留进度" },
    dependsOn: [], conflictsWith: [], conflictKey: "", materialRefs: ["c1"],
    progress: { done: 0, total: 1, text: "" }, reason: "", demo: true, createdAt: "", updatedAt: "",
  } as never;
}

/** 真实 409 形状（与 services/interactive.ts 解出的 payload 一致）。 */
function gateError(affectedTasks: unknown[]): Error {
  return Object.assign(new Error("409"), {
    status: 409,
    // 与 services/interactive.ts 解出的 payload 同形状（FastAPI 的 { detail: {...} }）
    payload: { error: "impact_confirmation_required", reason: "这次保存会改动正在执行任务依赖的材料", affectedTasks },
  });
}

const affectedA = [{ intentId: "A", title: "任务-A", materials: ["c1"], consequence: "暂停" }];
const affectedAB = [
  { intentId: "A", title: "任务-A", materials: ["c1"], consequence: "暂停" },
  { intentId: "B", title: "任务-B", materials: ["c1"], consequence: "暂停" },
];

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue(payload(3, [card("c1", "服务器原文")]));
  vi.mocked(api.saveBoardState).mockResolvedValue({
    ok: true, seq: 4, savedAt: "t", state: state(4, [card("c1", "服务器原文")]),
  } as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 },
  } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.submitBoard).mockResolvedValue({ status: "empty" } as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("R5 服务端兜底的影响确认必须拿到 checkId，确认后不许重复同一条说明", () => {
  it("409 兜底 → 重新预判拿 checkId → 确认后保存成功、说明消失", async () => {
    // 前端不知道有执行中任务（intents 为空）→ 保存前不发预判，由服务端门兜住
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] },
    } as never);
    const store = useInteractiveStore();
    await store.load();

    vi.mocked(api.saveBoardState).mockRejectedValueOnce(gateError(affectedA));
    // 重新预判能拿到有效 checkId（服务端 M4 协议）
    vi.mocked(api.checkMaterialImpact).mockResolvedValue({
      ok: true, checkId: "chk_fresh", stateVersion: 3, affected: affectedA, impactConfirmationRequired: true,
    } as never);

    store.commit(state(3, [card("c1", "改动后的正文")]), "改正文");
    await store.saveNow();
    await flushPromises();
    expect(store.pendingImpact).not.toBeNull();

    await store.confirmImpact();
    await flushPromises();

    // 正确行为 A：确认时必须把重新预判拿到的 checkId 交给服务端（用户确认才有真实依据）
    const calls = vi.mocked(api.saveBoardState).mock.calls;
    const lastConfirm = calls[calls.length - 1]?.[3] as { checkId?: string } | undefined;
    expect(lastConfirm?.checkId).toBe("chk_fresh");
    // 正确行为 B：确认后这次保存真的成功，说明不再出现
    expect(store.pendingImpact).toBeNull();
    expect(store.saveStatus).toBe("saved");
  });
});

describe("R6 说明里列出的任务不许被客户端漏掉", () => {
  it("服务端说明含客户端信息不全的 B：待确认说明里必须列出 B", async () => {
    // 客户端只知道 A（会话里 B 的信息还没加载出来）；服务端如实列出 A 与 B
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [intent("A")], conflicts: [], batchAvailable: false, recovery: { paused: [] },
    } as never);
    const store = useInteractiveStore();
    await store.load();
    expect(store.runningIntents.map((i) => i.id)).toEqual(["A"]);

    vi.mocked(api.checkMaterialImpact).mockResolvedValue({
      ok: true, checkId: "chk_ab", stateVersion: 3, affected: affectedAB, impactConfirmationRequired: true,
    } as never);

    store.commit(state(3, [card("c1", "改动后的正文")]), "改正文");
    await store.saveNow();
    await flushPromises();
    expect(store.pendingImpact).not.toBeNull();

    // 正确行为：说明里列出的 B 必须在待确认列表里；漏掉 B = 用户看不到 B 会被暂停
    await store.refreshBoardFromServer();
    await flushPromises();
    const ids = (store.pendingImpact?.affected ?? []).map((a) => a.intentId);
    expect(ids).toEqual(["A", "B"]);
  });
});
