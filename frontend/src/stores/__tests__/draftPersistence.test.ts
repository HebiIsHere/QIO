/**
 * 聊天草稿与卡片草稿的持久化（契约 §9.4 / §9.5）。
 *
 * 覆盖（都按「用户能观察到的结果」断言，而不是实现细节）：
 * 1. 跨刷新恢复草稿，且**不自动发送**；
 * 2. 草稿按实际话题分开：切话题不串稿、切回来还在；
 * 3. 只有受理成功才删草稿；失败不删、迟到写入不许写回来；
 * 4. 发送失败恢复原稿，但不覆盖用户在请求期间新输入的文字（组件级）；
 * 5. 存储不可用时明确失败、保留内存文字、可重试，且不显示「已保存」；
 * 6. 卡片草稿保存失败：状态/原因/内容保留 + 重试只写草稿（不建卡、不提交、不调 QIO）；
 * 7. 多个卡片草稿互不串用，旧请求的返回不丢新内容。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { useSessionStore } from "../session";
import { useInteractiveStore } from "../interactive";
import { api } from "../../services/api";
import * as imApi from "../../services/interactive";
import { draftStorageKey, readDraft, UNBOUND_DRAFT_ID } from "../../interactive/drafts";
import ChatDock from "../../components/interactive/ChatDock.vue";
import CardDraftHint from "../../components/interactive/CardDraftHint.vue";

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(),
    getSessionContext: vi.fn(),
    saveDrafts: vi.fn(),
    saveBoardState: vi.fn(),
    fetchBoardState: vi.fn(),
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
  },
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

const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");

/** 存储不可用（读写都抛）——隐私模式 / 企业策略下的真实形态 */
function breakLocalStorage(message: string): void {
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: () => { throw new Error(message); },
      setItem: () => { throw new Error(message); },
      removeItem: () => { throw new Error(message); },
    },
  });
}

function restoreLocalStorage(): void {
  if (originalDescriptor) Object.defineProperty(globalThis, "localStorage", originalDescriptor);
}

function chatKey(topicId: string): string {
  return draftStorageKey("chat", topicId);
}

function storedText(key: string): string | null {
  return readDraft(key)?.text ?? null;
}

function resetApiMocks(): void {
  vi.resetAllMocks();
  vi.mocked(api.sendTurn).mockResolvedValue({
    ok: true,
    accepted: true,
    turn_id: "turn_1",
    status: "accepted",
    topic_id: null,
  });
  vi.mocked(imApi.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "2026-10-07T00:00:00.000Z" });
}

beforeEach(() => {
  localStorage.clear();
  resetApiMocks();
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  restoreLocalStorage();
  localStorage.clear();
});

