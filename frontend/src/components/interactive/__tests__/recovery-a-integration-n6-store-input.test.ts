/**
 * [recovery-A / 合并后复核] N6：真实 BoardCard + **集成 store 的 setCardDraftInput**（不注入替身）。
 *
 * 目的：验证合并后「过渡行为」已经消失 —— 打开编辑器输入正文与附加字段时，走的是 store 的
 * 完整未完成输入写入入口，本机记录里真的带 meta；重新读取存储并重建恢复来源时，
 * URL+标题 / 文件名 / 图片名 / 代码语言都与未完成输入一致（不是只保存了正文）。
 *
 * 标注：【真实组件 DOM + 真实 store + 真实 localStorage】；服务器响应用 mock（本机记录层不依赖网络）。
 * 不注入 setCardDraftInput：本文件断言的正是 store 自己那一条路径。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as imApi from "../../../services/interactive";
import { readCardLocalDraft } from "../../../interactive/drafts";
import type { BoardCard as BoardCardType } from "../../../interactive/types";

vi.mock("../../../services/api", () => ({ api: { sendTurn: vi.fn() } }));

vi.mock("../../../services/interactive", () => ({
  /**
   * 本文件只验证「本机记录 + 恢复来源」这一段：草稿保存请求永远不返回，
   * 于是成功回执不会在断言之前把本机记录清理掉（防抖回执与本机恢复记录的清理
   * 由 src/stores/__tests__/recovery-a-integration-f2-retry-entry.test.ts 覆盖）。
   * 不这样固定住，慢环境下 600ms 防抖可能先跑完，断言就会看到已被清理的记录。
   */
  saveDrafts: vi.fn(() => new Promise(() => undefined)),
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

type Kind = "text" | "file" | "image" | "code" | "url";

function makeCard(kind: Kind, meta: Record<string, string> = {}) {
  return {
    id: "c1",
    kind,
    content: "正式正文",
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
    meta,
  };
}

