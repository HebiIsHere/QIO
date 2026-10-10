/**
 * [recovery-A / N6] BoardCard 编辑态的完整未完成输入恢复（真实组件 + 真实 store + 真实本地存储）。
 *
 * 触发：编辑卡片正文和附加字段，不完成编辑 → 刷新或正常关闭重开 → 再打开编辑。
 * 旧行为：正文可恢复，网址、标题、文件名、图片名、代码语言都回退成正式卡片的旧值。
 *
 * 本文件的「刷新/重开」在本机记录层面用**重新读取存储并重建恢复来源**的探针复用：
 * 每次挂载都是新的组件实例、新的 Pinia，恢复来源只来自 localStorage；
 * 真实浏览器进程重开由 D 负责（本文件不冒充）。
 *
 * 标注：【真实组件 DOM + 真实 localStorage】；存储写失败为模拟（配额异常）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";
import { cardLocalDraftStorageKey, readCardLocalDraft, writeCardDraftInput } from "../../../interactive/drafts";
import type { BoardCard as BoardCardType } from "../../../interactive/types";

const CARD = {
  id: "c1",
  kind: "url",
  x: 0,
  y: 0,
  w: 280,
  h: 180,
  content: "正式正文",
  meta: { href: "https://old.example", title: "旧标题" },
  deleted: false,
  folded: false,
  hidden: false,
  checked: true,
  bookmarked: false,
  createdAt: "2026-10-10T00:00:00.000Z",
  updatedAt: "2026-10-10T00:00:00.000Z",
} as unknown as BoardCardType;

function props(overrides: Partial<typeof CARD> = {}) {
  return {
    card: { ...(CARD as unknown as Record<string, unknown>), ...overrides } as unknown as BoardCardType,
    selected: true,
    highlight: false,
    dragging: false,
    x: 0,
    y: 0,
    groupName: null,
    groups: [],
    toolbarLeft: 0,
    toolbarTop: 0,
    multi: false,
    connecting: false,
  };
}

function seedInput(cardId: string, text: string, meta?: Record<string, string>): void {
  writeCardDraftInput(cardId, meta ? { text, meta } : { text }, { boardId: "b1", seq: 5 });
}

function value(wrapper: ReturnType<typeof mount>, selector: string): string {
  const found = wrapper.find(selector);
  expect(found.exists(), selector + " 应该存在").toBe(true);
  return (found.element as HTMLInputElement | HTMLTextAreaElement).value;
}

async function openEditor(wrapper: ReturnType<typeof mount>) {
  await wrapper.find('[data-im="card-edit"]').trigger("click");
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  const store = useInteractiveStore();
  store.boardId = "b1";
  store.board = { ...board.emptyState("b1"), selection: ["c1"] } as never;
});

describe("N6 打开编辑器时恢复完整未完成输入", () => {
  it("url 卡片：正文、网址、标题一起恢复", async () => {
    seedInput("c1", "未完成正文", { href: "https://draft.example/x", title: "未完成标题" });
    const w = mount(BoardCard, { props: props({ kind: "url" }) });
    await openEditor(w);

    expect(value(w, '[data-im="card-editor"]')).toBe("未完成正文");
    expect(value(w, '[data-im="card-meta-href"]')).toBe("https://draft.example/x");
    expect(value(w, '[data-im="card-meta-title"]')).toBe("未完成标题");
  });

  it("file 卡片：名称恢复（不是正式卡片上的旧名）", async () => {
    seedInput("c1", "材料说明", { name: "新文件名.pdf" });
    const w = mount(BoardCard, {
      props: props({ kind: "file", content: "材料说明", meta: { name: "旧文件名.pdf" } }),
    });
    await openEditor(w);
    expect(value(w, '[data-im="card-meta-name"]')).toBe("新文件名.pdf");
  });

  it("image 卡片：图片名恢复", async () => {
    seedInput("c1", "", { name: "新图.png" });
    const w = mount(BoardCard, {
      props: props({ kind: "image", content: "", meta: { name: "旧图.png" } }),
    });
    await openEditor(w);
    expect(value(w, '[data-im="card-meta-name"]')).toBe("新图.png");
  });

  it("code 卡片：语言恢复", async () => {
    seedInput("c1", "print(1)", { language: "rust" });
    const w = mount(BoardCard, {
      props: props({ kind: "code", content: "print(1)", meta: { language: "python" } }),
    });
    await openEditor(w);
    expect(value(w, '[data-im="card-meta-language"]')).toBe("rust");
  });

  it("文字注释：只有正文，没有附加字段输入（旧「仅正文」记录也能恢复）", async () => {
    seedInput("c1", "未完成的想法");
    const w = mount(BoardCard, { props: props({ kind: "text", content: "正式正文", meta: {} }) });
    await openEditor(w);
    expect(value(w, '[data-im="card-editor"]')).toBe("未完成的想法");
    expect(w.find('[data-im="card-meta-href"]').exists()).toBe(false);
    expect(w.find('[data-im="card-meta-title"]').exists()).toBe(false);
    expect(w.find('[data-im="card-meta-name"]').exists()).toBe(false);
    expect(w.find('[data-im="card-meta-language"]').exists()).toBe(false);
  });

  it("用户清空的字段必须保持为空，不许用正式值回填", async () => {
    seedInput("c1", "正文", { href: "", title: "" });
    const w = mount(BoardCard, { props: props({ kind: "url" }) });
    await openEditor(w);
    expect(value(w, '[data-im="card-meta-href"]')).toBe("");
    expect(value(w, '[data-im="card-meta-title"]')).toBe("");
  });

  it("仅改附加字段：正文仍是正式内容，附加字段用未完成输入", async () => {
    seedInput("c1", "材料说明", { name: "新文件名.pdf" });
    const w = mount(BoardCard, {
      props: props({ kind: "file", content: "材料说明", meta: { name: "旧文件名.pdf" } }),
    });
    await openEditor(w);
    expect(value(w, '[data-im="card-editor"]')).toBe("材料说明");
    expect(value(w, '[data-im="card-meta-name"]')).toBe("新文件名.pdf");
  });

  it("重新读取存储重建恢复来源（刷新/重开的记录层替身）：输入 → 落盘 → 新实例读到完整输入", async () => {
    const store = useInteractiveStore();
    /**
     * 合并后的 store 用 writeCardDraftInput 把「正文 + 附加字段」一次写进同一条记录
     * （Lead 的 setCardDraftInput）。这里按同一形状提供写入，才能验证「落盘 → 重读」这一段。
     */
    (store as unknown as Record<string, unknown>).setCardDraftInput = (cardId: string, input: never) => {
      writeCardDraftInput(cardId, input, { boardId: "b1", seq: 9 });
    };

    const first = mount(BoardCard, { props: props({ kind: "url" }) });
    await openEditor(first);
    await first.find('[data-im="card-editor"]').setValue("刷新前的未完成正文");
    await first.find('[data-im="card-meta-href"]').setValue("https://draft.example/x");
    await first.find('[data-im="card-meta-title"]').setValue("刷新前的标题");
    expect(readCardLocalDraft("c1")?.meta?.title).toBe("刷新前的标题");
    first.unmount();

    // 新 Pinia + 新组件实例：恢复来源只剩存储里的那条记录
    setActivePinia(createPinia());
    const reopened = useInteractiveStore();
    reopened.boardId = "b1";
    reopened.board = { ...board.emptyState("b1"), selection: ["c1"] } as never;
    const second = mount(BoardCard, { props: props({ kind: "url" }) });
    await openEditor(second);
    expect(value(second, '[data-im="card-editor"]')).toBe("刷新前的未完成正文");
    expect(value(second, '[data-im="card-meta-href"]')).toBe("https://draft.example/x");
    expect(value(second, '[data-im="card-meta-title"]')).toBe("刷新前的标题");
  });

  it("多卡片：各卡片的未完成输入互不串", async () => {
    writeCardDraftInput("c1", { text: "卡 1 正文", meta: { title: "卡 1 标题" } }, { boardId: "b1", seq: 1 });
    writeCardDraftInput("c2", { text: "卡 2 正文", meta: { title: "卡 2 标题" } }, { boardId: "b1", seq: 1 });
    const w = mount(BoardCard, { props: props({ kind: "url" }) });
    await openEditor(w);
    expect(value(w, '[data-im="card-editor"]')).toBe("卡 1 正文");
    expect(value(w, '[data-im="card-meta-title"]')).toBe("卡 1 标题");
  });
});

