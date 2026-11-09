/**
 * 阅读位置恢复（问题 2）：用户主动接管滚动后，整次恢复任务必须彻底失效。
 *
 * 复现路径：从设置返回聊天 → 恢复阅读位置的「更早历史」请求还在飞 → 用户自己滚到新位置
 * → 历史请求返回后，旧实现仍会继续按旧目标定位、继续补页，找不到位置时兜底跳到顶部。
 *
 * 这里用受控 Promise 控制历史请求返回顺序（假历史数据，不联网、不依赖真实等待）。
 */
import { describe, expect, it, beforeEach, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { nextTick } from "vue";
import MessageStream from "../MessageStream.vue";
import { useSessionStore } from "../../stores/session";
import { useChatViewportStore, type SavedReadingPosition } from "../../stores/chatViewport";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(async () => ({ ok: true })),
    getSessionContext: vi.fn(async () => ({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [],
    })),
    getSessionMessagesBefore: vi.fn(),
  },
}));

type HistoryPage = Awaited<ReturnType<typeof api.getSessionMessagesBefore>>;
type HistoryRequest = (topicId: string | null, before: string, limit?: number) => Promise<HistoryPage>;

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** jsdom 不做布局：手动给滚动容器一个可测的几何 */
function shape(el: HTMLElement, scrollHeight: number, clientHeight: number) {
  Object.defineProperty(el, "scrollHeight", { value: scrollHeight, configurable: true });
  Object.defineProperty(el, "clientHeight", { value: clientHeight, configurable: true });
}

function savedPosition(overrides: Partial<SavedReadingPosition> = {}): SavedReadingPosition {
  return {
    topicId: null,
    turnKey: "",
    anchorKey: null,
    anchorOffset: 0,
    turnOffset: 0,
    textSnippet: null,
    textOffset: null,
    scrollTop: 0,
    following: false,
    messageCount: 0,
    lastMessageId: null,
    savedAt: Date.now(),
    viewportWidth: 1000,
    ...overrides,
  };
}

interface FakeMessage {
  id: string;
  role: string;
  content: string;
  turn_id?: string | null;
}

function pageOf(
  messages: FakeMessage[],
  options: { hasMore?: boolean; nextBefore?: string | null } = {},
): HistoryPage {
  return {
    topic_id: "t1",
    messages: messages.map((m) => ({
      id: m.id,
      role: m.role,
      content: m.content,
      content_type: "text",
      created_at: "2026-01-01T00:00:00Z",
      turn_id: m.turn_id ?? null,
    })),
    tool_records: [],
    has_more: Boolean(options.hasMore),
    next_before: options.nextBefore ?? null,
  };
}

/** 一页更早的历史（每组：一条 user + 一条 assistant，同一轮） */
function olderPage(turnId: string): FakeMessage[] {
  return [
    { id: "old-user-1", role: "user", content: "更早的问题", turn_id: turnId },
    { id: "old-assistant-1", role: "assistant", content: "更早的回答", turn_id: turnId },
  ];
}

/** 等微任务 + 一帧/两帧（恢复流程里有 nextTick / rAF） */
async function flush(ms = 80): Promise<void> {
  await new Promise((r) => setTimeout(r, ms));
  await nextTick();
}

interface PendingRestore {
  pinia: Pinia;
  w: ReturnType<typeof mount>;
  session: ReturnType<typeof useSessionStore>;
  viewport: ReturnType<typeof useChatViewportStore>;
  stream: HTMLElement;
  history: Deferred<HistoryPage>;
}

/**
 * 构造「正在恢复中间阅读位置、卡在更早历史请求上」的状态：
 * 保存的轮次 turn_target 不在已加载消息里，只有历史请求返回后才可能出现。
 */
async function mountPendingRestore(): Promise<PendingRestore> {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  const viewport = useChatViewportStore();
  session.currentTopicId = "t1";
  session.pushUser("当前话题里已经加载的消息");
  session.history = { status: "ready", error: null };
  session.historyHasMore = true;
  session.historyCursor = "cursor-1";
  const last = session.messages[session.messages.length - 1];
  viewport.savePosition(
    savedPosition({
      topicId: "t1",
      turnKey: "turn_target",
      following: false,
      scrollTop: 300,
      messageCount: session.messages.length,
      lastMessageId: last?.id ?? null,
    }),
  );
  const history = deferred<HistoryPage>();
  vi.mocked(api.getSessionMessagesBefore).mockImplementation(
    () => history.promise as ReturnType<HistoryRequest>,
  );
  const w = mount(MessageStream, { global: { plugins: [pinia] } });
  await nextTick();
  const stream = w.find(".stream").element as HTMLElement;
  shape(stream, 1000, 400); // 底部 = scrollTop 600
  await nextTick();
  return { pinia, w, session, viewport, stream, history };
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(api.getSessionMessagesBefore).mockReset();
});