describe("聊天草稿持久化（契约 §9.4）", () => {
  it("输入防抖保存；刷新（新 pinia、同一份本机存储）后草稿还在，且不会自动发送", () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    session.draft = "刷新前还没发出去的字";
    vi.advanceTimersByTime(500);
    expect(session.draftSaveStatus).toBe("saved");
    expect(storedText(chatKey("t1"))).toBe("刷新前还没发出去的字");

    // 「刷新」：新的 pinia 实例，本机存储不变
    setActivePinia(createPinia());
    const reloaded = useSessionStore();
    expect(reloaded.draft).toBe("");
    // 会话上下文到达（真实路径是 loadHistory / ANCHOR 设 currentTopicId）
    reloaded.currentTopicId = "t1";
    expect(reloaded.draft).toBe("刷新前还没发出去的字");
    expect(reloaded.draftSaveStatus).toBe("saved");
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("会话上下文还没到达时输入的字，话题确定后跟过去（不丢也不串到别的话题）", () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.draft = "话题还没确定时的字";
    vi.advanceTimersByTime(500);
    expect(storedText(draftStorageKey("chat", UNBOUND_DRAFT_ID))).toBe("话题还没确定时的字");

    session.currentTopicId = "t7";
    expect(session.draft).toBe("话题还没确定时的字");
    expect(storedText(chatKey("t7"))).toBe("话题还没确定时的字");
    expect(readDraft(draftStorageKey("chat", UNBOUND_DRAFT_ID))).toBeNull();

    // 换个话题：上一个话题的字不许跟过来
    session.currentTopicId = "t8";
    expect(session.draft).toBe("");
    expect(storedText(chatKey("t7"))).toBe("话题还没确定时的字");
  });

  it("草稿按话题分开：切走再切回来，各自的草稿都还在", () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    session.draft = "属于话题一";
    vi.advanceTimersByTime(500);

    session.currentTopicId = "t2";
    expect(session.draft).toBe("");
    session.draft = "属于话题二";
    vi.advanceTimersByTime(500);

    session.currentTopicId = "t1";
    expect(session.draft).toBe("属于话题一");
    expect(storedText(chatKey("t1"))).toBe("属于话题一");
    expect(storedText(chatKey("t2"))).toBe("属于话题二");
  });

  it("只有受理成功才删草稿；等待防抖的旧写入不许把它写回来", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    session.draft = "要发出去的话";
    vi.advanceTimersByTime(500);
    expect(storedText(chatKey("t1"))).toBe("要发出去的话");

    // 组件行为：发送前先清空输入框
    session.draft = "";
    const ok = await session.send("要发出去的话");
    expect(ok).toBe(true);
    expect(readDraft(chatKey("t1"))).toBeNull();
    // 让任何残留的防抖写入到期：不许把已发出去的草稿写回来
    vi.advanceTimersByTime(2000);
    expect(readDraft(chatKey("t1"))).toBeNull();
  });

  it("发送失败不删草稿（受理成功才算发出）", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    session.draft = "发不出去的话";
    vi.advanceTimersByTime(500);
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));

    session.draft = "";
    const ok = await session.send("发不出去的话");
    expect(ok).toBe(false);
    vi.advanceTimersByTime(2000);
    expect(storedText(chatKey("t1"))).toBe("发不出去的话");
  });

  it("发送成功后用户又输入的新内容不许被这次回执删掉", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    const ok = await session.send("第一条");
    expect(ok).toBe(true);
    // 回执之后用户已经写了新的字
    session.draft = "第二条还没发";
    vi.advanceTimersByTime(500);
    expect(storedText(chatKey("t1"))).toBe("第二条还没发");
  });

  it("存储不可用：明确失败 + 内存文字保留 + 可重试（绝不显示已保存）", () => {
    vi.useFakeTimers();
    breakLocalStorage("本地存储被禁用");
    const session = useSessionStore();
    session.currentTopicId = "t1";
    session.draft = "写不进本机的字";
    vi.advanceTimersByTime(500);

    expect(session.draft).toBe("写不进本机的字");
    expect(session.draftSaveStatus).toBe("error");
    expect(session.draftSaveError).toContain("本地存储被禁用");

    // 存储恢复正常：重试成功，状态才改成「已保存」
    restoreLocalStorage();
    session.retryDraftSave();
    expect(session.draftSaveStatus).toBe("saved");
    expect(storedText(chatKey("t1"))).toBe("写不进本机的字");
  });

  it("话题始终没确定（currentTopicId 一直是 null）时，刷新后草稿同样恢复", () => {
    vi.useFakeTimers();
    const first = useSessionStore();
    // 真实路径：演示/独立板面页没有加载会话上下文，currentTopicId 一直是 null
    first.draft = "话题还没确定时写的字";
    vi.advanceTimersByTime(500);
    expect(storedText(draftStorageKey("chat", UNBOUND_DRAFT_ID))).toBe("话题还没确定时写的字");

    setActivePinia(createPinia());
    const reloaded = useSessionStore();
    expect(reloaded.draft).toBe("话题还没确定时写的字");
    expect(reloaded.draftSaveStatus).toBe("saved");
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("动作收到的 this 不是同一个 store 对象时，草稿保存器仍然有效（真机踩到过）", () => {
    vi.useFakeTimers();
    breakLocalStorage("本地存储被禁用");
    const session = useSessionStore();
    session.currentTopicId = "t1";
    session.draft = "写不进本机的字";
    vi.advanceTimersByTime(500);
    expect(session.draftSaveStatus).toBe("error");

    restoreLocalStorage();
    // 真实浏览器里，action 里拿到的 this 与 useSessionStore() 返回的对象**不是同一个**
    // （实测 WeakMap.get(this) 为 undefined，实例属性却读得到）。这里用原型链上的另一个
    // 接收者复现同一种情形：保存器必须挂在实例上，而不是按对象身份查表。
    const alias = Object.create(session) as typeof session;
    alias.retryDraftSave();
    expect(session.draftSaveStatus).toBe("saved");
    expect(storedText(chatKey("t1"))).toBe("写不进本机的字");
  });

  it("发送失败后恢复原稿，但不覆盖用户在请求期间新输入的文字（组件级）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const interactive = useInteractiveStore();
    interactive.chatOpen = true;
    session.currentTopicId = "t1";
    session.draft = "第一条要发的话";

    const pending: Array<(ok: boolean) => void> = [];
    vi.spyOn(session, "send").mockImplementation(
      () => new Promise<boolean>((resolve) => { pending.push(resolve); }),
    );

    const wrapper = mount(ChatDock, { global: { plugins: [pinia] } });
    await flushPromises();
    await wrapper.find('[data-im="chat-send"]').trigger("click");
    await flushPromises();
    // 请求在飞：输入框已经清空
    expect(session.draft).toBe("");

    // 用户又打了字
    await wrapper.find('[data-im="chat-input"]').setValue("请求还没回来时写的新内容");
    expect(session.draft).toBe("请求还没回来时写的新内容");

    // 失败回执到达：恢复逻辑必须看到「已经有新输入」，不许覆盖
    pending[0](false);
    await flushPromises();
    expect(session.draft).toBe("请求还没回来时写的新内容");
    expect(wrapper.find('[data-im="chat-failure"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("保存失败时悬浮聊天显示真实原因与重试入口，不说「已保存」", async () => {
    breakLocalStorage("本地存储被禁用");
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    useInteractiveStore().chatOpen = true;
    session.currentTopicId = "t1";
    session.draft = "写不进本机的字";
    session.flushDraft();
    expect(session.draftSaveStatus).toBe("error");

    const wrapper = mount(ChatDock, { global: { plugins: [pinia] } });
    const status = wrapper.find('[data-im="chat-draft-status"]').text();
    expect(status).toContain("草稿未保存");
    expect(status).toContain("本地存储被禁用");
    expect(status).not.toContain("已保存");
    expect(wrapper.find('[data-im="chat-draft-retry"]').exists()).toBe(true);

    // 存储恢复后点重试：只重写草稿，状态改成已保存
    restoreLocalStorage();
    await wrapper.find('[data-im="chat-draft-retry"]').trigger("click");
    await flushPromises();
    expect(session.draftSaveStatus).toBe("saved");
    expect(wrapper.find('[data-im="chat-draft-status"]').text()).toContain("已保存在本机");
    wrapper.unmount();
  });
});

describe("卡片草稿保存状态（契约 §9.5）", () => {
  it("保存失败：状态 error + 真实原因 + 内容保留；重试只重写草稿", async () => {
    const store = useInteractiveStore();
    vi.mocked(imApi.saveDrafts).mockRejectedValueOnce(new Error("草稿接口 500"));
    store.setDraft("card:c1", "没保存上的内容");
    await store.flushDrafts();

    expect(store.draftFor("card:c1")).toBe("没保存上的内容");
    expect(store.draftStateFor("card:c1")).toEqual({ status: "error", error: "草稿接口 500" });
    expect(store.draftSaveStatus).toBe("error");
    expect(store.draftSaveError).toBe("草稿接口 500");

    await store.retryDraftSave("card:c1");
    expect(store.draftStateFor("card:c1").status).toBe("saved");
    expect(imApi.saveDrafts).toHaveBeenCalledTimes(2);
    expect(vi.mocked(imApi.saveDrafts).mock.calls[1][1]).toEqual({ "card:c1": "没保存上的内容" });
    // 重试只写草稿：不建卡、不提交、不调用 QIO
    expect(imApi.submitBoard).not.toHaveBeenCalled();
    expect(imApi.createDemoIntents).not.toHaveBeenCalled();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("多个草稿互不串用；请求在飞时的新输入不会被旧返回覆盖", async () => {
    const store = useInteractiveStore();
    const payloads: Array<Record<string, string>> = [];
    const release: Array<(value: { drafts: Record<string, string>; updatedAt: string }) => void> = [];
    vi.mocked(imApi.saveDrafts).mockImplementation((_boardId: string, payload: Record<string, string>) => {
      payloads.push({ ...payload });
      if (payloads.length === 1) {
        return new Promise((resolve) => {
          release.push(resolve);
        });
      }
      return Promise.resolve({ drafts: {}, updatedAt: "2026-10-07T00:00:00.000Z" });
    });

    store.setDraft("card:c1", "第一版");
    const inFlight = store.flushDrafts();
    // 请求在飞：第一张卡又改了，另一张卡也开始了编辑
    store.setDraft("card:c1", "第二版");
    store.setDraft("card:c2", "另一张卡的内容");
    release[0]({ drafts: {}, updatedAt: "2026-10-07T00:00:00.000Z" });
    await inFlight;

    expect(payloads[0]).toEqual({ "card:c1": "第一版" });
    expect(payloads[1]).toEqual({ "card:c1": "第二版", "card:c2": "另一张卡的内容" });
    expect(store.draftFor("card:c1")).toBe("第二版");
    expect(store.draftFor("card:c2")).toBe("另一张卡的内容");
    expect(store.draftStateFor("card:c1").status).toBe("saved");
    expect(store.draftStateFor("card:c2").status).toBe("saved");
  });

  it("板面重新加载时，本地还没保存成功的草稿不会被服务端旧值覆盖", async () => {
    const store = useInteractiveStore();
    vi.mocked(imApi.saveDrafts).mockRejectedValueOnce(new Error("草稿接口 500"));
    store.setDraft("card:c1", "本地最新但没存上");
    await store.flushDrafts();

    vi.mocked(imApi.fetchBoardState).mockResolvedValue({
      board: { id: store.boardId, title: "板面" },
      state: {
        boardId: store.boardId,
        seq: 1,
        updatedAt: "2026-10-07T00:00:00.000Z",
        cards: [],
        groups: [],
        links: [],
        selection: [],
      },
      seq: 1,
      baseline: null,
      submissions: [],
      drafts: { drafts: { "card:c1": "服务端的旧内容" }, updatedAt: null },
    });
    await store.refreshBoardFromServer();

    expect(store.draftFor("card:c1")).toBe("本地最新但没存上");
    expect(store.draftStateFor("card:c1").status).toBe("error");
  });

  it("CardDraftHint：失败时贴近编辑对象显示原因，点重试只重写草稿", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useInteractiveStore();
    vi.mocked(imApi.saveDrafts).mockRejectedValueOnce(new Error("服务端拒绝了草稿"));
    store.setDraft("card:c9", "编辑中的字");
    await store.flushDrafts();

    const wrapper = mount(CardDraftHint, { props: { cardId: "c9" }, global: { plugins: [pinia] } });
    expect(wrapper.find('[data-im="card-draft-hint"]').exists()).toBe(true);
    expect(wrapper.text()).toContain("草稿未保存");
    expect(wrapper.text()).toContain("服务端拒绝了草稿");
    expect(wrapper.text()).toContain("已保留");

    await wrapper.find('[data-im="card-draft-retry"]').trigger("click");
    await flushPromises();
    expect(imApi.saveDrafts).toHaveBeenCalledTimes(2);
    expect(store.draftStateFor("card:c9").status).toBe("saved");
    expect(imApi.submitBoard).not.toHaveBeenCalled();
    expect(api.sendTurn).not.toHaveBeenCalled();
    wrapper.unmount();
  });
});
