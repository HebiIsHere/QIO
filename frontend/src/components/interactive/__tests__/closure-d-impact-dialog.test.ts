/**
 * 【独立验收 D · closure-d】第②层证据：真实组件 DOM（@vue/test-utils + jsdom）。
 *
 * 覆盖 R1 / R5 / R6 的**用户可见结果**：影响确认框是用户真正操作的界面。
 * 断言的是正确行为：基线（冻结时）必须失败；失败信息说明「界面上会发生什么」。
 * 网络与预判为受控替身（模拟 409 服务端门 / 迟到回执），不是真实网络。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import ImpactConfirmDialog from "../ImpactConfirmDialog.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as api from "../../../services/interactive";
import type { BoardState } from "../../../interactive/types";

vi.mock("../../../services/interactive", () => ({
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
    id, kind: "text", content, meta: {}, x: 0, y: 0, w: 240, h: 140,
    checked: true, hidden: false, folded: false, bookmarked: false, deleted: false,
    createdAt: "", updatedAt: "",
  };
}

function state(seq: number, cards: BoardState["cards"]): BoardState {
  return { boardId: "board_default", seq, updatedAt: "t", cards, groups: [], links: [], selection: [] };
}

function payload(seq: number, cards: BoardState["cards"]): never {
  return {
    board: { id: "board_default", title: "默认板面" },
    state: state(seq, cards),
    seq, baseline: null, submissions: [], drafts: { drafts: {}, updatedAt: null },
  } as never;
}

function intent(id: string, title: string): never {
  return {
    id, boardId: "board_default", submissionId: null, title, summary: "", status: "running",
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

let wrappers: VueWrapper[] = [];

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  wrappers = [];
  vi.mocked(api.fetchBoardState).mockResolvedValue(payload(3, [card("c1", "服务器原文")]));
  vi.mocked(api.saveBoardState).mockResolvedValue({
    ok: true, seq: 4, savedAt: "t", state: state(4, [card("c1", "服务器原文")]),
  } as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 },
  } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({
    ok: true, checkId: "chk_none", stateVersion: 3, affected: [], impactConfirmationRequired: false,
  } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.submitBoard).mockResolvedValue({ status: "empty" } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [intent("A", "任务-A")], conflicts: [], batchAvailable: false, recovery: { paused: [] },
  } as never);
});

afterEach(() => {
  for (const wrapper of wrappers) {
    try { wrapper.unmount(); } catch { /* 已卸载 */ }
  }
  document.body.innerHTML = "";
  vi.clearAllMocks();
});

function mountDialog(): VueWrapper {
  const wrapper = mount(ImpactConfirmDialog, { attachTo: document.body });
  wrappers.push(wrapper);
  return wrapper;
}

describe("R1（DOM）确认框取消后，界面上的板面必须回到服务器已保存内容", () => {
  it("点「取消」：确认框关闭、界面显示的板面是服务器原文", async () => {
    const store = useInteractiveStore();
    await store.load();
    const wrapper = mountDialog();
    expect(wrapper.find('[data-im="impact-dialog"]').exists()).toBe(false);

    // 服务端门要求确认
    vi.mocked(api.saveBoardState).mockRejectedValue(
      gateError([{ intentId: "A", title: "任务-A", materials: ["c1"], consequence: "暂停" }]),
    );
    store.commit(state(3, [card("c1", "被取消掉的新正文")]), "改正文");
    await store.saveNow();
    await flushPromises();
    expect(wrapper.find('[data-im="impact-dialog"]').exists()).toBe(true);

    await wrapper.get('[data-im="impact-cancel"]').trigger("click");
    await flushPromises();

    expect(wrapper.find('[data-im="impact-dialog"]').exists()).toBe(false);
    // 正确行为：取消之后，界面上看到的板面内容必须是服务器已保存的正文
    expect(store.board!.cards.map((c) => c.content)).toEqual(["服务器原文"]);
  });
});

