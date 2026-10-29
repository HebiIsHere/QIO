/**
 * 统一文字卡的板面接线用例（契约 2026-10-12 §1.1 / §1.6 / §1.7 / §2.3 / §2.4）。
 *
 * 覆盖四件事：
 * 1.【纯函数】board.ts：ADDABLE_CARD_KINDS 收敛、defaultCardSeed("text") 产 meta.doc、
 *    addTextCard / setCardDoc（内容模型是唯一权威）；
 * 2.【可见范围】未许可文字卡整体不进载荷；遗留 code / url 卡不进载荷、也不计入未许可统计；
 * 3.【许可】提交回执 checkedCleared 为空数组时绝不把 checked 改成 false；
 *    服务端快照合并不得覆盖用户更晚的手动勾选；
 * 4.【显示】BoardCard 按块渲染文字卡，链接显示地址、可编辑、可打开；代码块有语言与复制按钮；
 *    历史 code / url 卡片仍能显示并标注「旧卡片，尚未迁移」。
 *
 * 标注：【纯函数/真实 store】没有替身；只在文件系统/剪贴板这类外部能力上做受控替换。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as api from "../../../services/interactive";
import {
  ADDABLE_CARD_KINDS,
  MATERIAL_KINDS,
  addTextCard,
  cardById,
  defaultCardSeed,
  emptyState,
  setCardDoc,
  updateCard,
} from "../../../interactive/board";
import { cardDraftKey } from "../../../interactive/drafts";
import { isVisibleCard, localVisibleRange, visibleRangeCounts } from "../../../interactive/submission";
import { FENCE, emptyDoc, parseDocText, serializeDocText, type TextDoc } from "../../../interactive/textCard";
import type { BoardCard as BoardCardModel, CardKind } from "../../../interactive/types";
import { hasTipRecord } from "../../../composables/useQioTip";

/** 替身只覆盖真实服务模块**全部**用到的方法（缺一个就会在等待回执时抛「没有这个导出」） */
vi.mock("../../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  fetchVisibleRange: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  checkMaterialImpact: vi.fn(),
  previewMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchAcceptance: vi.fn(),
  fetchIntents: vi.fn(),
  approveIntent: vi.fn(),
  rejectIntent: vi.fn(),
  batchDecide: vi.fn(),
  createIntents: vi.fn(),
  advanceIntent: vi.fn(),
  updateIntentPreview: vi.fn(),
  fetchUnderstanding: vi.fn(),
  retryUnderstanding: vi.fn(),
  fetchExecutions: vi.fn(),
}));

const STAMP = "2026-10-12T00:00:00.000Z";

function mkCard(id: string, patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return {
    id,
    kind: "text",
    content: "",
    meta: {},
    x: 0,
    y: 0,
    w: 260,
    h: 170,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
    ...patch,
  };
}

function mountCard(card: BoardCardModel): VueWrapper {
  return mount(BoardCard, {
    props: {
      card,
      selected: true,
      highlight: false,
      dragging: false,
      x: card.x,
      y: card.y,
      groupName: null,
      groups: [],
      toolbarLeft: 0,
      toolbarTop: 0,
      multi: false,
      connecting: false,
    },
    attachTo: document.body,
  });
}

