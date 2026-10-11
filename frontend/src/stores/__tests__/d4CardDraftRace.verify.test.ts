/**
 * 独立验收 D4-B：卡片草稿的串行保存、版本与对象、空草稿（契约 §10.3 / §10.4 / §10.5）。
 *
 * 由独立验收子智能体 D 编写，**不修改任何产品代码**。
 * 每条用例按「用户能观察到的正确行为」断言；在旧基线 224bc63 上必须失败。
 *
 * 标注：【组件】挂载真实组件 · 【状态】只依赖 store 的公开状态与动作 · 【模拟失败】受控拒绝
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as imApi from "../../services/interactive";
import { draftStorageKey, hasDraftRecord, readDraft } from "../../interactive/drafts";
import BoardCard from "../../components/interactive/BoardCard.vue";
import CardDraftHint from "../../components/interactive/CardDraftHint.vue";

vi.mock("../../services/api", () => ({
  api: { sendTurn: vi.fn() },
}));

vi.mock("../../services/interactive", () => ({
  saveDrafts: vi.fn(),
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
  approveIntent: vi.fn(),
  rejectIntent: vi.fn(),
  batchDecide: vi.fn(),
  createDemoIntents: vi.fn(),
  advanceIntent: vi.fn(),
  updateIntentPreview: vi.fn(),
}));

const CARD_KEY = "card:c1";

let pendingSaves: Array<{ resolve: (v: unknown) => void; reject: (e: unknown) => void; payload: Record<string, string> }> = [];

/** 受控保存：返回一个「还没结束」的草稿保存请求 */
function pendingDraftSave() {
  const slots = {
    resolve: (_v: unknown) => {},
    reject: (_e: unknown) => {},
    payload: {} as Record<string, string>,
  };
  const promise = new Promise((resolve, reject) => {
    slots.resolve = resolve;
    slots.reject = reject;
  });
  vi.mocked(imApi.saveDrafts).mockImplementationOnce((_boardId: string, payload: Record<string, string>) => {
    slots.payload = payload;
    return promise as never;
  });
  pendingSaves.push(slots);
  return slots;
}