describe("N6 输入过程把正文与附加字段作为同一份未完成输入保存", () => {
  it("改网址 / 标题 / 正文都调用同一次完整输入写入（含空串）", async () => {
    const store = useInteractiveStore();
    const writer = vi.fn();
    (store as unknown as Record<string, unknown>).setCardDraftInput = writer;

    const w = mount(BoardCard, { props: props({ kind: "url" }) });
    await openEditor(w);
    expect(writer).toHaveBeenCalledTimes(1);

    await w.find('[data-im="card-meta-href"]').setValue("https://typed.example");
    expect(writer).toHaveBeenLastCalledWith("c1", {
      text: "正式正文",
      meta: { href: "https://typed.example", title: "旧标题" },
    });

    await w.find('[data-im="card-meta-title"]').setValue("");
    expect(writer).toHaveBeenLastCalledWith("c1", {
      text: "正式正文",
      meta: { href: "https://typed.example", title: "" },
    });

    await w.find('[data-im="card-editor"]').setValue("改过的正文");
    expect(writer).toHaveBeenLastCalledWith("c1", {
      text: "改过的正文",
      meta: { href: "https://typed.example", title: "" },
    });
  });

  it("本机存储写失败：如实提示恢复能力受限，输入内容仍留在编辑器里", async () => {
    const store = useInteractiveStore();
    const real = localStorage;
    /** 只有这张卡片的本机记录键写不进去（配额满 / 策略禁用），其它键不受影响 */
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: {
        get length() {
          return real.length;
        },
        key: (index: number) => real.key(index),
        getItem: (key: string) => real.getItem(key),
        removeItem: (key: string) => real.removeItem(key),
        setItem: (key: string, item: string) => {
          if (key === cardLocalDraftStorageKey("c1")) {
            const err = new Error("quota");
            err.name = "QuotaExceededError";
            throw err;
          }
          real.setItem(key, item);
        },
      },
    });

    const w = mount(BoardCard, { props: props({ kind: "url" }) });
    await openEditor(w);

    // 失败必须如实说清恢复能力受限；输入内容仍在编辑器里（不因失败被清掉）
    expect(w.text()).toMatch(/本机也没能留下恢复副本/);
    expect(value(w, '[data-im="card-editor"]')).toBe("正式正文");
    expect(value(w, '[data-im="card-meta-href"]')).toBe("https://old.example");
    expect(store.draftLocalStateFor("card:c1").ok).toBe(false);
    expect(store.draftLocalStateFor("card:c1").error).toMatch(/存储已满/);
    Object.defineProperty(globalThis, "localStorage", { configurable: true, value: real });
  });
});

