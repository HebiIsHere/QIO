/**
 * 独立验收 D5-A：卡片草稿的恢复发现与清除同步（契约 §11.1 / §11.2 / §11.3）。
 *
 * 由独立验收子智能体 D 编写，**不修改任何产品代码**。
 * 每条用例按「用户能观察到的正确行为」断言：先写输入框、刷新、再看草稿还在不在。
 * 在基线 f436ad8 上必须**行为性失败**（不是「函数不存在」「选择器改名」）。
 *
 * 标注：【状态】只依赖 store 的公开状态与动作；【模拟】用受控的接口替身模拟服务器。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as imApi from "../../services/interactive";
import { cardLocalDraftKey, writeDraft } from "../../interactive/drafts";
import type { BoardState, BoardStateResponse } from "../../interactive/types";

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  fetchVisibleRange: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  previewMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
  approveIntent: vi.fn(),
  rejectIntent: vi.fn(),
  batchDecide: vi.fn(),
  createDemoIntents: vi.fn(),
  advanceIntent: vi.fn(),
  updateIntentPreview: vi.fn(),
}));

function boardState(): BoardState {
  return {
    boardId: "board_default",
    cards: [
      {
        id: "c1",
        kind: "text",
        x: 40,
        y: 40,
        w: 240,
        h: 120,
        content: "正式正文",
        checked: true,
        hidden: false,
        folded: false,
        bookmarked: false,
        deleted: false,
        createdAt: "2026-10-08T00:00:00.000Z",
        updatedAt: "2026-10-08T00:00:00.000Z",
      },
    ],
    groups: [],
    links: [],
    selection: [],
    updatedAt: "2026-10-08T00:00:00.000Z",
  } as unknown as BoardState;
}

/** 受控的「服务端」：草稿整份替换保存，和真实接口的语义一致（未列出的键会被删掉）。 */
function fakeServer(initial: Record<string, string> = {}) {
  const drafts: Record<string, string> = { ...initial };
  let updatedAt = "2026-10-08T00:00:00.000Z";
  const state: { drafts: Record<string, string> } = { drafts };
  vi.mocked(imApi.fetchBoardState).mockImplementation(async (): Promise<BoardStateResponse> => ({
    board: { id: "board_default", title: "板面" },
    state: boardState(),
    seq: 1,
    baseline: null,
    submissions: [],
    drafts: { drafts: { ...drafts }, updatedAt },
  }));
  vi.mocked(imApi.saveDrafts).mockImplementation(async (_boardId: string, next: Record<string, string>) => {
    for (const key of Object.keys(drafts)) delete drafts[key];
    Object.assign(drafts, next);
    updatedAt = new Date().toISOString();
    return { drafts: { ...drafts }, updatedAt };
  });
  vi.mocked(imApi.fetchVisibleRange).mockResolvedValue({ visibleRange: null } as never);
  vi.mocked(imApi.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false } as never);
  return state;
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe("场景 1：首次编辑在防抖前刷新（§11.1）", () => {
  it("【状态】防抖未到就刷新：本机记录必须被发现并恢复出来", async () => {
    fakeServer({});
    const first = useInteractiveStore();
    // 真实路径：BoardCard 打开编辑器与每次输入都调 store.setDraft("card:<id>", 文字)
    first.setDraft("card:c1", "还没到 600ms 就刷新掉的新文字");
    // 用户立刻刷新：页面重建 = 新的 store，内存里的 drafts 已经没了；服务器也还没有这份草稿
    setActivePinia(createPinia());
    const reloaded = useInteractiveStore();
    await reloaded.load();
    await flushPromises();

    expect(
      reloaded.hasCardDraft("c1"),
      "刷新后没有发现只存在于本机的新草稿：输入框的文字恢复不出来",
    ).toBe(true);
    expect(reloaded.cardDraftText("c1"), "恢复出来的正文不对").toBe("还没到 600ms 就刷新掉的新文字");
  });

  it("【状态】恢复出来的本机草稿要重新排一次保存，不能只显示不保护", async () => {
    vi.useFakeTimers();
    fakeServer({});
    // 先模拟「上一页输入时同步写下的本机恢复副本」：与 setDraft 走的是同一条写入路径，
    // 但没有留下上一页的防抖计时器（用户在计时器到点前就刷新了）。
    writeDraft(cardLocalDraftKey("c1"), "只在本机的新草稿", 7);

    setActivePinia(createPinia());
    const reloaded = useInteractiveStore();
    await reloaded.load();
    await vi.advanceTimersByTimeAsync(800);
    await flushPromises();

    const calls = vi.mocked(imApi.saveDrafts).mock.calls;
    const saved = calls.some(([, payload]) => (payload as Record<string, string>)["card:c1"] === "只在本机的新草稿");
    expect(saved, "恢复后没有把这份草稿保存回服务器（saveDrafts 调用次数 " + calls.length + "）").toBe(true);
  });
});

describe("场景 2：服务器已有旧草稿，用户清除后刷新不许复活（§11.2）", () => {
  it("【状态】清除最后一份草稿也必须发出同步请求", async () => {
    const server = fakeServer({ "card:c1": "服务器上的旧草稿" });
    const store = useInteractiveStore();
    await store.load();
    expect(store.cardDraftText("c1")).toBe("服务器上的旧草稿");

    store.clearDraft("card:c1");
    await store.flushDrafts();
    await flushPromises();

    expect(
      server.drafts["card:c1"],
      "清除了草稿，服务器上却还留着它：这次清除没有同步（刷新后必然复活）",
    ).toBeUndefined();
  });

  it("【状态】清除后刷新，旧草稿不许复活", async () => {
    fakeServer({ "card:c1": "服务器上的旧草稿" });
    const store = useInteractiveStore();
    await store.load();
    store.clearDraft("card:c1");
    await store.flushDrafts();
    await flushPromises();

    setActivePinia(createPinia());
    const reloaded = useInteractiveStore();
    await reloaded.load();
    await flushPromises();

    expect(reloaded.cardDraftText("c1"), "刷新之后旧草稿又回来了（用户已经清除过它）").not.toBe("服务器上的旧草稿");
    expect(reloaded.hasCardDraft("c1"), "刷新之后仍然认为这张卡片有草稿").toBe(false);
  });
});