beforeEach(() => {
  localStorage.clear();
  pendingSaves = [];
  vi.resetAllMocks();
  vi.mocked(imApi.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" } as never);
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

/* ==========================================================================
 * 时序 6：旧卡片保存慢，第二版防抖已到，旧请求失败 →
 *         第二版实际保存或明确失败、可重试，不永久 saving。
 * ======================================================================== */
describe("时序 6：串行保存不许把新版本遗忘（§10.3）", () => {
  it("【状态】旧请求在飞时第二版到点且旧请求失败 → 第二版必须真的再发一次请求", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();

    // 第一版：先注册受控请求，再等防抖到点（请求在飞）
    pendingDraftSave();
    store.setDraft(CARD_KEY, "第一版");
    vi.advanceTimersByTime(700);
    await flushPromises();
    expect(imApi.saveDrafts).toHaveBeenCalledTimes(1);

    // 请求在飞：用户又输入第二版，并等它自己的防抖到点
    pendingDraftSave();
    store.setDraft(CARD_KEY, "第二版");
    vi.advanceTimersByTime(700);
    await flushPromises();

    // 旧请求失败
    pendingSaves[0].reject(new Error("网络中断"));
    await flushPromises();
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();

    pendingSaves[1]?.resolve({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" });
    await flushPromises();

    // 正确行为：第二版要么已经发出第二次请求（并成功），
    // 要么给出明确失败 + 可重试；绝不能停在「保存中」却没有任何请求与计时。
    const state = store.draftStateFor(CARD_KEY);
    const sentSecond = vi.mocked(imApi.saveDrafts).mock.calls.length >= 2;
    const explicitlyFailed = state.status === "error" && Boolean(state.error);
    expect(
      sentSecond || explicitlyFailed,
      "第二版被遗忘：既没有发出请求，也没有明确失败与重试入口",
    ).toBe(true);
    // 内存里的内容一个字都不许丢
    expect(store.draftFor(CARD_KEY)).toBe("第二版");
    expect(state.status).not.toBe("saving");
  });

  it("【状态】旧请求失败后第二版仍能落库，最终状态为已保存", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    pendingDraftSave();
    store.setDraft(CARD_KEY, "第一版");
    vi.advanceTimersByTime(700);
    await flushPromises();

    pendingDraftSave();
    store.setDraft(CARD_KEY, "第二版");
    vi.advanceTimersByTime(700);
    await flushPromises();

    pendingSaves[0].reject(new Error("网络中断"));
    await flushPromises();
    // 第二版成功（作者原意如此：受控请求必须真的收尾，否则状态永远是「保存中」）
    await vi.advanceTimersByTimeAsync(3000);
    await flushPromises();
    pendingSaves[1]?.resolve({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" });
    await flushPromises();

    expect(imApi.saveDrafts).toHaveBeenCalledTimes(2);
    expect(pendingSaves[1].payload[CARD_KEY]).toBe("第二版");
    expect(store.draftStateFor(CARD_KEY).status).toBe("saved");
  });

  it("【状态】旧请求成功后仍要补存第二版（成功回执不许吞掉新版本）", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    pendingDraftSave();
    store.setDraft(CARD_KEY, "第一版");
    vi.advanceTimersByTime(700);
    await flushPromises();
    pendingDraftSave();
    store.setDraft(CARD_KEY, "第二版");
    vi.advanceTimersByTime(700);
    await flushPromises();

    pendingSaves[0].resolve({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" });
    await flushPromises();
    await vi.advanceTimersByTimeAsync(3000);
    await flushPromises();
    pendingSaves[1]?.resolve({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" });
    await flushPromises();

    expect(imApi.saveDrafts).toHaveBeenCalledTimes(2);
    expect(pendingSaves[1].payload[CARD_KEY]).toBe("第二版");
    expect(store.draftStateFor(CARD_KEY).status).toBe("saved");
  });

  it("【状态】保存与重试都不建卡、不提交板面、不调用 QIO", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    store.setDraft(CARD_KEY, "只该写草稿的字");
    vi.mocked(imApi.saveDrafts).mockRejectedValueOnce(new Error("网络中断"));
    vi.advanceTimersByTime(700);
    await flushPromises();
    await store.retryDraftSave(CARD_KEY);
    await flushPromises();
    expect(imApi.submitBoard).not.toHaveBeenCalled();
    expect(imApi.saveBoardState).not.toHaveBeenCalled();
  });
});

/* ==========================================================================
 * 时序 7：旧请求成功 / 失败、连续输入、多个卡片同时编辑 →
 *         版本与对象正确，最终正文及状态一致。
 * ======================================================================== */
describe("时序 7：多卡片与连续输入的版本归属（§10.3）", () => {
  it("【状态】两个卡片交替编辑：各自保存到自己的键，不串内容", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    pendingDraftSave();
    store.setDraft("card:a", "A 的第一版");
    store.setDraft("card:b", "B 的第一版");
    vi.advanceTimersByTime(700);
    await flushPromises();
    expect(pendingSaves[0].payload["card:a"]).toBe("A 的第一版");
    expect(pendingSaves[0].payload["card:b"]).toBe("B 的第一版");
    pendingSaves[0].resolve({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" });
    await flushPromises();

    pendingDraftSave();
    store.setDraft("card:a", "A 的第二版");
    vi.advanceTimersByTime(700);
    await flushPromises();
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    pendingSaves[1]?.resolve({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" });
    await flushPromises();

    expect(store.draftFor("card:a")).toBe("A 的第二版");
    expect(store.draftFor("card:b")).toBe("B 的第一版");
    expect(store.draftStateFor("card:a").status).toBe("saved");
    expect(store.draftStateFor("card:b").status).toBe("saved");
    const lastPayload = vi.mocked(imApi.saveDrafts).mock.calls[vi.mocked(imApi.saveDrafts).mock.calls.length - 1]?.[1] as Record<string, string>;
    expect(lastPayload["card:a"]).toBe("A 的第二版");
    expect(lastPayload["card:b"]).toBe("B 的第一版");
  });

  it("【状态】旧请求失败不许把「期间又改过」的新版本标成已保存或这次的失败", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    pendingDraftSave();
    store.setDraft("card:a", "旧内容");
    vi.advanceTimersByTime(700);
    await flushPromises();
    pendingDraftSave();
    store.setDraft("card:a", "请求期间的新内容");
    pendingSaves[0].reject(new Error("网络中断"));
    await flushPromises();
    await vi.advanceTimersByTimeAsync(3000);
    await flushPromises();
    pendingSaves[1]?.resolve({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" });
    await flushPromises();

    expect(store.draftFor("card:a")).toBe("请求期间的新内容");
    // 新版本必须被真正保存（或明确失败可重试），不能假装成功
    const state = store.draftStateFor("card:a");
    if (state.status === "saved") {
      const lastPayload = vi.mocked(imApi.saveDrafts).mock.calls[vi.mocked(imApi.saveDrafts).mock.calls.length - 1]?.[1] as Record<string, string>;
      expect(lastPayload["card:a"]).toBe("请求期间的新内容");
    } else {
      expect(state.status).toBe("error");
      expect(state.error).toBeTruthy();
    }
  });
});

/* ==========================================================================
 * 时序 8：空草稿取消、重开、刷新、确认与清理 →
 *         空编辑状态能恢复，清理后不遮盖正式内容。
 * ======================================================================== */
describe("时序 8：空草稿是有效编辑状态（§10.4）", () => {
  it("【纯函数】存在且正文为空的草稿必须与「没有草稿」区分", () => {
    const key = draftStorageKey("card", "empty-case");
    expect(hasDraftRecord(key), "没有草稿时不该说有").toBe(false);
    expect(readDraft(key)).toBeNull();

    // 保存一条正文为空的草稿（用户把内容删空）
    const written = (globalThis as unknown as { localStorage: Storage }).localStorage;
    written.setItem(key, JSON.stringify({ text: "", updatedAt: Date.now(), seq: 1 }));
    expect(hasDraftRecord(key), "正文为空但记录存在：必须算「有草稿」").toBe(true);
    expect(readDraft(key)?.text).toBe("");

    written.removeItem(key);
    expect(hasDraftRecord(key), "清理后必须回到「没有草稿」").toBe(false);
  });

  it("【状态】store 必须提供「空草稿也算有」的判断与正文读取", () => {
    const store = useInteractiveStore() as unknown as {
      hasCardDraft?: (cardId: string) => boolean;
      cardDraftText?: (cardId: string) => string;
    };
    expect(typeof store.hasCardDraft, "store 未提供 hasCardDraft").toBe("function");
    expect(typeof store.cardDraftText, "store 未提供 cardDraftText").toBe("function");
  });

  it("【组件】把正文删空并保存后重开编辑器：输入框保持为空，不拿旧正文顶上来", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    const card = {
      id: "c1",
      kind: "text" as const,
      content: "卡片原来的正式正文",
      checked: false,
      hidden: false,
      folded: false,
      bookmarked: false,
      deleted: false,
      x: 0,
      y: 0,
      w: 200,
      h: 100,
      createdAt: "2026-10-08T00:00:00.000Z",
      updatedAt: "2026-10-08T00:00:00.000Z",
      // 驱动补全：BoardCard 的 props.card 需要完整形状（缺少 meta 时组件渲染会抛错）
      meta: {},
    };
    // 卡片局部工具栏只在「选中且是最后点中的那张」时出现
    // 驱动补全：板面状态必须是完整形状（store.board 初值为 null，展开它得不到 groups）
    store.board = {
      boardId: "board_default",
      seq: 0,
      updatedAt: "2026-10-08T00:00:00.000Z",
      cards: [card],
      groups: [],
      links: [],
      selection: [card.id],
    } as never;

    const props = {
      card,
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
    const wrapper = mount(BoardCard, { props, attachTo: document.body });
    await flushPromises();
    expect(wrapper.find('[data-im="card-edit"]').exists(), "未选中卡片应显示编辑入口").toBe(true);

    // 进入编辑 → 删空 → 等防抖把空草稿写进去
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await flushPromises();
    const editor = wrapper.find('[data-im="card-editor"]');
    expect(editor.exists(), "编辑态应有输入框").toBe(true);
    expect((editor.element as HTMLTextAreaElement).value).toBe("卡片原来的正式正文");
    await editor.setValue("");
    await editor.trigger("input");
    vi.advanceTimersByTime(900);
    await flushPromises();

    // 取消编辑（离开编辑器）再重开
    const cancel = wrapper.findAll("button").find((b) => b.text().includes("取消"));
    expect(cancel, "编辑态应有取消按钮").toBeTruthy();
    await cancel!.trigger("click");
    await flushPromises();
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await flushPromises();

    const reopened = wrapper.find('[data-im="card-editor"]');
    expect(reopened.exists()).toBe(true);
    expect(
      (reopened.element as HTMLTextAreaElement).value,
      "空草稿被旧正文盖掉了（把「正文为空」当成了「没有草稿」）",
    ).toBe("");
    // 正式内容在用户明确确认前不许改变
    const boardCards = ((store as unknown as { board?: { cards?: Array<{ id: string; content?: string }> } }).board?.cards ?? []);
    expect(boardCards.find((c) => c.id === card.id)?.content).toBe("卡片原来的正式正文");
    wrapper.unmount();
  });

  it("【组件】CardDraftHint：保存失败必须显示原因与重试入口", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    store.setDraft(CARD_KEY, "写不进去的字");
    vi.mocked(imApi.saveDrafts).mockRejectedValueOnce(new Error("网络中断"));
    vi.advanceTimersByTime(700);
    await flushPromises();

    const wrapper = mount(CardDraftHint, { props: { draftKey: CARD_KEY }, attachTo: document.body });
    await flushPromises();
    expect(wrapper.find('[data-im="card-draft-error"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="card-draft-retry"]').exists()).toBe(true);
    expect(wrapper.text()).toContain("网络中断");
    // 绝不显示「已保存」
    expect(wrapper.text()).not.toContain("已保存");
    wrapper.unmount();
  });
});
