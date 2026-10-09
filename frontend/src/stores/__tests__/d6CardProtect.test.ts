/**
 * 独立用例 D6-A：卡片内容保护（契约 §12.1 / §12.2，2026-10-09 第五轮）。
 *
 * §12.1 草稿冲突的选择不许被绕过：
 *  1. 未选择冲突时，另一张卡片的保存（整份替换）不许把 A 的本机候选写上服务器、
 *     也不许把服务器上的 A 从请求里删掉 —— 未决键按**服务器事实**回写；
 *  2. 服务器有一份不同的草稿时，本机记录版本号再大也不能证明比服务器新：不许直接覆盖，
 *     必须登记冲突等用户选择（未决时不许悄悄发保存请求）；
 *  3. 「打开编辑器」不等于选择本机版本：冲突提示不许在打开编辑器的同一动作里消失，
 *     而且没打开编辑器（刷新恢复后）也要在卡片上可见、可点、可选；
 *  4. 分别选择两种版本后行为正确：用本机 → 按选择保存；用服务器 → 编辑框跟随、
 *     本机候选清理只作用于**已确认的对应版本**（另一页面后来写入的更新副本不许被删）；
 *  5. 请求在飞期间的新输入（同浏览器另一页面写的本机副本）不被旧回执的清理误删。
 *
 * §12.2 清除依据只作用于当时的版本：
 *  6. 复现主反例：清除 A → 重新输入（这次本机写失败）→ 重新读取板面 ——
 *     内存里的新文字不许被旧的 cleared 记录删掉，失败状态与待保存内容都保留；
 *  7. 别的板面的清除依据不作用于本板面；
 *  8. 旧格式（无版本号）的清除记录同样不删它之后的新输入；
 *  9. 在飞的旧清除回执不清理后来的本地副本（按记录版本校验）。
 *
 * 标注：【状态】只依赖 store 公开状态与动作 ·【本机存储】直接读写 localStorage 预置记录 ·
 *       【组件】挂载真实组件 ·【模拟失败】受控拒绝/受控慢请求/受控写失败。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as imApi from "../../services/interactive";
import {
  cardDraftKey,
  cardDraftKey as cardKey,
  cardLocalDraftStorageKey,
  readCardLocalDraft,
  writeCardLocalDraft,
} from "../../interactive/drafts";
import BoardCard from "../../components/interactive/BoardCard.vue";

vi.mock("../../services/api", () => ({
  api: { sendTurn: vi.fn() },
}));

vi.mock("../../services/interactive", () => ({
  saveDrafts: vi.fn(),
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  fetchVisibleRange: vi.fn(),
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

const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");

/**
 * 可控写失败的本地存储（§12.2 的主反例需要「读取正常、写入失败」）：
 * 读与删始终正常，setItem 在 failWrites 打开时抛错 —— 对应「本机存储突然写不进去」。
 */
let failWrites = false;
function installControlledStorage(): void {
  const mem = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => mem.get(key) ?? null,
      setItem: (key: string, value: string) => {
        if (failWrites) throw new Error("本机存储这次没写进去（模拟）");
        mem.set(key, value);
      },
      removeItem: (key: string) => {
        mem.delete(key);
      },
      get length(): number {
        return mem.size;
      },
      key: (index: number) => Array.from(mem.keys())[index] ?? null,
    },
  });
}

function restoreLocalStorage(): void {
  if (originalDescriptor) Object.defineProperty(globalThis, "localStorage", originalDescriptor);
}

interface CardSpec {
  id: string;
  deleted?: boolean;
  content?: string;
}

/** 一份完整的板面响应（服务器草稿默认是空的：这正是「只存在于本机」的形态） */
function boardPayload(options: {
  cards?: Array<string | CardSpec>;
  drafts?: Record<string, string>;
  updatedAt?: string | null;
  seq?: number;
} = {}) {
  const cards = (options.cards ?? []).map((item) => {
    const spec: CardSpec = typeof item === "string" ? { id: item } : item;
    return {
      id: spec.id,
      kind: "text" as const,
      content: spec.content ?? "正式正文 " + spec.id,
      checked: false,
      hidden: false,
      folded: false,
      bookmarked: false,
      deleted: Boolean(spec.deleted),
      x: 0,
      y: 0,
      w: 220,
      h: 120,
      createdAt: "2026-10-08T00:00:00.000Z",
      updatedAt: "2026-10-08T00:00:00.000Z",
      meta: {},
    };
  });
  return {
    board: { id: "board_default", title: "板面" },
    state: {
      boardId: "board_default",
      seq: options.seq ?? 1,
      updatedAt: "2026-10-08T00:00:00.000Z",
      cards,
      groups: [],
      links: [],
      selection: [],
    },
    seq: options.seq ?? 1,
    baseline: null,
    submissions: [],
    drafts: { drafts: options.drafts ?? {}, updatedAt: options.updatedAt ?? null },
  };
}

