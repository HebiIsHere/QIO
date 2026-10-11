/**
 * [final-A2] 条目 12 关闭重开式探针（drafts 层 + 同一 localStorage 重新建 store 读取）。
 *
 * 关闭重开在真实验收里由 E 用新进程做；这里做的是**同一份本机存储、重新创建 store**
 * 的重开读取，覆盖条目 12 的「已清除旧稿复活」反例：
 * - 反例可复现：本机没有留下清除依据（本机清除写失败）时，重开会把旧稿重新恢复并上传；
 * - 正确行为：清除事实真的在本机落成（kind=cleared）后，重开不许把旧稿放回编辑内容，
 *   并且仍然知道要重发网络清除（服务器确认前不算已同步）；
 * - 重试顺序：本机与网络清除都失败后，必须**先补写本机清除依据再重发网络清除**；
 * - 不为清除旧稿误删后来输入：清除之后的新版本仍然是当前编辑内容（与服务器那份并存待选择）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../../stores/interactive";
import * as imApi from "../../services/interactive";
import {
  cardLocalDraftStorageKey,
  ensureCardLocalClear,
  hasCardLocalClear,
  readCardLocalDraft,
  writeCardLocalDraft,
} from "../drafts";
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

const CARD_KEY = "card:c1";

function boardState(): BoardState {
  return {
    boardId: "board_default",
    cards: [
      {
        id: "c1", kind: "text", x: 40, y: 40, w: 240, h: 120, content: "正式正文",
        checked: true, hidden: false, folded: false, bookmarked: false, deleted: false,
        createdAt: "2026-10-08T00:00:00.000Z", updatedAt: "2026-10-08T00:00:00.000Z",
      },
    ],
    groups: [], links: [], selection: [], updatedAt: "2026-10-08T00:00:00.000Z",
  } as unknown as BoardState;
}

/** 受控的「服务端」：草稿整份替换保存（未列出的键会被删掉），与真实接口语义一致 */
function fakeServer(initial: Record<string, string> = {}) {
  const drafts: Record<string, string> = { ...initial };
  let updatedAt = "2026-10-08T00:00:00.000Z";
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
  return { drafts };
}

const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");

/** 记忆型存储：跨「重开」保留，可随时开关某个键的写入失败 */
function installStorage(): { map: Map<string, string>; failSet: (fn: ((key: string) => Error | null) | null) => void } {
  let fail: ((key: string) => Error | null) | null = null;
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
        const failure = fail?.(key);
        if (failure) throw failure;
        map.set(key, value);
      },
      removeItem(key: string) {
        map.delete(key);
      },
      clear() {
        map.clear();
      },
    },
  });
  return { map, failSet: (fn) => { fail = fn; } };
}

function quotaError(): Error {
  const err = new Error("quota");
  err.name = "QuotaExceededError";
  return err;
}

/** 关闭重开（drafts 读取层）：同一份本机存储，重新创建 store */
async function reopen() {
  setActivePinia(createPinia());
  const store = useInteractiveStore();
  await store.load();
  await flushPromises();
  return store;
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.useFakeTimers();
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  if (originalDescriptor) Object.defineProperty(globalThis, "localStorage", originalDescriptor);
});

describe("[12] 重开：清除事实没落成本机依据时，旧稿会复活（反例可复现）", () => {
  it("本机只剩旧的编辑副本：重开会把它恢复出来并重新上传", async () => {
    installStorage();
    // 用户之前清除过、服务器也已清掉；本机那次清除写入失败，磁盘上只剩下旧的编辑副本
    writeCardLocalDraft("c1", "已经清掉的旧稿", { boardId: "board_default" });
    const server = fakeServer({});

    const store = await reopen();

    expect(store.cardDraftText("c1"), "本机没有清除依据时，重开确实会把旧稿恢复出来").toBe("已经清掉的旧稿");
    await store.flushDrafts();
    await flushPromises();
    expect(server.drafts[CARD_KEY], "恢复出来的旧稿被重新上传（这就是「已清除旧稿复活」）").toBe("已经清掉的旧稿");
  });
});

