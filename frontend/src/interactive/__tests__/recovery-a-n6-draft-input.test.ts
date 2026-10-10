/**
 * [recovery-A / N6] 未完成的附加字段与正文一起保存与恢复（drafts.ts 层）。
 *
 * 触发：编辑卡片正文和附加字段（网址/标题/文件名/图片名/代码语言），不完成编辑 →
 * 刷新或正常关闭重开 → 再打开编辑。旧行为：正文能恢复，附加字段全部回退成正式卡片的旧值
 * （它们只留在组件 ref 里，从未进入恢复来源）。
 *
 * 本文件断言：
 * - 同一对象的完整未完成输入（正文 + 适用附加字段 + **空值**）写进同一条本机记录；
 * - 兼容旧的「仅正文」记录与最早期写入的纯文本记录；
 * - 归属、清除依据、正文空串等既有规则不变（不新增第二写者、不产生第二套判断）；
 * - 「刷新/重开」在本机记录层面用「重新读取存储并重建恢复来源」探针复用。
 *
 * 标注：【纯逻辑 + 真实本地存储】不挂 Vue、不建 store。真实浏览器重开由 D 负责。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  cardDraftMetaFieldsForKind,
  cardLocalDraftStorageKey,
  normalizeCardDraftMeta,
  readCardDraftInput,
  readCardDraftInputForBoard,
  readCardLocalDraft,
  restoreCardDraftInput,
  writeCardDraftInput,
  writeCardLocalClear,
  writeCardLocalDraft,
} from "../drafts";

const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");

function installStorage(): { map: Map<string, string> } {
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
        map.delete(key);
      },
      clear() {
        map.clear();
      },
    },
  });
  return { map };
}

function rawRecord(cardId: string): Record<string, unknown> | null {
  const raw = localStorage.getItem(cardLocalDraftStorageKey(cardId));
  return raw === null ? null : (JSON.parse(raw) as Record<string, unknown>);
}

beforeEach(() => {
  installStorage();
});

afterEach(() => {
  if (originalDescriptor) Object.defineProperty(globalThis, "localStorage", originalDescriptor);
});

describe("N6 完整未完成输入写进同一条本机记录", () => {
  it("正文 + 四个附加字段一次写入、原样读回", () => {
    const written = writeCardDraftInput(
      "c1",
      { text: "新正文\n第二行", meta: { name: "架构图.png", language: "python", href: "https://example.com/x", title: "示例" } },
      { boardId: "b1", seq: 7 },
    );
    expect(written.ok).toBe(true);

    const input = readCardDraftInput("c1");
    expect(input?.text).toBe("新正文\n第二行");
    expect(input?.meta).toEqual({
      name: "架构图.png",
      language: "python",
      href: "https://example.com/x",
      title: "示例",
    });
    // 记录仍然是「编辑副本」，归属与序号照旧写在自己身上
    const record = readCardLocalDraft("c1");
    expect(record?.kind).toBe("draft");
    expect(record?.boardId).toBe("b1");
    expect(record?.seq).toBe(7);
    expect(record?.version).toBe(1);
  });

  it("空串是有效输入：明确清空的字段必须原样保留（不被当成「没有改」）", () => {
    writeCardDraftInput("c1", { text: "正文", meta: { href: "", title: "" } });
    const input = readCardDraftInput("c1");
    expect(input?.meta && Object.prototype.hasOwnProperty.call(input.meta, "href")).toBe(true);
    expect(input?.meta?.href).toBe("");
    expect(input?.meta?.title).toBe("");
  });

  it("只改正文（不传附加字段）：写出的记录与旧格式逐字节一致（没有 meta 键）", () => {
    writeCardLocalDraft("c1", "只有正文", { boardId: "b1", seq: 1 });
    const stored = rawRecord("c1");
    expect(stored).not.toBeNull();
    expect(Object.prototype.hasOwnProperty.call(stored, "meta")).toBe(false);
    expect(readCardDraftInput("c1")?.meta).toBeUndefined();
  });

  it("版本号仍然每次递增（写附加字段不会另起一条记录）", () => {
    expect(writeCardLocalDraft("c1", "第一版").version).toBe(1);
    expect(writeCardDraftInput("c1", { text: "第二版", meta: { name: "a" } }).version).toBe(2);
    expect(writeCardDraftInput("c1", { text: "第三版", meta: { name: "b" } }).version).toBe(3);
    expect(rawRecord("c1")?.version).toBe(3);
    expect(rawRecord("c1")?.text).toBe("第三版");
  });

  it("附加字段归一：只认四个已知字段、只认字符串；一个都没有时不写 meta", () => {
    expect(normalizeCardDraftMeta(undefined)).toBeNull();
    expect(normalizeCardDraftMeta(null)).toBeNull();
    expect(normalizeCardDraftMeta({})).toBeNull();
    expect(normalizeCardDraftMeta({ name: 42 as unknown as string, 未知: "x" } as never)).toBeNull();
    expect(normalizeCardDraftMeta({ name: 42 as unknown as string, title: "t" } as never)).toEqual({ title: "t" });
    expect(normalizeCardDraftMeta({ name: "" })).toEqual({ name: "" });
  });
});

describe("N6 兼容旧记录：仅正文、最早期纯文本、损坏值", () => {
  it("旧「仅正文」记录：正文能恢复，附加字段回退正式值（不是清空）", () => {
    // 旧版本实现写下的记录：有 JSON 外壳与 text，但没有 meta
    localStorage.setItem(
      cardLocalDraftStorageKey("c1"),
      JSON.stringify({ text: "旧正文草稿", updatedAt: 5, seq: 2, kind: "draft", version: 3 }),
    );
    const input = readCardDraftInput("c1");
    expect(input?.text).toBe("旧正文草稿");
    expect(input?.meta).toBeUndefined();
  });

  it("最早期纯文本记录：非 JSON 的值按正文认（可继续恢复）", () => {
    localStorage.setItem(cardLocalDraftStorageKey("c1"), "纯文本老草稿");
    const record = readCardLocalDraft("c1");
    expect(record?.text).toBe("纯文本老草稿");
    expect(record?.kind).toBeUndefined();
    expect(readCardDraftInput("c1")?.text).toBe("纯文本老草稿");
  });

  it("损坏值不当正文：JSON 形状不对、或像 JSON 结构的乱码一律按没有记录", () => {
    localStorage.setItem(cardLocalDraftStorageKey("c1"), JSON.stringify({ text: 42 }));
    expect(readCardLocalDraft("c1")).toBeNull();
    localStorage.setItem(cardLocalDraftStorageKey("c1"), "{坏掉的 JSON");
    expect(readCardLocalDraft("c1")).toBeNull();
    localStorage.setItem(cardLocalDraftStorageKey("c1"), JSON.stringify(["数组"]));
    expect(readCardLocalDraft("c1")).toBeNull();
  });

  it("待同步的清除依据不是草稿：不给恢复来源", () => {
    writeCardLocalClear("c1", { boardId: "b1", seq: 4 });
    expect(readCardLocalDraft("c1")?.kind).toBe("cleared");
    expect(readCardDraftInputForBoard("c1", "b1")).toBeNull();
  });
});

describe("N6 归属与卡片隔离", () => {
  it("别的板面的本机记录不给当前板面恢复；没有归属的旧记录不拦", () => {
    writeCardDraftInput("c1", { text: "b2 的稿", meta: { name: "n" } }, { boardId: "b2" });
    expect(readCardDraftInputForBoard("c1", "b1")).toBeNull();
    expect(readCardDraftInputForBoard("c1", "b2")?.text).toBe("b2 的稿");

    localStorage.setItem(
      cardLocalDraftStorageKey("c9"),
      JSON.stringify({ text: "没有归属的旧稿", updatedAt: 1, seq: 1, kind: "draft", version: 1 }),
    );
    expect(readCardDraftInputForBoard("c9", "b1")?.text).toBe("没有归属的旧稿");
  });

  it("多张卡片各写各的，互不串内容", () => {
    writeCardDraftInput("c1", { text: "卡 1 正文", meta: { name: "卡 1 名" } }, { boardId: "b1" });
    writeCardDraftInput("c2", { text: "卡 2 正文", meta: { name: "卡 2 名" } }, { boardId: "b1" });
    expect(readCardDraftInput("c1")?.meta?.name).toBe("卡 1 名");
    expect(readCardDraftInput("c2")?.meta?.name).toBe("卡 2 名");
    expect(readCardDraftInput("c2")?.text).toBe("卡 2 正文");
  });
});

describe("N6 恢复来源重建（restoreCardDraftInput）", () => {
  it("适用字段按卡片种类：url=网址+标题，file/image=名称，code=语言，文字注释没有附加字段", () => {
    expect(cardDraftMetaFieldsForKind("url")).toEqual(["href", "title"]);
    expect(cardDraftMetaFieldsForKind("file")).toEqual(["name"]);
    expect(cardDraftMetaFieldsForKind("image")).toEqual(["name"]);
    expect(cardDraftMetaFieldsForKind("code")).toEqual(["language"]);
    expect(cardDraftMetaFieldsForKind("text")).toEqual([]);
    expect(cardDraftMetaFieldsForKind("reply")).toEqual([]);
  });

  it("url：正文与网址、标题一起恢复；空串覆盖正式值（不把用户清掉的旧值塞回来）", () => {
    const restored = restoreCardDraftInput({
      kind: "url",
      content: "旧正文",
      meta: { href: "https://old.example", title: "旧标题" },
      hasUnfinishedInput: true,
      draftText: "新正文",
      draftMeta: { href: "https://new.example", title: "" },
    });
    expect(restored.text).toBe("新正文");
    expect(restored.meta).toEqual({ href: "https://new.example", title: "" });
  });

  it("file / code：名称与语言分别恢复；没有附加快照的旧记录回退正式值", () => {
    expect(
      restoreCardDraftInput({
        kind: "file",
        content: "材料说明",
        meta: { name: "旧文件名.pdf" },
        hasUnfinishedInput: true,
        draftText: "材料说明",
        draftMeta: { name: "新文件名.pdf" },
      }).meta,
    ).toEqual({ name: "新文件名.pdf" });

    expect(
      restoreCardDraftInput({
        kind: "file",
        content: "材料说明",
        meta: { name: "旧文件名.pdf" },
        hasUnfinishedInput: true,
        draftText: "改过的说明",
        draftMeta: null,
      }).meta,
    ).toEqual({ name: "旧文件名.pdf" });

    expect(
      restoreCardDraftInput({
        kind: "code",
        content: "print(1)",
        meta: { language: "python" },
        hasUnfinishedInput: true,
        draftText: "print(2)",
        draftMeta: { language: "javascript" },
      }).meta,
    ).toEqual({ language: "javascript" });
  });

  it("仅改附加字段：正文保持正式内容，附加字段用未完成输入", () => {
    const restored = restoreCardDraftInput({
      kind: "code",
      content: "print(1)",
      meta: { language: "python" },
      hasUnfinishedInput: true,
      draftText: "print(1)",
      draftMeta: { language: "rust" },
    });
    expect(restored.text).toBe("print(1)");
    expect(restored.meta.language).toBe("rust");
  });

  it("输入清空正文：空草稿是有效编辑状态（不许用正式正文回填）", () => {
    const restored = restoreCardDraftInput({
      kind: "text",
      content: "正式正文还在",
      meta: {},
      hasUnfinishedInput: true,
      draftText: "",
    });
    expect(restored.hasUnfinishedInput).toBe(true);
    expect(restored.text).toBe("");
  });

  it("没有未完成输入：全部回退正式内容", () => {
    const restored = restoreCardDraftInput({
      kind: "url",
      content: "正式正文",
      meta: { href: "https://formal.example", title: "正式标题" },
      hasUnfinishedInput: false,
    });
    expect(restored.text).toBe("正式正文");
    expect(restored.meta).toEqual({ href: "https://formal.example", title: "正式标题" });
  });
});

describe("N6 「重新读取存储并重建恢复来源」探针（刷新/重开的记录层替身）", () => {
  it("写入完整未完成输入 → 重新读取存储 → 重建：五个字段全部一致", () => {
    writeCardDraftInput(
      "c1",
      { text: "未完成的正文", meta: { name: "未完成的名字", language: "go", href: "https://draft.example", title: "未完成的标题" } },
      { boardId: "b1", seq: 3 },
    );

    // 重新读取（不依赖任何内存状态）：模拟刷新/重开后的恢复来源重建
    const reread = readCardDraftInputForBoard("c1", "b1");
    expect(reread).not.toBeNull();

    const restoredByKind: Record<string, { text: string; meta: Record<string, string | undefined> }> = {};
    for (const kind of ["text", "file", "code", "url"]) {
      const restored = restoreCardDraftInput({
        kind,
        content: "正式内容",
        meta: {},
        hasUnfinishedInput: reread !== null,
        draftText: reread?.text ?? "",
        draftMeta: reread?.meta ?? null,
      });
      restoredByKind[kind] = { text: restored.text, meta: { ...restored.meta } };
    }

    expect(restoredByKind.url.text).toBe("未完成的正文");
    expect(restoredByKind.url.meta).toEqual({ href: "https://draft.example", title: "未完成的标题" });
    expect(restoredByKind.file.meta).toEqual({ name: "未完成的名字" });
    expect(restoredByKind.code.meta).toEqual({ language: "go" });
    expect(restoredByKind.text.meta).toEqual({});
    // 再读一次仍然一致（可复跑）
    expect(readCardDraftInputForBoard("c1", "b1")?.meta?.title).toBe("未完成的标题");
  });
});
