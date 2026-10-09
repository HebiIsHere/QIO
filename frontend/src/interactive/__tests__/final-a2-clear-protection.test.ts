/**
 * [final-A2] 条目 12 / 契约 M2：计划清除（意图）与真实写入的本机清除保护必须分开。
 *
 * 正确行为期望：
 * - 区分**计划版本**与**真实写入版本**：写失败时不能把计划版本当成「已确认版本」登记
 *   （否则版本守卫会对不上，磁盘上的旧稿删不掉，重开后复活）；
 * - 重试必须能**先重建本机清除保护**（补写 cleared），再重发网络清除；
 * - 重建是**幂等**的：磁盘上已经是一份 cleared 记录时不许再写一版（版本一推进，
 *   之前登记的确认版本就失效，清理反而删不掉）；
 * - 重建失败时不得破坏已有记录（旧稿正文还在），也不误伤别的对象。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  cardLocalDraftStorageKey,
  ensureCardLocalClear,
  hasCardLocalClear,
  readCardLocalDraft,
  removeDraft,
  writeCardLocalClear,
  writeCardLocalDraft,
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

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  if (originalDescriptor) Object.defineProperty(globalThis, "localStorage", originalDescriptor);
});

describe("[12] 重建本机清除保护（ensureCardLocalClear）", () => {
  it("本来没有记录：真实写下 cleared，alreadyProtected=false、committedVersion 是落盘版本", () => {
    installStorage();

    const result = ensureCardLocalClear("A", { boardId: "board_default" });

    expect(result.ok).toBe(true);
    expect(result.alreadyProtected).toBe(false);
    expect(result.committedVersion).toBe(1);
    const stored = readCardLocalDraft("A");
    expect(stored?.kind).toBe("cleared");
    expect(stored?.version).toBe(1);
    expect(hasCardLocalClear("A")).toBe(true);
  });

  it("本机还有编辑副本：补写 cleared，版本推进到真实落盘的那一版", () => {
    installStorage();
    const draft = writeCardLocalDraft("A", "用户正在编辑的正文", { boardId: "board_default" });
    expect(draft.version).toBe(1);

    const result = ensureCardLocalClear("A", { boardId: "board_default" });

    expect(result.ok).toBe(true);
    expect(result.committedVersion).toBe(2);
    const stored = readCardLocalDraft("A");
    expect(stored?.kind).toBe("cleared");
    expect(stored?.version).toBe(2);
    expect(stored?.boardId).toBe("board_default");
  });

  it("磁盘上已经是一份 cleared：幂等，不推进版本（否则登记的确认版本会失效）", () => {
    installStorage();
    const first = ensureCardLocalClear("A", { boardId: "board_default" });
    expect(first.committedVersion).toBe(1);

    const again = ensureCardLocalClear("A", { boardId: "board_default" });

    expect(again.ok).toBe(true);
    expect(again.alreadyProtected).toBe(true);
    expect(again.committedVersion).toBe(1);
    expect(readCardLocalDraft("A")?.version).toBe(1);
    // 没有新写：下一次真正的写入才拿到版本 2
    expect(writeCardLocalClear("A", { boardId: "board_default" }).committedVersion).toBe(2);
  });

  it("写入失败：ok=false 带真实原因、committedVersion 缺省（没有落盘），旧正文一字不动", () => {
    const { map, failSet } = installStorage();
    seedRecord(map, "A", { text: "唯一的一份完整正文", updatedAt: 1, seq: 1, kind: "draft", version: 1, boardId: "board_default" });
    failSet(() => quotaError());

    const failed = ensureCardLocalClear("A", { boardId: "board_default" });

    expect(failed.ok).toBe(false);
    expect(failed.alreadyProtected).toBe(false);
    expect(failed.error).toContain("存储已满");
    expect(failed.committedVersion).toBeUndefined();
    const still = readCardLocalDraft("A");
    expect(still?.kind).toBe("draft");
    expect(still?.version).toBe(1);
    expect(still?.text).toBe("唯一的一份完整正文");
  });

  it("重试：存储恢复后先补写 cleared 再谈网络清除 —— 真落盘并给出可登记的确认版本", () => {
    const { map, failSet } = installStorage();
    seedRecord(map, "A", { text: "旧稿", updatedAt: 1, seq: 1, kind: "draft", version: 4, boardId: "board_default" });
    failSet(() => quotaError());
    expect(ensureCardLocalClear("A", { boardId: "board_default" }).ok).toBe(false);

    // 用户恢复本机存储能力后点重试：同一次「待确认清除」要先把本机依据补上
    failSet(null);
    const retry = ensureCardLocalClear("A", { boardId: "board_default" });

    expect(retry.ok).toBe(true);
    expect(retry.alreadyProtected).toBe(false);
    expect(retry.committedVersion).toBe(5);
    expect(hasCardLocalClear("A")).toBe(true);
    expect(readCardLocalDraft("A")?.version).toBe(5);
  });

  it("清理失败不误伤别的对象：B 的记录不受影响", () => {
    const { failSet } = installStorage();
    writeCardLocalDraft("B", "B 的正文", { boardId: "board_default" });
    failSet((key) => (key === cardLocalDraftStorageKey("A") ? quotaError() : null));

    const failed = ensureCardLocalClear("A", { boardId: "board_default" });

    expect(failed.ok).toBe(false);
    expect(readCardLocalDraft("B")?.text).toBe("B 的正文");
    expect(readCardLocalDraft("B")?.kind).toBe("draft");
  });
});

describe("[12] 计划版本 ≠ 真实写入版本", () => {
  it("writeCardLocalClear 写失败：version 只是计划值，committedVersion 缺省，磁盘仍是旧版本", () => {
    const { map, failSet } = installStorage();
    seedRecord(map, "A", { text: "磁盘上的 v1", updatedAt: 1, seq: 1, kind: "draft", version: 1, boardId: "board_default" });
    failSet(() => quotaError());

    const planned = writeCardLocalClear("A", { boardId: "board_default" });

    expect(planned.ok).toBe(false);
    expect(planned.version).toBe(2); // 计划写第 2 版
    expect(planned.committedVersion).toBeUndefined(); // 但这一版没有落盘
    expect(readCardLocalDraft("A")?.version).toBe(1); // 磁盘真实版本没推进
  });

  it("写成功后 version 与 committedVersion 一致（真实落盘版本）", () => {
    installStorage();
    const written = writeCardLocalDraft("A", "正文", { boardId: "board_default" });
    expect(written.ok).toBe(true);
    expect(written.version).toBe(1);
    expect(written.committedVersion).toBe(1);
    expect(readCardLocalDraft("A")?.version).toBe(1);
  });
});

describe("[12] 清除事实以磁盘为准（hasCardLocalClear）", () => {
  it("没有记录 / 编辑副本 / cleared 三种情况分得开", () => {
    installStorage();
    expect(hasCardLocalClear("A")).toBe(false);

    writeCardLocalDraft("A", "编辑中的正文", { boardId: "board_default" });
    expect(hasCardLocalClear("A")).toBe(false);

    writeCardLocalClear("A", { boardId: "board_default" });
    expect(hasCardLocalClear("A")).toBe(true);

    removeDraft(cardLocalDraftStorageKey("A"));
    expect(hasCardLocalClear("A")).toBe(false);
  });
});
