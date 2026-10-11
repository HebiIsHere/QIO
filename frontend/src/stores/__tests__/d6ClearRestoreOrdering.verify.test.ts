/**
 * 反例 D6-③：清除依据只作用于当时的版本（契约 §12.2）。
 *
 * 由子智能体 A 编写。先在基线 7ef7537 上运行，三条都必须失败：
 * - 主场景：清除 A → 本地清除依据留下 → 重新输入（这次本机写失败）→ 重新读取板面 →
 *   新文字被旧的 cleared 记录删掉；
 * - 别的板面的 cleared 记录作用于本板（恢复顺序里「先归属」缺失）；
 * - 在飞保存回执不按记录版本清理后来新写下的本地副本。
 *
 * 标注：【状态】只依赖 store 的公开状态与动作；【模拟】受控接口替身与受控的存储失败。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as imApi from "../../services/interactive";
import {
  cardDraftKey,
  cardLocalDraftStorageKey,
  readCardLocalDraft,
  writeCardLocalClear,
} from "../../interactive/drafts";
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
        id: "c1", kind: "text", x: 40, y: 40, w: 240, h: 120, content: "正式正文",
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

function newStore() {
  setActivePinia(createPinia());
  return useInteractiveStore();
}

const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");

/**
 * 受控的存储失败：**只让指定键的写入失败**（模拟配额/策略对该次写入的拒绝），
 * 读取与其它键照常 —— 这样才能精确复现「清除依据留下、之后的新输入写失败」这条时序。
 */
function failWritesFor(failingKey: string, message: string): void {
  const backing = new Map<string, string>();
  try {
    const existing = globalThis.localStorage as Storage | undefined;
    if (existing) {
      for (let i = 0; i < existing.length; i += 1) {
        const k = existing.key(i);
        if (k) backing.set(k, existing.getItem(k) ?? "");
      }
    }
  } catch {
    /* 读不出现有内容就当空的处理 */
  }
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => (backing.has(key) ? (backing.get(key) as string) : null),
      setItem: (key: string, value: string) => {
        if (key === failingKey) throw new Error(message);
        backing.set(key, value);
      },
      removeItem: (key: string) => {
        backing.delete(key);
      },
      get length() {
        return backing.size;
      },
      key(index: number) {
        return Array.from(backing.keys())[index] ?? null;
      },
    },
  });
}

function restoreLocalStorage(): void {
  if (originalDescriptor) Object.defineProperty(globalThis, "localStorage", originalDescriptor);
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  restoreLocalStorage();
  localStorage.clear();
});

describe("反例③：旧 cleared 不许删掉它之后的新输入（§12.2）", () => {
  it("【状态】清除后重输（本机写失败）→ 重新读取板面：新文字不被旧 cleared 删掉，失败状态保留", async () => {
    vi.useFakeTimers();
    const server = fakeServer({ "card:c1": "旧草稿" });
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();

    // 用户清除：本机留下待同步的清除依据（这一步写成功，所以依据确实「留下了」）
    store.clearDraft("card:c1");
    expect(readCardLocalDraft("c1")?.kind, "前提：本地应留下清除依据").toBe("cleared");
    // 注意：这次清除**尚未**同步（防抖没到点用户就开始重输了）

    // 用户重新输入新文字 —— 这次本机写失败（只有这条记录的写入被拒绝）
    failWritesFor(cardLocalDraftStorageKey("c1"), "本机写入被拒绝");
    store.setDraft("card:c1", "清除之后重新输入的新文字");
    expect(store.cardDraftText("c1"), "本机写失败时内存里的文字必须保留").toBe("清除之后重新输入的新文字");
    expect(
      store.draftLocalStateFor("card:c1").ok,
      "本机写失败必须如实登记，不能假装保护上了",
    ).toBe(false);

    // 重新读取板面（真实路径：重新打开/手动刷新都走这里）
    await store.refreshBoardFromServer();
    await flushPromises();

    expect(
      store.cardDraftText("c1"),
      "重新读取板面后，新输入被旧的 cleared 记录删掉了（恢复顺序没有先看「是否已被更晚的编辑取代」）",
    ).toBe("清除之后重新输入的新文字");
    expect(
      store.draftLocalStateFor("card:c1").ok,
      "写失败的状态被重读过程抹掉了（失败状态必须保留）",
    ).toBe(false);
    expect(
      store.draftRemovalStateFor("card:c1").status,
      "旧清除在内存文字还在时又被登记成待同步（它会去删服务器上后来的内容）",
    ).toBe("idle");

    // 后续照常保存：新文字真的落库
    await vi.advanceTimersByTimeAsync(700);
    await flushPromises();
    expect(server.drafts["card:c1"], "恢复出来的新文字没有被存回服务器").toBe("清除之后重新输入的新文字");
  });

  it("【状态】别的板面的清除依据不作用于本板（先归属，再谈清除）", async () => {
    vi.useFakeTimers();
    const server = fakeServer({ "card:c1": "本板的服务器草稿" });
    // 直接预置一条「别的板面」的清除依据：归属对不上，就轮不到它说话
    writeCardLocalClear("c1", { boardId: "board_other" });

    const store = useInteractiveStore();
    await store.load();
    await flushPromises();

    expect(
      store.cardDraftText("c1"),
      "别的板面的清除依据把本板的草稿删掉了（恢复顺序缺了「先归属」）",
    ).toBe("本板的服务器草稿");
    expect(readCardLocalDraft("c1"), "别的板面的清除依据不该被这次加载动到").not.toBeNull();
  });
});
