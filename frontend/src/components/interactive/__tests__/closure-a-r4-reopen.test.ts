/**
 * [closure-A / 反例 R4] 组件级红→绿：选服务器稿 → 本机副本删除失败 → 存储恢复 → 点真实重试 → 关闭重开。
 *
 * 反例 R4（基线实际行为）：重试写下一条 kind=cleared 记录（整份草稿清除的依据）。
 * 重开后用户选择保留的服务器稿变空、草稿集合不再含该键，服务器那份也会被删除。
 *
 * 正确行为（本文件断言）：这次重试的目的是「删本机冗余副本」——只删副本、**绝不写 cleared 依据**；
 * 重开后服务器稿、输入框内容、请求集合都保留正确正文。
 *
 * 标注：【组件/DOM + 真实 store + 真实 localStorage】挂载真实 BoardCard.vue（内部渲染真实
 * CardDraftHint.vue），点的是组件里真实按钮；「关闭重开」= 同一份本机存储 + 新建 pinia/store
 * 实例（真进程关闭重开由验收角色 D 负责，这里如实标注为组件级等价物）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import {
  cardDraftKey,
  cardLocalDraftStorageKey,
  hasCardLocalClear,
  readCardLocalDraft,
  writeCardLocalDraft,
} from "../../../interactive/drafts";
import { emptyBoardState, type BoardCard as BoardCardType, type BoardStateResponse } from "../../../interactive/types";
import * as imApi from "../../../services/interactive";

vi.mock("../../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  fetchVisibleRange: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
  approveIntent: vi.fn(),
  rejectIntent: vi.fn(),
  batchDecide: vi.fn(),
  createDemoIntents: vi.fn(),
  advanceIntent: vi.fn(),
  updateIntentPreview: vi.fn(),
}));

const CARD_ID = "c1";
const CARD_KEY = "card:c1";
/** 本机那份候选（用户上次没成功同步的编辑） */
const LOCAL = "本机那份候选";
/** 服务器那份草稿（用户明确选择要保留它） */
const SERVER = "服务器那份草稿，用户选择了保留";

function textCard(): BoardCardType {
  return {
    id: CARD_ID,
    kind: "text",
    x: 40,
    y: 40,
    w: 240,
    h: 140,
    content: "卡片正式正文",
    checked: true,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "2026-10-08T00:00:00.000Z",
    updatedAt: "2026-10-08T00:00:00.000Z",
  } as unknown as BoardCardType;
}

/** 受控的「服务端」：草稿整份替换保存（未列出的键会被删掉），与真实接口语义一致 */
function fakeServer(initial: Record<string, string>) {
  const drafts: Record<string, string> = { ...initial };
  let updatedAt = "2026-10-08T00:00:00.000Z";
  vi.mocked(imApi.fetchBoardState).mockImplementation(async (): Promise<BoardStateResponse> => ({
    board: { id: "board_default", title: "板面" },
    state: { ...emptyBoardState("board_default"), seq: 1, cards: [textCard()], selection: [CARD_ID] },
    seq: 1,
    baseline: null,
    submissions: [],
    drafts: { drafts: { ...drafts }, updatedAt },
  } as unknown as BoardStateResponse));
  vi.mocked(imApi.saveDrafts).mockImplementation(async (_boardId: string, next: Record<string, string>) => {
    for (const key of Object.keys(drafts)) delete drafts[key];
    Object.assign(drafts, next);
    updatedAt = new Date().toISOString();
    return { drafts: { ...drafts }, updatedAt } as never;
  });
  vi.mocked(imApi.fetchVisibleRange).mockResolvedValue({ visibleRange: null } as never);
  vi.mocked(imApi.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false } as never);
  return { drafts };
}

const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");

/** 记忆型存储：跨「关闭重开」保留，可随时开关某个键的写入/删除失败（模拟配额满、策略禁用） */
function installStorage(): {
  map: Map<string, string>;
  failSet: (fn: ((key: string) => Error | null) | null) => void;
  failRemove: (fn: ((key: string) => Error | null) | null) => void;
} {
  let failSet: ((key: string) => Error | null) | null = null;
  let failRemove: ((key: string) => Error | null) | null = null;
  const map = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      get length() {
        return map.size;
      },
      key(index: number) {
        return Array.from(map.keys())[index] ?? null;
      },
      getItem(key: string) {
        return map.has(key) ? (map.get(key) as string) : null;
      },
      setItem(key: string, value: string) {
        const failure = failSet?.(key);
        if (failure) throw failure;
        map.set(key, value);
      },
      removeItem(key: string) {
        const failure = failRemove?.(key);
        if (failure) throw failure;
        map.delete(key);
      },
      clear() {
        map.clear();
      },
    },
  });
  return {
    map,
    failSet: (fn) => {
      failSet = fn;
    },
    failRemove: (fn) => {
      failRemove = fn;
    },
  };
}

