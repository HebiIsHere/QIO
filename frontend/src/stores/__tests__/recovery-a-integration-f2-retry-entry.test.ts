/**
 * [recovery-A / 合并后复核 · 独立第二实现] F2：**生产重试入口 store.retryDraftSave** 的替换保护。
 *
 * 反例 F2：本机旧格式记录（无 version / version 0）与服务器另一稿冲突 → 用户选择服务器稿 →
 * 本机删除失败 → 同一浏览器另一页面为同一卡片写下新稿 → 原页重试。
 * 旧行为：重试按对象 id 删除，后来新建的本机记录被删掉。
 *
 * 本文件从**用户真正点的入口**（store.retryDraftSave）走完整流程，断言：
 * - 无版本 / version 0 / 当前格式（正版本号）三种记录形态；
 * - 重试后新稿的文字、本机记录、服务器请求集合（mock saveDrafts 捕获）与重开恢复都保留新稿；
 * - 没有被替换的旧副本仍然可以被清理；version-guard 是有意保留，不显示成失败。
 *
 * 命名与 D 的验收测试无关（独立第二实现，自建铺垫）。
 *
 * 历史（已修复，保留记录）：合并后复核时本文件先暴露了一个真实缺口 —— 重试末尾的 flushDrafts
 * 会把内存候选（用户选定的服务器正文）当作 card:c1 的载荷发给服务器，成功回执再按「请求时读到的
 * 版本」清理本机副本，于是把另一页面刚写下的新稿删掉（三种记录形态都一样，而且任何一次草稿保存
 * 都会触发）。Lead 已在 stores/interactive.ts（commit 3cb0cc4）修好：**只有这次真上传的正文等于
 * 本机记录正文**时才允许按版本/指纹清理。当时的红/诊断输出留在
 * .evidence/src-a/f2-prod-entry-red.txt 与 .evidence/src-a/f2-prod-entry-diagnosis.txt 作为对照。
 *
 * 标注：【状态 + 本机存储 + 真实 store】；服务器请求为 mock；删除失败为受控模拟。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useInteractiveStore } from "../interactive";
import * as imApi from "../../services/interactive";
import {
  cardDraftKey,
  cardLocalDraftStorageKey,
  readCardLocalDraft,
  writeCardLocalDraft,
} from "../../interactive/drafts";

vi.mock("../../services/api", () => ({ api: { sendTurn: vi.fn() } }));

vi.mock("../../services/interactive", () => ({
  saveDrafts: vi.fn(async () => undefined),
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  fetchVisibleRange: vi.fn(async () => ({ intents: [], materials: [], cards: [] })),
  previewMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(async () => ({ intents: [], batches: [] })),
  approveIntent: vi.fn(),
  rejectIntent: vi.fn(),
  batchDecide: vi.fn(),
  createDemoIntents: vi.fn(),
  advanceIntent: vi.fn(),
  updateIntentPreview: vi.fn(),
}));

const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");

/** 记忆型存储：删失败可以受控开关（模拟删除暂时不成功），其余读写正常 */
let failRemove = false;
function installStorage(): void {
  const mem = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => mem.get(key) ?? null,
      setItem: (key: string, value: string) => {
        mem.set(key, value);
      },
      removeItem: (key: string) => {
        if (failRemove) throw new Error("本机记录这次没删掉（模拟）");
        mem.delete(key);
      },
      clear: () => {
        mem.clear();
      },
      get length(): number {
        return mem.size;
      },
      key: (index: number) => Array.from(mem.keys())[index] ?? null,
    },
  });
}

type LegacyFormat = "no-version" | "version-0" | "current";

/** 预置一条旧格式（或当前格式）本机记录：无 version / version 0 / version 3 */
function seedLocalRecord(cardId: string, format: LegacyFormat, text: string): void {
  const base: Record<string, unknown> = { text, kind: "draft", updatedAt: 1000, seq: 1, boardId: "board_default" };
  if (format === "version-0") base.version = 0;
  if (format === "current") base.version = 3;
  localStorage.setItem(cardLocalDraftStorageKey(cardId), JSON.stringify(base));
}

function boardPayload(cards: string[], drafts: Record<string, string>) {
  return {
    board: { id: "board_default", title: "板面" },
    state: {
      boardId: "board_default",
      seq: 1,
      updatedAt: "2026-10-10T00:00:00.000Z",
      cards: cards.map((id) => ({
        id,
        kind: "text" as const,
        content: "正式正文 " + id,
        checked: false,
        hidden: false,
        folded: false,
        bookmarked: false,
        deleted: false,
        x: 0,
        y: 0,
        w: 240,
        h: 160,
        createdAt: "2026-10-10T00:00:00.000Z",
        updatedAt: "2026-10-10T00:00:00.000Z",
        meta: {},
      })),
      groups: [],
      links: [],
      selection: [] as string[],
    },
    seq: 1,
    baseline: null,
    submissions: [],
    drafts: { drafts, updatedAt: null },
  };
}