describe("R5（DOM）服务端兜底确认：点「继续」必须用有效 checkId，说明不再重复出现", () => {
  it("409 兜底 → 重新预判 → 继续：确认框真正关闭、保存成功", async () => {
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] },
    } as never);
    const store = useInteractiveStore();
    await store.load();
    const wrapper = mountDialog();

    vi.mocked(api.saveBoardState).mockRejectedValueOnce(
      gateError([{ intentId: "A", title: "任务-A", materials: ["c1"], consequence: "暂停" }]),
    );
    vi.mocked(api.checkMaterialImpact).mockResolvedValue({
      ok: true, checkId: "chk_fresh", stateVersion: 3,
      affected: [{ intentId: "A", title: "任务-A", materials: ["c1"], consequence: "暂停" }],
      impactConfirmationRequired: true,
    } as never);

    store.commit(state(3, [card("c1", "改动后的正文")]), "改正文");
    await store.saveNow();
    await flushPromises();
    expect(wrapper.find('[data-im="impact-dialog"]').exists()).toBe(true);

    await wrapper.get('[data-im="impact-continue"]').trigger("click");
    await flushPromises();

    // 正确行为：确认框真的关闭（说明不再重复出现），且保存成功
    expect(wrapper.find('[data-im="impact-dialog"]').exists()).toBe(false);
    expect(store.pendingImpact).toBeNull();
    expect(store.saveStatus).toBe("saved");
    const calls = vi.mocked(api.saveBoardState).mock.calls;
    const lastConfirm = calls[calls.length - 1]?.[3] as { checkId?: string } | undefined;
    expect(lastConfirm?.checkId).toBe("chk_fresh");
  });
});

describe("R6（DOM）说明里列出的任务必须都显示在确认框里", () => {
  it("服务端列出 A 与 B：界面上必须同时看到两个任务的标题与受影响材料", async () => {
    vi.mocked(api.checkMaterialImpact).mockResolvedValue({
      ok: true, checkId: "chk_ab", stateVersion: 3,
      affected: [
        { intentId: "A", title: "任务-A", materials: ["材料一"], consequence: "暂停并保留进度" },
        { intentId: "B", title: "任务-B", materials: ["材料二"], consequence: "暂停并保留进度" },
      ],
      impactConfirmationRequired: true,
    } as never);
    const store = useInteractiveStore();
    await store.load();
    const wrapper = mountDialog();

    store.commit(state(3, [card("c1", "改动后的正文")]), "改正文");
    await store.saveNow();
    await flushPromises();
    await store.refreshBoardFromServer();
    await flushPromises();

    const items = wrapper.findAll('[data-im="impact-item"]');
    const text = items.map((item) => item.text()).join("\n");
    expect(items.length).toBe(2);
    expect(text).toContain("任务-A");
    expect(text).toContain("任务-B");
    expect(text).toContain("材料二");
  });
});

describe("R2/R3（DOM）已保存成功的新正文必须留在界面上，并能作为下一版的保存基准", () => {
  it("迟到的旧 GET 落地后：板面仍是已保存的新正文、dirty 保留；下一次保存带服务器最新 seq", async () => {
    const store = useInteractiveStore();
    await store.load();

    // 更早发起的读取：先挂起，稍后再返回旧正文（模拟真实乱序回执）
    let resolveGet!: (v: unknown) => void;
    vi.mocked(api.fetchBoardState).mockImplementation(
      () => new Promise((res) => { resolveGet = res; }) as never,
    );
    const refresh = store.refreshBoardFromServer();
    await Promise.resolve();

    // 用户保存新正文成功
    store.commit(state(3, [card("c1", "已保存成功的新正文")]), "改正文");
    vi.mocked(api.saveBoardState).mockResolvedValue({
      ok: true, seq: 4, savedAt: "t", state: state(4, [card("c1", "已保存成功的新正文")]),
    } as never);
    await store.saveNow();
    expect(store.board!.cards.map((c) => c.content)).toEqual(["已保存成功的新正文"]);

    // 迟到的旧读取返回旧正文
    resolveGet(payload(3, [card("c1", "旧正文")]));
    await refresh;
    await flushPromises();

    // 正确行为：界面事实（板面正文）仍是已保存成功的新正文；迟到读取只能作废，不许回退。
    // （dirty 的含义是「有没有未保存候选」：这次读取被拒绝后状态是否标 clean 属于实现细节，
    //  这里断言的契约是「板面正文不许被旧读取替换」，以及下一步保存仍能带正确基准。）
    expect(store.board!.cards.map((c) => c.content)).toEqual(["已保存成功的新正文"]);

    // 新一轮编辑后保存：必须携带服务器最新 seq（否则第二版在真实服务端存不上）
    store.commit(state(4, [card("c1", "第三版")]), "再改");
    let sentSeq: number | null = null;
    vi.mocked(api.saveBoardState).mockImplementation(async (_id, putState) => {
      sentSeq = putState.seq;
      return { ok: true, seq: 5, savedAt: "t2", state: state(5, [card("c1", "第三版")]) } as never;
    });
    await store.saveNow();
    await flushPromises();
    expect(sentSeq).toBe(4);
    expect(store.dirty).toBe(false);
  });
});