describe("board.ts：可添加的种类与文字卡构造", () => {
  it("ADDABLE_CARD_KINDS 只有 text / file（代码 / 网址 / 图片都不再是独立入口）", () => {
    // 2026-10-14 第九个问题：文件与图片合并成同一种材料，图片由文件类型决定
    expect(ADDABLE_CARD_KINDS).toEqual(["text", "file"]);
    expect(ADDABLE_CARD_KINDS).not.toContain("image");
    expect(ADDABLE_CARD_KINDS).not.toContain("code");
    expect(ADDABLE_CARD_KINDS).not.toContain("url");
  });

  it("MATERIAL_KINDS 只保留 file：遗留 code / url 不是材料通道，也不再有不存在的独立 image 卡片类型", () => {
    expect(MATERIAL_KINDS).toEqual(["file"]);
  });

  it("defaultCardSeed(\"text\") 产出空的块序列与空编辑区文本", () => {
    const seed = defaultCardSeed("text");
    expect(seed.kind).toBe("text");
    expect(seed.content).toBe("");
    expect(seed.meta.doc).toEqual({ schema: "qio.textcard/1", blocks: [] });
    expect(seed.meta.docText).toBe("");
  });

  it("历史种类仍然可读（CARD_KINDS 不缩水）：code / url / reply 的种子还在", () => {
    for (const kind of ["file", "code", "url", "reply"] as CardKind[]) {
      expect(defaultCardSeed(kind).kind).toBe(kind);
    }
  });

  it("addTextCard：默认提交给 QIO、位置可给、id 稳定返回", () => {
    const result = addTextCard(emptyState("board_t"), {});
    expect(result.cardId).toBeTruthy();
    const card = cardById(result.state, result.cardId)!;
    expect(card.kind).toBe("text");
    // 2026-10-14 第十个问题：新建文字卡默认已勾选（可取消）
    expect(card.checked).toBe(true);
    expect(card.meta.doc).toEqual({ schema: "qio.textcard/1", blocks: [] });
    expect(card.meta.docText).toBe("");
    expect(card.content).toBe("");

    const placed = addTextCard(emptyState("board_t"), { x: 120, y: 240 });
    const placedCard = cardById(placed.state, placed.cardId)!;
    expect([placedCard.x, placedCard.y]).toEqual([120, 240]);
  });

  it("addTextCard：给了 doc 就按块序列落 content 与 meta（content 是纯文本派生）", () => {
    const doc = parseDocText("普通文字\n\n" + FENCE + "py\nx = 1\n" + FENCE + "\n\n[QIO](https://example.com)\n");
    const { state, cardId } = addTextCard(emptyState("board_t"), { doc });
    const card = cardById(state, cardId)!;
    expect((card.meta.doc as TextDoc).blocks.map((b) => b.kind)).toEqual(["paragraph", "code", "link"]);
    expect(card.meta.docText).toBe(serializeDocText(doc));
    expect(card.content).toContain("QIO <https://example.com>");
    expect(card.content).toContain("x = 1");
  });

  it("setCardDoc：接受块序列或编辑区文本，两条路都落在同一份权威内容模型上", () => {
    const { state, cardId } = addTextCard(emptyState("board_t"), {});
    const byDoc = setCardDoc(state, cardId, parseDocText("一段话\n"));
    const docCard = cardById(byDoc, cardId)!;
    expect(docCard.content).toBe("一段话");
    expect((docCard.meta.doc as TextDoc).blocks[0].text).toBe("一段话");

    const byText = setCardDoc(byDoc, cardId, { docText: "另一段\n\n## 标题\n" });
    const textCard = cardById(byText, cardId)!;
    expect((textCard.meta.doc as TextDoc).blocks.map((b) => b.kind)).toEqual(["paragraph", "heading"]);
    expect(textCard.meta.docText).toBe("另一段\n\n## 标题\n");
    expect(textCard.content).toContain("## 标题");
  });

  it("setCardDoc：代码块逐字保真（缩进、换行、语言都不改写）", () => {
    const { state, cardId } = addTextCard(emptyState("board_t"), {});
    const text = FENCE + "python\n  def f():\n      return 1\n" + FENCE + "\n";
    const next = setCardDoc(state, cardId, { docText: text });
    const doc = cardById(next, cardId)!.meta.doc as TextDoc;
    expect(doc.blocks[0].text).toBe("  def f():\n      return 1\n");
    expect(doc.blocks[0].attrs?.language).toBe("python");
    // 幂等：再走一遍语义不变（块 id 是每次解析新生成的，比对时忽略它）
    const again = setCardDoc(next, cardId, { docText: serializeDocText(doc) });
    const strip = (value: TextDoc) => value.blocks.map(({ id: _id, ...rest }) => rest);
    expect(strip(again.cards[0].meta.doc as TextDoc)).toEqual(strip(doc));
    expect(again.cards[0].meta.docText).toBe(serializeDocText(doc));
  });

  it("setCardDoc：卡片不存在时原样返回（不新建、不抛错）", () => {
    const state = emptyState("board_t");
    expect(setCardDoc(state, "没有这张卡", emptyDoc()).cards).toEqual([]);
  });
});

