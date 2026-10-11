/**
 * 反例 D6-①/②/④：冲突由用户选择，不许被绕过（契约 §12.1）。
 *
 * 由子智能体 A 编写。先在基线 7ef7537 上运行：
 * - 反例①（两个变体）在基线上必须失败：未决冲突的本机候选被 flushDrafts 直接写上服务器，
 *   且本机候选副本在保存成功后被删；
 * - 反例②（组件级，仅点击「编辑」）在基线上必须失败：startEdit 一开编辑器就把冲突清掉并自动保存本机候选；
 * - 反例④ 部分子项在基线已通过（此前轮次的修复已覆盖），如实标注。
 *
 * 标注：【状态】只依赖 store 的公开状态与动作；【组件】挂载真实组件；【模拟】受控接口替身。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as imApi from "../../services/interactive";
import { cardLocalDraftStorageKey, readCardLocalDraft, writeDraft } from "../../interactive/drafts";
import BoardCard from "../../components/interactive/BoardCard.vue";
import type { BoardState, BoardStateResponse } from "../../interactive/types";

vi.mock("../../services/api", () => ({
  api: { sendTurn: vi.fn() },
}));

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
        id: "c1", kind: "text", x: 40, y: 40, w: 240, h: 120, content: "正式正文",
        checked: true, hidden: false, folded: false, bookmarked: false, deleted: false,
        createdAt: "2026-10-08T00:00:00.000Z", updatedAt: "2026-10-08T00:00:00.000Z",
      },
      {
        id: "c2", kind: "text", x: 300, y: 40, w: 240, h: 120, content: "B 卡正文",
        checked: true, hidden: false, folded: false, bookmarked: false, deleted: false,
        createdAt: "2026-10-08T00:00:00.000Z", updatedAt: "2026-10-08T00:00:00.000Z",
      },
    ],
    groups: [], links: [], selection: [], updatedAt: "2026-10-08T00:00:00.000Z",
  } as unknown as BoardState;
}

/** 受控的「服务端」：草稿整份替换保存（未列出的键会被删掉），和真实接口语义一致。 */
function fakeServer(initial: Record<string, string> = {}) {
  const drafts: Record<string, string> = { ...initial };
  let updatedAt = "2026-10-08T00:00:00.000Z";
  const view = { drafts };
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
  return view;
}

function lastPayload(): Record<string, string> {
  const calls = vi.mocked(imApi.saveDrafts).mock.calls;
  return (calls[calls.length - 1]?.[1] ?? {}) as Record<string, string>;
}

function newStore() {
  setActivePinia(createPinia());
  return useInteractiveStore();
}

/** 旧格式本机记录：没有归属、没有版本、没有种类 —— 按它自己无法判定和服务器那份谁新 */
function seedOldFormatLocal(cardId: string, text: string): void {
  writeDraft(cardLocalDraftStorageKey(cardId), text, 4);
}

