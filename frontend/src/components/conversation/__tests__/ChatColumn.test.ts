/**
 * 中央工作区（B）：话题栏 / 全局状态区 / 消息流空状态。
 *
 * 钉住的行为：
 * - 话题标题常驻，来自真实 session；点击才去读话题说明，读不到如实说、可重试、不编造描述；
 * - 历史读取失败出现在话题栏下方（可重试），文案明确「不代表没有历史」，且不使用全局空态；
 * - 全局错误 / 凭据 / 同步失败进顶部状态区，不塞进消息流；
 * - 消息流的空 / 加载 / 失败三态如实；「回到最新消息」的未读计数来自流内真实逻辑。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({
  getTopicDetail: vi.fn(),
  sendTurn: vi.fn(async () => ({ ok: true, accepted: true, turn_id: "turn_1", topic_id: null })),
  getSessionContext: vi.fn(async () => ({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] })),
  cancelTurn: vi.fn(async (id: string) => ({ ok: true, cancelled: true, turn_id: id })),
  restoreRuntimeState: vi.fn(async () => ({ ok: true })),
}));

vi.mock("../../../services/api", () => ({
  api: {
    getTopicDetail: mocks.getTopicDetail,
    sendTurn: mocks.sendTurn,
    getSessionContext: mocks.getSessionContext,
    cancelTurn: mocks.cancelTurn,
  },
}));

vi.mock("../../../stores/restore", () => ({
  restoreRuntimeState: mocks.restoreRuntimeState,
}));

import ChatColumn from "../ChatColumn.vue";
import MessageStream from "../../MessageStream.vue";
import { useSessionStore } from "../../../stores/session";
import { useEventStore } from "../../../stores/events";

function freshPinia(): Pinia {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

function mountColumn(pinia = freshPinia()) {
  return mount(ChatColumn, {
    global: {
      plugins: [pinia],
      stubs: {
        MessageStream: { template: '<div class="stream-stub" />' },
        ComposerDock: { template: '<div class="dock-stub" />' },
        KnowledgeCandidateCard: true,
        RouterLink: { props: ["to"], template: "<a><slot /></a>" },
      },
    },
  });
}

beforeEach(() => {
  localStorage.clear();
  mocks.getTopicDetail.mockReset();
  mocks.getTopicDetail.mockResolvedValue({
    topic_id: "t1",
    name: "架构认知",
    summary: "关于工具与记忆的讨论",
    keywords: ["工具", "记忆"],
    message_count: 12,
    last_activity: "2026-10-20T10:00:00+08:00",
    fragments: [{ fragment_id: "f1", summary: null, closed_at: null, message_count: 3 }],
    entities: [],
    knowledge: [],
  });
});

describe("话题栏：标题常驻 + 完整说明", () => {
  it("标题来自当前话题；没有话题时使用如实的默认称法", async () => {
    const pinia = freshPinia();
    const session = useSessionStore();
    session.topicName = "架构认知";
    const w = mountColumn(pinia);
    expect(w.find('[data-test="topic-title"]').text()).toBe("架构认知");
    w.unmount();

    session.topicName = null;
    session.currentTopicId = null;
    const w2 = mountColumn(pinia);
    expect(w2.find('[data-test="topic-title"]').text()).toBe("默认话题");
    w2.unmount();
  });

  it("点击标题才读取话题说明；描述 / 关键词 / 规模都来自后端真实数据", async () => {
    const pinia = freshPinia();
    useSessionStore().currentTopicId = "t1";
    const w = mountColumn(pinia);
    expect(mocks.getTopicDetail).not.toHaveBeenCalled();
    await w.find(".topic-main").trigger("click");
    await flushPromises();
    expect(mocks.getTopicDetail).toHaveBeenCalledWith("t1");
    const details = w.find("#topic-details");
    expect(details.exists()).toBe(true);
    expect(details.text()).toContain("关于工具与记忆的讨论");
    expect(details.text()).toContain("工具 · 记忆");
    expect(details.text()).toContain("12 条");
    w.unmount();
  });

  it("说明读取失败：如实说原因并给重试，不编造描述", async () => {
    const pinia = freshPinia();
    useSessionStore().currentTopicId = "t1";
    mocks.getTopicDetail.mockRejectedValueOnce(new Error("network down"));
    const w = mountColumn(pinia);
    await w.find(".topic-main").trigger("click");
    await flushPromises();
    const details = w.find("#topic-details");
    expect(details.text()).toContain("话题说明没有读出来");
    expect(details.text()).not.toContain("关于工具与记忆的讨论");
    expect(details.text()).not.toContain("还没有描述");

    mocks.getTopicDetail.mockResolvedValueOnce({
      topic_id: "t1",
      name: "架构认知",
      summary: null,
      fragments: [],
      entities: [],
      knowledge: [],
    });
    await details.find(".qio-btn").trigger("click");
    await flushPromises();
    // 重试成功但没有摘要：如实说「没有描述」，不编一段
    expect(w.find("#topic-details").text()).toContain("这个话题还没有描述");
    w.unmount();
  });

  it("历史续接状态说明「当前位置」与「下一条生效」的区别", async () => {
    const pinia = freshPinia();
    const session = useSessionStore();
    session.currentTopicId = "t1";
    session.setAnchor("t1", "f13", "话题A", { id: "f13", title: "锚点片段" }, true);
    session.setPendingContinuation({ intentId: "intent_1", sourceTitle: "锚点片段" });
    const w = mountColumn(pinia);
    expect(w.find('[data-test="topic-continuation"]').text()).toContain("下一条将从「锚点片段」继续");
    await w.find(".topic-main").trigger("click");
    await flushPromises();
    const text = w.find("#topic-details").text();
    expect(text).toContain("已经登记");
    expect(text).toContain("下一条消息时生效");
    w.unmount();
  });
});

describe("全局状态区：影响整个对话的异常在话题栏下方", () => {
  it("历史读取失败：可重试、明确不是没有历史，且不进消息流", async () => {
    const pinia = freshPinia();
    const session = useSessionStore();
    session.history = { status: "error", error: "timeout" };
    const retry = vi.spyOn(session, "retryHistory").mockResolvedValue(undefined as never);
    const w = mountColumn(pinia);
    const notice = w.find('[data-notice="history"]');
    expect(notice.exists()).toBe(true);
    expect(notice.text()).toContain("不代表没有历史");
    await notice.find("button.link").trigger("click");
    expect(retry).toHaveBeenCalledTimes(1);
    // 状态区在消息流之上：消息流桩里不该出现这条提示
    expect(w.find(".stream-stub").text()).not.toContain("历史记录暂时无法读取");
    w.unmount();
  });

  it("全局错误与凭据提示分别显示（有错误时不重复显示警告）", async () => {
    const pinia = freshPinia();
    const session = useSessionStore();
    const events = useEventStore();
    session.lastError = "模型凭据已失效";
    events.credentialNotice = "必须显示在设置里";
    const w = mountColumn(pinia);
    expect(w.find('[data-notice="lastError"]').text()).toContain("模型凭据已失效");
    expect(w.find('[data-notice="credential"]').exists()).toBe(false);
    w.unmount();

    const w2 = mountColumn(freshPinia());
    useEventStore().credentialNotice = "当前没有可用的模型凭据";
    await nextTick();
    expect(w2.find('[data-notice="credential"]').text()).toContain("当前没有可用的模型凭据");
    w2.unmount();
  });

  it("断线后同步失败：说「可能不是最新」并可重新同步", async () => {
    const pinia = freshPinia();
    const session = useSessionStore();
    session.resyncState = "failed";
    const w = mountColumn(pinia);
    const notice = w.find('[data-notice="resync"]');
    expect(notice.exists()).toBe(true);
    expect(notice.text()).toContain("可能不是最新");
    await notice.find("button.link").trigger("click");
    await flushPromises();
    expect(mocks.restoreRuntimeState).toHaveBeenCalledWith("resync");
    w.unmount();
  });
});

describe("消息流空 / 加载 / 失败三态（真实 MessageStream）", () => {
  async function mountStream(pinia = freshPinia()) {
    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    await nextTick();
    return w;
  }

  it("正在读取历史 → 加载态，不显示成没有消息", async () => {
    const pinia = freshPinia();
    useSessionStore().history = { status: "loading", error: null };
    const w = await mountStream(pinia);
    const empty = w.find('[data-test="stream-empty"]');
    expect(empty.text()).toContain("正在读取历史");
    expect(empty.text()).not.toContain("今天想聊点什么");
    w.unmount();
  });

  it("历史读取失败 → 如实说没有读出来，不显示空对话引导", async () => {
    const pinia = freshPinia();
    useSessionStore().history = { status: "error", error: "timeout" };
    const w = await mountStream(pinia);
    const empty = w.find('[data-test="stream-empty"]');
    expect(empty.text()).toContain("历史没有读取出来");
    expect(empty.text()).not.toContain("今天想聊点什么");
    w.unmount();
  });

  it("真的没有消息才显示空对话引导（不再有未接线的假按钮）", async () => {
    const pinia = freshPinia();
    useSessionStore().history = { status: "ready", error: null };
    const w = await mountStream(pinia);
    const empty = w.find('[data-test="stream-empty"]');
    expect(empty.text()).toContain("今天想聊点什么");
    expect(empty.find(".chip").exists()).toBe(false);
    w.unmount();
  });

  it("「回到最新消息」的未读数量来自流内真实计数（上翻后新消息才累加）", async () => {
    const pinia = freshPinia();
    const session = useSessionStore();
    const w = await mountStream(pinia);
    const stream = w.find(".stream").element as HTMLElement;
    Object.defineProperty(stream, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(stream, "clientHeight", { value: 400, configurable: true });
    stream.scrollTop = 100; // 距底部很远 → 停止跟随
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();
    await nextTick();

    session.pushUser("上翻之后来的消息");
    await nextTick();
    await nextTick();
    await nextTick();

    const button = w.find(".back-latest");
    expect(button.exists()).toBe(true);
    expect(button.text()).toContain("回到最新消息");
    expect(button.find(".count").exists()).toBe(true);
    expect(button.find(".count").text()).toBe("1");
    w.unmount();
  });
});