describe("可见范围与提交载荷（许可）", () => {
  it("未许可的文字卡整体不可见：链接、代码、派生正文都进不去", () => {
    const doc = parseDocText("[QIO](https://example.com)\n\n" + FENCE + "py\nsecret = 1\n" + FENCE + "\n");
    const created = addTextCard(emptyState("board_t"), { doc });
    const cardId = created.cardId;
    // 新建默认已勾选；这条用例要的是「未许可」的语义，显式关掉
    const state = { ...created.state, cards: created.state.cards.map((c) => ({ ...c, checked: false })) };
    const unchecked = cardById(state, cardId)!;
    expect(isVisibleCard(unchecked)).toBe(false);
    const range = localVisibleRange(state);
    expect(range.cards).toHaveLength(0);
    expect(range.notVisibleCount).toBe(1);
    // 勾选后才进范围（整卡一个许可）
    const checked = setCardDoc(state, cardId, doc);
    const visible = { ...checked, cards: checked.cards.map((c) => ({ ...c, checked: true })) };
    expect(isVisibleCard(visible.cards[0])).toBe(true);
    expect(localVisibleRange(visible).cards).toHaveLength(1);
  });

  it("遗留 code / url 卡不进载荷、也不计入未许可统计", () => {
    const state = mkState([
      mkCard("legacy_code", { kind: "code", content: "print(1)", checked: true }),
      mkCard("legacy_url", { kind: "url", meta: { href: "https://example.com" } }),
      mkCard("text_off", { kind: "text", content: "未许可的注释" }),
      mkCard("text_on", { kind: "text", content: "许可了", checked: true }),
      mkCard("doc", { kind: "file", meta: { name: "a.pdf" } }),
    ]);
    expect(isVisibleCard(cardById(state, "legacy_code")!)).toBe(false);
    expect(isVisibleCard(cardById(state, "legacy_url")!)).toBe(false);

    const range = localVisibleRange(state);
    expect(range.cards.map((c) => c.id).sort()).toEqual(["doc", "text_on"]);
    const counts = visibleRangeCounts(range);
    expect(counts.notes).toBe(1); // 只有许可过的文字卡算注释
    expect(counts.materials).toBe(1); // 材料只剩 file / image
    expect(counts.notVisible).toBe(3); // legacy_code / legacy_url / text_off 都不在范围内
  });
});

function mkState(cards: BoardCardModel[]) {
  return { ...emptyState("board_t"), cards };
}