/** 当前格式本机记录：带归属与正数版本号（但也只有一个本地序号，证明不了比服务器新） */
function seedVersionedLocal(cardId: string, text: string, version: number): void {
  localStorage.setItem(
    cardLocalDraftStorageKey(cardId),
    JSON.stringify({ text, updatedAt: Date.now(), seq: 9, kind: "draft", version, boardId: "board_default" }),
  );
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

describe("反例①：A 卡冲突未决，用户只编辑 B 并保存（§12.1）", () => {
  it("【状态/旧格式】服务器的 A 不被本机候选覆盖，A 的本机副本不被删，冲突不被绕过", async () => {
    const server = fakeServer({ "card:c1": "服务器版本A" });
    seedOldFormatLocal("c1", "本机候选A");

    const store = useInteractiveStore();
    await store.load();
    await flushPromises();
    // 前提成立：冲突确实在，等用户选择
    expect(store.draftConflictFor("c1"), "前提：加载后应存在未决冲突").toEqual({
      local: "本机候选A",
      server: "服务器版本A",
    });

    // 用户只编辑 B，然后保存（防抖到点）
    store.setDraft("card:c2", "B 的新输入");
    await store.flushDrafts();
    await flushPromises();

    expect(server.drafts["card:c2"], "B 的编辑应正常保存").toBe("B 的新输入");
    expect(
      server.drafts["card:c1"],
      "未选之前，服务器的 A 被本机候选覆盖了（服务端保存是整份替换，未决键把本机候选写了上去）",
    ).toBe("服务器版本A");
    expect(lastPayload()["card:c1"], "未决冲突键不允许把本机候选放进请求").toBe("服务器版本A");
    expect(
      readCardLocalDraft("c1"),
      "保存成功后把 A 的本机候选副本也删了（两份文字必须都保留到用户选择为止）",
    ).not.toBeNull();
    expect(store.draftConflictFor("c1"), "没有用户选择，冲突却消失了").toEqual({
      local: "本机候选A",
      server: "服务器版本A",
    });
  });

  it("【状态/带正数版本号】只有一个本地版本号不足以证明比服务器新 —— 同样不许覆盖", async () => {
    const server = fakeServer({ "card:c1": "服务器版本A" });
    seedVersionedLocal("c1", "本机候选A", 7);

    const store = useInteractiveStore();
    await store.load();
    await flushPromises();
    expect(store.draftConflictFor("c1"), "带版本号也不能判定新旧，应留给用户选择").toEqual({
      local: "本机候选A",
      server: "服务器版本A",
    });

    store.setDraft("card:c2", "B 的新输入");
    await store.flushDrafts();
    await flushPromises();

    expect(server.drafts["card:c1"], "服务器的 A 被本机候选覆盖了").toBe("服务器版本A");
    expect(readCardLocalDraft("c1"), "A 的本机候选副本被删了").not.toBeNull();
    expect(store.draftConflictFor("c1")).toEqual({ local: "本机候选A", server: "服务器版本A" });
  });
});

describe("反例②：A 卡冲突未决，仅点击「编辑」（§12.1：打开编辑器不等于选择本机版本）", () => {
  it("【组件】只点编辑：冲突仍在、提示仍在、服务器未改、本机候选不被自动保存", async () => {
    vi.useFakeTimers();
    const server = fakeServer({ "card:c1": "服务器版本A" });
    seedOldFormatLocal("c1", "本机候选A");

    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();
    expect(store.draftConflictFor("c1")).toEqual({ local: "本机候选A", server: "服务器版本A" });

    // 让这张卡成为工具栏的持有者（真实路径：选中后最后点中的那张渲染工具栏）
    store.board!.selection = ["c1"];
    const card = (store.board!.cards as Array<Record<string, unknown>>).find((c) => c.id === "c1")!;
    const wrapper = mount(BoardCard, {
      props: {
        card: card as never,
        selected: true,
        highlight: false,
        dragging: false,
        x: 0,
        y: 0,
        groupName: null,
        groups: [],
        toolbarLeft: 0,
        toolbarTop: 120,
        multi: false,
        connecting: false,
      },
      global: { plugins: [pinia] },
    });
    await flushPromises();

    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await flushPromises();

    // 冲突没有被「打开编辑器」这个动作消灭
    expect(
      store.draftConflictFor("c1"),
      "仅点击编辑，冲突就消失了（打开编辑器不等于选择本机版本）",
    ).toEqual({ local: "本机候选A", server: "服务器版本A" });
    expect(wrapper.find('[data-im="card-draft-conflict"]').exists(), "冲突选择入口在编辑处仍要可见").toBe(true);
    // 编辑框里可以显示本机候选，但两份都保留、服务器未改
    expect((wrapper.find('[data-im="card-editor"]').element as HTMLTextAreaElement).value).toBe("本机候选A");
    // 防抖到点：也不许把本机候选自动写上服务器
    await vi.advanceTimersByTimeAsync(900);
    await flushPromises();
    expect(vi.mocked(imApi.saveDrafts).mock.calls.length, "打开编辑器就自动发起了草稿保存").toBe(0);
    expect(server.drafts["card:c1"], "本机候选被自动保存覆盖了服务器上的 A").toBe("服务器版本A");
    wrapper.unmount();
  });
});

describe("反例④：分别选择两种版本后，保存与恢复符合选择；清理只作用于已确认的对应版本", () => {
  it("【状态/已含此前修复口径】选「本机的」→ 保存按选择落库，刷新后无冲突", async () => {
    const server = fakeServer({ "card:c1": "服务器版本A" });
    seedVersionedLocal("c1", "本机候选A", 7);
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();

    store.resolveDraftConflict("c1", "local");
    await store.flushDrafts();
    await flushPromises();

    expect(server.drafts["card:c1"], "选了本机的，保存的却不是它").toBe("本机候选A");

    const reloaded = newStore();
    await reloaded.load();
    await flushPromises();
    expect(reloaded.draftConflictFor("c1"), "按选择处理完之后不应再有冲突").toBeNull();
    expect(reloaded.cardDraftText("c1")).toBe("本机候选A");
  });

  it("【状态/已含此前修复口径】选「服务器上的」→ 服务器未改、编辑内容跟随、本机候选按确认清理", async () => {
    const server = fakeServer({ "card:c1": "服务器版本A" });
    seedVersionedLocal("c1", "本机候选A", 7);
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();

    store.resolveDraftConflict("c1", "server");
    await flushPromises();

    expect(store.cardDraftText("c1"), "选了服务器上的，编辑内容应跟随服务器那份").toBe("服务器版本A");
    expect(server.drafts["card:c1"], "选服务器版不该改写服务器").toBe("服务器版本A");
    expect(readCardLocalDraft("c1"), "已确认的那份本机候选没有按选择清理").toBeNull();

    const reloaded = newStore();
    await reloaded.load();
    await flushPromises();
    expect(reloaded.draftConflictFor("c1")).toBeNull();
    expect(reloaded.cardDraftText("c1")).toBe("服务器版本A");
  });

  it("【状态/基线应失败】清理只作用于已确认的对应版本：记录已被更晚的写入替换时不许删", async () => {
    const server = fakeServer({ "card:c1": "服务器版本A" });
    seedVersionedLocal("c1", "本机候选A", 7);
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();

    // 冲突登记之后、用户选择之前，记录被更晚的一次写入替换（如同一浏览器的另一个页面）
    writeDraft(cardLocalDraftStorageKey("c1"), "更新过的本机候选", 12);

    store.resolveDraftConflict("c1", "server");
    await flushPromises();

    expect(
      readCardLocalDraft("c1")?.text,
      "把已经变成新版本的记录当作「已确认的那一版」删掉了（清理必须按版本对准）",
    ).toBe("更新过的本机候选");
  });

  it("【状态/基线应失败】在飞保存回执按记录版本清理：别的页面写入的新副本不被旧回执删掉", async () => {
    vi.useFakeTimers();
    let release: (value: unknown) => void = () => {};
    fakeServer({});
    vi.mocked(imApi.saveDrafts).mockImplementationOnce(
      () => new Promise((resolve) => { release = resolve; }) as never,
    );
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();

    store.setDraft("card:c1", "本页在飞的一版");
    await vi.advanceTimersByTimeAsync(700); // 防抖到点，请求在飞
    expect(vi.mocked(imApi.saveDrafts).mock.calls.length).toBe(1);

    // 请求在飞期间，同一浏览器的另一个页面写下了更新版本的本机记录
    localStorage.setItem(
      cardLocalDraftStorageKey("c1"),
      JSON.stringify({ text: "另一个页面写下的新副本", updatedAt: Date.now(), seq: 3, kind: "draft", version: 5, boardId: "board_default" }),
    );

    release({ drafts: { "card:c1": "本页在飞的一版" }, updatedAt: new Date().toISOString() });
    await flushPromises();

    expect(
      readCardLocalDraft("c1")?.text,
      "在飞的旧保存回执把别的页面新写下的本机副本清理掉了（清理必须按记录版本校验）",
    ).toBe("另一个页面写下的新副本");
  });
});
