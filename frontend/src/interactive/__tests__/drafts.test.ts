/**
 * 草稿持久化的纯逻辑（契约 §9.4 / §9.5）。
 *
 * 这里验证「存储这一层」的硬约束：
 * - 不同作用域/不同对象的键互不干扰，空 id 有稳定默认键；
 * - 没记录、内容损坏、形状不对都不许冒充草稿；
 * - 存储不可用（隐私模式连读属性都抛、配额满）时必须**明确失败**，不许静默吞掉；
 * - 序号：小于当前序号的回执属于旧内容，必须丢弃。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  draftStorageAvailable,
  draftStorageKey,
  isStaleReceipt,
  readDraft,
  removeDraft,
  UNBOUND_DRAFT_ID,
  writeDraft,
} from "../drafts";

const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");

/** 换成「访问就抛」的版本：隐私模式 / 企业策略下的真实形态 */
function breakLocalStorage(message: string): void {
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get() {
      throw new Error(message);
    },
  });
}

/** 换成 setItem 抛配额错误的版本 */
function fullLocalStorage(): void {
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: () => null,
      setItem: () => {
        const err = new Error("quota");
        err.name = "QuotaExceededError";
        throw err;
      },
      removeItem: () => undefined,
    },
  });
}

function restoreLocalStorage(): void {
  if (originalDescriptor) Object.defineProperty(globalThis, "localStorage", originalDescriptor);
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  restoreLocalStorage();
});

describe("存储键", () => {
  it("不同作用域、不同对象互不干扰；空 id 用稳定默认键", () => {
    expect(draftStorageKey("chat", "t1")).toBe("qio.draft.chat.t1");
    expect(draftStorageKey("card", "c1")).toBe("qio.draft.card.c1");
    expect(draftStorageKey("chat", "t1")).not.toBe(draftStorageKey("card", "t1"));
    expect(draftStorageKey("chat", "")).toBe("qio.draft.chat.default");
    expect(draftStorageKey("card", "")).toBe("qio.draft.card.default");
    expect(UNBOUND_DRAFT_ID).toBe("__unbound__");
  });
});

describe("读写一条草稿", () => {
  it("写进去什么就读出来什么（含换行），并记住序号", () => {
    const key = draftStorageKey("chat", "t1");
    const result = writeDraft(key, "第一行\n第二行", 3);
    expect(result.ok).toBe(true);
    const record = readDraft(key);
    expect(record?.text).toBe("第一行\n第二行");
    expect(record?.seq).toBe(3);
    expect(typeof record?.updatedAt).toBe("number");
    expect(record === null ? 0 : record.updatedAt).toBeGreaterThan(0);
  });

  it("没有记录 / 内容损坏 / 形状不对都不冒充草稿", () => {
    const key = draftStorageKey("chat", "t1");
    expect(readDraft(key)).toBeNull();
    localStorage.setItem(key, "{不是 JSON");
    expect(readDraft(key)).toBeNull();
    localStorage.setItem(key, JSON.stringify({ text: 42 }));
    expect(readDraft(key)).toBeNull();
    localStorage.setItem(key, JSON.stringify(["数组也不行"]));
    expect(readDraft(key)).toBeNull();
    // 旧版本可能没写 seq/updatedAt：正文还在就认，序号按 0 处理（不会因此变“过期”）
    localStorage.setItem(key, JSON.stringify({ text: "只有正文" }));
    expect(readDraft(key)?.text).toBe("只有正文");
    expect(readDraft(key)?.seq).toBe(0);
  });

  it("删除后读不到；重复删除不抛", () => {
    const key = draftStorageKey("card", "c1");
    writeDraft(key, "待删除", 1);
    expect(readDraft(key)?.text).toBe("待删除");
    removeDraft(key);
    expect(readDraft(key)).toBeNull();
    removeDraft(key);
  });
});

describe("迟到的保存回执", () => {
  it("小于当前序号的回执过期；等于或大于不算过期", () => {
    expect(isStaleReceipt(3, 4)).toBe(true);
    expect(isStaleReceipt(4, 4)).toBe(false);
    expect(isStaleReceipt(5, 4)).toBe(false);
  });
});

describe("存储不可用时必须明确失败（不许静默吞掉）", () => {
  it("连读 localStorage 都抛：写入失败带原因，读取当作没有草稿", () => {
    breakLocalStorage("SecurityError: 本地存储被禁用");
    const key = draftStorageKey("chat", "t1");
    const result = writeDraft(key, "写不进去", 1);
    expect(result.ok).toBe(false);
    expect(result.error).toContain("本地存储被禁用");
    expect(readDraft(key)).toBeNull();
    removeDraft(key); // 删除失败不许抛出去
    const available = draftStorageAvailable();
    expect(available.ok).toBe(false);
    expect(available.error).toContain("本地存储被禁用");
  });

  it("配额满：说清是「存储满了」，不是含糊的失败", () => {
    fullLocalStorage();
    const result = writeDraft(draftStorageKey("chat", "t1"), "很大的草稿", 1);
    expect(result.ok).toBe(false);
    expect(result.error).toContain("存储已满");
  });

  it("环境里根本没有 localStorage：同样明确失败", () => {
    Object.defineProperty(globalThis, "localStorage", { configurable: true, value: undefined });
    const result = writeDraft(draftStorageKey("card", "c1"), "草稿", 1);
    expect(result.ok).toBe(false);
    expect(result.error).toContain("本地存储");
    expect(draftStorageAvailable().ok).toBe(false);
  });
});
