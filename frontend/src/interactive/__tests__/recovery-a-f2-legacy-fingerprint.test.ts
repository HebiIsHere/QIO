/**
 * [recovery-A / F2] 旧格式（无 version / version 0）本机副本的删除替换保护（drafts.ts 层）。
 *
 * 反例 F2：本机旧格式记录没有版本，服务器上有一份另一稿 → 用户选择服务器稿 →
 * 本机删除失败 → **另一页面为同一张卡片写下新稿** → 原页重试。旧行为：重试按对象 id 删除，
 * 新的本机记录被删掉（用户后来输入的内容没了）。
 *
 * 正确行为（本文件断言）：
 * - 无版本 / version 0 的记录必须保留一份**可验证的原始身份依据**（内容指纹：text + 记录种类 +
 *   记录时戳等稳定字段，确定性、可复跑、无随机数）；
 * - 重试前用同一依据复核：证明不了「当前仍是原记录」就**保留新稿**（version-guard，不是失败、不重试）；
 * - 没有被替换的旧副本仍可清理；当前格式（正版本号）行为不变；
 * - 未传指纹的两参调用保持既有语义（向后兼容）。
 *
 * 标注：【纯逻辑 + 真实本地存储】不挂 Vue、不建 store；模拟「另一页面写入」与「存储删除失败」。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { DraftRecord } from "../drafts";
import {
  cardLocalDraftStorageKey,
  localRecordFingerprint,
  readCardLocalDraft,
  removeCardLocalDraftIfUnchanged,
  retryLocalRemovalByPurpose,
  writeCardLocalClear,
  writeCardLocalDraft,
} from "../drafts";

const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");

/** 记忆型存储：可随时让 removeItem 失败（模拟配额满 / 策略禁用 / 只读环境） */
function installStorage(): {
  map: Map<string, string>;
  failRemove: (fn: (() => Error | null) | null) => void;
} {
  let fail: (() => Error | null) | null = null;
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
        map.set(key, value);
      },
      removeItem(key: string) {
        const failure = fail?.();
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
    failRemove: (fn) => {
      fail = fn;
    },
  };
}

/** 写入一条「旧格式」本机记录：没有 version（或显式 version 0），可以没有 kind */
function seedLegacyRecord(
  cardId: string,
  record: { text: string; kind?: string; version?: number; updatedAt?: number; seq?: number; boardId?: string },
): void {
  localStorage.setItem(cardLocalDraftStorageKey(cardId), JSON.stringify(record));
}

function rawRecord(cardId: string): Record<string, unknown> | null {
  const raw = localStorage.getItem(cardLocalDraftStorageKey(cardId));
  if (raw === null) return null;
  return JSON.parse(raw) as Record<string, unknown>;
}

/** 每个用例一份全新存储；删除失败通过同一个句柄切换（不能重装，否则会丢掉已写入的记录） */
let storage: ReturnType<typeof installStorage>;

beforeEach(() => {
  storage = installStorage();
});

afterEach(() => {
  if (originalDescriptor) Object.defineProperty(globalThis, "localStorage", originalDescriptor);
});

