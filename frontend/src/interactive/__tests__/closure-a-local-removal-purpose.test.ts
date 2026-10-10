/**
 * [closure-A / 反例 R4] 本机记录「处理目的」单元测试（drafts.ts 层）。
 *
 * 反例 R4：本机与服务器两份稿冲突 → 用户选「用服务器上的」→ 本机副本删除失败 →
 * 存储恢复后点重试。旧行为：重试一律按「补写 cleared 依据」（整份草稿清除）处理，
 * 于是「选择服务器稿」变成了「删除这份服务器稿的依据」，重开后用户保留的稿子变空、
 * 草稿集合不再含该键、服务器那份也会被删。
 *
 * 正确行为（本文件断言）：
 * - 目的 remove-local-copy：重试只删本机冗余副本，**绝不写 cleared 依据**；
 * - 目的 clear-draft：才允许 ensureCardLocalClear 补写/确认 cleared 依据；
 * - 版本守卫不削弱：另一页面写入的更新版本一律保留；
 * - 旧登记形状（只记了版本）目的不明：两件破坏性动作都不做，保留两份候选 + 可操作说明。
 *
 * 标注：【纯逻辑 + 真实本地存储】不挂 Vue、不建 store；目的不明之外的三种守卫口径逐一验。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  cardLocalDraftStorageKey,
  cardLocalRecordRole,
  hasCardLocalClear,
  inspectLegacyCardLocalRecord,
  normalizeLocalRemovalIntent,
  readCardLocalDraft,
  retryLocalRemovalByPurpose,
  writeCardLocalClear,
  writeCardLocalDraft,
  LEGACY_LOCAL_RECORD_ADVICE,
  UNKNOWN_LOCAL_REMOVAL_PURPOSE_ADVICE,
} from "../drafts";

const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");

/** 记忆型存储：跨调用保留，可随时开关某个键的写入/删除失败（模拟配额满、策略禁用） */
function installStorage(): {
  map: Map<string, string>;
  failSet: (fn: ((key: string) => Error | null) | null) => void;
  failRemove: (fn: ((key: string) => Error | null) | null) => void;
} {
  let fail: ((key: string) => Error | null) | null = null;
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
        const failure = fail?.(key);
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
      fail = fn;
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

/** 旧版本写下的本机记录：没有 kind 字段 */
function writeLegacyRecord(cardId: string, text: string, version: number): void {
  localStorage.setItem(
    cardLocalDraftStorageKey(cardId),
    JSON.stringify({ text, updatedAt: Date.now(), seq: 1, version }),
  );
}

beforeEach(() => {
  installStorage();
});

afterEach(() => {
  if (originalDescriptor) Object.defineProperty(globalThis, "localStorage", originalDescriptor);
});

describe("[R4] remove-local-copy：只删本机冗余副本，绝不写 cleared 依据", () => {
  it("删除失败 → 重试成功：记录真的被删掉，且全程没有 cleared 依据（选服务器稿不许变清除）", () => {
    const { failRemove } = installStorage();
    const written = writeCardLocalDraft("c1", "本机那份候选", { boardId: "board_default" });
    expect(written.ok).toBe(true);
    expect(readCardLocalDraft("c1")?.kind, "基线：磁盘上是编辑副本").toBe("draft");

    // 第一次删除失败（配额满 / 存储被禁用），store 登记 purpose=remove-local-copy
    failRemove((key) => (key === cardLocalDraftStorageKey("c1") ? quotaError() : null));
    const first = retryLocalRemovalByPurpose("c1", {
      purpose: "remove-local-copy",
      expectVersion: written.version,
      guard: "version",
    });
    expect(first.ok).toBe(false);
    expect(first.action).toBe("failed");
    expect(first.reason).toBe("storage-failure");
    expect(readCardLocalDraft("c1")?.kind, "删除真失败时旧副本仍在（内容没丢）").toBe("draft");
    expect(hasCardLocalClear("c1"), "删除失败时绝不能顺手写下 cleared 依据").toBe(false);

    // 存储恢复后点重试：这次必须是「删掉副本」，不是「补写清除依据」
    failRemove(null);
    const retry = retryLocalRemovalByPurpose("c1", {
      purpose: "remove-local-copy",
      expectVersion: written.version,
      guard: "version",
    });
    expect(retry.ok, "重试应当删掉这份本机冗余副本").toBe(true);
    expect(retry.action).toBe("removed");
    expect(retry.purpose).toBe("remove-local-copy");
    expect(readCardLocalDraft("c1"), "本机冗余副本已删除").toBeNull();
    expect(hasCardLocalClear("c1"), "★反例 R4：重试不许写下整份清除的依据").toBe(false);
  });

  it("版本守卫不削弱：另一页面写入更新版本时，重试保留它、不删也不写 cleared", () => {
    const first = writeCardLocalDraft("c1", "我第一次的输入", { boardId: "board_default" });
    const newer = writeCardLocalDraft("c1", "另一页面后来写的更新输入", { boardId: "board_default" });
    expect(newer.version).toBeGreaterThan(first.version);

    const outcome = retryLocalRemovalByPurpose("c1", {
      purpose: "remove-local-copy",
      expectVersion: first.version,
      guard: "version",
    });

    expect(outcome.ok).toBe(false);
    expect(outcome.action, "版本对不上必须是有意保留，不是失败").toBe("kept-newer");
    expect(outcome.reason).toBe("version-guard");
    expect(readCardLocalDraft("c1")?.text, "更新的一版必须原样保留").toBe("另一页面后来写的更新输入");
    expect(hasCardLocalClear("c1"), "被守卫拒绝时更不能写 cleared").toBe(false);
  });

  it("guard=object：旧记录没有版本可比时按对象删除（不写 cleared）", () => {
    writeLegacyRecord("c1", "旧版本写下的副本文本", 1);
    const outcome = retryLocalRemovalByPurpose("c1", {
      purpose: "remove-local-copy",
      expectVersion: null,
      guard: "object",
    });
    expect(outcome.ok).toBe(true);
    expect(outcome.action).toBe("removed");
    expect(readCardLocalDraft("c1")).toBeNull();
    expect(hasCardLocalClear("c1")).toBe(false);
  });

  it("guard=absent：登记时本来就没有记录、现在仍然没有 → 无事可做（不是失败）", () => {
    const outcome = retryLocalRemovalByPurpose("c1", {
      purpose: "remove-local-copy",
      expectVersion: null,
      guard: "absent",
    });
    expect(outcome.ok).toBe(true);
    expect(outcome.action).toBe("nothing");
    expect(readCardLocalDraft("c1")).toBeNull();
  });

  it("guard=absent：登记后另一页面又写了记录 → 拒绝清理，保留新记录", () => {
    writeCardLocalDraft("c1", "登记之后另一页面写的新输入", { boardId: "board_default" });
    const outcome = retryLocalRemovalByPurpose("c1", {
      purpose: "remove-local-copy",
      expectVersion: null,
      guard: "absent",
    });
    expect(outcome.ok).toBe(false);
    expect(outcome.action).toBe("kept-newer");
    expect(readCardLocalDraft("c1")?.text).toBe("登记之后另一页面写的新输入");
  });
});

describe("[R4] clear-draft：只有这个目的才允许补写 cleared 依据", () => {
  it("补写保护成功：磁盘上出现 cleared 依据，并给出可登记的落盘版本（旧稿不再复活）", () => {
    const written = writeCardLocalDraft("c1", "用户清掉的旧稿", { boardId: "board_default" });
    const outcome = retryLocalRemovalByPurpose("c1", {
      purpose: "clear-draft",
      expectVersion: written.version,
      guard: "version",
    });
    expect(outcome.ok).toBe(true);
    expect(outcome.action).toBe("protected");
    expect(typeof outcome.committedVersion, "必须给真实落盘版本，调用方才能登记").toBe("number");
    expect(readCardLocalDraft("c1")?.kind).toBe("cleared");
    expect(hasCardLocalClear("c1")).toBe(true);
  });

  it("幂等：磁盘上已经是 cleared 时不新写、不推进版本", () => {
    const first = writeCardLocalClear("c1", { boardId: "board_default" });
    const again = retryLocalRemovalByPurpose("c1", { purpose: "clear-draft", expectVersion: null, guard: "object" });
    expect(again.ok).toBe(true);
    expect(again.action).toBe("protected");
    expect(again.alreadyProtected).toBe(true);
    expect(again.committedVersion, "幂等时不推进版本（否则登记的确认版本会被换掉）").toBe(first.version);
    expect(readCardLocalDraft("c1")?.version).toBe(first.version);
  });

  it("版本守卫不削弱：清除之后又输入的新版本不许被这次清除覆盖", () => {
    const cleared = writeCardLocalClear("c1", { boardId: "board_default" });
    const typed = writeCardLocalDraft("c1", "清除之后重新输入的新文字", { boardId: "board_default" });
    expect(typed.version).toBeGreaterThan(cleared.version);

    const outcome = retryLocalRemovalByPurpose("c1", {
      purpose: "clear-draft",
      expectVersion: cleared.version,
      guard: "version",
    });
    expect(outcome.ok).toBe(false);
    expect(outcome.action).toBe("kept-newer");
    expect(outcome.reason).toBe("version-guard");
    expect(readCardLocalDraft("c1")?.text, "更新的输入必须原样保留").toBe("清除之后重新输入的新文字");
    expect(readCardLocalDraft("c1")?.kind).toBe("draft");
  });

  it("本机写失败：如实失败 + 真实原因，磁盘上仍是旧副本（不当成完成）", () => {
    const { failSet } = installStorage();
    writeCardLocalDraft("c1", "清除失败留下的旧稿", { boardId: "board_default" });
    failSet((key) => (key === cardLocalDraftStorageKey("c1") ? quotaError() : null));
    const outcome = retryLocalRemovalByPurpose("c1", { purpose: "clear-draft", expectVersion: null, guard: "object" });
    expect(outcome.ok).toBe(false);
    expect(outcome.action).toBe("failed");
    expect(outcome.error, "必须带可直接显示的真实原因").toBeTruthy();
    expect(readCardLocalDraft("c1")?.kind).toBe("draft");
    expect(hasCardLocalClear("c1")).toBe(false);
  });

  it("既有保护不被撤掉：清除依据写好后，重开读取仍能区分「清除」与「草稿」", () => {
    writeCardLocalClear("c1", { boardId: "board_default", seq: 3 });
    const record = readCardLocalDraft("c1");
    expect(record?.kind).toBe("cleared");
    expect(record?.text).toBe("");
    expect(hasCardLocalClear("c1")).toBe(true);
  });
});

describe("[R4·第 7 条] 旧登记/旧记录：无法判断来源或目的时不许当成整份清除", () => {
  it("旧登记只记了版本 → 目的不明：不删、不写，保留两份候选并给可操作说明", () => {
    const written = writeCardLocalDraft("c1", "本机那份候选", { boardId: "board_default" });
    const outcome = retryLocalRemovalByPurpose("c1", written.version);
    expect(outcome.ok).toBe(false);
    expect(outcome.action).toBe("needs-choice");
    expect(outcome.purpose).toBe("unknown");
    expect(outcome.advice, "必须给可操作说明").toBe(UNKNOWN_LOCAL_REMOVAL_PURPOSE_ADVICE);
    expect(readCardLocalDraft("c1")?.text, "本机候选必须保留").toBe("本机那份候选");
    expect(readCardLocalDraft("c1")?.kind, "不许被改写成 cleared 依据").toBe("draft");
    expect(hasCardLocalClear("c1")).toBe(false);
  });

  it("旧登记 null 形状同样目的不明：不删、不写", () => {
    writeCardLocalDraft("c1", "本机那份候选", { boardId: "board_default" });
    const outcome = retryLocalRemovalByPurpose("c1", null);
    expect(outcome.action).toBe("needs-choice");
    expect(readCardLocalDraft("c1")?.kind).toBe("draft");
    expect(hasCardLocalClear("c1")).toBe(false);
  });

  it("旧记录判定：没有 kind 的记录是 unknown（不许猜成 cleared），显式两类各自可判", () => {
    expect(cardLocalRecordRole("c1")).toBe("missing");
    writeCardLocalDraft("c1", "编辑副本", { boardId: "board_default" });
    expect(cardLocalRecordRole("c1")).toBe("draft");
    writeCardLocalClear("c1", { boardId: "board_default" });
    expect(cardLocalRecordRole("c1")).toBe("cleared");
    writeLegacyRecord("c2", "", 1);
    expect(cardLocalRecordRole("c2"), "旧记录没有 kind → 无法判断，不许当成清除依据").toBe("unknown");
    const inspect = inspectLegacyCardLocalRecord("c2");
    expect(inspect.needsUserChoice).toBe(true);
    expect(inspect.advice).toBe(LEGACY_LOCAL_RECORD_ADVICE);
  });

  it("归一化：认识的新形状原样保留，旧形状一律 unknown 且带可操作说明", () => {
    const modern = normalizeLocalRemovalIntent({ purpose: "clear-draft", expectVersion: 2, guard: "version" });
    expect(modern.purpose).toBe("clear-draft");
    expect(modern.expectVersion).toBe(2);
    expect(modern.guard).toBe("version");
    const old = normalizeLocalRemovalIntent(2);
    expect(old.purpose).toBe("unknown");
    expect(old.expectVersion).toBe(2);
    expect(old.advice).toBe(UNKNOWN_LOCAL_REMOVAL_PURPOSE_ADVICE);
  });
});