describe("[12] 重开：清除依据落成本机后，旧稿不许复活且会重发网络清除", () => {
  it("磁盘上是 cleared：重开不把它放进编辑内容，并保持「待同步清除」，flush 真的删掉服务器那份", async () => {
    installStorage();
    const ensured = ensureCardLocalClear("c1", { boardId: "board_default" });
    expect(ensured.ok).toBe(true);
    // 服务器上那份还没清掉（网络清除当时失败）
    const server = fakeServer({ [CARD_KEY]: "已经清掉的旧稿" });

    const store = await reopen();

    expect(store.cardDraftText("c1"), "清除依据还在，旧稿不许再出现在编辑内容里").toBe("");
    expect(store.draftRemovalStateFor(CARD_KEY).status, "服务器确认之前必须还是「待同步清除」").toBe("pending");

    await store.flushDrafts();
    await flushPromises();

    expect(Object.prototype.hasOwnProperty.call(server.drafts, CARD_KEY), "待同步的清除必须真的重发出去").toBe(false);
    expect(hasCardLocalClear("c1"), "服务器确认之后本机清除依据按版本清理掉").toBe(false);

    // 再重开一次：两边都没有这份旧稿了，它彻底不复活
    const again = await reopen();
    expect(again.cardDraftText("c1")).toBe("");
  });
});

describe("[12] 重试顺序：先补写本机清除依据，再重发网络清除", () => {
  it("本机清除与网络清除都失败 → 重开复活 → 补写本机依据后重开不再复活", async () => {
    const { failSet } = installStorage();
    writeCardLocalDraft("c1", "清除失败留下的旧稿", { boardId: "board_default" });
    // 服务器那份已经被清掉了；本机这次清除没写成，磁盘上只剩旧的编辑副本
    fakeServer({});

    // 本机清除写入失败（配额满 / 策略禁用）
    failSet((key) => (key === cardLocalDraftStorageKey("c1") ? quotaError() : null));
    const failed = ensureCardLocalClear("c1", { boardId: "board_default" });
    expect(failed.ok).toBe(false);
    expect(failed.committedVersion, "没落盘就不能给可登记的确认版本").toBeUndefined();
    expect(readCardLocalDraft("c1")?.kind, "本机仍只有旧的编辑副本（清除没建成）").toBe("draft");

    // 反例：此时重开，旧稿会回来
    const wrong = await reopen();
    expect(wrong.cardDraftText("c1"), "本机清除没建成时重开确实会复活旧稿").toBe("清除失败留下的旧稿");

    // 用户恢复本机存储后点重试：先把本机清除依据补上（再重发网络清除）
    failSet(null);
    const retry = ensureCardLocalClear("c1", { boardId: "board_default" });
    expect(retry.ok).toBe(true);
    expect(retry.alreadyProtected).toBe(false);
    expect(retry.committedVersion, "补写落盘后给出可登记的确认版本").toBe(2);
    expect(hasCardLocalClear("c1")).toBe(true);

    // 重开：不再复活，且知道要重发网络清除
    const store = await reopen();
    expect(store.cardDraftText("c1")).toBe("");
    expect(store.draftRemovalStateFor(CARD_KEY).status).toBe("pending");
    await store.flushDrafts();
    await flushPromises();
    expect(hasCardLocalClear("c1")).toBe(false);
  });
});

describe("[12] 不为清除旧稿误删后来输入", () => {
  it("清除之后又输入的新版本：重开恢复的是新版本，并与服务器那份并存等待选择", async () => {
    installStorage();
    ensureCardLocalClear("c1", { boardId: "board_default" });
    // 清除之后用户又输入了新文字（新版本的本机编辑副本）
    const typed = writeCardLocalDraft("c1", "清除之后重新输入的新文字", { boardId: "board_default" });
    expect(typed.version).toBe(2);
    // 服务器上还留着那份旧草稿
    fakeServer({ [CARD_KEY]: "服务器上那份旧草稿" });

    const store = await reopen();

    expect(store.cardDraftText("c1"), "旧清除不许删掉它之后的新输入").toBe("清除之后重新输入的新文字");
    expect(store.draftConflictFor("c1"), "服务器另一份仍在：两份都要保留，交给用户选择").not.toBeNull();
  });
});