describe("F2 内容指纹：无版本旧格式记录的身份依据", () => {
  it("没有记录返回 null；有记录返回稳定、可复跑的身份依据", () => {
    expect(localRecordFingerprint(null)).toBeNull();
    seedLegacyRecord("c1", { text: "旧副本", kind: "draft", updatedAt: 1000, seq: 3 });
    const record = readCardLocalDraft("c1");
    const first = localRecordFingerprint(record);
    const second = localRecordFingerprint(record);
    expect(first).toBeTruthy();
    expect(first).toBe(second);
    // 换一个进程重新读盘（JSON 重新解析）也必须得到同一个依据
    expect(localRecordFingerprint(readCardLocalDraft("c1"))).toBe(first);
  });

  it("内容、记录种类、记录时戳、版本任一不同 → 依据不同（不会把新记录认成旧记录）", () => {
    seedLegacyRecord("c1", { text: "旧副本", kind: "draft", updatedAt: 1000, seq: 3 });
    const base = localRecordFingerprint(readCardLocalDraft("c1"));
    seedLegacyRecord("c1", { text: "旧副本改", kind: "draft", updatedAt: 1000, seq: 3 });
    expect(localRecordFingerprint(readCardLocalDraft("c1"))).not.toBe(base);
    seedLegacyRecord("c1", { text: "旧副本", kind: "cleared", updatedAt: 1000, seq: 3 });
    expect(localRecordFingerprint(readCardLocalDraft("c1"))).not.toBe(base);
    seedLegacyRecord("c1", { text: "旧副本", kind: "draft", updatedAt: 2000, seq: 3 });
    expect(localRecordFingerprint(readCardLocalDraft("c1"))).not.toBe(base);
    seedLegacyRecord("c1", { text: "旧副本", kind: "draft", updatedAt: 1000, seq: 3, version: 1 });
    expect(localRecordFingerprint(readCardLocalDraft("c1"))).not.toBe(base);
    seedLegacyRecord("c1", { text: "旧副本", kind: "draft", updatedAt: 1000, seq: 3, boardId: "b9" });
    expect(localRecordFingerprint(readCardLocalDraft("c1"))).not.toBe(base);
    // 同一份内容、同一时间戳：依据必须回到原值（确定性，不含随机成分）
    seedLegacyRecord("c1", { text: "旧副本", kind: "draft", updatedAt: 1000, seq: 3 });
    expect(localRecordFingerprint(readCardLocalDraft("c1"))).toBe(base);
  });

  it("没有 updatedAt 的旧记录：可用的 createdAt / ts 也进依据（不同记录不会撞成同一条）", () => {
    const noStamp: DraftRecord = { text: "只有正文", updatedAt: 0, seq: 0 };
    const withCreatedAt = { text: "只有正文", updatedAt: 0, seq: 0, createdAt: 7 } as unknown as DraftRecord;
    const withTs = { text: "只有正文", updatedAt: 0, seq: 0, ts: 9 } as unknown as DraftRecord;
    expect(localRecordFingerprint(noStamp)).toBeTruthy();
    expect(localRecordFingerprint(withCreatedAt)).not.toBe(localRecordFingerprint(noStamp));
    expect(localRecordFingerprint(withTs)).not.toBe(localRecordFingerprint(noStamp));
    expect(localRecordFingerprint(withCreatedAt)).not.toBe(localRecordFingerprint(withTs));
    // 同一记录重复调用仍然一致
    expect(localRecordFingerprint(withCreatedAt)).toBe(localRecordFingerprint(withCreatedAt));
  });
});

