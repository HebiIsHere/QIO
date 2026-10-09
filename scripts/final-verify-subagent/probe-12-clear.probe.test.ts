/**
 * 独立验收（E）反例 12 / M2：本机清除写入失败 + 服务器清除成功 → 重开后旧稿不得复活。
 *
 * 构造（全是真实 store + 真实 interactive/drafts.ts；只有网络层用 mock）：
 *   1) 本机已有 v1 编辑副本（磁盘事实）；
 *   2) 存储写满：新稿写入失败（不推进版本），随后 cleared 写入也失败；
 *   3) 服务器清除成功（草稿是整份替换：一次不含该键的 PUT 就删掉了服务器那份）；
 *   4) 用户点「重试」（CardDraftHint.vue 走的就是 store.retryDraftSave(key)）；
 *   5) 模拟关闭重开：新 pinia + 新 store.load()。
 * 契约 M2：服务器清除成功且本机 cleared 写入失败时，重试必须**先补写本机 cleared**，
 * 否则重开时磁盘上的旧稿会再次取得恢复权限 —— 本探针断言重开后旧稿不出现。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../../frontend/src/stores/interactive";
import * as api from "../../frontend/src/services/interactive";
import { readCardLocalDraft, writeCardLocalDraft } from "../../frontend/src/interactive/drafts";
import type { BoardState } from "../../frontend/src/interactive/types";

vi.mock("../../frontend/src/services/interactive", () => ({
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

function boardPayload(seq = 3, drafts: Record<string, string> = {}, cards: BoardState["cards"] = []) {
  return {
    board: { id: "board_default", title: "默认板面" },
    state: { boardId: "board_default", seq, updatedAt: "2026-10-09T10:00:00Z", cards, groups: [], links: [], selection: [] },
    seq,
    baseline: null,
    submissions: [],
    drafts: { drafts, updatedAt: "2026-10-09T10:00:00Z" },
  } as Awaited<ReturnType<typeof api.fetchBoardState>>;
}

const protoSetItem = Storage.prototype.setItem;
let storageWriteFails = false;

beforeEach(() => {
  localStorage.clear();
  storageWriteFails = false;
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key: string, value: string) {
    if (storageWriteFails) {
      const err = new Error("QuotaExceededError: 模拟存储已满") as Error & { name: string };
      err.name = "QuotaExceededError";
      throw err;
    }
    return protoSetItem.call(this, key, value);
  });
  setActivePinia(createPinia());
  // 服务器板面上有 c1；服务器草稿集为空（v1 只存在于本机）。
  vi.mocked(api.fetchBoardState).mockResolvedValue(boardPayload(3, {}, [card("c1", ""), card("c2", "")]));
  vi.mocked(api.saveBoardState).mockResolvedValue({ ok: true, seq: 4, savedAt: "t", state: boardPayload(4).state } as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 } } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } } as never);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe("E-12/M2 本机 cleared 写失败 + 服务器清除成功后的重试与重开", () => {
  it("重试必须先重建本机清除保护；重开后旧稿不得复活", async () => {
    // 1) 本机 v1 编辑副本（磁盘事实）
    writeCardLocalDraft("c1", "旧稿-v1", { boardId: "board_default" });
    expect(readCardLocalDraft("c1")?.version).toBe(1);
    expect(readCardLocalDraft("c1")?.kind).toBe("draft");

    const store = useInteractiveStore();
    // 2) 存储写满：新稿写入失败、cleared 写入也失败（磁盘上仍是 v1）
    storageWriteFails = true;
    store.setDraft("card:c1", "新稿-v2");
    store.setDraft("card:c2", "另一张卡的稿子"); // 让这次 flush 真的会发出网络请求
    store.clearDraft("card:c1"); // 新稿与 cleared 都写失败（同一段存储写满期间）
    storageWriteFails = false;

    const removalAfterClear = store.draftRemovalStateFor("card:c1").status;
    const diskAfterClear = readCardLocalDraft("c1");
    expect(removalAfterClear, "本机清除没写成 → 必须如实显示错误（不是静默成功）").toBe("error");
    expect(diskAfterClear?.kind, "写失败时磁盘上仍是旧编辑副本 v1").toBe("draft");

    // 3) 服务器清除成功：整份替换的草稿 PUT 不含 c1（c2 让请求真的发出）
    await store.flushDrafts();
    await flushPromises();
    const sent = vi.mocked(api.saveDrafts).mock.calls.at(-1)?.[1] as Record<string, string>;
    expect(sent, "网络清除必须真的发出过").toBeTruthy();
    expect(Object.prototype.hasOwnProperty.call(sent, "card:c1"), "服务器清除 = PUT 不再包含 c1").toBe(false);

    // 4) 用户点重试（CardDraftHint 的真实路径）
    await store.retryDraftSave("card:c1");
    await flushPromises();
    const diskAfterRetry = readCardLocalDraft("c1");
    const removalAfterRetry = store.draftRemovalStateFor("card:c1").status;

    // 5) 模拟关闭重开
    setActivePinia(createPinia());
    const reopened = useInteractiveStore();
    await reopened.load();
    await flushPromises();

    // eslint-disable-next-line no-console
    console.log("[E-12 观测]", JSON.stringify({
      removalAfterClear,
      diskAfterClearKind: diskAfterClear?.kind,
      removalAfterRetry,
      diskAfterRetryKind: diskAfterRetry?.kind ?? null,
      diskAfterRetryText: diskAfterRetry?.text ?? null,
      reopenedDraft_c1: reopened.drafts["card:c1"] ?? null,
      serverSeenDraftKeys: sent ? Object.keys(sent) : [],
    }, null, 0));

    // 修复后的合法收敛有两种：留下 cleared 保护（等网络确认），或保护补写成功且网络确认后记录被删掉。
    // 不许出现的是：磁盘上仍是可恢复的 kind=draft 旧稿。
    expect(diskAfterRetry?.kind ?? null, "重试后不得留下可恢复的 draft 旧稿").not.toBe("draft");
    expect(reopened.drafts["card:c1"] ?? null, "重开后旧稿不得复活").toBeNull();
  });
});
