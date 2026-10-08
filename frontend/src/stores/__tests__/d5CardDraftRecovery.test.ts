/**
 * 独立验收 D5-A：卡片草稿的恢复补齐与失败处理（契约 §11.1 / §11.2 / §11.3）。
 *
 * 每条用例都按「用户能观察到的正确行为」断言，覆盖本轮验收清单：
 * 1. 第一次编辑、防抖（600ms）还没到就刷新 → 新 store 重读必须能恢复；
 * 2. 本机有记录而服务器没有这份草稿 → 也要能发现并恢复；
 * 3. 用户把正文删空后的草稿是**存在**的草稿（按记录是否存在判断，不看字符串空不空）；
 * 4. 服务器返回慢时不覆盖恢复期间用户新输入的文字；
 * 5. 对应卡片已删除（或不存在）时不恢复；
 * 6. 清除后刷新不复活；
 * 7. 清除最后一份草稿仍然发出同步请求；
 * 8. 在飞的旧保存晚到不复活已清除的草稿；
 * 9. 清除之后又编辑的新版本不被旧清除删掉；
 * 10. 本机恢复记录写入失败要可见、可重试；服务器已成功时不许显示成服务器失败；
 * 11. A 卡片的本地新文字不被 B 的保存时间（或整个草稿集合的更新时间）否掉；
 * 12. 无法按记录判定新旧的冲突：两份都保留，给用户明确选择。
 *
 * 标注：【状态】只依赖 store 公开状态与动作 ·【本机存储】直接读写 localStorage 预置记录 ·
 *      【组件】挂载真实组件 ·【模拟失败】受控拒绝/受控慢请求。
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
  listLocalCardDraftIds,
  readCardLocalDraft,
  readDraft,
  writeCardLocalDraft,
} from "../../interactive/drafts";
import CardDraftHint from "../../components/interactive/CardDraftHint.vue";

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

/** 存储不可用（读写都抛）——隐私模式 / 企业策略下的真实形态 */
function breakLocalStorage(message: string): void {
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: () => { throw new Error(message); },
      setItem: () => { throw new Error(message); },
      removeItem: () => { throw new Error(message); },
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

/** 最近一次真正发给服务端的草稿集合（服务端是整份替换，所以「不带某个键」就是删除它） */
function lastDraftsPayload(): Record<string, string> {
  const calls = vi.mocked(imApi.saveDrafts).mock.calls;
  return (calls[calls.length - 1]?.[1] ?? {}) as Record<string, string>;
}

function draftSaveCalls(): number {
  return vi.mocked(imApi.saveDrafts).mock.calls.length;
}

/** 一个还没结束的草稿保存请求（受控慢请求） */
let pendingSaves: Array<{ resolve: (v: unknown) => void; reject: (e: unknown) => void; payload: Record<string, string> }> = [];

function pendingDraftSave() {
  const slots = {
    resolve: (_v: unknown) => {},
    reject: (_e: unknown) => {},
    payload: {} as Record<string, string>,
  };
  const promise = new Promise((resolve, reject) => {
    slots.resolve = resolve;
    slots.reject = reject;
  });
  vi.mocked(imApi.saveDrafts).mockImplementationOnce((_boardId: string, payload: Record<string, string>) => {
    slots.payload = payload;
    return promise as never;
  });
  pendingSaves.push(slots);
  return slots;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

/** 「刷新」/「重开」：新的 pinia（新的 store 对象），本机存储原样保留 */
function newStore() {
  setActivePinia(createPinia());
  return useInteractiveStore();
}

function cardContent(store: ReturnType<typeof useInteractiveStore>, cardId: string): string {
  const cards = (store.board?.cards ?? []) as Array<{ id: string; content: string }>;
  return cards.find((card) => card.id === cardId)?.content ?? "";
}

beforeEach(() => {
  localStorage.clear();
  pendingSaves = [];
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
 * §11.1 首次输入必须能恢复：只存在于本机的记录也要被发现
 * ======================================================================== */
describe("§11.1 首次输入在防抖之前刷新也能恢复", () => {
  it("【状态】第一次编辑、600ms 防抖还没到就刷新：新 store 必须能恢复这段文字", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    store.setDraft(cardKey("c1"), "第一次编辑还没到防抖");
    vi.advanceTimersByTime(300);
    // 防抖没到点：一个字都没发给服务器
    expect(draftSaveCalls(), "防抖还没到就发请求，用例前提不成立").toBe(0);

    // 「刷新」：新的 store（同一份本机存储），服务器上**没有**这份草稿
    mockBoard(boardPayload({ cards: ["c1"] }));
    const reloaded = newStore();
    await reloaded.load();

    expect(
      reloaded.draftFor(cardKey("c1")),
      "第一次输入在刷新后丢了：恢复只看了服务器/内存里已有的键，没枚举本机记录",
    ).toBe("第一次编辑还没到防抖");
    expect(reloaded.hasCardDraft("c1")).toBe(true);
    expect(reloaded.cardDraftText("c1")).toBe("第一次编辑还没到防抖");
    // 恢复只回到**编辑草稿**：正式内容一点没变，也没有自动保存板面/提交
    expect(cardContent(reloaded, "c1")).toBe("正式正文 c1");
    expect(imApi.saveBoardState).not.toHaveBeenCalled();
    expect(imApi.submitBoard).not.toHaveBeenCalled();
    // 恢复出来的内容会真的被存回服务器（否则下次刷新又没了）
    await vi.advanceTimersByTimeAsync(700);
    expect(lastDraftsPayload()[cardKey("c1")]).toBe("第一次编辑还没到防抖");
  });

  it("【本机存储】本机有记录、服务器没有：只按记录是否存在也要能发现（含空正文）", async () => {
    // 直接预置本机记录（相当于上一次运行留下的恢复副本；服务器上从来没有这份草稿）
    writeCardLocalDraft("c2", "只在本机的一份", { boardId: "board_default", seq: 1 });
    expect(listLocalCardDraftIds()).toContain("c2");

    mockBoard(boardPayload({ cards: ["c2"] }));
    const store = newStore();
    await store.load();
    expect(store.draftFor(cardKey("c2"))).toBe("只在本机的一份");
    expect(store.cardDraftText("c2")).toBe("只在本机的一份");
  });

  it("【本机存储】内容被删空的草稿是「存在」的草稿：恢复后输入框为空，不是旧正文", async () => {
    writeCardLocalDraft("c3", "", { boardId: "board_default", seq: 1 });
    const storageKey = cardLocalDraftStorageKey("c3");
    expect(readDraft(storageKey), "空正文也必须留下记录").not.toBeNull();

    mockBoard(boardPayload({ cards: ["c3"] }));
    const store = newStore();
    await store.load();

    expect(store.hasCardDraft("c3"), "空草稿被当成「没有草稿」了").toBe(true);
    expect(store.cardDraftText("c3")).toBe("");
    expect(store.draftFor(cardKey("c3"))).toBe("");
    expect(cardContent(store, "c3")).toBe("正式正文 c3");
  });

  it("【状态】恢复期间用户继续输入：服务器返回慢也不许覆盖新输入的文字", async () => {
    writeCardLocalDraft("c4", "上次留下的文字", { boardId: "board_default", seq: 1 });
    const slow = deferred<ReturnType<typeof boardPayload>>();
    vi.mocked(imApi.fetchBoardState).mockReturnValue(slow.promise as never);

    const store = newStore();
    const loading = store.refreshBoardFromServer();
    // 服务器还没返回：用户已经在同一张卡片上继续输入
    store.setDraft(cardKey("c4"), "用户在恢复期间新输入的文字");
    slow.resolve(boardPayload({ cards: ["c4"], drafts: { [cardDraftKey("c4")]: "服务器上的旧文字" } }));
    await loading;

    expect(store.draftFor(cardKey("c4")), "迟到的服务器返回值盖掉了用户新输入的文字").toBe(
      "用户在恢复期间新输入的文字",
    );
  });

  it("【状态】对应卡片已删除（或不存在）时不恢复，也不留下会误恢复的记录", async () => {
    writeCardLocalDraft("c5", "这张卡片已经被删掉了", { boardId: "board_default", seq: 1 });
    writeCardLocalDraft("c6", "这张卡片服务器上根本没有", { boardId: "board_default", seq: 1 });

    mockBoard(boardPayload({ cards: [{ id: "c5", deleted: true }] }));
    const store = newStore();
    await store.load();

    expect(store.hasCardDraft("c5")).toBe(false);
    expect(store.draftFor(cardDraftKey("c5"))).toBe("");
    expect(store.hasCardDraft("c6")).toBe(false);
    expect(readCardLocalDraft("c5"), "已删除对象的记录留着会误恢复").toBeNull();
  });

  it("【状态】属于别的板面的本机记录不许在当前板面恢复", async () => {
    writeCardLocalDraft("c7", "别的板面留下的内容", { boardId: "board_other", seq: 1 });
    mockBoard(boardPayload({ cards: ["c7"] }));
    const store = newStore();
    await store.load();
    expect(store.hasCardDraft("c7")).toBe(false);
    expect(store.draftFor(cardDraftKey("c7"))).toBe("");
    // 别的板面的恢复数据不能被这次加载删掉
    expect(readCardLocalDraft("c7")?.text).toBe("别的板面留下的内容");
  });

  it("【状态】正常关闭重开（新 store、本机存储还在）同样能恢复", async () => {
    vi.useFakeTimers();
    const first = useInteractiveStore();
    first.setDraft(cardKey("c8"), "关掉之前的未完成输入");
    // 还没有任何请求发出（模拟「关闭时防抖还没到、pagehide 的异步请求也没完成」）
    expect(draftSaveCalls()).toBe(0);

    mockBoard(boardPayload({ cards: ["c8"] }));
    const reopened = newStore();
    await reopened.load();
    expect(reopened.draftFor(cardKey("c8"))).toBe("关掉之前的未完成输入");
  });
});

/* ==========================================================================
 * §11.2 清除必须同步：清除是一项待确认的变化
 * ======================================================================== */
describe("§11.2 清除草稿必须同步到服务器", () => {
  it("【状态】清除后刷新不复活；清除真的发出去了", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    store.setDraft(cardKey("c1"), "要被清掉的字");
    await store.flushDrafts();
    expect(draftSaveCalls()).toBe(1);
    expect(readCardLocalDraft("c1")).toBeNull(); // 服务器确认后本机副本按对象清理

    store.clearDraft(cardKey("c1"));
    expect(store.hasCardDraft("c1")).toBe(false);
    // 本机留下删除依据：刷新后仍然知道要清
    expect(readCardLocalDraft("c1")?.kind).toBe("cleared");

    // 「刷新」：服务器上那份旧记录还在（请求失败/还没到）
    mockBoard(boardPayload({ cards: ["c1"], drafts: { [cardDraftKey("c1")]: "要被清掉的字" } }));
    const reloaded = newStore();
    await reloaded.load();

    expect(reloaded.hasCardDraft("c1"), "清掉的草稿刷新后又复活了").toBe(false);
    expect(reloaded.draftFor(cardDraftKey("c1"))).toBe("");
    await vi.advanceTimersByTimeAsync(700);
    expect(draftSaveCalls()).toBeGreaterThanOrEqual(2);
    expect(lastDraftsPayload(), "清除没有随同步请求发出").not.toHaveProperty(cardDraftKey("c1"));
    // 服务器确认之前不许标成已同步
    expect(reloaded.draftStateFor(cardDraftKey("c1")).status).not.toBe("saved");
  });

  it("【状态】清空最后一份草稿也要发出请求（payload 是剩余集合 = 空集）", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    store.setDraft(cardKey("c1"), "唯一一份草稿");
    await store.flushDrafts();
    expect(draftSaveCalls()).toBe(1);

    store.clearDraft(cardKey("c1"));
    await vi.advanceTimersByTimeAsync(700);
    expect(draftSaveCalls(), "清空最后一份草稿后没有任何请求，服务器上的旧记录还在").toBe(2);
    expect(lastDraftsPayload()).toEqual({});
  });

  it("【状态】在飞的旧保存晚到时不许复活已清除的草稿", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    pendingDraftSave();
    store.setDraft(cardKey("c1"), "在飞的那一版");
    vi.advanceTimersByTime(700);
    await flushPromises();
    expect(draftSaveCalls()).toBe(1);

    // 请求还在飞：用户清掉了这份草稿
    store.clearDraft(cardKey("c1"));
    pendingSaves[0].resolve({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" });
    await flushPromises();
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();

    expect(store.hasCardDraft("c1"), "旧保存的回执把已清除的草稿复活了").toBe(false);
    expect(store.draftFor(cardKey("c1"))).toBe("");
    // 旧请求落库之后，清除必须真的补发一次
    expect(draftSaveCalls(), "清除没有补发请求").toBeGreaterThanOrEqual(2);
    expect(lastDraftsPayload()).not.toHaveProperty(cardDraftKey("c1"));
  });

  it("【状态】清除之后再次编辑的新版本不被旧清除删掉", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    store.setDraft(cardKey("c1"), "旧内容");
    await store.flushDrafts();
    expect(readCardLocalDraft("c1"), "服务器确认后本机副本应该已清理").toBeNull();

    // 受控的清除请求：它针对的是「旧内容」这一版
    pendingDraftSave();
    store.clearDraft(cardKey("c1"));
    vi.advanceTimersByTime(700);
    await flushPromises();
    expect(pendingSaves[0].payload).toEqual({});

    // 清除还在飞：用户又编辑了新版本（新版本必须取代这次清除）
    store.setDraft(cardKey("c1"), "清除之后的新内容");
    // 新版本的保存也受控，便于在「旧清除确认之后、新版本落库之前」检查本机记录
    pendingDraftSave();

    // 旧清除的确认晚到
    pendingSaves[0].resolve({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" });
    await flushPromises();
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();

    expect(store.draftFor(cardKey("c1"))).toBe("清除之后的新内容");
    expect(store.hasCardDraft("c1")).toBe(true);
    expect(
      readCardLocalDraft("c1")?.text,
      "旧版本的清除确认把后来新建的本机记录删掉了",
    ).toBe("清除之后的新内容");
    expect(pendingSaves[1]?.payload[cardDraftKey("c1")]).toBe("清除之后的新内容");

    pendingSaves[1]?.resolve({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" });
    await flushPromises();
    expect(store.draftStateFor(cardKey("c1")).status).toBe("saved");
    expect(readCardLocalDraft("c1"), "新版本落库后本机副本才按对象清理").toBeNull();
  });

  it("【状态】清除同步失败：准确状态 + 本机保留删除依据 + 可以重试", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    store.setDraft(cardKey("c1"), "要清掉的字");
    await store.flushDrafts();

    vi.mocked(imApi.saveDrafts).mockRejectedValueOnce(new Error("清除没有同步上"));
    store.clearDraft(cardKey("c1"));
    await vi.advanceTimersByTimeAsync(700);

    const failed = store.draftRemovalStateFor(cardDraftKey("c1"));
    expect(failed.status, "清除失败不能静默").toBe("error");
    expect(failed.error).toContain("清除没有同步上");
    expect(readCardLocalDraft("c1")?.kind, "失败后本机必须留下删除依据").toBe("cleared");

    const callsBeforeRetry = draftSaveCalls();
    await store.retryDraftSave(cardDraftKey("c1"));
    expect(store.draftRemovalStateFor(cardDraftKey("c1")).status).toBe("idle");
    expect(readCardLocalDraft("c1"), "服务器确认后删除依据才清掉").toBeNull();
    expect(draftSaveCalls(), "重试没有真的再发一次").toBeGreaterThan(callsBeforeRetry);
    expect(lastDraftsPayload()).not.toHaveProperty(cardDraftKey("c1"));
  });
});

/* ==========================================================================
 * §11.3 本地保护失败要可见，时间判断要按记录
 * ======================================================================== */
describe("§11.3 本机保护失败与按记录判断新旧", () => {
  it("【状态】本机写入失败：内容保留、状态可见、重试能补上本机那一路", async () => {
    breakLocalStorage("本地存储被禁用");
    const store = useInteractiveStore();
    store.setDraft(cardKey("c1"), "本机写不进去的字");

    const status = store.draftProtectionStatus("c1");
    expect(status.local).toBe("failed");
    expect(status.error).toContain("本地存储被禁用");
    // 内存里的内容一个字都不许丢
    expect(store.draftFor(cardKey("c1"))).toBe("本机写不进去的字");

    // 服务器也没存上：两件事都要如实说明，而不是把本机失败说成服务器失败
    vi.mocked(imApi.saveDrafts).mockRejectedValueOnce(new Error("草稿接口 500"));
    await store.flushDrafts();
    const afterBoth = store.draftProtectionStatus("c1");
    expect(afterBoth.server).toBe("error");
    expect(afterBoth.error).toContain("草稿接口 500");

    // 本机存储恢复 + 服务器正常：重试后本机与服务器都补上
    restoreLocalStorage();
    await store.retryDraftSave(cardKey("c1"));
    expect(store.draftProtectionStatus("c1").local).toBe("ok");
    expect(store.draftStateFor(cardKey("c1")).status).toBe("saved");
    expect(readCardLocalDraft("c1")).toBeNull(); // 服务器确认后按对象清理本机副本
  });

  it("【状态】服务器已成功时不许显示成服务器保存失败", async () => {
    breakLocalStorage("本地存储被禁用");
    const store = useInteractiveStore();
    store.setDraft(cardKey("c1"), "服务器存上了，本机没写进去");
    await store.flushDrafts();

    const status = store.draftProtectionStatus("c1");
    expect(status.server, "服务器明明成功了").toBe("saved");
    expect(store.draftStateFor(cardKey("c1")).status).toBe("saved");
    expect(store.draftSaveStatus, "本机失败被显示成服务器保存失败").not.toBe("error");
    expect(store.draftSaveError).toBeNull();
  });

  it("【状态】A 卡片的本地新文字不被 B 的保存时间（整个草稿集合的更新时间）否掉", async () => {
    const store = useInteractiveStore();
    store.setDraft(cardKey("A"), "A 在本机的新文字");
    expect(draftSaveCalls()).toBe(0);

    // 「刷新」：服务器上 A 是旧文字，B 更晚保存导致**整个草稿集合的更新时间**比 A 的本机记录晚
    mockBoard(
      boardPayload({
        cards: ["A", "B"],
        drafts: { [cardDraftKey("A")]: "服务器上 A 的旧文字", [cardDraftKey("B")]: "B 保存的内容" },
        updatedAt: new Date(Date.now() + 3_600_000).toISOString(),
      }),
    );
    const reloaded = newStore();
    await reloaded.load();

    expect(
      reloaded.draftFor(cardDraftKey("A")),
      "A 的本机新文字被 B 的保存时间否掉了（用了整个草稿集合的更新时间）",
    ).toBe("A 在本机的新文字");
  });

  it("【状态】无法按记录判定新旧时：两份都保留，用户选择后按选择继续", async () => {
    // 旧格式的本机记录：没有归属/版本/种类，按它自己无法判断和服务器那份谁新
    const storageKey = cardLocalDraftStorageKey("c1");
    localStorage.setItem(storageKey, JSON.stringify({ text: "无法判定的本机内容", updatedAt: Date.now(), seq: 4 }));

    mockBoard(boardPayload({ cards: ["c1"], drafts: { [cardDraftKey("c1")]: "服务器上的内容" } }));
    const store = newStore();
    await store.load();

    expect(store.draftFor(cardDraftKey("c1"))).toBe("无法判定的本机内容");
    expect(store.draftConflictFor("c1")).toEqual({ local: "无法判定的本机内容", server: "服务器上的内容" });

    store.resolveDraftConflict("c1", "server");
    expect(store.draftFor(cardDraftKey("c1"))).toBe("服务器上的内容");
    expect(store.draftConflictFor("c1")).toBeNull();
  });
});

/* ==========================================================================
 * 组件级：提示要能区分两种失败，并能做出选择/重试
 * ======================================================================== */
describe("CardDraftHint：本机保护失败与冲突选择", () => {
  it("【组件】本机写失败且服务器未保存：如实说明恢复能力 + 重试入口", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    breakLocalStorage("本地存储被禁用");
    const store = useInteractiveStore();
    store.setDraft(cardKey("c9"), "写不进本机的字");

    const wrapper = mount(CardDraftHint, { props: { cardId: "c9" }, global: { plugins: [pinia] } });
    await flushPromises();

    expect(wrapper.find('[data-im="card-draft-hint"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="card-draft-local-error"]').exists()).toBe(true);
    expect(wrapper.text()).toContain("本地存储被禁用");
    expect(wrapper.text()).toContain("重试");
    // 不许把「内容仍在当前页面」写成「关闭后一定能恢复」
    expect(wrapper.text()).not.toContain("一定能恢复");
    expect(wrapper.text()).not.toContain("已保存");

    // 存储恢复后点重试：本机那一路真的补上了（即使服务器这一次仍然失败，也不许假装成功）
    restoreLocalStorage();
    vi.mocked(imApi.saveDrafts).mockRejectedValueOnce(new Error("服务器还没好"));
    await wrapper.find('[data-im="card-draft-retry"]').trigger("click");
    await flushPromises();
    expect(readCardLocalDraft("c9"), "重试没有补上本机恢复副本").not.toBeNull();
    expect(store.draftProtectionStatus("c9").local).toBe("ok");
    expect(store.draftStateFor(cardDraftKey("c9")).status, "服务器这次仍然失败，不许显示成已保存").toBe("error");

    // 服务器恢复后再重试一次：两边都成功
    await store.retryDraftSave(cardDraftKey("c9"));
    expect(store.draftStateFor(cardDraftKey("c9")).status).toBe("saved");
    wrapper.unmount();
  });

  it("【组件】清除没同步成功：说清「还没同步」，不假装已清除，并可重试", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    vi.useFakeTimers();
    const store = useInteractiveStore();
    store.setDraft(cardDraftKey("c1"), "要清掉的字");
    await store.flushDrafts();
    vi.mocked(imApi.saveDrafts).mockRejectedValueOnce(new Error("清除没有同步上"));
    store.clearDraft(cardDraftKey("c1"));
    await vi.advanceTimersByTimeAsync(700);

    const wrapper = mount(CardDraftHint, { props: { cardId: "c1" }, global: { plugins: [pinia] } });
    await flushPromises();
    expect(wrapper.find('[data-im="card-draft-removal-error"]').exists()).toBe(true);
    expect(wrapper.text()).toContain("清除没有同步上");
    expect(wrapper.text(), "服务器还没确认，不许说已清除").not.toContain("已清除");
    expect(wrapper.find('[data-im="card-draft-retry"]').exists()).toBe(true);

    await wrapper.find('[data-im="card-draft-retry"]').trigger("click");
    await flushPromises();
    expect(store.draftRemovalStateFor(cardDraftKey("c1")).status).toBe("idle");
    expect(readCardLocalDraft("c1")).toBeNull();
    wrapper.unmount();
  });

  it("【组件】冲突时两份都保留，选「用服务器上的」后界面按选择继续", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    localStorage.setItem(
      cardLocalDraftStorageKey("c1"),
      JSON.stringify({ text: "本机那份", updatedAt: Date.now(), seq: 2 }),
    );
    mockBoard(boardPayload({ cards: ["c1"], drafts: { [cardDraftKey("c1")]: "服务器那份" } }));
    const store = useInteractiveStore();
    await store.load();

    const wrapper = mount(CardDraftHint, { props: { cardId: "c1" }, global: { plugins: [pinia] } });
    await flushPromises();
    expect(wrapper.find('[data-im="card-draft-keep-local"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="card-draft-keep-server"]').exists()).toBe(true);

    await wrapper.find('[data-im="card-draft-keep-server"]').trigger("click");
    await flushPromises();
    expect(store.draftFor(cardDraftKey("c1"))).toBe("服务器那份");
    expect(store.draftConflictFor("c1")).toBeNull();
    wrapper.unmount();
  });
});
