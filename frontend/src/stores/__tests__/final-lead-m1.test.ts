/**
 * Lead 收尾轮 M1 反例（docs/interactive-final-closure-contract.md M1）：
 *
 * 01 迟到读取回退已保存的新卡片草稿：
 *   PUT 与 GET 的相对顺序乱掉时，已保存成功的新正文不许被旧 GET 回退。
 * 06 反例 A：保存回执在飞期间的新候选不被旧回执覆盖，dirty 不被清掉。
 * 06 反例 B：审批收尾保存失败后回读服务器，本地候选保留。
 * 08 路径1：影响预判失败不落库，真实原因可见，候选保留。
 * 08 路径3：等待影响确认时不发出提交；提交携带本次候选版本。
 *
 * 标注：【会话层】只依赖 stores/interactive.ts 的公开状态与动作；
 *      【模拟失败/模拟乱序】用受控的 services/interactive mock 制造挂起与乱序回执。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import type { BoardState } from "../../interactive/types";
import { writeCardLocalDraft } from "../../interactive/drafts";

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
    id,
    kind: "text",
    content,
    meta: {},
    x: 0,
    y: 0,
    w: 1,
    h: 1,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "",
    updatedAt: "",
  };
}

function boardPayload(
  seq = 3,
  drafts: Record<string, string> = {},
  cards: BoardState["cards"] = [],
): Awaited<ReturnType<typeof api.fetchBoardState>> {
  return {
    board: { id: "board_default", title: "默认板面" },
    state: {
      boardId: "board_default",
      seq,
      updatedAt: "2026-10-09T10:00:00Z",
      cards,
      groups: [],
      links: [],
      selection: [],
    },
    seq,
    baseline: null,
    submissions: [],
    drafts: { drafts, updatedAt: drafts ? "2026-10-09T10:00:00Z" : null },
  } as Awaited<ReturnType<typeof api.fetchBoardState>>;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue(boardPayload(3));
  vi.mocked(api.saveBoardState).mockResolvedValue({ ok: true, seq: 4, savedAt: "t", state: boardPayload(4).state } as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 } } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({
    ok: true,
    checkId: "chk_none",
    stateVersion: 3,
    affected: [],
    impactConfirmationRequired: false,
  } as never);
  vi.mocked(api.submitBoard).mockResolvedValue({
    status: "empty", submission: { id: "s1", seq: 4, status: "empty", createdAt: "" }, before: { cards: [], groups: [], links: [], selection: [], empty: true }, after: { cards: [], groups: [], links: [], selection: [], empty: true }, expressions: [], baseline: { updated: false }, delivery: { delivered: false, reason: "", detail: "" }, visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true }, checkedCleared: [],
  } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } } as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("01 迟到读取不许回退已保存的新卡片草稿", () => {
  it("GET 在飞期间 flushDrafts 先成功：旧 GET 返回后编辑草稿仍是新正文", async () => {
    const store = useInteractiveStore();
    await store.load();
    const key = "card:c1";
    store.setDraft(key, "新正文-v2");
    // 让 GET 返回的正文比「GET 结束时刻的已保存版本」旧：GET 在飞时先推进保存事实，
    // 再返回一份「不含这份草稿」的旧服务端正文 —— 相当于旧 GET 晚到。
    vi.mocked(api.fetchBoardState).mockImplementation(async () => {
      // GET 在飞期间：flushDrafts 的服务端保存成功
      await store.flushDrafts();
      return boardPayload(3, {}) as never; // 旧正文：服务器上还没有这份草稿
    });
    await store.refreshBoardFromServer();
    await flushPromises();
    expect(store.drafts[key]).toBe("新正文-v2");
    expect(store.draftStateFor(key).status).toBe("saved");
  });
});

describe("06 正式板面的版本保护", () => {
  it("反例A：保存回执在飞期间新提交的候选不被旧回执覆盖、dirty 不被清", async () => {
    const store = useInteractiveStore();
    await store.load();
    let resolveSave!: (v: unknown) => void;
    vi.mocked(api.saveBoardState).mockImplementation(
      () => new Promise((res) => { resolveSave = res; }) as never,
    );
    store.commit(
      { ...store.board!, seq: 99, cards: [card("c1", "v1")] } as BoardState,
      "第一版",
    );
    const savePromise = store.saveNow();
    await flushPromises();
    // 回执在飞时，用户完成第二版
    store.commit(
      { ...store.board!, seq: 99, cards: [card("c2", "v2")] } as BoardState,
      "第二版",
    );
    resolveSave({ ok: true, seq: 4, savedAt: "t", state: { boardId: "board_default", seq: 4, updatedAt: "t", cards: [card("c1", "v1")], groups: [], links: [], selection: [] } });
    await savePromise;
    await flushPromises();
    expect(store.board!.cards.map((c) => c.id)).toEqual(["c2"]);
    expect(store.dirty).toBe(true);
    // 第二版的保存真的会发出去并收敛
    vi.mocked(api.saveBoardState).mockResolvedValue({ ok: true, seq: 5, savedAt: "t2", state: store.board! } as never);
    await store.saveNow();
    await flushPromises();
    expect(store.dirty).toBe(false);
    expect(store.saveStatus).toBe("saved");
  });

  it("反例B：候选未保存时审批收尾回读服务器，候选保留", async () => {
    const store = useInteractiveStore();
    await store.load();
    store.commit({ ...store.board!, seq: 99, cards: [card("c9", "未保存编辑")] } as BoardState, "编辑");
    vi.mocked(api.saveBoardState).mockRejectedValue(new Error("保存失败"));
    await store.saveNow();
    await flushPromises();
    expect(store.saveStatus).toBe("error");
    expect(store.dirty).toBe(true);
    await store.refreshBoardFromServer();
    await flushPromises();
    expect(store.board!.cards.map((c) => c.content)).toEqual(["未保存编辑"]);
    expect(store.dirty).toBe(true);
  });
});

describe("08/06 服务端门：409 的真实原因与不落库", () => {
  it("PUT 返回 impact_confirmation_required：不落库、候选保留、按服务端列出的任务等待确认", async () => {
    const store = useInteractiveStore();
    await store.load();
    // 没有运行中意图 → 前端不发影响检查，由服务端门兜住
    const gate = Object.assign(new Error("409"), {
      status: 409,
      payload: {
        error: "impact_confirmation_required",
        reason: "这次保存会改动正在执行任务依赖的材料",
        affectedTasks: [{ intentId: "t1", title: "任务一", materials: ["c1"], consequence: "暂停并保留进度" }],
      },
    });
    vi.mocked(api.saveBoardState).mockRejectedValue(gate);
    store.commit({ ...store.board!, seq: 99, cards: [card("c1", "改动")] } as BoardState, "改动");
    await store.saveNow();
    await flushPromises();
    expect(store.pendingImpact).not.toBeNull();
    expect(store.pendingImpact?.affected.map((a) => a.intentId)).toEqual(["t1"]);
    expect(store.dirty).toBe(true);
    expect(store.saveStatus).not.toBe("saved");
  });

  it("服务端 dict detail（FastAPI 形状 {detail:{...}}）也能读到真实原因并按 409 处理", async () => {
    const store = useInteractiveStore();
    await store.load();
    const apiErr = Object.assign(new Error("409"), {
      status: 409,
      payload: { error: "impact_confirmation_required", reason: "会改动执行中任务依赖的材料", affectedTasks: [{ intentId: "t9", title: "任务九", materials: ["c1"], consequence: "暂停" }] },
    });
    vi.mocked(api.saveBoardState).mockRejectedValue(apiErr);
    store.commit({ ...store.board!, seq: 99, cards: [card("c1", "改动")] } as BoardState, "改动");
    await store.saveNow();
    await flushPromises();
    expect(store.pendingImpact?.affected.map((a) => a.intentId)).toEqual(["t9"]);
    expect(store.dirty).toBe(true);
  });

  it("提交返回 stale_state：给出可操作的真实原因，不推进提交状态、不清改动", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.saveBoardState).mockImplementation((async (_boardId: string, state: BoardState) => ({
      ok: true,
      seq: 4,
      savedAt: "t",
      state,
    })) as never);
    store.commit({ ...store.board!, seq: 4, cards: [card("c1", "改动")] } as BoardState, "改动");
    await store.saveNow();
    await flushPromises();
    vi.mocked(api.submitBoard).mockRejectedValue(
      Object.assign(new Error("409"), {
        status: 409,
        payload: { error: "stale_state", reason: "服务端已有更新的已保存版本（seq=9）" },
      }),
    );
    const r = await store.submit();
    expect(r).toBeNull();
    expect(store.submitStatus).toBe("failed");
    expect(store.submitError).toContain("板面版本已经变化");
    expect(store.submitError).toContain("seq=9");
    // 提交失败保留改动与勾选
    expect(store.board?.cards.map((c) => c.id)).toEqual(["c1"]);
  });
});

describe("05 未决冲突：改一个字不清冲突", () => {
  it("改一个字：两份来源保留、服务器不被本机候选覆盖；明确选本机才落地", async () => {
    const store = useInteractiveStore();
    writeCardLocalDraft("c1", "本机候选", { boardId: "board_default", seq: 1 });
    vi.mocked(api.fetchBoardState).mockResolvedValue(
      boardPayload(3, { "card:c1": "服务器版本" }, [card("c1", "")]) as never,
    );
    await store.load();
    expect(store.draftConflictFor("c1")).toEqual({ local: "本机候选", server: "服务器版本" });
    // 用户在冲突 A 上「改一个字」
    store.setDraft("card:c1", "本机候选改了");
    expect(store.draftConflictFor("c1")?.local).toBe("本机候选改了");
    expect(store.draftConflictFor("c1")?.server).toBe("服务器版本");
    // 未选择期间发保存：冲突键按服务器事实回写，本机候选不上去、服务器那份也不被删
    vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: { "card:c1": "服务器版本" }, updatedAt: "t" } as never);
    await store.flushDrafts();
    const calls = vi.mocked(api.saveDrafts).mock.calls;
    const sent = calls.length ? (calls[calls.length - 1][1] as Record<string, string>) : undefined;
    if (sent && "card:c1" in sent) expect(sent["card:c1"]).toBe("服务器版本");
    expect(store.draftConflictFor("c1")).not.toBeNull();
    // 明确选择「用本机的」：按用户选择落地，冲突消失
    store.resolveDraftConflict("c1", "local");
    expect(store.draftConflictFor("c1")).toBeNull();
    expect(store.drafts["card:c1"]).toBe("本机候选改了");
  });
});

describe("08 影响确认约束保存与提交", () => {
  it("路径1：影响预判失败不落库，原因可见，候选保留", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [{ id: "t1", title: "任务", status: "running" } as never],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    } as never);
    await store.loadIntents();
    vi.mocked(api.checkMaterialImpact).mockRejectedValue(new Error("后端预判不可用"));
    store.commit({ ...store.board!, seq: 99, cards: [card("c1", "改动")] } as BoardState, "依赖材料的改动");
    await store.saveNow();
    await flushPromises();
    expect(api.saveBoardState).not.toHaveBeenCalled();
    expect(store.impactCheckError).toContain("影响预判");
    expect(store.dirty).toBe(true);
  });

  it("路径2：等待确认期间又改别处，确认时重新核实，不放行未说明的改动", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [{ id: "t1", title: "任务", status: "running" } as never],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    } as never);
    await store.loadIntents();
    vi.mocked(api.checkMaterialImpact).mockResolvedValue({
      ok: true,
      checkId: "chk_1",
      stateVersion: 3,
      affected: [{ intentId: "t1", title: "任务", materials: ["c1"], consequence: "暂停" }],
      impactConfirmationRequired: true,
    } as never);
    store.commit({ ...store.board!, seq: 99, cards: [card("c1", "A")] } as BoardState, "改动A");
    await store.saveNow();
    await flushPromises();
    expect(store.pendingImpact).not.toBeNull();
    // 等待确认期间用户改了 B
    store.commit({ ...store.board!, seq: 99, cards: [card("c1", "A"), card("c2", "B")] } as BoardState, "改动B");
    await store.confirmImpact();
    await flushPromises();
    // 版本变了：这次确认不落地旧的授权，重新核实（预判仍然报影响 → 等待新的确认）
    expect(api.saveBoardState).not.toHaveBeenCalled();
    expect(store.pendingImpact).not.toBeNull();
    // 重新核实的事实写在**新的确认说明**里（N2：不能再把上一次的预判失败留在 impactCheckError，
    // 否则界面会同时宣称「未保存未暂停」与「已保存已暂停」）
    expect(store.pendingImpact?.note ?? "").toContain("重新核实");
    expect(store.impactCheckError, "新的说明已经给出，还留着旧的预判失败").toBeNull();
  });

  it("路径3：等待影响确认时不发出提交；确认后提交携带本次候选版本", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [{ id: "t1", title: "任务", status: "running" } as never],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    } as never);
    await store.loadIntents();
    vi.mocked(api.checkMaterialImpact).mockResolvedValue({
      ok: true,
      checkId: "chk_2",
      stateVersion: 3,
      affected: [{ intentId: "t1", title: "任务", materials: ["c1"], consequence: "暂停" }],
      impactConfirmationRequired: true,
    } as never);
    store.commit({ ...store.board!, seq: 99, cards: [card("c1", "改动")] } as BoardState, "改动");
    await store.saveNow();
    await flushPromises();
    expect(store.pendingImpact).not.toBeNull();
    const r = await store.submit();
    expect(r).toBeNull();
    expect(api.submitBoard).not.toHaveBeenCalled();
    vi.mocked(api.checkMaterialImpact).mockResolvedValue({
      ok: true,
      checkId: "chk_2",
      stateVersion: 3,
      affected: [],
      impactConfirmationRequired: false,
    } as never);
    await store.confirmImpact();
    await flushPromises();
    // 确认句柄随这次保存一起提交（M4）
    const saveCall = vi.mocked(api.saveBoardState).mock.calls[0];
    expect((saveCall[3] as { checkId: string }).checkId).toBe("chk_2");
    await store.submit();
    expect(api.submitBoard).toHaveBeenCalledTimes(1);
    const submitCall = (api.submitBoard as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(submitCall[3]).toBeTypeOf("number");
    expect(submitCall[4]).toBe("chk_2");
  });
});
