/**
 * 失败原文展示层的「查看其余」反例(契约 §12.5 / §12.8,2026-10-09 第五轮)。
 *
 * 展示数量与保留数量分开:可以默认只显示较少项目并提供「查看其余」,
 * 但**隐藏不等于删除** —— store 里所有未处理失败原文都必须原样保留。
 *
 * 标注:【组件/DOM】挂载真实 FailedSendNotice.vue;【会话层】直接写 store 的失败记录。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { useSessionStore, type FailedSend } from "../../stores/session";
import { api } from "../../services/api";
import FailedSendNotice from "../interactive/FailedSendNotice.vue";

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(),
    getSessionContext: vi.fn(),
    stopTurn: vi.fn(),
    cancelContinuation: vi.fn(),
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
    fetchHistoryPage: vi.fn(),
    fetchMessages: vi.fn(),
  },
}));

/** 造 9 条带 id 与原因的失败记录(话题未定,与组件默认断言一致) */
function nineRecords(): FailedSend[] {
  return Array.from({ length: 9 }, (_, i) => ({
    id: "fail_" + (i + 1) + "_" + i,
    topicId: null,
    text: "没发出去的原文 " + (i + 1),
    draftSeq: i,
    at: 1000 + (9 - i),
  }));
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  vi.mocked(api.getSessionContext).mockResolvedValue({ topic_id: null } as never);
  setActivePinia(createPinia());
});

afterEach(() => {
  localStorage.clear();
});

describe("§12.5 展示数量与保留数量分开", () => {
  it("【会话层】九份失败原文:一份不裁剪,全部留在 store 里", async () => {
    const session = useSessionStore();
    session.currentTopicId = null;
    session.failedSends = nineRecords();

    expect(session.failedSends.length, "数据面只负责保留,不做静默裁剪").toBe(9);
    expect(session.failedSendsForTopic(null).length).toBe(9);
  });

  it("【组件/DOM】默认只显示较少项目并提供「查看其余」,隐藏不删除任何记录", async () => {
    const session = useSessionStore();
    session.currentTopicId = null;
    session.failedSends = nineRecords();

    const wrapper = mount(FailedSendNotice, { attachTo: document.body });
    await flushPromises();

    const items = wrapper.findAll('[data-im="chat-recovery-item"]');
    expect(
      items.length,
      "默认展示数量应当少于保留数量(§12.5:默认显示较少 + 「查看其余」)",
    ).toBeLessThan(9);
    expect(items.length).toBeGreaterThan(0);

    const more = wrapper.find('[data-im="chat-recovery-more"]');
    expect(
      more.exists(),
      "有被收起的项目时必须有「查看其余」入口",
    ).toBe(true);
    expect(more.text()).toContain("查看其余");
    // 总数如实显示:保留数量不因为收起而少报
    expect(wrapper.text()).toContain("9 条");

    // 隐藏 != 删除:store 里 9 条原样保留
    expect(session.failedSends.length).toBe(9);
    expect(session.failedSends[0].text).toBe("没发出去的原文 1");
    expect(session.failedSends[8].text).toBe("没发出去的原文 9");

    // 点「查看其余」:全部可看,恢复操作每条都齐
    await more.trigger("click");
    await flushPromises();
    expect(wrapper.findAll('[data-im="chat-recovery-item"]').length).toBe(9);
    expect(wrapper.findAll('[data-im="chat-recovery-restore"]').length).toBe(9);
  });

  it("【组件/DOM】不足默认展示数量时不出现「查看其余」,层数如实显示", async () => {
    const session = useSessionStore();
    session.currentTopicId = null;
    const records = nineRecords().slice(0, 2);
    session.failedSends = records;

    const wrapper = mount(FailedSendNotice, { props: { scope: "conversation" }, attachTo: document.body });
    await flushPromises();

    expect(wrapper.findAll('[data-im="composer-recovery-item"]').length).toBe(2);
    expect(wrapper.find('[data-im="composer-recovery-more"]').exists()).toBe(false);
  });
});