describe("MessageStream 阅读恢复：用户接管后整次恢复任务失效（问题 2）", () => {
  it("恢复等待历史期间用户滚轮接管：请求返回后保持用户新位置，不兜底跳转、不继续补页", async () => {
    const { w, stream, session, viewport, history } = await mountPendingRestore();
    expect(api.getSessionMessagesBefore).toHaveBeenCalledTimes(1);

    // 用户自己滚到新位置（滚轮 + 浏览器随后补的 scroll 事件）
    stream.dispatchEvent(new Event("wheel"));
    stream.scrollTop = 140;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    // 旧的历史请求这时才返回：目标轮不在这一页，而且后端说还有更多
    history.resolve(
      pageOf(olderPage("turn_other"), { hasMore: true, nextBefore: "cursor-2" }),
    );
    await flush(140);

    // 用户新选择的位置保持不变（旧实现会继续补页并最终兜底跳到 0）
    expect(stream.scrollTop).toBe(140);
    expect(w.find('[data-test="restore-note"]').exists()).toBe(false);
    expect(api.getSessionMessagesBefore).toHaveBeenCalledTimes(1);
    // 已经发出的请求仍然完成了数据读取，并落进当前话题（不因恢复作废而丢掉数据）
    expect(session.messages[0]?.id).toBe("old-user-1");
    expect(session.historyOlderLoading).toBe(false);
    expect(viewport.following).toBe(false);
    w.unmount();
  });

  it("恢复等待历史期间用滚动条拖动（只有 scroll 事件，没有滚轮）：同样整次恢复作废", async () => {
    const { w, stream, history } = await mountPendingRestore();
    expect(api.getSessionMessagesBefore).toHaveBeenCalledTimes(1);

    // 拖动滚动条不会冒泡出 wheel / pointerdown，只有 scroll 事件能表达用户接管
    stream.scrollTop = 90;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    history.resolve(pageOf(olderPage("turn_other"), { hasMore: false }));
    await flush(140);

    expect(stream.scrollTop).toBe(90);
    expect(w.find('[data-test="restore-note"]').exists()).toBe(false);
    w.unmount();
  });

  it("延迟历史返回的页里正好有旧目标轮：用户接管后也不按旧目标定位", async () => {
    const { w, stream, session, history } = await mountPendingRestore();

    stream.dispatchEvent(new Event("touchstart"));
    stream.scrollTop = 160;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    history.resolve(pageOf(olderPage("turn_target"), { hasMore: false }));
    await flush(160);

    expect(stream.scrollTop).toBe(160);
    expect(w.find('[data-test="restore-note"]').exists()).toBe(false);
    // 数据读入后旧目标轮确实存在，但恢复任务已失效，不再驱动位置
    expect(session.messages.some((m) => m.turnId === "turn_target")).toBe(true);
    w.unmount();
  });

  it("恢复等待历史期间切换话题：旧请求结果不改新话题位置，也不串进新话题消息", async () => {
    const { w, stream, session, history } = await mountPendingRestore();
    const before = session.messages.map((m) => m.id);

    session.currentTopicId = "t2";
    await nextTick();
    await flush(60);
    // 切话题后默认回到最新位置
    expect(stream.scrollTop).toBe(600);

    history.resolve(
      pageOf(olderPage("turn_other"), { hasMore: true, nextBefore: "cursor-2" }),
    );
    await flush(160);

    expect(stream.scrollTop).toBe(600);
    expect(session.messages.map((m) => m.id)).toEqual(before);
    expect(w.find('[data-test="restore-note"]').exists()).toBe(false);
    w.unmount();
  });

  it("连续进出设置：卸载重挂后，旧实例的延迟恢复不改变新实例的位置", async () => {
    const first = await mountPendingRestore();
    expect(api.getSessionMessagesBefore).toHaveBeenCalledTimes(1);
    first.w.unmount();
    await nextTick();
    // 重新进入对话：不需要再恢复旧位置（本次测试只关心旧请求迟到不得动新实例）
    first.viewport.clearPosition("t1");

    const w2 = mount(MessageStream, { global: { plugins: [first.pinia] } });
    await nextTick();
    const stream2 = w2.find(".stream").element as HTMLElement;
    shape(stream2, 1000, 400);
    await flush(80);
    expect(stream2.scrollTop).toBe(600); // 跟随到底

    first.history.resolve(pageOf(olderPage("turn_other"), { hasMore: false }));
    await flush(160);

    expect(stream2.scrollTop).toBe(600);
    expect(w2.find('[data-test="restore-note"]').exists()).toBe(false);
    // 同一份历史读回来仍属于当前话题：数据不丢
    expect(first.session.messages[0]?.id).toBe("old-user-1");
    w2.unmount();
  });

  it("连续进出设置：上一实例的历史请求还在飞，新实例等它读完再恢复（不误判失败跳顶）", async () => {
    const first = await mountPendingRestore();
    expect(api.getSessionMessagesBefore).toHaveBeenCalledTimes(1);

    first.w.unmount();
    await nextTick();
    // 不清理阅读位置：重新进入后仍要恢复到原消息
    const w2 = mount(MessageStream, { global: { plugins: [first.pinia] } });
    await nextTick();
    const stream2 = w2.find(".stream").element as HTMLElement;
    shape(stream2, 1000, 400);
    await flush(80);

    // 新实例的恢复卡在「上一实例正在读的那一页」上：不能急着宣告失败、重复发请求
    expect(w2.find('[data-test="restore-note"]').exists()).toBe(false);
    expect(api.getSessionMessagesBefore).toHaveBeenCalledTimes(1);

    // 上一实例的请求带着目标轮返回
    first.history.resolve(pageOf(olderPage("turn_target"), { hasMore: false }));
    await flush(240);

    expect(w2.find('[data-test="restore-note"]').exists()).toBe(false);
    // 同一份历史读回来属于当前话题：数据不丢，新实例据此继续恢复
    expect(first.session.messages.some((m) => m.turnId === "turn_target")).toBe(true);
    w2.unmount();
  });

  it("恢复等待历史期间来新消息（用户没有接管）：恢复继续，不把阅读位置拉回最新", async () => {
    const { w, stream, session, viewport, history } = await mountPendingRestore();

    session.pushUser("恢复期间到达的新消息");
    await nextTick();

    history.resolve(pageOf(olderPage("turn_target"), { hasMore: false }));
    await flush(160);

    expect(session.messages[0]?.id).toBe("old-user-1");
    expect(w.find('[data-test="restore-note"]').exists()).toBe(false);
    expect(session.streamFollowing).toBe(false);
    expect(viewport.following).toBe(false);
    expect(viewport.unseen).toBe(1); // 新消息只计数
    expect(stream.scrollTop).not.toBe(600); // 没有被新消息拉回底部
    w.unmount();
  });

  it("恢复失败有界：目标一直找不到时补页次数有上限，失败只说明一次，不无限加载", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const viewport = useChatViewportStore();
    session.currentTopicId = "t1";
    session.pushUser("当前消息");
    session.history = { status: "ready", error: null };
    session.historyHasMore = true;
    session.historyCursor = "cursor-1";
    const last = session.messages[session.messages.length - 1];
    viewport.savePosition(
      savedPosition({
        topicId: "t1",
        turnKey: "turn_never_exists",
        following: false,
        scrollTop: 300,
        messageCount: 1,
        lastMessageId: last?.id ?? null,
      }),
    );
    vi.mocked(api.getSessionMessagesBefore).mockImplementation(async (_topicId, before) =>
      pageOf(olderPage("turn_" + before), { hasMore: true, nextBefore: "next-" + before }),
    );
    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    await nextTick();
    const stream = w.find(".stream").element as HTMLElement;
    shape(stream, 1000, 400);
    await flush(240);

    const calls = vi.mocked(api.getSessionMessagesBefore).mock.calls.length;
    expect(calls).toBeGreaterThan(0);
    expect(calls).toBeLessThanOrEqual(6);
    expect(w.find('[data-test="restore-note"]').exists()).toBe(true);
    // 有界：再等一段时间也不会继续补页
    await flush(160);
    expect(vi.mocked(api.getSessionMessagesBefore).mock.calls.length).toBe(calls);
    w.unmount();
  });

  it("恢复等待历史期间键盘滚动（PageUp）：同样让整次恢复失效", async () => {
    const { w, stream, history } = await mountPendingRestore();
    expect(api.getSessionMessagesBefore).toHaveBeenCalledTimes(1);

    stream.dispatchEvent(new KeyboardEvent("keydown", { key: "PageUp" }));
    stream.scrollTop = 200;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    history.resolve(pageOf(olderPage("turn_other"), { hasMore: true, nextBefore: "cursor-2" }));
    await flush(140);

    expect(stream.scrollTop).toBe(200);
    expect(w.find('[data-test="restore-note"]').exists()).toBe(false);
    expect(api.getSessionMessagesBefore).toHaveBeenCalledTimes(1);
    w.unmount();
  });

  it("拖动 QIO 指示条（滑块 pointerdown 会 stopPropagation）：捕获阶段也要让恢复让位", async () => {
    const { w, stream, history } = await mountPendingRestore();
    // 先滚动一次让共享指示条创建滑块（jsdom 里几何是手动给的）
    stream.dispatchEvent(new Event("scroll"));
    await flush(40);
    const thumb = stream.querySelector(".qio-scroll-thumb");
    expect(thumb).not.toBeNull();

    // 滑块自己的 pointerdown 监听会 stopPropagation：冒泡阶段收不到，只有捕获阶段能拦到
    thumb!.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, cancelable: true, button: 0 }));
    stream.scrollTop = 110;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    history.resolve(pageOf(olderPage("turn_other"), { hasMore: false }));
    await flush(140);

    expect(stream.scrollTop).toBe(110);
    expect(w.find('[data-test="restore-note"]').exists()).toBe(false);
    w.unmount();
  });

  it("恢复作废后历史才读回来：只补上方插入的高度，用户当前内容不被顶走", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const viewport = useChatViewportStore();
    session.currentTopicId = "t1";
    // 先让首屏历史处于 loading：恢复要等消息到位才开始（那时容器已有真实高度）
    session.history = { status: "loading", error: null };
    session.historyHasMore = true;
    session.historyCursor = "cursor-1";
    viewport.savePosition(
      savedPosition({
        topicId: "t1",
        turnKey: "turn_target",
        following: false,
        scrollTop: 300,
        messageCount: 1,
        lastMessageId: "seed",
      }),
    );
    const history = deferred<HistoryPage>();
    vi.mocked(api.getSessionMessagesBefore).mockImplementation(
      () => history.promise as ReturnType<HistoryRequest>,
    );
    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    await nextTick();
    const stream = w.find(".stream").element as HTMLElement;
    shape(stream, 1000, 400);

    // 首屏历史到位 → 恢复开始（此时记录的上方高度基线是 1000）
    session.pushUser("当前消息");
    session.history = { status: "ready", error: null };
    await flush(60);
    expect(api.getSessionMessagesBefore).toHaveBeenCalledTimes(1);

    // 用户接管，滚到 140
    stream.dispatchEvent(new Event("wheel"));
    stream.scrollTop = 140;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    // 历史页返回后插到列表上方：内容变高 400px（真实浏览器由布局决定，这里手动模拟）
    shape(stream, 1400, 400);
    history.resolve(pageOf(olderPage("turn_other"), { hasMore: false }));
    await flush(160);

    // 上方插入 400px → 为用户补回 400px：视口里仍是用户刚选中的那段内容，不是旧目标位置
    expect(stream.scrollTop).toBe(540);
    expect(w.find('[data-test="restore-note"]').exists()).toBe(false);
    w.unmount();
  });

  it("历史返回、虚拟列表定位/校正进行中用户滚轮接管：旧目标不再把位置拽走", async () => {
    const { w, stream, history } = await mountPendingRestore();

    // 没有用户接管时恢复继续：请求返回后进入按轮次定位 / 逐帧校正
    history.resolve(pageOf(olderPage("turn_target"), { hasMore: false }));
    await flush(40);

    // 校正期间用户接管（滚轮优先于恢复程序的任何延迟回调）
    stream.dispatchEvent(new Event("wheel"));
    stream.scrollTop = 220;
    stream.dispatchEvent(new Event("scroll"));
    await flush(200);

    expect(stream.scrollTop).toBe(220);
    expect(w.find('[data-test="restore-note"]').exists()).toBe(false);
    w.unmount();
  });
});
