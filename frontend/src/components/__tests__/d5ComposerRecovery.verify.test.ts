/**
 * 独立验收 D5-C：普通对话页的发送失败恢复入口（契约 §11.4）。
 *
 * 由独立验收子智能体 D 编写，**不修改任何产品代码**。
 * 按用户行为断言：在对话页输入并发送、失败后，页面上必须能看到本次真实原因，
 * 并且始终有一条能取回失败原文的入口（输入框里已有新文字时也不覆盖，两份都保留）。
 *
 * v2（2026-10-22）：这些界面不再内联渲染在 Composer 里，而是移到输入区上方入口①
 * （ComposerDock → ComposerEntries → SendIssuesPanel）。入口默认收起：先点开再断言。
 *
 * 标注：【组件/DOM】挂载真实 ComposerDock；【模拟失败】api.sendTurn 拒绝。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { useSessionStore } from "../../stores/session";
import { api } from "../../services/api";
import ComposerDock from "../conversation/ComposerDock.vue";

// 模式切换与这些用例无关：用桩替换，避免测试环境缺少 router 造成噪声。
vi.mock("../conversation/ConversationModeSwitch.vue", () => ({
  default: { name: "ConversationModeSwitch", template: '<div class="mode-switch-stub" />' },
}));

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

const REASON = "网络中断：请求没有到达服务端";

/** 挂载输入区（模式切换与本组用例无关：避免测试环境缺少 router） */
function mountDock() {
  return mount(ComposerDock, { attachTo: document.body });
}

/** 找到「能取回失败原文」的可见按钮：放回 / 找回 / 取回 / 恢复 / 互换 */
function recoveryButtons(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll("button").filter((node) => {
    const el = node.element as HTMLButtonElement;
    const name = ((node.text() || "") + " " + (el.getAttribute("aria-label") ?? "") + " " + (el.getAttribute("title") ?? "")).trim();
    if (!/(放回|找回|取回|恢复|互换)/.test(name)) return false;
    /**
     * 驱动修正（主智能体）：jsdom 不做布局，getBoundingClientRect() 恒为全 0，
     * 原来那句 `rect.width > 0` 会让这个辅助函数**永远找不到按钮**（不是产品缺入口）。
     * 这里只排除禁用按钮；「按钮真的可见、能点」由真实浏览器验收（D 的探针与实机场景）覆盖。
     */
    return !el.disabled;
  });
}

/** v2：入口①默认收起；有失败事项时图标才出现，点开才渲染面板 */
async function openSendIssues(wrapper: ReturnType<typeof mount>): Promise<void> {
  const entry = wrapper.find('.entry-btn[data-entry="sendIssues"]');
  expect(entry.exists(), "输入区上方没有「发送异常与草稿恢复」入口").toBe(true);
  await entry.trigger("click");
  await flushPromises();
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

function rejectedSend(message: string) {
  // 合并后的契约（main 契约 5 + 互动线 §11.4）：只有**明确未受理**（4xx）才登记
  // 可恢复原文并显示失败入口；没有回执一律走「正在确认」。这些用例要考的是
  // 「明确失败后的恢复入口」，所以把 mock 换成带 status 的明确拒绝。
  return Object.assign(new Error(message), { status: 400 });
}

describe("场景 4：普通对话页发送失败（§11.4）", () => {
  it("【组件/DOM】失败后页面上直接看得见本次真实原因", async () => {
    vi.mocked(api.sendTurn).mockRejectedValueOnce(rejectedSend(REASON));
    const wrapper = mountDock();
    await wrapper.find("#composer-input").setValue("这段字发不出去");
    await wrapper.find(".send-btn").trigger("click");
    await flushPromises();

    // v2：原因在入口①面板里（入口默认收起，有失败事项才出现）
    await openSendIssues(wrapper);
    expect(wrapper.text(), "对话页没有显示这次发送失败的真实原因").toContain("网络中断");
  });

  it("【组件/DOM】输入框已有新文字时，仍要有取回失败原文的入口且两份都保留", async () => {
    vi.mocked(api.sendTurn).mockRejectedValueOnce(rejectedSend(REASON));
    const session = useSessionStore();
    const wrapper = mountDock();
    await wrapper.find("#composer-input").setValue("失败的那份原文");
    await wrapper.find(".send-btn").trigger("click");
    await flushPromises();
    await openSendIssues(wrapper);

    // 用户没有理会失败提示，继续输入了新的内容
    await wrapper.find("#composer-input").setValue("后来写的新内容");
    await flushPromises();
    expect(session.draft).toBe("后来写的新内容");

    const buttons = recoveryButtons(wrapper);
    expect(
      buttons.length,
      "对话页没有任何取回失败原文的入口（失败原因也不可见）",
    ).toBeGreaterThan(0);

    // 优先用「互换」入口（输入框已有新文字时的既有恢复方式），没有就点放回
    const swap = buttons.find((node) => /互换|交换|换回/.test(node.text())) ?? buttons[0];
    await swap.trigger("click");
    await flushPromises();

    const kept = [session.draft, session.failedSend?.text ?? ""].sort();
    expect(kept, "取回失败原文时丢掉了其中一份文字").toEqual(["后来写的新内容", "失败的那份原文"].sort());
  });
});