function mockBoard(payload: ReturnType<typeof boardPayload>): void {
  vi.mocked(imApi.fetchBoardState).mockResolvedValue(payload as never);
}

/** 最近一次真正发给服务端的草稿集合（服务端是整份替换，「不带某个键」= 删掉它） */
function lastDraftsPayload(): Record<string, string> {
  const calls = vi.mocked(imApi.saveDrafts).mock.calls;
  return (calls[calls.length - 1]?.[1] ?? {}) as Record<string, string>;
}

function draftSaveCalls(): number {
  return vi.mocked(imApi.saveDrafts).mock.calls.length;
}

/** 一个还没结束的草稿保存请求（受控慢请求） */
let pendingSaves: Array<{ resolve: (v: unknown) => void; payload: Record<string, string> }> = [];

function pendingDraftSave() {
  const slots = {
    resolve: (_v: unknown) => {},
    payload: {} as Record<string, string>,
  };
  const promise = new Promise((resolve) => {
    slots.resolve = resolve;
  });
  vi.mocked(imApi.saveDrafts).mockImplementationOnce((_boardId: string, payload: Record<string, string>) => {
    slots.payload = payload;
    return promise as never;
  });
  pendingSaves.push(slots);
  return slots;
}

/** 「刷新」/「重开」：新的 pinia（新的 store 对象），本机存储原样保留 */
function newStore() {
  setActivePinia(createPinia());
  return useInteractiveStore();
}

/** 预置一份本机记录（比 writeCardLocalDraft 更底层：可以造旧格式、任意版本） */
function seedLocalRecord(cardId: string, record: Record<string, unknown>): void {
  localStorage.setItem(
    cardLocalDraftStorageKey(cardId),
    JSON.stringify({ updatedAt: Date.now(), seq: 1, ...record }),
  );
}

/** BoardCard 挂载所需的最小完整 props（驱动补全，与既有 d4 用例同款） */
function boardCardProps(cardId: string) {
  const card = {
    id: cardId,
    kind: "text" as const,
    content: "正式正文 " + cardId,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    x: 0,
    y: 0,
    w: 220,
    h: 120,
    createdAt: "2026-10-08T00:00:00.000Z",
    updatedAt: "2026-10-08T00:00:00.000Z",
    meta: {},
  };
  return { card, props: {
    card,
    selected: true,
    highlight: false,
    dragging: false,
    x: 0,
    y: 0,
    groupName: null,
    groups: [],
    toolbarLeft: 0,
    toolbarTop: 140,
    multi: false,
    connecting: false,
  } };
}

async function mountBoardCard(cardId: string) {
  const { card, props } = boardCardProps(cardId);
  const store = useInteractiveStore();
  const payload = boardPayload({ cards: [card] });
  // 卡片局部工具栏只在「选中且是最后点中的那张」时渲染：这里让这张卡成为工具栏持有者
  store.board = { ...payload.state, selection: [cardId] };
  const wrapper = mount(BoardCard, { props, attachTo: document.body });
  await flushPromises();
  return { wrapper, store, card };
}

beforeEach(() => {
  failWrites = false;
  pendingSaves = [];
  localStorage.clear();
  vi.resetAllMocks();
  vi.mocked(imApi.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" } as never);
  vi.mocked(imApi.fetchVisibleRange).mockResolvedValue({ visibleRange: { materials: [], notes: [] } } as never);
  vi.mocked(imApi.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false } as never);
  mockBoard(boardPayload());
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  restoreLocalStorage();
  localStorage.clear();
});

/* ==========================================================================
 * §12.1 冲突的选择不许被绕过
 * ======================================================================== */
