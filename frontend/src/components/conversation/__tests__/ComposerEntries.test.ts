/**
 * 输入区上方五入口的交互（B）。
 *
 * 钉住的都是行为，不是样式：
 * - 有事项才出现；默认收起；
 * - 每次最多展开一个：打开另一个收起当前，再点当前收起；
 * - 收起 ≠ 取消 / 处理：接续选择与事项状态原样保留；
 * - 审批入口直接打开统一弹窗（openNow），并收起其余四项；
 * - 排队可逐条取消（按 turn_id 精确取消，不误伤别的消息）；
 * - 「服务端已接受」与「受理未确认」分开表达。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({
  cancelTurn: vi.fn(async (turnId: string) => ({ ok: true, cancelled: true, turn_id: turnId })),
  cancelContinuation: vi.fn(async () => ({ ok: true, cancelled: true })),
}));

vi.mock("../../../services/api", () => ({
  api: {
    cancelTurn: mocks.cancelTurn,
    cancelContinuation: mocks.cancelContinuation,
  },
}));

import ComposerEntries from "../ComposerEntries.vue";
import { useSessionStore } from "../../../stores/session";
import { useApprovalsStore } from "../../../stores/approvals";

function freshPinia(): Pinia {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

async function mountEntries(pinia = freshPinia()) {
  const w = mount(ComposerEntries, { global: { plugins: [pinia] } });
  await nextTick();
  return w;
}

function entries(w: ReturnType<typeof mount>): string[] {
  return w.findAll(".entry-btn").map((b) => b.attributes("data-entry") ?? "");
}

function openPanel(w: ReturnType<typeof mount>): string | null {
  const panel = w.find(".panel");
  return panel.exists() ? panel.attributes("data-panel") ?? null : null;
}

function seedFailedSend(session: ReturnType<typeof useSessionStore>) {
  session.currentTopicId = null;
  (session as unknown as { failedSends: unknown[] }).failedSends = [
    {
      id: "failed_1",
      draftId: "draft_1",
      topicId: null,
      text: "发不出去的那句话",
      contentVersion: 1,
      at: Date.now(),
    },
  ];
  session.failedSendErrors = { failed_1: "后端拒绝了这次发送" };
}

beforeEach(() => {
  localStorage.clear();
  mocks.cancelTurn.mockClear();
  mocks.cancelContinuation.mockClear();
});

describe("五入口：出现、互斥展开、再点收起", () => {
  it("没有任何事项时不渲染入口行（不常驻空入口）", async () => {
    const w = await mountEntries();
    expect(w.find(".entry-row").exists()).toBe(false);
    expect(entries(w)).toEqual([]);
    w.unmount();
  });

  it("有事项才出现，并带真实数量", async () => {
    const pinia = freshPinia();
    const w = mount(ComposerEntries, { global: { plugins: [pinia] } });
    const session = useSessionStore();
    seedFailedSend(session);
    await nextTick();
    expect(entries(w)).toContain("sendIssues");
    const badge = w.find('.entry-btn[data-entry="sendIssues"] .count');
    expect(badge.exists()).toBe(true);
    expect(badge.text()).toBe("1");
    w.unmount();
  });

  it("每次最多展开一个：打开另一个收起当前；再点当前收起", async () => {
    const pinia = freshPinia();
    const w = mount(ComposerEntries, { global: { plugins: [pinia] } });
    const session = useSessionStore();
    seedFailedSend(session);
    session.setPendingContinuation({ intentId: "intent_1", sourceTitle: "旧片段" });
    await nextTick();

    await w.find('.entry-btn[data-entry="sendIssues"]').trigger("click");
    expect(openPanel(w)).toBe("sendIssues");
    expect(w.find('.entry-btn[data-entry="sendIssues"]').attributes("aria-expanded")).toBe("true");

    await w.find('.entry-btn[data-entry="continuation"]').trigger("click");
    expect(openPanel(w)).toBe("continuation");
    expect(w.find('.entry-btn[data-entry="sendIssues"]').attributes("aria-expanded")).toBe("false");

    await w.find('.entry-btn[data-entry="continuation"]').trigger("click");
    expect(openPanel(w)).toBeNull();
    w.unmount();
  });

  it("收起接续面板不会取消已选位置（取消只由面板里的按钮触发）", async () => {
    const pinia = freshPinia();
    const w = mount(ComposerEntries, { global: { plugins: [pinia] } });
    const session = useSessionStore();
    session.setPendingContinuation({ intentId: "intent_1", sourceTitle: "旧片段" });
    await nextTick();

    await w.find('.entry-btn[data-entry="continuation"]').trigger("click");
    expect(w.find(".panel").text()).toContain("下一条消息将从「旧片段」继续");
    await w.find('.entry-btn[data-entry="continuation"]').trigger("click");
    expect(w.find(".panel").exists()).toBe(false);
    expect(session.pendingContinuation).not.toBeNull();
    expect(mocks.cancelContinuation).not.toHaveBeenCalled();
    w.unmount();
  });

  it("接续面板里的取消是真实请求；失败保留状态并说明", async () => {
    const pinia = freshPinia();
    const w = mount(ComposerEntries, { global: { plugins: [pinia] } });
    const session = useSessionStore();
    session.setPendingContinuation({ intentId: "intent_1", sourceTitle: "旧片段" });
    mocks.cancelContinuation.mockResolvedValueOnce({ ok: false, cancelled: false } as never);
    await nextTick();

    await w.find('.entry-btn[data-entry="continuation"]').trigger("click");
    await w.find(".panel .qio-btn").trigger("click");
    await flushPromises();
    expect(mocks.cancelContinuation).toHaveBeenCalledTimes(1);
    // 后端没有确认取消 → 本地不能假装已取消
    expect(session.pendingContinuation).not.toBeNull();
    expect(w.find(".panel").text()).toContain("没有取消");
    w.unmount();
  });
});

describe("审批入口：打开统一弹窗并收起其余四项", () => {
  it("点击审批入口调用 openNow()，不展开自己的面板，也不留别的面板", async () => {
    const pinia = freshPinia();
    const w = mount(ComposerEntries, { global: { plugins: [pinia] } });
    const session = useSessionStore();
    const approvals = useApprovalsStore();
    seedFailedSend(session);
    approvals.queue = [
      { approval_id: "ap1", kind: "tool_call", payload: {}, stale: false },
      { approval_id: "ap2", kind: "tool_call", payload: {}, stale: false },
    ] as never;
    approvals.deferred = true;
    const spy = vi.spyOn(approvals, "openNow");
    await nextTick();

    await w.find('.entry-btn[data-entry="sendIssues"]').trigger("click");
    expect(openPanel(w)).toBe("sendIssues");

    await w.find('.entry-btn[data-entry="approvals"]').trigger("click");
    expect(spy).toHaveBeenCalledTimes(1);
    expect(approvals.deferred).toBe(false);
    expect(openPanel(w)).toBeNull();
    expect(w.find('.entry-btn[data-entry="approvals"] .count').text()).toBe("2");
    w.unmount();
  });
});

describe("排队入口：真实数据与取消", () => {
  it("展开后列出排队消息，取消按 turn_id 精确提交（不宣称已停止）", async () => {
    const pinia = freshPinia();
    const w = mount(ComposerEntries, { global: { plugins: [pinia] } });
    const session = useSessionStore();
    session.turnQueue = {
      running: null,
      queued: [
        { turn_id: "q1", message: "排队一" },
        { turn_id: "q2", message: "排队二" },
      ],
      cancelled: [],
    };
    await nextTick();

    await w.find('.entry-btn[data-entry="queue"]').trigger("click");
    expect(openPanel(w)).toBe("queue");
    expect(w.find(".panel").text()).toContain("排队一");

    const cancelButtons = w.findAll(".panel .row-actions button");
    await cancelButtons[0].trigger("click");
    await flushPromises();
    expect(mocks.cancelTurn).toHaveBeenCalledWith("q1");
    expect(w.find(".panel").text()).toContain("以后端确认为准");
    w.unmount();
  });

  it("本机已提交但后端还没确认的行不提供按身份取消（不假装能取消）", async () => {
    const pinia = freshPinia();
    const w = mount(ComposerEntries, { global: { plugins: [pinia] } });
    const session = useSessionStore();
    session.pushUser("本机已提交的消息");
    session.messages[session.messages.length - 1].queued = true;
    await nextTick();

    await w.find('.entry-btn[data-entry="queue"]').trigger("click");
    expect(w.find(".panel").text()).toContain("本机已提交");
    expect(w.find(".panel .row-actions button").exists()).toBe(false);
    expect(w.find(".panel").text()).toContain("不能按身份取消");
    w.unmount();
  });
});

describe("发送异常入口与草稿恢复", () => {
  it("失败原文与原因都在，找回原文会放回输入框但不自动发送", async () => {
    const pinia = freshPinia();
    const w = mount(ComposerEntries, { global: { plugins: [pinia] } });
    const session = useSessionStore();
    seedFailedSend(session);
    session.draft = "";
    await nextTick();

    await w.find('.entry-btn[data-entry="sendIssues"]').trigger("click");
    expect(w.find(".panel").text()).toContain("发不出去的那句话");
    expect(w.find(".panel").text()).toContain("后端拒绝了这次发送");
    const retry = w.find('[data-section="failed"] .qio-btn');
    await retry.trigger("click");
    await nextTick();
    expect(session.draft).toBe("发不出去的那句话");
    expect(w.find(".panel").text()).toContain("还没有发送");
    w.unmount();
  });

  it("受理未确认的发送不提供「直接再发」的语义，只给查证 / 原身份重试 / 放弃", async () => {
    const pinia = freshPinia();
    const w = mount(ComposerEntries, { global: { plugins: [pinia] } });
    const session = useSessionStore();
    session.currentTopicId = null;
    session.pendingSends = [
      {
        id: "pending_1",
        clientRequestId: "req_1",
        topicId: null,
        snapshot: { text: "可能已经发出去的消息" },
        state: "needs-confirm",
        lastError: "等待回执超时",
        lastCheck: null,
        createdAt: Date.now(),
        updatedAt: Date.now(),
      },
    ] as never;
    await nextTick();

    await w.find('.entry-btn[data-entry="sendIssues"]').trigger("click");
    const text = w.find(".panel").text();
    expect(text).toContain("可能已经发出去的消息");
    expect(text).toContain("未确认");
    expect(text).toContain("再查一次");
    expect(text).toContain("用原身份重试");
    expect(text).toContain("放弃");
    // 不提供「直接发送」这样的重复提交按钮
    expect(text).not.toContain("直接发送");
    w.unmount();
  });

  it("话题切换建议只给选择，不自动切换", async () => {
    const pinia = freshPinia();
    const w = mount(ComposerEntries, { global: { plugins: [pinia] } });
    const session = useSessionStore();
    session.pendingSwitch = { topicId: "t2", topicName: "另一个话题", reason: "关键词更像它" };
    const confirm = vi.spyOn(session, "confirmPendingSwitch").mockResolvedValue(undefined as never);
    await nextTick();

    await w.find('.entry-btn[data-entry="topicSwitch"]').trigger("click");
    expect(w.find(".panel").text()).toContain("另一个话题");
    expect(w.find(".panel").text()).toContain("不会自行切换");
    expect(confirm).not.toHaveBeenCalled();
    await w.find(".panel .qio-btn.primary").trigger("click");
    await flushPromises();
    expect(confirm).toHaveBeenCalledTimes(1);
    w.unmount();
  });
});
