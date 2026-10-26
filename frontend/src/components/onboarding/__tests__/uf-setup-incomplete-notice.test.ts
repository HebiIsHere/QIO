/**
 * D · 初始设置未完成的一次性轻提醒（SetupIncompleteNotice）。
 *
 * 锁定的行为：
 * - 只在「未完成 + 已允许进主页（向导没开着）」时入队一次；
 * - 「已显示过」只在通知**真的开始显示**（activeKey 等于本条 key）时才持久化；
 *   排队未显示不算（中央弹窗期间排队）；
 * - 点击激活 → 接续引导 onboarding.reopen()；
 * - 第二次启动不再自动出现（localStorage 标记）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import SetupIncompleteNotice, {
  SETUP_INCOMPLETE_NOTICE_KEY,
  SETUP_INCOMPLETE_SHOWN_KEY,
} from "../SetupIncompleteNotice.vue";
import { useOnboardingStore } from "../../../stores/onboarding";
import { useNotificationsStore, noticeActivation } from "../../../stores/notifications";
import { useModalArbiterStore } from "../../../stores/modalArbiter";
import { api } from "../../../services/api";

const baseStatus = {
  done: false,
  has_credential: false,
  has_name: false,
  has_content: true,
  wizard_seen: true,
  welcome_version: "0.1.6",
  app_version: "0.1.6",
  show_wizard: false,
  hint_dismissed: true,
};

function stubStatus(overrides: Record<string, unknown> = {}) {
  vi.spyOn(api, "getOnboardingStatus").mockResolvedValue({ ...baseStatus, ...overrides } as never);
}

async function mountNotice() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mount(SetupIncompleteNotice, { global: { plugins: [pinia] } });
  return { w, pinia };
}

beforeEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("初始设置未完成轻提醒", () => {
  it("未完成且已允许进主页：入队并在真正显示时写「已显示过」", async () => {
    stubStatus();
    const { w } = await mountNotice();
    const onboarding = useOnboardingStore();
    const notifications = useNotificationsStore();
    await onboarding.load();
    await flushPromises();

    expect(notifications.activeKey).toBe(SETUP_INCOMPLETE_NOTICE_KEY);
    expect(notifications.active?.kind).toBe("setup_incomplete");
    expect(notifications.active?.text).toContain("初始设置尚未完成");
    expect(localStorage.getItem(SETUP_INCOMPLETE_SHOWN_KEY)).toBe("1");
    w.unmount();
  });

  it("向导正开着时不打扰（不出一次性提醒）", async () => {
    stubStatus({ show_wizard: true });
    const { w } = await mountNotice();
    const onboarding = useOnboardingStore();
    const notifications = useNotificationsStore();
    await onboarding.load();
    await flushPromises();
    expect(onboarding.showWizard).toBe(true);
    expect(notifications.activeKey).toBe(null);
    expect(localStorage.getItem(SETUP_INCOMPLETE_SHOWN_KEY)).toBe(null);
    w.unmount();
  });

  it("中央弹窗期间排队：排队未显示不算已提醒；弹窗关闭后才显示并持久化", async () => {
    stubStatus();
    const { w } = await mountNotice();
    const onboarding = useOnboardingStore();
    const notifications = useNotificationsStore();
    const arbiter = useModalArbiterStore();
    arbiter.open({ id: "other-modal", kind: "confirm", dismissible: true });

    await onboarding.load();
    await flushPromises();
    expect(notifications.activeKey).toBe(null);
    expect(localStorage.getItem(SETUP_INCOMPLETE_SHOWN_KEY)).toBe(null);

    arbiter.close("other-modal");
    notifications.flushQueue();
    await flushPromises();
    expect(notifications.activeKey).toBe(SETUP_INCOMPLETE_NOTICE_KEY);
    expect(localStorage.getItem(SETUP_INCOMPLETE_SHOWN_KEY)).toBe("1");
    w.unmount();
  });

  it("点击激活：接续引导（onboarding.reopen），并关闭本次通知", async () => {
    stubStatus();
    const { w } = await mountNotice();
    const onboarding = useOnboardingStore();
    const notifications = useNotificationsStore();
    await onboarding.load();
    await flushPromises();

    const activate = noticeActivation(notifications.active);
    expect(activate).toBeTypeOf("function");
    activate?.();
    await flushPromises();
    expect(onboarding.showWizard).toBe(true);
    expect(notifications.activeKey).not.toBe(SETUP_INCOMPLETE_NOTICE_KEY);
    w.unmount();
  });

  it("已经提醒过一次：下次启动不再自动出现", async () => {
    localStorage.setItem(SETUP_INCOMPLETE_SHOWN_KEY, "1");
    stubStatus();
    const { w } = await mountNotice();
    const onboarding = useOnboardingStore();
    const notifications = useNotificationsStore();
    await onboarding.load();
    await flushPromises();
    expect(notifications.activeKey).toBe(null);
    w.unmount();
  });

  it("排队期间设置已完成：显示前校验为 false，不显示也不写已提醒", async () => {
    stubStatus();
    const { w } = await mountNotice();
    const onboarding = useOnboardingStore();
    const notifications = useNotificationsStore();
    const arbiter = useModalArbiterStore();
    arbiter.open({ id: "other-modal", kind: "confirm", dismissible: true });
    await onboarding.load();
    await flushPromises();
    expect(localStorage.getItem(SETUP_INCOMPLETE_SHOWN_KEY)).toBe(null);

    // 用户在其他地方完成了设置
    vi.spyOn(api, "getOnboardingStatus").mockResolvedValue({
      ...baseStatus,
      done: true,
      show_wizard: false,
    } as never);
    await onboarding.load();
    arbiter.close("other-modal");
    notifications.flushQueue();
    await flushPromises();
    expect(notifications.activeKey).toBe(null);
    expect(localStorage.getItem(SETUP_INCOMPLETE_SHOWN_KEY)).toBe(null);
    w.unmount();
  });
});
