/**
 * 【独立验收 D · 第八轮】F2：旧格式本机记录的删除重试，不许误删后来新建的新稿。
 *
 * 触发（契约 §3 / 本轮反例 F2）：
 *   本机旧格式记录没有版本（或版本为 0），服务器有另一稿 → 用户选服务器稿 →
 *   本机删除失败 → 另一页面写新稿 → 原页重试。
 * 基线现状：删除登记只有「对象 id」，重试按对象删 —— 把后来另一页面写下的新稿一起删了。
 * 正确行为：处理旧记录的决定不作用于后来新建的记录；无法证明「当前仍是原记录」就保留新稿。
 *
 * 反例走的是**生产重试入口**（store.retryDraftSave），不是底层 helper。
 * 本文件在 b3245e5 上必须失败（红）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../stores/interactive";
import * as api from "../services/interactive";
import {
  cardLocalDraftStorageKey,
  readCardLocalDraft,
  writeCardLocalDraft,
} from "../interactive/drafts";
import { card, installControllableStorage, payload } from "./support/harness";

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

const LOCAL_OLD = "本机那份旧稿（没有版本）";
const SERVER_DRAFT = "服务器那份草稿（用户选择保留）";
const OTHER_PAGE_NEW = "另一个页面在这之后写下的新稿";

/** 整份替换语义的服务器草稿替身，与真实接口一致。 */
function fakeServerDrafts(initial: Record<string, string>) {
  const drafts: Record<string, string> = { ...initial };
  const puts: Record<string, string>[] = [];
  vi.mocked(api.fetchBoardState).mockImplementation(
    async () => payload(3, [card("c1", "正式正文")], { ...drafts }) as never,
  );
  vi.mocked(api.saveDrafts).mockImplementation(async (_boardId, next) => {
    puts.push({ ...(next as Record<string, string>) });
    for (const key of Object.keys(drafts)) delete drafts[key];
    Object.assign(drafts, next as Record<string, string>);
    return { drafts: { ...drafts }, updatedAt: "t" } as never;
  });
  return { drafts, puts };
}

let storage: ReturnType<typeof installControllableStorage>;

beforeEach(() => {
  storage = installControllableStorage();
  setActivePinia(createPinia());
  vi.mocked(api.saveBoardState).mockResolvedValue({ ok: true, seq: 4, savedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 },
  } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({
    ok: true,
    checkId: "chk",
    stateVersion: 3,
    affected: [],
    impactConfirmationRequired: false,
  } as never);
  vi.mocked(api.submitBoard).mockResolvedValue({ status: "empty" } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  } as never);
});

afterEach(() => {
  storage.restore();
  vi.clearAllMocks();
});

describe("F2 旧格式本机记录的删除重试", () => {
  it("重试不许把另一页面后来写下的新稿当成「那份旧记录」删掉", async () => {
    // 旧格式记录：没有 version 字段（旧版本写下的）
    localStorage.setItem(
      cardLocalDraftStorageKey("c1"),
      JSON.stringify({
        text: LOCAL_OLD,
        updatedAt: Date.now(),
        seq: 1,
        kind: "draft",
        boardId: "board_default",
      }),
    );
    const server = fakeServerDrafts({ "card:c1": SERVER_DRAFT });
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();
    expect(store.draftConflictFor("c1")).toEqual({ local: LOCAL_OLD, server: SERVER_DRAFT });

    // 用户选「用服务器上的」；本机这一次删除真的失败（存储故障）
    storage.failNextRemove();
    store.resolveDraftConflict("c1", "server");
    await flushPromises();
    expect(store.draftLocalRemovalErrorFor("c1"), "删除失败必须先如实登记成待处理").not.toBeNull();

    // 删除失败期间，**另一个页面**写下了这张卡的新稿（同一份 localStorage）
    writeCardLocalDraft("c1", OTHER_PAGE_NEW, { boardId: "board_default", seq: 9 });
    expect(readCardLocalDraft("c1")?.text).toBe(OTHER_PAGE_NEW);

    // 原页重试「处理那条旧记录」的决定（生产重试入口）
    await store.retryDraftSave("card:c1");
    await flushPromises();

    const after = readCardLocalDraft("c1");
    expect(
      after?.text,
      "重试按对象删掉了另一页面后来写下的新稿（F2）：旧格式记录没有版本可证明，必须保留新稿",
    ).toBe(OTHER_PAGE_NEW);
    expect(after?.kind).not.toBe("cleared");
    // 服务器那份用户选择保留的稿子不许被动
    expect(server.drafts["card:c1"]).toBe(SERVER_DRAFT);
    expect(store.cardDraftText("c1")).toBe(SERVER_DRAFT);
  });
});