function quotaError(): Error {
  const err = new Error("quota");
  err.name = "QuotaExceededError";
  return err;
}

/** 旧版本写下的本机记录：没有 kind 字段（来源/种类不可判定） */
function writeLegacyRecord(cardId: string, text: string, version: number): void {
  localStorage.setItem(
    cardLocalDraftStorageKey(cardId),
    JSON.stringify({ text, updatedAt: Date.now(), seq: 1, version }),
  );
}

function mountCard() {
  return mount(BoardCard, {
    attachTo: document.body,
    props: {
      card: textCard(),
      selected: true,
      highlight: false,
      dragging: false,
      x: 40,
      y: 40,
      groupName: null,
      groups: [],
      toolbarLeft: 40,
      toolbarTop: 200,
      multi: false,
      connecting: false,
    },
  });
}

/** 按真实路径造出未决冲突：本机恢复记录与服务器草稿正文不同 → 两份都保留等用户选择 */
async function mountWithConflict() {
  const store = useInteractiveStore();
  store.board = { ...emptyBoardState("board_default"), cards: [textCard()], selection: [CARD_ID] };
  writeCardLocalDraft(CARD_ID, LOCAL, { boardId: "board_default", seq: 1 });
  const wrapper = mountCard();
  await store.refreshBoardFromServer();
  await wrapper.vm.$nextTick();
  return { wrapper, store };
}

/** 关闭重开（组件级等价物）：同一份本机存储，新建 pinia + store 实例后重新加载 */
async function reopen() {
  setActivePinia(createPinia());
  const store = useInteractiveStore();
  await store.load();
  await flushPromises();
  return store;
}

function editorValue(wrapper: ReturnType<typeof mount>): string {
  return (wrapper.find('[data-im="card-editor"]').element as HTMLTextAreaElement).value;
}

beforeEach(() => {
  installStorage();
  vi.resetAllMocks();
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = "";
  if (originalDescriptor) Object.defineProperty(globalThis, "localStorage", originalDescriptor);
});