async function loadStore(cards: string[], serverDrafts: Record<string, string>) {
  const store = useInteractiveStore();
  vi.mocked(imApi.fetchBoardState).mockResolvedValue(boardPayload(cards, serverDrafts) as never);
  await store.load();
  return store;
}

/** 「重开」：新 Pinia + 新 store，只靠存储与服务器事实恢复 */
async function reopenStore(cards: string[], serverDrafts: Record<string, string>) {
  setActivePinia(createPinia());
  return loadStore(cards, serverDrafts);
}

function serverDraftsPayload(): Record<string, string> {
  const calls = vi.mocked(imApi.saveDrafts).mock.calls;
  return (calls[calls.length - 1]?.[1] ?? {}) as Record<string, string>;
}

beforeEach(() => {
  failRemove = false;
  vi.clearAllMocks();
  setActivePinia(createPinia());
  // 每个用例一份全新存储（不依赖 clear，也不把上一个用例的键带进来）
  installStorage();
});

afterEach(() => {
  if (originalDescriptor) Object.defineProperty(globalThis, "localStorage", originalDescriptor);
});

describe.each<LegacyFormat>(["no-version", "version-0", "current"])(
  "F2 生产重试入口（%s 记录）",
  (format) => {
    it("删除失败 → 另一页面写新稿 → 重试：文字、本机记录、请求集合与重开恢复都保留新稿", async () => {
      seedLocalRecord("c1", format, "本机旧稿");
      const store = await loadStore(["c1", "c2"], { "card:c1": "服务器正文" });
      expect(store.draftConflictFor("c1")).toEqual({ local: "本机旧稿", server: "服务器正文" });

      // 用户选择服务器稿，但本机记录这次没删掉（受控失败）
      failRemove = true;
      store.resolveDraftConflict("c1", "server");
      expect(store.draftLocalRemovalErrorFor("c1"), "删除真的失败时要如实显示原因").toBeTruthy();

      // 同一浏览器另一页面为同一张卡片写下新稿：本机记录被替换
      failRemove = false;
      const written = writeCardLocalDraft("c1", "另一页面的新稿", { boardId: "board_default", seq: 9 });
      expect(written.ok).toBe(true);

      // 本页另一张卡片还有未保存输入：重试会真的发一次请求（用于观察服务器请求集合）
      store.setDraft(cardDraftKey("c2"), "另一张卡的新输入");

      // 用户点重试（生产入口）
      await store.retryDraftSave("card:c1");

      // 1) 新稿的文字与本机记录都还在
      expect(readCardLocalDraft("c1")?.text).toBe("另一页面的新稿");
      // 2) version-guard 是有意保留：不显示成删除失败、不重试
      expect(store.draftLocalRemovalErrorFor("c1")).toBeNull();
      // 3) 服务器请求集合仍然保留服务器那份正确正文（没被删掉，也没把新稿当服务器内容发上去）
      const payload = serverDraftsPayload();
      expect(payload["card:c1"]).toBe("服务器正文");
      expect(payload["card:c1"]).not.toBe("另一页面的新稿");
      expect(payload["card:c2"]).toBe("另一张卡的新输入");

      // 4) 重开恢复：新稿仍是恢复来源（冲突仍在，等用户选择）
      const reopened = await reopenStore(["c1", "c2"], { "card:c1": "服务器正文" });
      expect(readCardLocalDraft("c1")?.text).toBe("另一页面的新稿");
      expect(reopened.cardDraftText("c1")).toBe("另一页面的新稿");
      expect(reopened.draftConflictFor("c1")).toEqual({ local: "另一页面的新稿", server: "服务器正文" });
    });

    it("没有被替换的旧副本：重试后真的被清理（不能因为保护而清不掉）", async () => {
      seedLocalRecord("c1", format, "没人动过的本机旧稿");
      const store = await loadStore(["c1"], { "card:c1": "服务器正文" });
      expect(store.draftConflictFor("c1")).toEqual({ local: "没人动过的本机旧稿", server: "服务器正文" });

      failRemove = true;
      store.resolveDraftConflict("c1", "server");
      expect(store.draftLocalRemovalErrorFor("c1")).toBeTruthy();

      failRemove = false;
      await store.retryDraftSave("card:c1");

      // 记录没被替换：这次重试必须真的清掉它（不是一律保留）
      expect(readCardLocalDraft("c1")).toBeNull();
      expect(store.draftLocalRemovalErrorFor("c1")).toBeNull();
    });
  },
);
