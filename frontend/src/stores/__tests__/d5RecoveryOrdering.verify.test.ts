/**
 * 独立验收 D5-G：恢复顺序与清除竞态（契约 §11.1 / §11.2 / §11.3）。
 *
 * 这些用例检查「先后关系」：在飞的旧请求、服务器慢返回、别的卡片的保存时间，
 * 都不许决定这张卡片的草稿命运。按用户行为断言（用户看到的是新输入/已清除/恢复出来的文字）。
 *
 * 标注：【状态】只依赖 store 的公开状态与动作；【模拟】受控接口替身与受控延迟。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import { useSessionStore } from "../session";
import * as imApi from "../../services/interactive";
import { cardLocalDraftKey, cardLocalDraftStorageKey, writeDraft } from "../../interactive/drafts";
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

function boardState(content = "正式正文"): BoardState {
  return {
    boardId: "board_default",
    cards: [
      {
        id: "c1", kind: "text", x: 40, y: 40, w: 240, h: 120, content: content,
        checked: true, hidden: false, folded: false, bookmarked: false, deleted: false,
        createdAt: "2026-10-08T00:00:00.000Z", updatedAt: "2026-10-08T00:00:00.000Z",
      },
    ],
    groups: [], links: [], selection: [], updatedAt: "2026-10-08T00:00:00.000Z",
  } as unknown as BoardState;
}

function response(drafts: Record<string, string>, updatedAt: string | null): BoardStateResponse {
  return {
    board: { id: "board_default", title: "板面" },
    state: boardState(),
    seq: 1,
    baseline: null,
    submissions: [],
    drafts: { drafts: drafts, updatedAt: updatedAt },
  } as unknown as BoardStateResponse;
}

/** 受控服务器：草稿整份替换保存（未列出的键被删掉），与真实接口语义一致。 */
function fakeServer(initial: Record<string, string>, updatedAt: string | null) {
  const drafts: Record<string, string> = { ...initial };
  let stamp = updatedAt;
  const view = { drafts: drafts };
  vi.mocked(imApi.fetchBoardState).mockImplementation(async () => response({ ...drafts }, stamp));
  vi.mocked(imApi.saveDrafts).mockImplementation(async (_boardId: string, next: Record<string, string>) => {
    for (const key of Object.keys(drafts)) delete drafts[key];
    Object.assign(drafts, next);
    stamp = new Date().toISOString();
    return { drafts: { ...drafts }, updatedAt: stamp as string };
  });
  vi.mocked(imApi.fetchVisibleRange).mockResolvedValue({ visibleRange: null } as never);
  vi.mocked(imApi.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false } as never);
  return view;
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

describe("场景 9：在飞的旧保存请求晚到，不许重建已经清除的草稿（§11.2）", () => {
  it("【状态】用户清除后，旧请求才返回：服务器与刷新都不能再看到这份草稿", async () => {
    vi.useFakeTimers();
    const server = fakeServer({ "card:c1": "服务器上的旧草稿" }, "2026-10-08T00:00:00.000Z");
    const store = useInteractiveStore();
    await store.load();
    expect(store.cardDraftText("c1")).toBe("服务器上的旧草稿");

    store.setDraft("card:c1", "新版本");
    let release: (value: unknown) => void = () => {};
    vi.mocked(imApi.saveDrafts).mockImplementationOnce(
      () => new Promise((resolve) => { release = resolve; }) as never,
    );
    await vi.advanceTimersByTimeAsync(700); // 防抖到点，请求在飞但还没有返回
    const sent = vi.mocked(imApi.saveDrafts).mock.calls.length;
    expect(sent, "防抖到点后没有发出保存请求").toBeGreaterThan(0);

    // 请求还没回来，用户清除了这份草稿
    store.clearDraft("card:c1");
    // 旧请求现在才返回（内容对应清除之前的版本）
    release({ drafts: { "card:c1": "新版本" }, updatedAt: new Date().toISOString() });
    await flushPromises();
    await store.flushDrafts();
    await flushPromises();

    expect(
      server.drafts["card:c1"],
      "在飞的旧保存请求晚到后，又把已经清除的草稿建立起来了",
    ).toBeUndefined();

    setActivePinia(createPinia());
    const reloaded = useInteractiveStore();
    await reloaded.load();
    await flushPromises();
    expect(reloaded.hasCardDraft("c1"), "刷新后又能看到这份已经清除的草稿").toBe(false);
  });
});