describe("[R4] 选服务器稿 → 本机副本删除失败 → 恢复后重试 → 关闭重开", () => {
  /**
   * 走完整真实路径：编辑器里的冲突 → 点「用服务器上的」（真实按钮）→ 本机副本删除失败 →
   * 存储恢复 → 点真实「重试」按钮。返回现场供各用例断言。
   */
  async function runFailedRemovalThenRetry() {
    const { failRemove } = installStorage();
    const server = fakeServer({ [CARD_KEY]: SERVER });
    const { wrapper, store } = await mountWithConflict();

    // 编辑器打开着：冲突入口在编辑器上方可见可点（§12.1）
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await wrapper.vm.$nextTick();
    expect(store.draftConflictFor(CARD_ID), "基线路径没造出冲突，本用例无效").toEqual({ local: LOCAL, server: SERVER });
    expect(wrapper.find('[data-im="card-draft-keep-server"]').exists()).toBe(true);

    // 删除本机副本这次临时失败（配额满 / 存储被禁用）
    failRemove((key) => (key === cardLocalDraftStorageKey(CARD_ID) ? quotaError() : null));
    await wrapper.find('[data-im="card-draft-keep-server"]').trigger("click");
    await wrapper.vm.$nextTick();

    expect(store.draftConflictFor(CARD_ID), "选服务器后冲突应当结束").toBeNull();
    expect(store.cardDraftText(CARD_ID), "选服务器后草稿应当是服务器那份").toBe(SERVER);
    const retryButton = wrapper.find('[data-im="card-draft-local-removal-retry"]');
    expect(retryButton.exists(), "本机副本删除失败必须给出真实重试入口").toBe(true);

    // 存储恢复后点真实「重试」按钮
    failRemove(null);
    await retryButton.trigger("click");
    await flushPromises();
    return { wrapper, store, server };
  }

  it("【组件/DOM】重试只删本机冗余副本：磁盘上不许出现 cleared 依据", async () => {
    const { wrapper } = await runFailedRemovalThenRetry();

    expect(wrapper.find('[data-im="card-draft-local-removal-error"]').exists(), "重试成功后错误提示应当消失").toBe(false);
    // ★反例 R4 的核心断言：这是一次「删本机冗余副本」，不是「整份草稿清除」
    expect(readCardLocalDraft(CARD_ID), "本机冗余副本应当被删掉（基线：留下了一条 kind=cleared）").toBeNull();
    expect(localStorage.getItem(cardLocalDraftStorageKey(CARD_ID)), "存储键上不许留下任何记录").toBeNull();
    expect(hasCardLocalClear(CARD_ID), "★重试不许把它写成整份草稿清除的依据（R4）").toBe(false);
  });

  it("【组件/DOM + 关闭重开】重开后服务器稿、输入框、请求集合都保留正确正文", async () => {
    const { wrapper, server } = await runFailedRemovalThenRetry();
    wrapper.unmount();

    // 关闭重开（组件级等价物）：同一份本机存储 + 新建 pinia/store 实例
    const reopened = await reopen();
    expect(reopened.cardDraftText(CARD_ID), "★重开后用户选择保留的服务器稿必须还在").toBe(SERVER);
    expect(reopened.draftConflictFor(CARD_ID), "重开后不该又冒出冲突").toBeNull();
    expect(reopened.draftRemovalStateFor(CARD_KEY).status, "重开后不该产生整份清除的待同步项").toBe("idle");

    // 真实输入框：重开后打开编辑，正文必须是服务器那份
    const reopenedWrapper = mountCard();
    await reopenedWrapper.find('[data-im="card-edit"]').trigger("click");
    expect(editorValue(reopenedWrapper), "★重开后真实输入框必须显示服务器那份正文").toBe(SERVER);

    await reopened.flushDrafts();
    await flushPromises();

    expect(server.drafts[CARD_KEY], "★服务器那份被用户保留的草稿不许被删").toBe(SERVER);
    expect(Object.keys(server.drafts), "服务器草稿集合仍应含该键").toEqual([CARD_KEY]);
    for (const payload of vi.mocked(imApi.saveDrafts).mock.calls.map((call) => call[1])) {
      expect(payload[CARD_KEY], "任何一次请求集合里该键都必须是服务器那份正文，不许被删掉").toBe(SERVER);
    }
  });

  it("【组件/DOM】版本守卫：故障窗口里另一页面写入更新草稿时，旧重试不覆盖也不删除它", async () => {
    const { failRemove } = installStorage();
    fakeServer({ [CARD_KEY]: SERVER });
    const { wrapper } = await mountWithConflict();
    await wrapper.find('[data-im="card-edit"]').trigger("click");

    failRemove((key) => (key === cardLocalDraftStorageKey(CARD_ID) ? quotaError() : null));
    await wrapper.find('[data-im="card-draft-keep-server"]').trigger("click");
    await wrapper.vm.$nextTick();
    failRemove(null);

    // 另一页面在故障窗口里写下了更新的一版本机记录（版本号更大）
    writeCardLocalDraft(CARD_ID, "另一页面后来写的新输入", { boardId: "board_default" });
    const newerText = readCardLocalDraft(CARD_ID)?.text;

    await wrapper.find('[data-im="card-draft-local-removal-retry"]').trigger("click");
    await flushPromises();

    expect(readCardLocalDraft(CARD_ID)?.text, "更新的一版必须原样保留（版本守卫不得被削弱）").toBe(newerText);
    expect(readCardLocalDraft(CARD_ID)?.kind, "更新的一版仍是编辑副本，不许被改写成清除依据").toBe("draft");
    expect(hasCardLocalClear(CARD_ID)).toBe(false);
  });
});

