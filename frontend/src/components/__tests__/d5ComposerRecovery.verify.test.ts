/**
 * 独立验收 D5-C：普通对话页的发送失败恢复入口（契约 §11.4）。
 *
 * 由独立验收子智能体 D 编写，**不修改任何产品代码**。
 * 按用户行为断言：在对话页输入并发送、失败后，页面上必须能看到本次真实原因，
 * 并且始终有一条能取回失败原文的入口（输入框里已有新文字时也不覆盖，两份都保留）。
 *
 * 标注：【组件/DOM】挂载真实 Composer.vue；【模拟失败】api.sendTurn 拒绝。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { useSessionStore } from "../../stores/session";
import { api } from "../../services/api";
import Composer from "../Composer.vue";

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

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  vi.mocked(api.getSessionContext).mockResolvedValue({ topic_id: null } as never);
  setActivePinia(createPinia());
});

afterEach(() => {
  localStorage.clear();
});

describe("场景 4：普通对话页发送失败（§11.4）", () => {
  it("【组件/DOM】失败后页面上直接看得见本次真实原因", async () => {
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error(REASON));
    const wrapper = mount(Composer, { attachTo: document.body });
    await wrapper.find("#composer-input").setValue("这段字发不出去");
    await wrapper.find(".send-btn").trigger("click");
    await flushPromises();

    expect(wrapper.text(), "对话页没有显示这次发送失败的真实原因").toContain("网络中断");
  });

  it("【组件/DOM】输入框已有新文字时，仍要有取回失败原文的入口且两份都保留", async () => {
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error(REASON));
    const session = useSessionStore();
    const wrapper = mount(Composer, { attachTo: document.body });
    await wrapper.find("#composer-input").setValue("失败的那份原文");
    await wrapper.find(".send-btn").trigger("click");
    await flushPromises();

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
