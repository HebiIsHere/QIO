/**
 * 收尾轮 15 的组件反例（Lead 接手 B3 范围）：
 *
 * - 普通对话页 Composer 必须显示共享聊天草稿的保存失败状态与重试入口；
 * - FailedSendNotice 的错误显示不能依赖「已经成功恢复出内容」：
 *   零条记录 + 读取失败 / 写入失败都要显示真实错误，且不虚报「已丢失 / 已恢复」。
 *
 * 标注：【组件/DOM】挂载真实 Composer.vue 与 FailedSendNotice.vue；
 *      【模拟失败】直接设置会话层的真实失败状态（等同存储不可用/读取失败）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { useSessionStore } from "../../stores/session";
import Composer from "../Composer.vue";
import FailedSendNotice from "../interactive/FailedSendNotice.vue";

vi.mock("../services/api", () => ({
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

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
});

afterEach(() => {
  localStorage.clear();
});

describe("15 普通对话页要看得见聊天草稿保存失败", () => {
  it("【组件/DOM】草稿保存失败时显示真实原因与重试入口，重试只重写草稿", async () => {
    const session = useSessionStore();
    session.draftSaveStatus = "error";
    session.draftSaveError = "本地存储被禁用";
    const retry = vi.spyOn(session, "retryDraftSave").mockResolvedValue(undefined);
    const wrapper = mount(Composer, { attachTo: document.body });
    await flushPromises();

    expect(wrapper.text(), "对话页看不到共享聊天草稿的保存失败").toContain("本地存储被禁用");
    expect(wrapper.text(), "提示要说清是「还没保存在本机」").toContain("还没保存在本机");
    const btn = wrapper.find('[data-im="composer-draft-retry"]');
    expect(btn.exists(), "对话页没有重试保存入口").toBe(true);
    await btn.trigger("click");
    expect(retry).toHaveBeenCalled();
    wrapper.unmount();
  });

  it("【组件/DOM】草稿保存正常时不显示失败提示", async () => {
    const session = useSessionStore();
    session.draftSaveStatus = "saved";
    session.draftSaveError = null;
    const wrapper = mount(Composer, { attachTo: document.body });
    await flushPromises();
    expect(wrapper.find('[data-im="composer-draft-status"]').exists()).toBe(false);
    wrapper.unmount();
  });
});

describe("15 失败原文：零条记录也要显示真实存储错误", () => {
  it("【组件/DOM】零条记录 + 写入失败：仍然显示「没能保存在本机」", async () => {
    const session = useSessionStore();
    session.failedSends = [];
    session.failedSendPersistError = "本地存储被禁用";
    const wrapper = mount(FailedSendNotice, { props: { scope: "conversation" }, attachTo: document.body });
    await flushPromises();
    expect(wrapper.text()).toContain("没能保存在本机");
    expect(wrapper.text()).toContain("本地存储被禁用");
    wrapper.unmount();
  });

  it("【组件/DOM】零条记录 + 读取失败：显示读取错误与「不代表原文已经丢失」，并提供重新读取", async () => {
    const session = useSessionStore();
    session.failedSends = [];
    session.failedSendPersistError = null;
    (session as unknown as { failedSendReadError: string | null }).failedSendReadError = "本机存储读取被拒绝";
    const retryRead = vi.fn();
    (session as unknown as { retryFailedSendRestore: () => void }).retryFailedSendRestore = retryRead;
    const wrapper = mount(FailedSendNotice, { props: { scope: "conversation" }, attachTo: document.body });
    await flushPromises();
    expect(wrapper.text(), "零条恢复出来时读取失败被吞掉").toContain("读取本机保存的失败原文时出错");
    expect(wrapper.text()).toContain("不代表原文已经丢失");
    const btn = wrapper.find('[data-im="composer-recovery-read-retry"]');
    expect(btn.exists(), "读取失败没有重新读取入口").toBe(true);
    await btn.trigger("click");
    expect(retryRead).toHaveBeenCalled();
    wrapper.unmount();
  });

  it("【组件/DOM】零条记录且没有错误：不出现恢复区，也不提「已丢失」", async () => {
    const session = useSessionStore();
    session.failedSends = [];
    session.failedSendPersistError = null;
    (session as unknown as { failedSendReadError: string | null }).failedSendReadError = null;
    const wrapper = mount(FailedSendNotice, { props: { scope: "conversation" }, attachTo: document.body });
    await flushPromises();
    expect(wrapper.find(".failed-send").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("丢失");
    wrapper.unmount();
  });
});