describe("§12.1 未决冲突键按服务器事实回写", () => {
  it("【状态】未选择冲突时编辑并保存 B：A 的键按服务器事实回写，本机候选留在本机记录，冲突仍在", async () => {
    vi.useFakeTimers();
    // 本机候选 X（带版本与归属），服务器上是不同的 Y
    seedLocalRecord("A", { text: "本机候选 X", kind: "draft", version: 3, boardId: "board_default" });
    mockBoard(boardPayload({ cards: ["A", "B"], drafts: { [cardDraftKey("A")]: "服务器上的 Y" } }));
    const store = newStore();
    await store.load();

    expect(store.draftConflictFor("A"), "恢复时必须登记冲突").toEqual({
      local: "本机候选 X",
      server: "服务器上的 Y",
    });

    // 用户编辑另一张卡片 B 并保存（服务端草稿是**整份替换**）
    store.setDraft(cardKey("B"), "B 的编辑内容");
    vi.advanceTimersByTime(700);
    await flushPromises();

    const payload = lastDraftsPayload();
    expect(payload[cardDraftKey("A")], "未决冲突键不许把本机候选写上服务器").toBe("服务器上的 Y");
    expect(payload[cardDraftKey("B")]).toBe("B 的编辑内容");
    expect(readCardLocalDraft("A")?.text, "本机候选必须还留在本机记录里").toBe("本机候选 X");
    expect(store.draftConflictFor("A"), "未选择前冲突不许消失").not.toBeNull();

    // 刷新（新 store）：冲突仍在，两份都还能处理
    const reloaded = newStore();
    await reloaded.load();
    expect(reloaded.draftConflictFor("A")).toEqual({ local: "本机候选 X", server: "服务器上的 Y" });
  });

  it("【状态】服务器有一份不同文字、本机记录带正数版本号：不许直接覆盖，也不许悄悄发保存请求", async () => {
    vi.useFakeTimers();
    // 本机记录是**当前格式**（带正数版本号）：一个本地版本号不足以证明比服务器新
    seedLocalRecord("A", { text: "本机候选 X", kind: "draft", version: 7, boardId: "board_default" });
    mockBoard(boardPayload({ cards: ["A"], drafts: { [cardDraftKey("A")]: "服务器上的 Y" } }));
    const store = newStore();
    await store.load();

    expect(store.draftConflictFor("A"), "无法判定新旧的必须登记冲突").toEqual({
      local: "本机候选 X",
      server: "服务器上的 Y",
    });
    expect(store.draftFor(cardDraftKey("A"))).toBe("本机候选 X");

    // 等过防抖窗口：未决的冲突不许让 store 悄悄把本机候选（或任何覆盖）发上服务器
    vi.advanceTimersByTime(2000);
    await flushPromises();
    expect(draftSaveCalls(), "未决冲突不许自动发保存请求（自动覆盖或自动删除都不行）").toBe(0);
  });

  it("【状态】选「用本机的」：按选择把本机候选保存上去；选「用服务器上的」：编辑框跟随、本机候选清理", async () => {
    vi.useFakeTimers();
    seedLocalRecord("A", { text: "本机候选 X", kind: "draft", version: 3, boardId: "board_default" });
    mockBoard(boardPayload({ cards: ["A"], drafts: { [cardDraftKey("A")]: "服务器上的 Y" } }));
    const store = newStore();
    await store.load();
    expect(store.draftConflictFor("A")).not.toBeNull();

    // 明确选本机：之后那次保存的 payload 就是本机候选
    store.resolveDraftConflict("A", "local");
    vi.advanceTimersByTime(700);
    await flushPromises();
    expect(lastDraftsPayload()[cardDraftKey("A")], "选了本机就要按选择保存本机那份").toBe("本机候选 X");
    expect(store.draftConflictFor("A")).toBeNull();

    // 另一个实例：选服务器
    seedLocalRecord("A", { text: "本机候选 X", kind: "draft", version: 3, boardId: "board_default" });
    const store2 = newStore();
    await store2.load();
    store2.resolveDraftConflict("A", "server");
    expect(store2.draftFor(cardDraftKey("A")), "选服务器后编辑框要跟随服务器那份").toBe("服务器上的 Y");
    expect(store2.draftConflictFor("A")).toBeNull();
    expect(readCardLocalDraft("A"), "服务器上已经有这份：本机候选副本按已确认版本清理").toBeNull();
  });

  it("【状态】清理只作用于已确认的对应版本：另一页面后来写入的更新副本不许被「用服务器上的」删掉", async () => {
    seedLocalRecord("A", { text: "本机候选 X", kind: "draft", version: 3, boardId: "board_default" });
    mockBoard(boardPayload({ cards: ["A"], drafts: { [cardDraftKey("A")]: "服务器上的 Y" } }));
    const store = newStore();
    await store.load();
    expect(store.draftConflictFor("A")).not.toBeNull();

    // 同一浏览器的另一个页面在这期间写入了更新的本机副本（版本 4）
    writeCardLocalDraft("A", "别页更新的候选", { boardId: "board_default" });
    expect(readCardLocalDraft("A")?.version).toBe(4);

    store.resolveDraftConflict("A", "server");
    expect(
      readCardLocalDraft("A")?.text,
      "清理不许越过版本守卫删掉别的页面刚写下的更新副本",
    ).toBe("别页更新的候选");
  });

  it("【状态】请求在飞期间的新输入不被旧保存回执的清理误删（同浏览器多页面按记录版本校验）", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    store.setDraft(cardKey("A"), "页面一的内容");
    vi.advanceTimersByTime(700);
    await flushPromises();
    const inFlight = pendingDraftSave(); // 页面一的保存请求在飞

    // 同一浏览器的另一个页面在请求期间写入了更新的本机副本（版本 2）
    writeCardLocalDraft("A", "别页在请求期间写下的新输入", { boardId: "board_default" });

    inFlight.resolve({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" });
    await flushPromises();

    expect(
      readCardLocalDraft("A")?.text,
      "旧保存回执的清理不许删掉请求期间别的页面写下的新副本",
    ).toBe("别页在请求期间写下的新输入");
  });
});

describe("§12.1 冲突入口在真实操作路径可见可点", () => {
  it("【组件】未选择冲突时打开 A 的编辑器：提示仍在、编辑框是本机候选、服务器没有被改动", async () => {
    vi.useFakeTimers();
    seedLocalRecord("A", { text: "本机候选 X", kind: "draft", version: 3, boardId: "board_default" });
    mockBoard(boardPayload({ cards: ["A"], drafts: { [cardDraftKey("A")]: "服务器上的 Y" } }));
    const store = newStore();
    await store.load();
    expect(draftSaveCalls()).toBe(0);

    const { wrapper } = await mountBoardCard("A");
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await flushPromises();

    // 打开编辑器这个动作本身不许把冲突提示清掉
    expect(
      wrapper.find('[data-im="card-draft-conflict"]').exists(),
      "打开编辑器不等于选择本机版本：冲突提示必须还在",
    ).toBe(true);
    const editor = wrapper.find('[data-im="card-editor"]');
    expect((editor.element as HTMLTextAreaElement).value).toBe("本机候选 X");

    // 只打开编辑器：不许发出任何草稿保存请求（服务器上的 A 一点没变）
    vi.advanceTimersByTime(2000);
    await flushPromises();
    expect(draftSaveCalls(), "只打开编辑器不许触发保存").toBe(0);
    wrapper.unmount();
  });

  it("【组件】刷新恢复出冲突后，没打开编辑器的卡片上提示也可见、可选（关闭态渲染）", async () => {
    seedLocalRecord("A", { text: "本机候选 X", kind: "draft", version: 3, boardId: "board_default" });
    mockBoard(boardPayload({ cards: ["A"], drafts: { [cardDraftKey("A")]: "服务器上的 Y" } }));
    const store = newStore();
    await store.load();

    const { wrapper } = await mountBoardCard("A");
    // 没有点过「编辑」：冲突提示也必须在卡片上可见
    expect(
      wrapper.find('[data-im="card-draft-conflict"]').exists(),
      "冲突入口在没打开编辑器时也要可见（刷新后冲突仍可处理）",
    ).toBe(true);
    expect(wrapper.find('[data-im="card-draft-keep-local"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="card-draft-keep-server"]').exists()).toBe(true);

    await wrapper.find('[data-im="card-draft-keep-server"]').trigger("click");
    await flushPromises();
    expect(store.draftFor(cardDraftKey("A"))).toBe("服务器上的 Y");
    expect(store.draftConflictFor("A")).toBeNull();
    wrapper.unmount();
  });

  it("【状态】冲突未决时原样写入本机候选（打开编辑器的回写）：冲突不许消失、不许排保存", async () => {
    vi.useFakeTimers();
    seedLocalRecord("A", { text: "本机候选 X", kind: "draft", version: 3, boardId: "board_default" });
    mockBoard(boardPayload({ cards: ["A"], drafts: { [cardDraftKey("A")]: "服务器上的 Y" } }));
    const store = newStore();
    await store.load();

    // 「打开编辑器」会按当前草稿原样回写一次（BoardCard.startEdit 的真实调用）：
    // 这不等于选择本机版本 —— 冲突保持，也不许排一次把候选写上服务器的保存
    store.setDraft(cardDraftKey("A"), "本机候选 X");
    expect(store.draftConflictFor("A"), "原样回写本机候选不许把冲突清掉").toEqual({
      local: "本机候选 X",
      server: "服务器上的 Y",
    });
    vi.advanceTimersByTime(2000);
    await flushPromises();
    expect(draftSaveCalls(), "原样回写不许触发把候选写上服务器的保存").toBe(0);
  });

  it("【状态】冲突未决时用户改写出不同文字：视为用户显式接管这份草稿（冲突随这次编辑结束，新文字可保存）", async () => {
    vi.useFakeTimers();
    seedLocalRecord("A", { text: "本机候选 X", kind: "draft", version: 3, boardId: "board_default" });
    mockBoard(boardPayload({ cards: ["A"], drafts: { [cardDraftKey("A")]: "服务器上的 Y" } }));
    const store = newStore();
    await store.load();

    // 用户不理提示直接改写：这是对本卡内容的显式编辑（区别于「打开编辑器」「编辑别卡」）
    store.setDraft(cardDraftKey("A"), "用户改写的新字");
    expect(store.draftConflictFor("A"), "用户显式改写后冲突随这次编辑结束").toBeNull();
    expect(store.draftFor(cardDraftKey("A"))).toBe("用户改写的新字");

    vi.advanceTimersByTime(700);
    await flushPromises();
    expect(lastDraftsPayload()[cardDraftKey("A")], "用户写下的新文字按草稿保存").toBe("用户改写的新字");
    // 刷新后不再有冲突：服务器上已经是用户写下的这份（模拟服务器已受理这次保存）
    mockBoard(boardPayload({ cards: ["A"], drafts: { [cardDraftKey("A")]: "用户改写的新字" } }));
    const reloaded = newStore();
    await reloaded.load();
    expect(reloaded.draftConflictFor("A")).toBeNull();
    expect(reloaded.cardDraftText("A")).toBe("用户改写的新字");
  });

  it("【状态】同一页面里清除草稿、防抖未到就重新读取板面：已清除的草稿不许复活", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    store.setDraft(cardKey("A"), "要被清掉的字");
    await store.flushDrafts();
    expect(draftSaveCalls()).toBe(1);

    // 用户清除：本机留下待同步的清除依据（还没发出去）
    store.clearDraft(cardKey("A"));
    expect(readCardLocalDraft("A")?.kind).toBe("cleared");

    // 重新读取板面（真实路径：提交/审批后的重读都走这里，不关页面）
    mockBoard(boardPayload({ cards: ["A"], drafts: { [cardDraftKey("A")]: "要被清掉的字" } }));
    await store.refreshBoardFromServer();

    expect(
      store.hasCardDraft("A"),
      "已清除的草稿在同一页面的重读后复活了（恢复顺序里 cleared 的排除被内存新旧判定跳过）",
    ).toBe(false);
    expect(store.draftFor(cardKey("A"))).toBe("");
    expect(store.draftRemovalStateFor(cardKey("A")).status, "清除依据仍要等同步").toBe("pending");
  });
});