function boardPayload(card: ReturnType<typeof makeCard>, drafts: Record<string, string> = {}) {
  return {
    board: { id: "board_default", title: "板面" },
    state: {
      boardId: "board_default",
      seq: 1,
      updatedAt: "2026-10-10T00:00:00.000Z",
      cards: [card],
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

function cardProps(card: ReturnType<typeof makeCard>) {
  return {
    card: card as unknown as BoardCardType,
    selected: true,
    highlight: false,
    dragging: false,
    x: 0,
    y: 0,
    groupName: null,
    groups: [],
    toolbarLeft: 0,
    toolbarTop: 120,
    multi: false,
    connecting: false,
  };
}

function value(wrapper: ReturnType<typeof mount>, selector: string): string {
  const found = wrapper.find(selector);
  expect(found.exists(), selector + " 应该存在").toBe(true);
  return (found.element as HTMLInputElement | HTMLTextAreaElement).value;
}

/** 打开一张卡片的编辑器（工具栏只在选中且为最后点中的那张时渲染） */
async function openEditor(store: ReturnType<typeof useInteractiveStore>, card: ReturnType<typeof makeCard>) {
  const currentBoard = store.board;
  if (!currentBoard) throw new Error("板面还没加载，先 await store.load()");
  // 卡片局部工具栏只在「选中且为最后点中的那张」时渲染
  store.board = { ...currentBoard, selection: ["c1"] };
  const wrapper = mount(BoardCard, { props: cardProps(card) });
  await flushPromises();
  await wrapper.find('[data-im="card-edit"]').trigger("click");
  return wrapper;
}

/** 「重开」：新的 Pinia + 新的 store 实例，恢复来源只能来自存储（重新 load 读服务器事实） */
async function reopen(card: ReturnType<typeof makeCard>, serverDrafts: Record<string, string> = {}) {
  setActivePinia(createPinia());
  const store = useInteractiveStore();
  vi.mocked(imApi.fetchBoardState).mockResolvedValue(boardPayload(card, serverDrafts) as never);
  await store.load();
  await flushPromises();
  return { store, wrapper: await openEditor(store, card) };
}

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  setActivePinia(createPinia());
});

describe("N6 合并后：完整未完成输入经 store 写入本机记录", () => {
  it("集成 store 提供 setCardDraftInput（不是过渡期的只写正文路径）", () => {
    const store = useInteractiveStore();
    expect(typeof (store as unknown as Record<string, unknown>).setCardDraftInput).toBe("function");
  });

  it("url：输入正文、网址、标题后本机记录带 meta；重开后三项都恢复", async () => {
    const card = makeCard("url", { href: "https://old.example", title: "旧标题" });
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(boardPayload(card) as never);
    const store = useInteractiveStore();
    await store.load();
    const wrapper = await openEditor(store, card);

    await wrapper.find('[data-im="card-editor"]').setValue("未完成正文");
    await wrapper.find('[data-im="card-meta-href"]').setValue("https://draft.example/x");
    await wrapper.find('[data-im="card-meta-title"]').setValue("未完成标题");

    // 真实 store 路径写下的本机记录必须带完整附加字段
    expect(readCardLocalDraft("c1")?.meta).toEqual({
      href: "https://draft.example/x",
      title: "未完成标题",
    });
    expect(readCardLocalDraft("c1")?.text).toBe("未完成正文");

    const { wrapper: reopened } = await reopen(card);
    expect(value(reopened, '[data-im="card-editor"]')).toBe("未完成正文");
    expect(value(reopened, '[data-im="card-meta-href"]')).toBe("https://draft.example/x");
    expect(value(reopened, '[data-im="card-meta-title"]')).toBe("未完成标题");
  });

  it("file：文件名随正文一起写入并恢复", async () => {
    const card = makeCard("file", { name: "旧文件名.pdf" });
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(boardPayload(card) as never);
    const store = useInteractiveStore();
    await store.load();
    const wrapper = await openEditor(store, card);

    await wrapper.find('[data-im="card-editor"]').setValue("材料说明改过了");
    await wrapper.find('[data-im="card-meta-name"]').setValue("新文件名.pdf");

    expect(readCardLocalDraft("c1")?.meta).toEqual({ name: "新文件名.pdf" });
    const { wrapper: reopened } = await reopen(card);
    expect(value(reopened, '[data-im="card-editor"]')).toBe("材料说明改过了");
    expect(value(reopened, '[data-im="card-meta-name"]')).toBe("新文件名.pdf");
  });

  it("image：图片名随正文一起写入并恢复", async () => {
    const card = makeCard("image", { name: "旧图.png" });
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(boardPayload(card) as never);
    const store = useInteractiveStore();
    await store.load();
    const wrapper = await openEditor(store, card);

    await wrapper.find('[data-im="card-meta-name"]').setValue("新图.png");
    expect(readCardLocalDraft("c1")?.meta).toEqual({ name: "新图.png" });

    const { wrapper: reopened } = await reopen(card);
    expect(value(reopened, '[data-im="card-meta-name"]')).toBe("新图.png");
  });

  it("code：代码语言随正文一起写入并恢复", async () => {
    const card = makeCard("code", { language: "python" });
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(boardPayload(card) as never);
    const store = useInteractiveStore();
    await store.load();
    const wrapper = await openEditor(store, card);

    await wrapper.find('[data-im="card-editor"]').setValue("print(2)");
    await wrapper.find('[data-im="card-meta-language"]').setValue("rust");
    expect(readCardLocalDraft("c1")?.meta).toEqual({ language: "rust" });

    const { wrapper: reopened } = await reopen(card);
    expect(value(reopened, '[data-im="card-editor"]')).toBe("print(2)");
    expect(value(reopened, '[data-im="card-meta-language"]')).toBe("rust");
  });

  it("清空附加字段：空串进本机记录并原样恢复（不被正式值回填）", async () => {
    const card = makeCard("url", { href: "https://old.example", title: "旧标题" });
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(boardPayload(card) as never);
    const store = useInteractiveStore();
    await store.load();
    const wrapper = await openEditor(store, card);

    await wrapper.find('[data-im="card-meta-href"]').setValue("");
    await wrapper.find('[data-im="card-meta-title"]').setValue("");
    expect(readCardLocalDraft("c1")?.meta).toEqual({ href: "", title: "" });

    const { wrapper: reopened } = await reopen(card);
    expect(value(reopened, '[data-im="card-meta-href"]')).toBe("");
    expect(value(reopened, '[data-im="card-meta-title"]')).toBe("");
  });
});
