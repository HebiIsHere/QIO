/**
 * 独立验收 D2-A：两张卡片的草稿记录必须互不干扰（契约 §11.1 / §11.2 / §11.3）。
 *
 * 由独立验收子智能体 D2 编写，**不修改任何产品代码**。
 * 我挑的是「同时存在多张卡片、各有各的记录来源」这条最容易被「整集合处理」写坏的路：
 * A 卡只有本机记录、B 卡只有服务器草稿；清除 A 卡时 B 卡的两份都不能被动到。
 *
 * 标注：【状态】只依赖 store 的公开状态与动作；【模拟】用受控的接口替身模拟服务器。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as imApi from "../../services/interactive";
import { readCardLocalDraft, writeCardLocalDraft } from "../../interactive/drafts";
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

function card(id: string, content: string) {
  return {
    id,
    kind: "text",
    x: 40,
    y: 40,
    w: 240,
    h: 120,
    content,
    checked: true,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "2026-10-08T00:00:00.000Z",
    updatedAt: "2026-10-08T00:00:00.000Z",
  };
}

function boardState(): BoardState {
  return {
    boardId: "board_default",
    cards: [card("c1", "A 卡正式正文"), card("c2", "B 卡正式正文")],
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

describe("D2 反例：两张卡片的草稿记录互不干扰（§11.1 / §11.2）", () => {
  it("【状态】A 卡只有本机记录、B 卡只有服务器草稿：两份都按各自的记录恢复，谁也不覆盖谁", async () => {
    fakeServer({ "card:c2": "B 卡服务器上的草稿" });
    // A 卡的本机记录是上一页同步写下的（真实路径：setDraft 写的就是这条记录）
    writeCardLocalDraft("c1", "A 卡只在本机的草稿", { boardId: "board_default", seq: 3 });

    setActivePinia(createPinia());
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();

    expect(store.hasCardDraft("c1"), "A 卡只存在于本机的草稿没有被发现（枚举本机记录失败）").toBe(true);
    expect(store.cardDraftText("c1"), "A 卡恢复出来的正文不对").toBe("A 卡只在本机的草稿");
    expect(store.cardDraftText("c2"), "B 卡服务器上的草稿被 A 卡的恢复挤掉了").toBe("B 卡服务器上的草稿");
    expect(store.cardDraftText("c2"), "A 卡的本机草稿被串写到 B 卡上").not.toBe("A 卡只在本机的草稿");
    expect(readCardLocalDraft("c1")?.text, "A 卡的本机记录在对账后不该被当成已确认清掉").toBe("A 卡只在本机的草稿");
  });

  it("【状态】清除 A 卡的草稿：B 卡的服务器草稿与本机新版本都不受影响，刷新后各自仍然正确", async () => {
    const server = fakeServer({ "card:c1": "A 卡服务器上的旧草稿", "card:c2": "B 卡服务器上的旧草稿" });
    writeCardLocalDraft("c2", "B 卡本机的新文字", { boardId: "board_default", seq: 5 });

    const store = useInteractiveStore();
    await store.load();
    await flushPromises();
    expect(store.cardDraftText("c2"), "B 卡本机的新版本没有赢过服务器上的旧草稿").toBe("B 卡本机的新文字");
    expect(readCardLocalDraft("c2")?.text, "B 卡的本机记录在同步之前就不见了").toBe("B 卡本机的新文字");

    store.clearDraft("card:c1");
    await store.flushDrafts();
    await flushPromises();

    expect(server.drafts["card:c1"], "清除了 A 卡的草稿，服务器上却还留着它").toBeUndefined();
    // §12.1 修订（2026-10-09：本节与前面冲突时以本节为准）：c1 的保存请求带上了 c2 的键
    // —— c2 本机记录与服务器草稿不同、用户没有选择过，按「服务器事实」回写该键，
    // 既不把本机候选写上去、也不许从整份替换的请求里把它删掉。两份内容都保留：
    // 服务器的那份在服务器上、本机候选在本机记录与冲突登记里（见 store.draftConflictFor）。
    // 旧断言「服务器落库成 B 卡本机的新文字」描述的是 §11 时代的自动覆盖行为，已被 §12.1 废止。
    expect(server.drafts["card:c2"], "未决冲突键被整份替换保存删掉了").toBe("B 卡服务器上的旧草稿");
    expect(
      store.draftConflictFor("c2"),
      "未选择的冲突不许被另一张卡片的保存绕过",
    ).toEqual({ local: "B 卡本机的新文字", server: "B 卡服务器上的旧草稿" });
    expect(readCardLocalDraft("c2")?.text, "B 卡的本机候选副本在保存请求后被删了").toBe("B 卡本机的新文字");
    expect(store.cardDraftText("c2"), "编辑中的 B 卡内容不许被这次保存改变").toBe("B 卡本机的新文字");
    // B 卡的本机记录同步成功后按对象清掉是正确行为；这里要证明的是它没有被 A 卡的清除带坏：
    // 记录要么已经因为「服务器已确认同一份内容」被清掉，要么还是自己的那份文字。
    const c2LocalAfterSync = readCardLocalDraft("c2");
    expect(
      c2LocalAfterSync === null || c2LocalAfterSync.text === "B 卡本机的新文字",
      "B 卡的本机记录被 A 卡的清除改成了别的东西：" + JSON.stringify(c2LocalAfterSync),
    ).toBe(true);

    // 刷新：A 卡不许复活、B 卡的新文字照样恢复
    setActivePinia(createPinia());
    const reloaded = useInteractiveStore();
    await reloaded.load();
    await flushPromises();

    expect(reloaded.hasCardDraft("c1"), "A 卡被清除的草稿在刷新后复活了").toBe(false);
    expect(reloaded.cardDraftText("c2"), "刷新后 B 卡的草稿丢了").toBe("B 卡本机的新文字");
  });
});