describe("场景 10：服务器慢返回不许覆盖恢复期间的新输入（§11.1）", () => {
  it("【状态】恢复还没回来时用户又打了字：新输入必须赢", async () => {
    fakeServer({}, null);
    writeDraft(cardLocalDraftStorageKey("c1"), "刷新前存下的本机草稿", 3);
    const store = useInteractiveStore();

    let release: (value: unknown) => void = () => {};
    vi.mocked(imApi.fetchBoardState).mockImplementationOnce(
      () => new Promise((resolve) => { release = resolve; }) as never,
    );
    const loading = store.load();
    await flushPromises();
    // 服务器还没回来，用户已经开始输入新内容
    store.setDraft("card:c1", "恢复期间新输入的文字");
    release(response({}, null));
    await loading;
    await flushPromises();

    expect(
      store.cardDraftText("c1"),
      "服务器慢返回时把用户在恢复期间新输入的文字覆盖成了旧的本机副本",
    ).toBe("恢复期间新输入的文字");
  });
});

describe("场景 11：过期判断只按这张卡片自己的记录（§11.3）", () => {
  it("【状态】别的卡片的保存时间不许让这张卡的本机副本消失", async () => {
    // 服务器上只有 c2 的草稿，而且集合时间是「未来」（属于 c2）
    fakeServer({ "card:c2": "B 卡的服务器草稿" }, "2030-01-01T00:00:00.000Z");
    // c1 只有本机恢复副本（时间就是现在，比服务器集合时间早很多）
    writeDraft(cardLocalDraftStorageKey("c1"), "C1 只在本机的草稿", 5);

    const store = useInteractiveStore();
    await store.load();
    await flushPromises();

    expect(
      store.hasCardDraft("c1"),
      "拿别的卡片（或整份草稿集合）的时间把这张卡的本机副本判没了",
    ).toBe(true);
    expect(store.cardDraftText("c1")).toBe("C1 只在本机的草稿");
    expect(store.cardDraftText("c2"), "另一张卡的服务器草稿被弄丢了").toBe("B 卡的服务器草稿");
    expect(
      store.board?.cards?.[0]?.content,
      "恢复出来的内容被直接写成了正式卡片内容（应该只回到待编辑内容）",
    ).toBe("正式正文");
  });
});

describe("场景 12：恢复不是发送（§11.4）", () => {
  it("【状态】取回失败原文不触发任何发送", async () => {
    /**
     * 驱动修正（主智能体）：失败原文属于**会话层**（`stores/session.ts` 的 failedSend/retryFailedSend），
     * 不是互动板 store。原来对 useInteractiveStore() 调用这些动作会拿到 undefined 而报
     * "store.retryFailedSend is not a function"，测的不是产品行为。断言不变。
     */
    const session = useSessionStore();
    const store = useInteractiveStore();
    session.failedSend = { topicId: null, text: "没发出去的原文", draftSeq: 1, at: Date.now() };
    session.failedSendError = "网络中断";
    const before = vi.mocked(imApi.saveDrafts).mock.calls.length;
    session.retryFailedSend();
    await flushPromises();
    expect(session.draft, "取回失败原文时没有把文字放回输入框").toBe("没发出去的原文");
    expect(
      vi.mocked(imApi.saveDrafts).mock.calls.length - before,
      "取回原文竟然发起了新的草稿写入（恢复了就不该再动服务器）",
    ).toBe(0);
  });
});
