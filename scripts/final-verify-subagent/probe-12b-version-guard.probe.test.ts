/**
 * 独立验收（E）附加反例 12b：修复后的「重试先补写 cleared」是否破坏多页面版本守卫。
 *
 * 契约 M2 原文：「已确认清除的事实优先于旧副本……但**不得为清除旧稿误删后来输入的更新版本**。」
 * 场景（同一浏览器两个页面共享 localStorage，§12.2 明确要求按记录版本校验）：
 *   1) 本机 v1 编辑副本；
 *   2) 本页面存储写满：新稿与 cleared 都写失败 → 本页面登记「本地清除保护待补写」；
 *   3) 另一个页面在此期间写入了**更新的一版**编辑草稿（v2）；
 *   4) 用户在本页面点重试（store.retryDraftSave）。
 * 期望：v2 那份更新输入必须原样保留；重试只许补写清除保护 / 清理它自己确认的版本。
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

describe("E-12b 重试补写清除保护不得覆盖别的页面后来写入的新版本", () => {
  it("本页面清除写失败 + 另一页面写入 v2 → 重试不得把 v2 覆盖成 cleared", async () => {
    writeCardLocalDraft("c1", "旧稿-v1", { boardId: "board_default" });
    expect(readCardLocalDraft("c1")?.version).toBe(1);

    const store = useInteractiveStore();
    storageWriteFails = true;
    store.setDraft("card:c1", "本页面的新稿");
    store.setDraft("card:c2", "让 flush 有工作");
    store.clearDraft("card:c1");
    storageWriteFails = false;
    expect(store.draftRemovalStateFor("card:c1").status).toBe("error");

    // 另一个页面（同一浏览器、同一 localStorage）在此期间写入了更新的一版编辑草稿
    const otherPage = writeCardLocalDraft("c1", "另一页面的新输入-v2", { boardId: "board_default" });
    expect(otherPage.ok).toBe(true);
    expect(readCardLocalDraft("c1")?.version).toBe(2);
    expect(readCardLocalDraft("c1")?.text).toBe("另一页面的新输入-v2");

    await store.retryDraftSave("card:c1");
    await flushPromises();

    const after = readCardLocalDraft("c1");
    // eslint-disable-next-line no-console
    console.log("[E-12b 观测]", JSON.stringify({
      version: after?.version ?? null,
      kind: after?.kind ?? null,
      text: after?.text ?? null,
    }));
    expect(after, "另一页面后来写入的更新记录被删除/覆盖（版本守卫失效）").not.toBeNull();
    expect(after?.kind, "另一页面的更新草稿必须仍是 draft，而不是被换成 cleared").toBe("draft");
    expect(after?.text, "另一页面写入的更新内容必须原样保留").toBe("另一页面的新输入-v2");
  });
});