describe("BoardCard：文字卡的编辑与显示", () => {
  let store: ReturnType<typeof useInteractiveStore>;
  let wrapper: VueWrapper | null = null;

  beforeEach(() => {
    localStorage.clear();
    setActivePinia(createPinia());
    store = useInteractiveStore();
  });

  /** 卡片工具栏只在「所选里的最后一张」上渲染：渲染前先把这张卡设成当前所选。 */
  function select(cardId: string) {
    store.board = { ...store.board!, selection: [cardId] };
  }

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
    document.body.innerHTML = "";
  });

  it("编辑态渲染统一编辑区（一个 textarea + 工具栏），内容取自 meta.docText", async () => {
    const text = "第一段\n\n## 标题\n";
    // 真实落地形状：meta.doc 与 meta.docText 成对（addTextCard / setCardDoc 都这么写）
    const card = mkCard("t1", { meta: { doc: parseDocText(text), docText: text } });
    store.board = mkState([card]);
    select("t1");
    // 用户在编辑区里写的内容就是「未完成输入」：编辑面必须显示这一份（不是卡片正式字段）
    store.setDraft(cardDraftKey("t1"), "第一段\n\n## 标题\n");
    wrapper = mountCard(card);
    await wrapper.get('[data-im="card-edit"]').trigger("click");

    expect(wrapper.findAll('[data-im="card-editor"]')).toHaveLength(1);
    expect((wrapper.get('[data-im="card-editor"]').element as HTMLTextAreaElement).value).toBe(text);
    expect(wrapper.find('[data-im="card-editor-toolbar"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="card-fmt-bold"]').exists()).toBe(true);
  });

  it("空块序列就是「没有内容」：不拿 content 顶上来（与后端 doc_from_card 同口径）", () => {
    const card = mkCard("t1b", { content: "旧的纯文本正文", meta: { doc: { schema: "qio.textcard/1", blocks: [] }, docText: "" } });
    store.board = mkState([card]);
    select("t1b");
    wrapper = mountCard(card);
    expect(wrapper.text()).toContain("还没有内容");
  });

  it("关闭态按块渲染：标题 / 列表 / 引用 / 段落 / 代码 / 链接各自有外观", () => {
    const text = [
      "普通段落",
      "",
      "## 标题二",
      "",
      "- 第一条",
      "- 第二条",
      "",
      "> 引用一句",
      "",
      FENCE + "python",
      "  def f():",
      "      return 1",
      FENCE,
      "",
      "[QIO 仓库](https://github.com/HebiIsHere/QIO)",
      "",
    ].join("\n");
    const card = mkCard("t2", { kind: "text", content: "普通段落", meta: { docText: text, doc: parseDocText(text) } });
    store.board = mkState([card]);
    wrapper = mountCard(card);

    for (const hook of ["card-block-heading", "card-block-list", "card-block-quote", "card-block-paragraph", "card-block-code", "card-block-link"]) {
      expect(wrapper.find('[data-im="' + hook + '"]').exists(), hook).toBe(true);
    }
    expect(wrapper.get('[data-im="card-block-code"]').text()).toContain("python");
    // 链接显示地址（不能只显示标题）
    expect(wrapper.get('[data-im="card-block-link"]').text()).toContain("https://github.com/HebiIsHere/QIO");
  });

  it("代码块：语言与复制按钮都在；复制成功/失败都如实反馈", async () => {
    const text = FENCE + "python\nx = 1\n" + FENCE + "\n";
    const card = mkCard("t3", { meta: { docText: text, doc: parseDocText(text) } });
    store.board = mkState([card]);
    wrapper = mountCard(card);
    expect(wrapper.get('[data-im="card-code-language"]').text()).toBe("python");

    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await wrapper.get('[data-im="card-code-copy"]').trigger("click");
    await Promise.resolve();
    expect(writeText).toHaveBeenCalledWith("x = 1\n");
    expect(wrapper.get('[data-im="card-code-copy"]').text()).toContain("已复制");
  });

  it("代码块：剪贴板不存在时如实说复制失败，不假装成功", async () => {
    const text = FENCE + "python\nx = 1\n" + FENCE + "\n";
    const card = mkCard("t4", { meta: { docText: text, doc: parseDocText(text) } });
    store.board = mkState([card]);
    wrapper = mountCard(card);
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    await wrapper.get('[data-im="card-code-copy"]').trigger("click");
    await Promise.resolve();
    const copy = wrapper.get('[data-im="card-code-copy"]');
    expect(copy.text()).toContain("复制失败");
    expect(copy.attributes("title")).toBeUndefined();
    expect(hasTipRecord(copy.element as HTMLElement), "手动复制退路要走统一用途说明").toBe(true);
  });

  it("链接：不安全 scheme 如实说明打不开（不假装打开、也不静默失败）", async () => {
    // 注意：带括号的 href（如 javascript:alert(1)）按契约 §1.3 根本不算链接块 ——
    // 这里要验的是「被识别成链接、但 scheme 不在白名单里」这条路，所以用 file: 地址。
    const text = "[坏链接](file:///etc/passwd)\n";
    const card = mkCard("t5", { meta: { docText: text, doc: parseDocText(text) } });
    store.board = mkState([card]);
    select("t5");
    wrapper = mountCard(card);
    const open = wrapper.get('[data-im="card-link-open"]');
    expect(open.attributes("data-href")).toBe("file:///etc/passwd");
    expect(open.attributes("data-openable")).toBe("0");
    // 界面必须写明这个地址不可打开（不是静默失败、也不是假装打开）
    expect(wrapper.get('[data-im="card-block-link"]').text()).toContain("不可打开");
  });

  it("链接：点「编辑」进入编辑态并把光标落到链接那一行", async () => {
    const text = "正文\n\n[QIO 仓库](https://github.com/HebiIsHere/QIO)\n";
    const card = mkCard("t6", { meta: { docText: text, doc: parseDocText(text) } });
    store.board = mkState([card]);
    select("t6");
    wrapper = mountCard(card);
    await wrapper.get('[data-im="card-link-edit"]').trigger("click");

    const el = wrapper.get('[data-im="card-editor"]').element as HTMLTextAreaElement;
    expect(wrapper.find('[data-im="card-editor"]').exists()).toBe(true);
    // 光标落在链接那一行（第三行 = 链接，前两行是正文与空行）
    expect(text.slice(el.selectionStart)).toContain("[QIO 仓库]");
    expect(document.activeElement).toBe(el);
  });

  it("历史 code / url 卡片仍能显示，并标注「旧卡片，尚未迁移」", () => {
    const code = mkCard("c1", { kind: "code", content: "print(1)", meta: { language: "python" } });
    const url = mkCard("u1", { kind: "url", meta: { href: "https://example.com", title: "示例" } });
    store.board = mkState([code, url]);
    const a = mountCard(code);
    expect(a.get('[data-im="card-legacy-note"]').text()).toContain("旧卡片，尚未迁移");
    expect(a.text()).toContain("print(1)");
    a.unmount();
    const b = mountCard(url);
    expect(b.get('[data-im="card-legacy-note"]').text()).toContain("旧卡片，尚未迁移");
    // 有标题时显示标题（地址仍在 meta 里，可点「编辑」看到）
    expect(b.text()).toContain("示例");
    expect(String(store.board?.cards.find((item) => item.id === "u1")?.meta?.href)).toBe("https://example.com");
    b.unmount();
  });

  it("许可 UI 已移除：工具栏里不再有勾选框或「说明」详情", () => {
    const card = mkCard("t7", { content: "注释" });
    store.board = mkState([card]);
    select("t7");
    wrapper = mountCard(card);
    expect(wrapper.find('[data-im="check"]').exists()).toBe(false);
    expect(wrapper.find(".check").exists()).toBe(false);
    expect(wrapper.find('[data-im="check-detail-text"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="card-license"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="card-participation"]').exists()).toBe(true);
  });
});

