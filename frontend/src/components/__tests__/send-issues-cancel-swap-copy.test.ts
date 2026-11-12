/**
 * 文案 3：「发送异常」里取消的中间状态用「正在取消」，收到回执后再更新。
 * 文案 4：草稿取回写清与当前草稿互换、两份保留；按钮用「交换草稿」。
 *
 * 红（修复前）：取消已受理的发送时界面没有任何中间态（按钮直接消失、状态不变）；
 * 被让位草稿的按钮写「取回（与当前文字互换）」，没有「交换草稿」这个明确说法。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  cancelTurn: vi.fn(),
  resendInterruptedTurn: vi.fn(),
  dismissInterruptedTurn: vi.fn(),
}));

vi.mock("../../../services/api", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly path: string,
      detail: string,
    ) {
      super(detail);
      this.name = "ApiError";
    }
  },
  api: {
    cancelTurn: mocks.cancelTurn,
    cancelActiveTurn: vi.fn(async () => ({ ok: true, cancelled: true })),
    resendInterruptedTurn: mocks.resendInterruptedTurn,
    dismissInterruptedTurn: mocks.dismissInterruptedTurn,
  },
}));

import SendIssuesPanel from "../conversation/SendIssuesPanel.vue";
import { useSessionStore } from "../../stores/session";

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.currentTopicId = "t1";
  const w = mount(SendIssuesPanel, { global: { plugins: [pinia] } });
  return { w, session };
}

function seedAccepted(session: ReturnType<typeof useSessionStore>, messageId: string) {
  session.sendAttempts = [
    {
      messageId,
      clientRequestId: "req_1",
      state: "accepted",
      turnId: "turn_1",
      message: "已经受理的消息",
      topicId: "t1",
      queued: false,
    },
  ] as never;
  session.pendingSends = [
    {
      id: messageId,
      clientRequestId: "req_1",
      topicId: "t1",
      snapshot: {
        text: "已经受理的消息",
        topicId: "t1",
        attachmentIds: [],
        attachmentsExplicit: true,
        clientRequestId: "req_1",
        retryOfTurnId: null,
        draftId: "draft_1",
        continuation: null,
        queued: false,
        capturedAt: Date.now(),
      },
      state: "needs-confirm",
      lastError: null,
      lastCheck: null,
      createdAt: Date.now(),
      updatedAt: Date.now(),
    },
  ] as never;
  session.sendConfirm = {
    messageId,
    clientRequestId: "req_1",
    notice: "查证命中：这条消息已经被后端受理",
    unknown: false,
    accepted: true,
    busy: false,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mocks.cancelTurn.mockResolvedValue({ ok: true, cancelled: true, turn_id: "turn_1" });
});

describe("文案 3：取消已受理发送的中间状态", () => {
  it("请求在飞时显示「正在取消」，收到回执后才更新为结果", async () => {
    // 取消接口延迟 30ms 返回：期间界面只能显示「正在取消」，不能提前给出结果
    mocks.cancelTurn.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          setTimeout(() => resolve({ ok: true, cancelled: true, turn_id: "turn_1" }), 30);
        }) as never,
    );
    const { w, session } = setup();
    seedAccepted(session, "m1");
    await flushPromises();

    expect(w.text(), "点击前不该出现中间态").not.toContain("正在取消");
    await w.find('[data-im="composer-pending-cancel"]').trigger("click");
    await flushPromises();
    expect(w.text(), "请求在飞时应显示「正在取消」").toContain("正在取消");

    await vi.waitFor(() => expect(w.find(".notice").exists()).toBe(true), { timeout: 3000 });
    expect(w.text(), "收到回执后中间态应消失").not.toContain("正在取消");
    expect(w.find(".notice").text()).toContain("取消");
  });
});

describe("文案 4：草稿取回与当前草稿互换、两份保留、按钮「交换草稿」", () => {
  it("被让位的草稿：写清与当前草稿互换、两份保留；按钮用「交换草稿」", async () => {
    const { w, session } = setup();
    session.draft = "现在输入框里的草稿";
    session.chatDraftDisplaced = [
      {
        id: "d1",
        topicId: "t1",
        key: "k1",
        text: "被让位的旧草稿",
        seq: 1,
        at: Date.now(),
      },
    ] as never;
    await flushPromises();

    const section = w.find('[data-section="displaced"]');
    expect(section.exists()).toBe(true);
    expect(section.text()).toContain("当前草稿");
    expect(section.text()).toContain("两份都保留");
    const button = section.find("button");
    expect(button.text()).toBe("交换草稿");

    await button.trigger("click");
    await flushPromises();
    expect(session.draft).toBe("被让位的旧草稿");
    expect(session.chatDraftDisplaced[0]?.text).toBe("现在输入框里的草稿");
  });

  it("失败原文：输入框已有新文字时按钮同样用「交换草稿」", async () => {
    const { w, session } = setup();
    session.draft = "新写的文字";
    session.failedSends = [
      {
        id: "f1",
        draftId: "draft_1",
        topicId: "t1",
        text: "失败的那份原文",
        draftSeq: 1,
        contentVersion: 1,
        at: Date.now(),
      },
    ] as never;
    await flushPromises();

    const swap = w.find('[data-im="composer-recovery-swap"]');
    expect(swap.exists()).toBe(true);
    expect(swap.text()).toBe("交换草稿");
  });
});