describe("[R4 对照] 另外三条路径各自的断言", () => {
  it("对照 a：选本机 → 本机候选成为草稿并写回服务器，冲突结束且没有清除依据", async () => {
    const server = fakeServer({ [CARD_KEY]: SERVER });
    const { wrapper, store } = await mountWithConflict();
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    expect(editorValue(wrapper)).toBe(LOCAL);

    await wrapper.find('[data-im="card-draft-keep-local"]').trigger("click");
    await wrapper.vm.$nextTick();

    expect(store.draftConflictFor(CARD_ID)).toBeNull();
    expect(store.cardDraftText(CARD_ID), "选本机后草稿必须是本机那份").toBe(LOCAL);
    expect(editorValue(wrapper), "真实编辑框要跟随用户选择").toBe(LOCAL);

    await store.flushDrafts();
    await flushPromises();
    expect(server.drafts[CARD_KEY], "选了本机就必须把本机那份写回服务器").toBe(LOCAL);
    expect(hasCardLocalClear(CARD_ID), "选本机不是清除，不许留清除依据").toBe(false);
  });

  it("对照 b：普通保存成功后的本机清理 → 服务器已拿到正文，本机副本按版本清掉（不是清除）", async () => {
    const server = fakeServer({});
    setActivePinia(createPinia());
    const store = useInteractiveStore();
    store.board = { ...emptyBoardState("board_default"), cards: [textCard()], selection: [CARD_ID] };
    const wrapper = mountCard();
    await store.refreshBoardFromServer();
    await wrapper.vm.$nextTick();

    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await wrapper.find('[data-im="card-editor"]').setValue("第一次输入的内容");
    await wrapper.vm.$nextTick();
    expect(readCardLocalDraft(CARD_ID)?.kind, "输入时本机同步写下编辑副本").toBe("draft");

    await store.flushDrafts();
    await flushPromises();

    expect(server.drafts[CARD_KEY], "服务器要拿到这次正文").toBe("第一次输入的内容");
    expect(readCardLocalDraft(CARD_ID), "保存成功后本机副本按版本清掉").toBeNull();
    expect(hasCardLocalClear(CARD_ID), "保存成功的清理绝不是整份清除").toBe(false);
  });

  it("对照 c：整份清除失败后重试 → 先补写 cleared 依据（既有保护不许被撤掉）", async () => {
    const { failSet } = installStorage();
    fakeServer({ [CARD_KEY]: "要清掉的旧草稿" });
    setActivePinia(createPinia());
    const store = useInteractiveStore();
    store.board = { ...emptyBoardState("board_default"), cards: [textCard()], selection: [CARD_ID] };
    const wrapper = mountCard();
    await store.refreshBoardFromServer();
    await wrapper.vm.$nextTick();
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await wrapper.vm.$nextTick();

    // 清除时本机 cleared 依据写失败
    failSet((key) => (key === cardLocalDraftStorageKey(CARD_ID) ? quotaError() : null));
    store.clearDraft(CARD_KEY);
    await wrapper.vm.$nextTick();
    expect(hasCardLocalClear(CARD_ID), "本机清除依据没写成").toBe(false);

    const retryButton = wrapper.find('[data-im="card-draft-retry"]');
    expect(retryButton.exists(), "清除失败必须给出重试入口").toBe(true);
    /**
     * 本机存储恢复，但网络清除仍失败：这时「已清除」这份事实**只能**靠本机 cleared 依据撑住
     * （服务器确认前不算已同步）。如果重试把 cleared 机制撤掉，旧稿重开就会复活。
     */
    failSet(null);
    vi.mocked(imApi.saveDrafts).mockRejectedValue(new Error("网络还没恢复"));
    await retryButton.trigger("click");
    await flushPromises();

    expect(hasCardLocalClear(CARD_ID), "重试必须先补写本机清除依据（旧稿不许复活）").toBe(true);

    wrapper.unmount();
    const reopened = await reopen();
    expect(reopened.cardDraftText(CARD_ID), "重开不许把已清除的旧稿放回编辑内容").toBe("");
    expect(reopened.draftRemovalStateFor(CARD_KEY).status, "服务器还没确认时必须仍是「待同步清除」").toBe("pending");

    // 网络恢复：待同步的清除要真的发出去，服务器确认后本机依据按版本清掉
    const server = fakeServer({ [CARD_KEY]: "要清掉的旧草稿" });
    await reopened.flushDrafts();
    await flushPromises();
    expect(Object.prototype.hasOwnProperty.call(server.drafts, CARD_KEY), "服务器那份要被真的清掉").toBe(false);
    expect(hasCardLocalClear(CARD_ID), "服务器确认之后本机清除依据按版本清掉").toBe(false);
  });

  it("对照 d（第 7 条）：旧记录（没有 kind）绝不被当成整份清除，两份候选都保留", async () => {
    installStorage();
    writeLegacyRecord(CARD_ID, "", 1);
    const server = fakeServer({ [CARD_KEY]: SERVER });

    const store = await reopen();

    expect(server.drafts[CARD_KEY], "旧记录不许被当成整份清除的依据").toBe(SERVER);
    expect(store.draftConflictFor(CARD_ID), "旧记录与服务器稿两份都要保留，等用户选择").not.toBeNull();
    expect(store.draftRemovalStateFor(CARD_KEY).status, "不许为旧记录登记整份清除").toBe("idle");

    // 可操作的说明与两个选择入口必须在真实卡片上可见可点（两份候选都保留）
    const wrapper = mountCard();
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-im="card-draft-conflict"]').exists(), "必须给出可操作说明").toBe(true);
    expect(wrapper.find('[data-im="card-draft-keep-local"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="card-draft-keep-server"]').exists()).toBe(true);

    await store.flushDrafts();
    await flushPromises();
    expect(server.drafts[CARD_KEY], "未决冲突期间按服务器事实回写，服务器稿不许被删").toBe(SERVER);
  });
});
