/**
 * [final-A2] 条目 13 / 契约 M2：本机记录的删除必须返回**真实结果**。
 *
 * 正确行为期望（不是「断言异常确实发生」）：
 * - 删除成功 → ok=true、removed=true，记录确实读不到；
 * - 本来就没有记录 → ok=true、removed=false、reason="missing-record"（不是失败）；
 * - removeItem 抛错 / 存储被禁用 → ok=false、reason="storage-failure"、带**真实原因**，
 *   且记录仍然在（内容没丢），调用方据此提示失败并提供重试；
 * - 版本守卫拒绝删除（记录已被更晚的写入替换）→ reason="version-guard"，
 *   与「真的删失败」严格区分：前者是有意保留，后者必须报错；
 * - 删除失败后记录仍能被读到 → 重开不会「已放弃的本机稿又消失/又出现」的说法自相矛盾。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  cardLocalDraftStorageKey,
  draftStorageKey,
  hasDraftRecord,
  readDraft,
  removeCardLocalDraft,
  removeCardLocalDraftIfUnchanged,
  removeDraft,
  writeCardLocalDraft,
  writeDraft,
} from "../drafts";

const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");

/** 记忆型存储替身：可注入 removeItem 失败、setItem 失败，键枚举也可用 */
function installStorage(options: {
  failRemove?: Error;
  failSet?: Error;
  failGet?: Error;
  /** removeItem 不抛错却什么也没做（某些环境/策略下的真实形态） */
  silentRemove?: boolean;
} = {}): { map: Map<string, string> } {
  const map = new Map<string, string>();
  const backend = {
    get length() {
      return map.size;
    },
    key(index: number) {
      return Array.from(map.keys())[index] ?? null;
    },
    getItem(key: string) {
      if (options.failGet) throw options.failGet;
      return map.has(key) ? (map.get(key) as string) : null;
    },
    setItem(key: string, value: string) {
      if (options.failSet) throw options.failSet;
      map.set(key, value);
    },
    removeItem(key: string) {
      if (options.failRemove) throw options.failRemove;
      if (options.silentRemove) return;
      map.delete(key);
    },
  };
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: backend });
  return { map };
}

/** 连访问 localStorage 属性都抛（隐私模式 / 企业策略） */
function breakLocalStorageAccess(message: string): void {
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get() {
      throw new Error(message);
    },
  });
}

function restoreLocalStorage(): void {
  if (originalDescriptor) Object.defineProperty(globalThis, "localStorage", originalDescriptor);
}

function quotaError(): Error {
  const err = new Error("quota");
  err.name = "QuotaExceededError";
  return err;
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  restoreLocalStorage();
});

describe("[13] 底层删除返回真实结果", () => {
  it("删除成功：ok=true / removed=true，随后读不到", () => {
    const key = draftStorageKey("chat", "t1");
    writeDraft(key, "待删除", 1);

    const result = removeDraft(key);

    expect(result.ok).toBe(true);
    expect(result.removed).toBe(true);
    expect(readDraft(key)).toBeNull();
  });

  it("本来就没有记录：ok=true / removed=false / reason=missing-record（不是失败）", () => {
    const result = removeDraft(draftStorageKey("chat", "nobody"));

    expect(result.ok).toBe(true);
    expect(result.removed).toBe(false);
    expect(result.reason).toBe("missing-record");
    expect(result.error).toBeUndefined();
  });

  it("removeItem 抛错：返回 storage-failure 与真实原因，不抛异常，记录仍在", () => {
    const key = draftStorageKey("chat", "t1");
    // 先换成「removeItem 会失败」的存储，再把记录写进去（写入本身不失败）
    installStorage({ failRemove: quotaError() });
    writeDraft(key, "删不掉也不能丢", 1);

    const result = removeDraft(key);

    expect(result.ok).toBe(false);
    expect(result.removed).toBe(false);
    expect(result.reason).toBe("storage-failure");
    expect(result.error).toContain("存储已满");
    expect(readDraft(key)?.text).toBe("删不掉也不能丢");
  });

  it("removeItem 不抛错却什么都没删：不能报成功", () => {
    const key = draftStorageKey("card", "c9");
    installStorage({ silentRemove: true });
    writeDraft(key, "假装删掉了", 1);

    const result = removeDraft(key);

    expect(result.ok).toBe(false);
    expect(result.reason).toBe("storage-failure");
    expect(hasDraftRecord(key)).toBe(true);
  });

  it("连访问 localStorage 都抛：删除明确失败并带原因", () => {
    breakLocalStorageAccess("SecurityError: 本地存储被禁用");

    const result = removeDraft(draftStorageKey("chat", "t1"));

    expect(result.ok).toBe(false);
    expect(result.removed).toBe(false);
    expect(result.reason).toBe("storage-failure");
    expect(result.error).toContain("本地存储被禁用");
  });
});