describe("许可的持久性（store，契约 §2.1 / §2.3 / §2.4）", () => {
  const NOW = "2026-10-12T00:00:00.000Z";

  beforeEach(() => {
    localStorage.clear();
    setActivePinia(createPinia());
    vi.clearAllMocks();
  });

  function emptySnapshot() {
    return { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 };
  }

  function submitResult(over: Record<string, unknown> = {}) {
    return {
      status: "succeeded",
      submission: { id: "s1", seq: 4, status: "succeeded", createdAt: NOW },
      before: emptySnapshot(),
      after: emptySnapshot(),
      expressions: [],
      baseline: { updated: true, previousSeq: 3, seq: 4 },
      delivery: { delivered: true, reason: "", detail: "", understandingId: "u1", status: "accepted" },
      visibleRange: emptySnapshot(),
      checkedCleared: [],
      ...over,
    };
  }

  /** 服务端读取回执的真实形状（BoardStateResponse）：board / state / seq / drafts 一个都不能少 */
  function serverPayload(cards: BoardCardModel[]) {
    return {
      board: { id: "board_t", title: "" },
      state: { ...mkState(cards), seq: 4 },
      seq: 4,
      baseline: null,
      submissions: [],
      drafts: { drafts: {}, updatedAt: NOW },
      pending: [],
    };
  }

  async function seedStore(cards: BoardCardModel[]) {
    const store = useInteractiveStore();
    vi.mocked(api.fetchBoardState).mockResolvedValue(serverPayload(cards) as never);
    vi.mocked(api.saveBoardState).mockImplementation(
      async (_boardId: string, putState: unknown) =>
        ({ ok: true, seq: 4, savedAt: NOW, state: putState }) as never,
    );
    vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: NOW } as never);
    vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: emptySnapshot() } as never);
    vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
    vi.mocked(api.checkMaterialImpact).mockResolvedValue({
      ok: true, checkId: "chk", stateVersion: 3, affected: [], impactConfirmationRequired: false,
    } as never);
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] },
    } as never);
    vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understanding: null, understandings: [] } as never);
    vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
    vi.mocked(api.createIntents).mockResolvedValue({ created: [], demo: false } as never);
    await store.load();
    return store;
  }

  function deferred<T>() {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>((res) => {
      resolve = res;
    });
    return { promise, resolve };
  }

  it("提交回执的 checkedCleared 为空数组时，许可绝不被改成 false", async () => {
    const cards = [mkCard("t1", { content: "许可过的注释", checked: true })];
    const store = await seedStore(cards);
    expect(store.board?.cards[0].checked).toBe(true);

    vi.mocked(api.submitBoard).mockResolvedValue(submitResult({ checkedCleared: [] }) as never);
    await store.submit();
    await flushPromises();

    expect(store.submitStatus).toBe("succeeded");
    expect(store.board?.cards[0].checked, "回执里没有清除项 → 许可必须原样保留").toBe(true);
    // 服务端快照（同一份许可）也不会把它改掉
    vi.mocked(api.fetchBoardState).mockResolvedValue(serverPayload(cards) as never);
    await store.refreshBoardFromServer();
    expect(store.board?.cards[0].checked).toBe(true);
  });

  it("快照合并不覆盖用户更晚的手动取消：等待回执期间取消许可，迟到的服务端事实不许勾回来", async () => {
    const checkedCards = [mkCard("t1", { content: "许可过的注释", checked: true })];
    const store = await seedStore(checkedCards);

    // 提交挂在飞行中（真实等待窗口）
    const inflight = deferred<never>();
    vi.mocked(api.submitBoard).mockReturnValue(inflight.promise as never);
    const pending = store.submit();
    await flushPromises();

    // 用户在等待期间自己取消了许可
    store.commit(updateCard(store.board!, "t1", { checked: false }), "取消勾选");
    expect(store.board?.cards[0].checked).toBe(false);

    // 服务端事实迟到返回：它仍然认为这张卡是勾选的
    vi.mocked(api.fetchBoardState).mockResolvedValue(serverPayload(checkedCards) as never);
    inflight.resolve(submitResult({ checkedCleared: [] }) as never);
    await pending;
    await flushPromises();

    expect(store.submitStatus).toBe("succeeded");
    expect(store.board?.cards[0].checked, "用户更晚的取消必须赢过迟到的服务端快照").toBe(false);
  });
});