describe("F2 无版本旧格式记录：删除失败后另一页面重写，重试必须保留新稿", () => {
  it("无 version 记录：删除失败 → 另一页面写新稿 → 重试按指纹复核并保留新稿", () => {
    seedLegacyRecord("c1", { text: "旧副本", kind: "draft", updatedAt: 1000, seq: 1, boardId: "b1" });
    const fingerprint = localRecordFingerprint(readCardLocalDraft("c1"));
    expect(fingerprint).toBeTruthy();

    // 登记决定之后、重试之前：存储删除失败（第一次落地失败）
    storage.failRemove(() => new Error("quota"));
    const failed = removeCardLocalDraftIfUnchanged("c1", null, fingerprint);
    expect(failed.ok).toBe(false);
    expect(failed.reason).toBe("storage-failure");
    expect(readCardLocalDraft("c1")?.text).toBe("旧副本");

    // 存储恢复；同一浏览器的另一页面为同一张卡片写下新稿（本机版本推进到 1）
    storage.failRemove(null);
    const written = writeCardLocalDraft("c1", "另一页面的新稿", { boardId: "b1", seq: 2 });
    expect(written.ok).toBe(true);

    // 原页重试那次「删旧副本」：当前记录已经不是当时那一条 → 保留，不算失败、不重试
    const retry = removeCardLocalDraftIfUnchanged("c1", null, fingerprint);
    expect(retry.ok).toBe(false);
    expect(retry.removed).toBe(false);
    expect(retry.reason).toBe("version-guard");
    expect(retry.error).toBeUndefined();

    // 新稿的文字与本机记录都还在
    expect(readCardLocalDraft("c1")?.text).toBe("另一页面的新稿");
    const stored = rawRecord("c1");
    expect(stored?.text).toBe("另一页面的新稿");
    expect(stored?.version).toBe(1);
  });

  it("version 0 记录：同样必须按指纹复核（0 不许当成「按对象删」）", () => {
    seedLegacyRecord("c1", { text: "旧副本", kind: "draft", version: 0, updatedAt: 1000, seq: 1 });
    const fingerprint = localRecordFingerprint(readCardLocalDraft("c1"));

    storage.failRemove(() => new Error("readonly"));
    expect(removeCardLocalDraftIfUnchanged("c1", 0, fingerprint).reason).toBe("storage-failure");
    storage.failRemove(null);

    // 另一页面写入新稿（旧记录没有正版本 → 新记录从 1 开始）
    writeCardLocalDraft("c1", "新稿", { boardId: "b1", seq: 5 });
    const retry = removeCardLocalDraftIfUnchanged("c1", 0, fingerprint);
    expect(retry.reason).toBe("version-guard");
    expect(readCardLocalDraft("c1")?.text).toBe("新稿");
  });

  it("另一页面写入的新记录同样是旧格式（无版本）也不能被删", () => {
    seedLegacyRecord("c1", { text: "旧副本", kind: "draft", updatedAt: 1000, seq: 1 });
    const fingerprint = localRecordFingerprint(readCardLocalDraft("c1"));
    // 另一个仍是旧版本实现的页面直接写下一条无版本记录
    seedLegacyRecord("c1", { text: "旧页面写的新稿", kind: "draft", updatedAt: 4000, seq: 2 });
    const retry = removeCardLocalDraftIfUnchanged("c1", null, fingerprint);
    expect(retry.reason).toBe("version-guard");
    expect(readCardLocalDraft("c1")?.text).toBe("旧页面写的新稿");
  });

  it("记录种类变了（旧副本被新的待确认清除依据替换）也不能被删", () => {
    seedLegacyRecord("c1", { text: "旧副本", kind: "draft", updatedAt: 1000, seq: 1 });
    const fingerprint = localRecordFingerprint(readCardLocalDraft("c1"));
    writeCardLocalClear("c1", { boardId: "b1", seq: 9 });
    expect(removeCardLocalDraftIfUnchanged("c1", null, fingerprint).reason).toBe("version-guard");
    expect(readCardLocalDraft("c1")?.kind).toBe("cleared");
  });

  it("没有被替换的旧副本仍可清理（无版本与 version 0 都覆盖）", () => {
    seedLegacyRecord("c1", { text: "无版本旧副本", kind: "draft", updatedAt: 1000, seq: 1 });
    const fp1 = localRecordFingerprint(readCardLocalDraft("c1"));
    const removed1 = removeCardLocalDraftIfUnchanged("c1", null, fp1);
    expect(removed1.ok).toBe(true);
    expect(removed1.removed).toBe(true);
    expect(readCardLocalDraft("c1")).toBeNull();

    seedLegacyRecord("c2", { text: "version 0 旧副本", kind: "draft", version: 0, updatedAt: 1000, seq: 1 });
    const fp2 = localRecordFingerprint(readCardLocalDraft("c2"));
    const removed2 = removeCardLocalDraftIfUnchanged("c2", 0, fp2);
    expect(removed2.ok).toBe(true);
    expect(removed2.removed).toBe(true);
    expect(readCardLocalDraft("c2")).toBeNull();
  });

  it("记录已经不在了：按指纹清理算「本来就没有」，不报失败", () => {
    seedLegacyRecord("c1", { text: "旧副本", kind: "draft", updatedAt: 1000, seq: 1 });
    const fingerprint = localRecordFingerprint(readCardLocalDraft("c1"));
    localStorage.removeItem(cardLocalDraftStorageKey("c1"));
    const result = removeCardLocalDraftIfUnchanged("c1", null, fingerprint);
    expect(result.ok).toBe(true);
    expect(result.removed).toBe(false);
    expect(result.reason).toBe("missing-record");
  });

  it("当时没有记录时给 null 指纹：现在冒出的记录不许被删", () => {
    const absentFingerprint = localRecordFingerprint(readCardLocalDraft("c1"));
    expect(absentFingerprint).toBeNull();
    seedLegacyRecord("c1", { text: "后来新建的稿", kind: "draft", updatedAt: 1000, seq: 1 });
    const retry = removeCardLocalDraftIfUnchanged("c1", null, absentFingerprint);
    expect(retry.reason).toBe("version-guard");
    expect(readCardLocalDraft("c1")?.text).toBe("后来新建的稿");
  });
});