/* ==========================================================================
 * §12.2 清除依据只作用于当时的版本
 * ======================================================================== */
describe("§12.2 旧清除记录不许删除后来的新输入", () => {
  it("【状态】复现主反例：清除 A → 重新输入（本机写失败）→ 重新读取板面，内存新文字不被旧 cleared 删掉", async () => {
    vi.useFakeTimers();
    installControlledStorage();
    const store = newStore();

    // 先有一份已同步的草稿
    store.setDraft(cardKey("A"), "要清掉的旧内容");
    vi.advanceTimersByTime(700);
    await flushPromises();
    const callsAt = vi.mocked(imApi.saveDrafts).mock.calls;
    expect(callsAt[callsAt.length - 1]?.[1]?.[cardDraftKey("A")]).toBe("要清掉的旧内容");

    // 用户清除：本机留下待同步的清除依据（写入成功）
    store.clearDraft(cardDraftKey("A"));
    expect(readCardLocalDraft("A")?.kind).toBe("cleared");

    // 这次重新输入时本机存储写不进去了
    failWrites = true;
    store.setDraft(cardKey("A"), "重新输入的新文字");
    expect(store.draftFor(cardDraftKey("A")), "写失败时内存文字要保留").toBe("重新输入的新文字");
    expect(readCardLocalDraft("A")?.kind, "写失败不许破坏已有的清除依据").toBe("cleared");

    // 重新读取板面（不关页面）：内存里的新文字不许被旧的 cleared 删掉
    mockBoard(boardPayload({ cards: ["A"], drafts: { [cardDraftKey("A")]: "要清掉的旧内容" } }));
    await store.refreshBoardFromServer();

    expect(
      store.draftFor(cardDraftKey("A")),
      "内存里的新文字被旧清除依据删掉了（§12.2 处理顺序错误）",
    ).toBe("重新输入的新文字");
    expect(
      store.draftRemovalStateFor(cardDraftKey("A")).status,
      "旧清除依据已被更晚的编辑取代，不许再登记待同步删除",
    ).not.toBe("pending");
    expect(store.draftProtectionStatus("A").local, "本机写失败的失败状态要保留").toBe("failed");

    // 之后保存：发出去的是新文字（不是「不带键」的删除）
    vi.mocked(imApi.saveDrafts).mockRejectedValueOnce(new Error("先不发给服务器"));
    await store.flushDrafts();
    expect(
      lastDraftsPayload()[cardDraftKey("A")],
      "恢复后的新输入必须按草稿保存，不许被当成清除",
    ).toBe("重新输入的新文字");
  });

  it("【状态】别板面的清除依据不作用于本板面", async () => {
    vi.useFakeTimers();
    seedLocalRecord("A", { text: "", kind: "cleared", version: 2, boardId: "board_other" });
    mockBoard(boardPayload({ cards: ["A"], drafts: { [cardDraftKey("A")]: "本板服务器草稿" } }));
    const store = newStore();
    await store.load();

    expect(store.draftFor(cardDraftKey("A")), "别板面的清除依据把本板的服务器草稿删了").toBe("本板服务器草稿");
    expect(store.draftRemovalStateFor(cardDraftKey("A")).status, "别板面的清除不许在本板登记待同步删除").toBe("idle");
    vi.advanceTimersByTime(2000);
    await flushPromises();
    expect(draftSaveCalls(), "别板面的清除依据不许触发本板的删除请求").toBe(0);
  });

  it("【状态】旧格式（无版本号）的清除记录同样不删它之后的新输入", async () => {
    vi.useFakeTimers();
    installControlledStorage();
    // 旧版写的清除记录：没有 version、没有归属
    seedLocalRecord("A", { text: "", kind: "cleared", seq: 9 });
    const store = newStore();

    failWrites = true; // 这次新输入本机写失败：内存里有字，持久层还是那条旧 cleared
    store.setDraft(cardKey("A"), "清除之后的新输入");

    mockBoard(boardPayload({ cards: ["A"], drafts: { [cardDraftKey("A")]: "服务器上的旧草稿" } }));
    await store.refreshBoardFromServer();

    expect(store.draftFor(cardDraftKey("A")), "旧格式清除记录删掉了它之后的新输入").toBe("清除之后的新输入");
    expect(store.draftRemovalStateFor(cardDraftKey("A")).status).not.toBe("pending");
  });

  it("【状态·回归】在飞的旧清除回执不清理后来的本地副本（按记录版本校验）", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    store.setDraft(cardKey("A"), "旧内容");
    vi.advanceTimersByTime(700);
    await flushPromises();

    store.clearDraft(cardDraftKey("A"));
    const inFlight = pendingDraftSave(); // 清除请求在飞（payload 不带这个键）
    expect(Object.keys(inFlight.payload)).not.toContain(cardDraftKey("A"));

    // 同一浏览器的另一个页面在清除确认前写入了新输入
    writeCardLocalDraft("A", "别页后来写下的新输入", { boardId: "board_default" });

    inFlight.resolve({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" });
    await flushPromises();

    expect(
      readCardLocalDraft("A")?.text,
      "旧清除回执的确认不许删掉后来写下的本地副本",
    ).toBe("别页后来写下的新输入");
  });
});