describe("[13] 卡片本机副本：版本守卫与真实失败必须区分", () => {
  it("版本相符且删除成功：ok=true / removed=true", () => {
    const written = writeCardLocalDraft("A", "本机候选", { boardId: "board_default" });
    expect(written.ok).toBe(true);

    const result = removeCardLocalDraft("A", written.version);

    expect(result.ok).toBe(true);
    expect(result.removed).toBe(true);
    expect(readDraft(cardLocalDraftStorageKey("A"))).toBeNull();
  });

  it("版本不符：version-guard 有意保留，不误删后来写入的版本", () => {
    writeCardLocalDraft("A", "第一版", { boardId: "board_default" });
    const second = writeCardLocalDraft("A", "后来写下的第二版", { boardId: "board_default" });

    const result = removeCardLocalDraft("A", second.version - 1);

    expect(result.ok).toBe(false);
    expect(result.removed).toBe(false);
    expect(result.reason).toBe("version-guard");
    expect(result.error).toBeUndefined();
    expect(readDraft(cardLocalDraftStorageKey("A"))?.text).toBe("后来写下的第二版");
  });

  it("版本相符但 removeItem 失败：storage-failure + 真实原因，副本保留", () => {
    installStorage({ failRemove: quotaError() });
    const written = writeCardLocalDraft("A", "本机候选", { boardId: "board_default" });

    const result = removeCardLocalDraft("A", written.version);

    expect(result.ok).toBe(false);
    expect(result.reason).toBe("storage-failure");
    expect(result.error).toContain("存储已满");
    expect(readDraft(cardLocalDraftStorageKey("A"))?.text).toBe("本机候选");
  });

  it("removeCardLocalDraftIfUnchanged：无记录算无事可做（ok=true），有更新的版本算 version-guard", () => {
    const nothing = removeCardLocalDraftIfUnchanged("A", null);
    expect(nothing.ok).toBe(true);
    expect(nothing.removed).toBe(false);
    expect(nothing.reason).toBe("missing-record");

    const written = writeCardLocalDraft("A", "别页写下的新输入", { boardId: "board_default" });
    const guarded = removeCardLocalDraftIfUnchanged("A", written.version - 1);
    expect(guarded.ok).toBe(false);
    expect(guarded.reason).toBe("version-guard");
    expect(readDraft(cardLocalDraftStorageKey("A"))?.text).toBe("别页写下的新输入");
  });

  it("removeCardLocalDraftIfUnchanged：版本一致但删除真失败 → storage-failure（不是「故意保留」）", () => {
    installStorage({ failRemove: new Error("存储被策略禁止写入") });
    const written = writeCardLocalDraft("A", "服务器已收到这一版", { boardId: "board_default" });

    const result = removeCardLocalDraftIfUnchanged("A", written.version);

    expect(result.ok).toBe(false);
    expect(result.removed).toBe(false);
    expect(result.reason).toBe("storage-failure");
    expect(result.error).toContain("存储被策略禁止写入");
    expect(readDraft(cardLocalDraftStorageKey("A"))?.text).toBe("服务器已收到这一版");
  });
});

describe("[13] 写入路径同样如实返回（防回归）", () => {
  it("写入失败带真实原因；失败不推进版本，成功后的版本号不被失败次数影响", () => {
    installStorage({ failSet: quotaError() });
    const failed = writeCardLocalDraft("A", "写不进去", { boardId: "board_default" });
    expect(failed.ok).toBe(false);
    expect(failed.error).toContain("存储已满");
    expect(readDraft(cardLocalDraftStorageKey("A"))).toBeNull();

    installStorage();
    const ok = writeCardLocalDraft("A", "这次写进去了", { boardId: "board_default" });
    expect(ok.ok).toBe(true);
    expect(ok.version).toBe(1);
  });

  it("普通草稿写入失败同样返回原因", () => {
    installStorage({ failSet: new Error("被禁用") });
    const result = writeDraft(draftStorageKey("chat", "t1"), "写不进去", 1);
    expect(result.ok).toBe(false);
    expect(result.error).toContain("被禁用");
  });
});