describe("F2 当前格式（正版本号）行为不变", () => {
  it("版本相等且指纹一致才删；版本被推进或指纹不符都保留", () => {
    writeCardLocalDraft("c1", "第三版", { boardId: "b1", seq: 3 });
    writeCardLocalDraft("c1", "第三版", { boardId: "b1", seq: 3 });
    const third = readCardLocalDraft("c1");
    expect(third?.version).toBe(2);
    const fp = localRecordFingerprint(third);

    // 要不要先删一次？不删：直接模拟另一页面推到下一版
    writeCardLocalDraft("c1", "第四版（另一页面）", { boardId: "b1", seq: 4 });
    expect(removeCardLocalDraftIfUnchanged("c1", 2, fp).reason).toBe("version-guard");
    expect(readCardLocalDraft("c1")?.text).toBe("第四版（另一页面）");

    const fourth = readCardLocalDraft("c1");
    const fp4 = localRecordFingerprint(fourth);
    expect(fourth?.version).toBe(3);
    // 版本相同但依据不符（内容已被改写为同版本）→ 同样保留
    expect(removeCardLocalDraftIfUnchanged("c1", 3, "fp1:deadbeef00000000").reason).toBe("version-guard");
    const removed = removeCardLocalDraftIfUnchanged("c1", 3, fp4);
    expect(removed.ok).toBe(true);
    expect(removed.removed).toBe(true);
    expect(readCardLocalDraft("c1")).toBeNull();
  });

  it("不传指纹的两参调用保持既有语义（无版本按版本守卫保留，version 0 按对象删的旧口径不变）", () => {
    seedLegacyRecord("c1", { text: "无版本旧副本", kind: "draft", updatedAt: 1000, seq: 1 });
    const guarded = removeCardLocalDraftIfUnchanged("c1", null);
    expect(guarded.ok).toBe(false);
    expect(guarded.reason).toBe("version-guard");
    expect(readCardLocalDraft("c1")?.text).toBe("无版本旧副本");

    seedLegacyRecord("c2", { text: "version 0 旧副本", kind: "draft", version: 0, updatedAt: 1000, seq: 1 });
    const deleted = removeCardLocalDraftIfUnchanged("c2", 0);
    expect(deleted.ok).toBe(true);
    expect(deleted.removed).toBe(true);
  });
});

describe("F2 生产重试形状：retryLocalRemovalByPurpose 的指纹守卫", () => {
  it("remove-local-copy + fingerprint：被替换则 kept-newer，未被替换则真的删掉", () => {
    seedLegacyRecord("c1", { text: "旧副本", kind: "draft", updatedAt: 1000, seq: 1 });
    const fingerprint = localRecordFingerprint(readCardLocalDraft("c1"));
    writeCardLocalDraft("c1", "新稿", { boardId: "b1", seq: 2 });
    const kept = retryLocalRemovalByPurpose("c1", {
      purpose: "remove-local-copy",
      expectVersion: null,
      expectFingerprint: fingerprint,
      guard: "fingerprint",
    });
    expect(kept.ok).toBe(false);
    expect(kept.action).toBe("kept-newer");
    expect(readCardLocalDraft("c1")?.text).toBe("新稿");
    // 目的没有被改成整份清除
    expect(readCardLocalDraft("c1")?.kind).toBe("draft");

    localStorage.removeItem(cardLocalDraftStorageKey("c1"));
    seedLegacyRecord("c3", { text: "未被打扰的旧副本", kind: "draft", updatedAt: 1000, seq: 1 });
    const fp3 = localRecordFingerprint(readCardLocalDraft("c3"));
    const done = retryLocalRemovalByPurpose("c3", {
      purpose: "remove-local-copy",
      expectVersion: null,
      expectFingerprint: fp3,
      guard: "fingerprint",
    });
    expect(done.ok).toBe(true);
    expect(done.action).toBe("removed");
    expect(readCardLocalDraft("c3")).toBeNull();
  });
});