describe("N6 完成编辑后的清理与冲突未决", () => {
  it("完成编辑：正式变更带上正文与附加字段，并登记草稿清除；编辑器关闭", async () => {
    seedInput("c1", "未完成正文", { href: "https://draft.example", title: "未完成标题" });
    const store = useInteractiveStore();
    const w = mount(BoardCard, { props: props({ kind: "url" }) });
    await openEditor(w);

    await w.find(".btn.primary").trigger("click");

    const patches = w.emitted("patch") as unknown[][];
    expect(patches?.length).toBe(1);
    expect(patches?.[0]?.[0]).toBe("c1");
    expect(patches?.[0]?.[1]).toMatchObject({
      content: "未完成正文",
      meta: { href: "https://draft.example", title: "未完成标题" },
    });
    expect(store.pendingDraftClearKeys).toContain("card:c1");
    expect(w.find('[data-im="card-editor"]').exists()).toBe(false);
  });

  it("冲突未决：编辑器显示本机候选 + 本机记录里的附加字段，且不写草稿（打开≠选择）", async () => {
    seedInput("c1", "本机记录正文", { href: "https://local.example", title: "本机标题" });
    const store = useInteractiveStore();
    const writer = vi.fn();
    (store as unknown as Record<string, unknown>).setCardDraftInput = writer;
    vi.spyOn(store, "draftConflictFor").mockReturnValue({ local: "本机候选正文", server: "服务器正文" });

    const w = mount(BoardCard, { props: props({ kind: "url" }) });
    await openEditor(w);

    expect(value(w, '[data-im="card-editor"]')).toBe("本机候选正文");
    expect(value(w, '[data-im="card-meta-href"]')).toBe("https://local.example");
    expect(writer).not.toHaveBeenCalled();
    expect(w.text()).toMatch(/先选一份继续/);
  });

  it("另一页面写下的新稿（本机记录被替换）优先于正式内容恢复", async () => {
    seedInput("c1", "另一页面的新稿", { title: "另一页面的标题" });
    const record = readCardLocalDraft("c1");
    expect(record?.text).toBe("另一页面的新稿");
    const w = mount(BoardCard, { props: props({ kind: "url" }) });
    await openEditor(w);
    expect(value(w, '[data-im="card-editor"]')).toBe("另一页面的新稿");
    expect(value(w, '[data-im="card-meta-title"]')).toBe("另一页面的标题");
  });
});
