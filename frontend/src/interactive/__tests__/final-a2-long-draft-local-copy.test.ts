/**
 * [final-A2] 条目 09 前端：长草稿的本机副本必须**完整**，本机层绝不截短。
 *
 * 正确行为期望：
 * - 20,000 / 20,001 / 20,008 字（结尾带唯一标识）写入本机副本后，读回来一字不差、长度精确、
 *   结尾标识还在 —— 这层不做任何长度裁剪；
 * - 服务器拒绝超限（本次模拟：不触碰本机副本）之后，「关闭重开」式重读仍是完整原文；
 * - 本机写入失败（配额满）时**不许留下截短的副本、也不许改写已有那份完整副本**：
 *   以后把半截内容恢复成「完整原文」比报错更糟。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  cardLocalDraftStorageKey,
  draftStorageKey,
  readCardLocalDraft,
  readDraft,
  writeCardLocalDraft,
  writeDraft,
} from "../drafts";

const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");

/** 记忆型存储替身：可随时打开/关闭按键的写入失败（模拟配额满 / 策略禁用），映射可直接预置记录 */
function installStorage(): {
  map: Map<string, string>;
  failSet: (fn: ((key: string) => Error | null) | null) => void;
} {
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
    },
  });
  return { map, failSet: (fn) => { fail = fn; } };
}

function quotaError(): Error {
  const err = new Error("quota");
  err.name = "QuotaExceededError";
  return err;
}

/** 预置一条本机记录（等价于之前某次成功写入留下的） */
function seedRecord(map: Map<string, string>, cardId: string, record: Record<string, unknown>): void {
  map.set(cardLocalDraftStorageKey(cardId), JSON.stringify(record));
}

/** 指定总长度、结尾带唯一标识的长草稿（被截短就会立刻丢掉这个标识） */
function longDraft(total: number, marker: string): string {
  return "字".repeat(total - marker.length) + marker;
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  if (originalDescriptor) Object.defineProperty(globalThis, "localStorage", originalDescriptor);
});

describe("[09] 本机副本不截短", () => {
  it.each([20000, 20001, 20008])("%i 字：本机副本完整读回，长度精确、结尾标识还在", (total) => {
    installStorage();
    const marker = "【结尾-" + total + "-Zz9】";
    const text = longDraft(total, marker);
    expect(text.length).toBe(total);

    const written = writeCardLocalDraft("long", text, { boardId: "board_default", seq: 7 });

    expect(written.ok).toBe(true);
    const stored = readCardLocalDraft("long");
    expect(stored?.text.length).toBe(total);
    expect(stored?.text).toBe(text);
    expect(stored?.text.endsWith(marker)).toBe(true);
    // 原始存储串里也找不到被裁过的痕迹：长度与标识都完整
    const raw = localStorage.getItem(cardLocalDraftStorageKey("long")) ?? "";
    expect(raw.includes(marker)).toBe(true);
  });

  it("服务器拒绝超限后（重开式重读）仍是完整原文与完整结尾", () => {
    installStorage();
    const marker = "【结尾-20008-重开】";
    const text = longDraft(20008, marker);
    writeCardLocalDraft("long", text, { boardId: "board_default", seq: 9 });

    // 模拟：服务器返回超限错误、前端没有清理本机副本；此后关闭重开 → 重新读本机记录
    const reopened = readCardLocalDraft("long");

    expect(reopened?.text).toBe(text);
    expect(reopened?.text.endsWith(marker)).toBe(true);
  });

  it("重试只重写本机副本：版本推进，正文仍完整", () => {
    installStorage();
    const first = longDraft(20000, "【第一版结尾】");
    writeCardLocalDraft("long", first, { boardId: "board_default" });
    const second = longDraft(20008, "【第二版结尾】");

    const retried = writeCardLocalDraft("long", second, { boardId: "board_default" });

    expect(retried.ok).toBe(true);
    expect(retried.version).toBe(2);
    expect(readCardLocalDraft("long")?.text).toBe(second);
    expect(readCardLocalDraft("long")?.text.length).toBe(20008);
  });
});

describe("[09] 本机写入失败时不许留下半份/截短副本", () => {
  it("新增长稿写失败：磁盘上不会出现被截短的记录（要么没有，要么是原来那份）", () => {
    const { failSet } = installStorage();
    failSet((key) => (key === cardLocalDraftStorageKey("long") ? quotaError() : null));

    const failed = writeCardLocalDraft("long", longDraft(20008, "【写不进去的结尾】"), { boardId: "board_default" });

    expect(failed.ok).toBe(false);
    expect(failed.error).toContain("存储已满");
    expect(failed.committedVersion).toBeUndefined();
    expect(readCardLocalDraft("long")).toBeNull();
    expect(localStorage.getItem(cardLocalDraftStorageKey("long"))).toBeNull();
  });

  it("已有完整长稿时新写入失败：原来那份完整副本一字未改（唯一完整副本不许被清掉/改写）", () => {
    const { failSet } = installStorage();
    const kept = longDraft(20000, "【保住的完整结尾】");
    const first = writeCardLocalDraft("long", kept, { boardId: "board_default" });
    expect(first.ok).toBe(true);

    failSet((key) => (key === cardLocalDraftStorageKey("long") ? quotaError() : null));
    const failed = writeCardLocalDraft("long", longDraft(20008, "【写不进去的新结尾】"), { boardId: "board_default" });

    expect(failed.ok).toBe(false);
    const still = readCardLocalDraft("long");
    expect(still?.text).toBe(kept);
    expect(still?.text.length).toBe(20000);
    expect(still?.text.endsWith("【保住的完整结尾】")).toBe(true);
    expect(still?.version).toBe(first.version);
  });

  it("聊天草稿同样不截短（同一层，一视同仁）", () => {
    installStorage();
    const marker = "【聊天结尾-20008】";
    const text = longDraft(20008, marker);

    const written = writeDraft(draftStorageKey("chat", "t1"), text, 5);

    expect(written.ok).toBe(true);
    const stored = readDraft(draftStorageKey("chat", "t1"));
    expect(stored?.text.length).toBe(20008);
    expect(stored?.text.endsWith(marker)).toBe(true);
  });
});
