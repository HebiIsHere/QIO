/**
 * 收尾轮 13（Lead 接线部分）：本机副本删除失败必须如实可见、可重试；成功才显示成功。
 *
 * 标注：【会话层】只依赖 stores/interactive.ts 的公开状态与动作；
 *      【模拟失败】让 Storage.removeItem 抛错制造真实的本机删除失败。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import { writeCardLocalDraft } from "../../interactive/drafts";
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

function card(id: string): BoardState["cards"][number] {
  return {
    id, kind: "text", content: "", meta: {}, x: 0, y: 0, w: 1, h: 1,
    checked: false, hidden: false, folded: false, bookmarked: false, deleted: false,
    createdAt: "", updatedAt: "",
  };
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue({
    board: { id: "board_default", title: "默认板面" },
    state: { boardId: "board_default", seq: 3, updatedAt: "t", cards: [card("c1")], groups: [], links: [], selection: [] },
    seq: 3,
    baseline: null,
    submissions: [],
    drafts: { drafts: {}, updatedAt: null },
  } as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true } } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } } as never);
});

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("13 本机删除失败不许静默报成完成", () => {
  it("删除失败：显示真实原因、服务端保存仍然算成功；重试后真的删掉", async () => {
    const store = useInteractiveStore();
    await store.load();
    const key = "card:c1";
    writeCardLocalDraft("c1", "本机副本", { boardId: "board_default", seq: 1 });
    store.setDraft(key, "本机副本 v2");
    const spy = vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("本机存储被禁用");
    });
    // 服务端保存成功 → 该清理本机副本 → 删除真的失败
    await store.flushDrafts();
    await flushPromises();
    expect(store.draftStateFor(key).status, "服务端那一路成功了").toBe("saved");
    expect(store.draftLocalStateFor(key).ok, "本机删除失败却报成功").toBe(false);
    expect(store.draftProtectionStatus("c1").error).toContain("本机存储被禁用");

    // 用户点重试：这次真的删掉，状态如实收敛
    spy.mockRestore();
    await store.retryDraftSave(key);
    await flushPromises();
    expect(store.draftLocalStateFor(key).ok).toBe(true);
  });
});
