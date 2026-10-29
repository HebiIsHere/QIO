/**
 * 顶部短通知组件：点击定位任务 / 激活动作、暂停恢复、不抢焦点、完整读屏文案、只关本次。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import TopNotice from "../TopNotice.vue";
import {
  NOTICE_VISIBLE_MS,
  useNotificationsStore,
} from "../../../stores/notifications";
import { useConversationTasksStore } from "../../../stores/conversationTasks";
import { useConversationShellStore } from "../../../stores/conversationShell";

// 跨页通知导航由 C 的 services/noticeNavigation 提供（A 只锁调用契约，不测其内部实现）。
const navigation = vi.hoisted(() => ({ openTaskNoticeTargets: vi.fn() }));
vi.mock("../../../services/noticeNavigation", () => ({
  openTaskNoticeTargets: navigation.openTaskNoticeTargets,
}));

function setup() {
  setActivePinia(createPinia());
  return {
    notifications: useNotificationsStore(),
    tasks: useConversationTasksStore(),
    shell: useConversationShellStore(),
  };
}

const LONG_TITLE = "一个特别特别长的后台任务名称用来验证可见省略与完整读屏文案";

describe("TopNotice：一条、可定位、暂停恢复、不抢焦点", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
  });

  it("任务失败通知：点击交给跨页导航定位整组任务，只关本次通知", async () => {
    const { notifications } = setup();
    navigation.openTaskNoticeTargets.mockReset();
    navigation.openTaskNoticeTargets.mockResolvedValue({
      ok: true,
      reason: "ok",
      taskIds: ["subagent:a", "subagent:b"],
      message: "",
    });
    notifications.enqueue({
      key: "task-failed:a",
      kind: "task_failed",
      text: "「" + LONG_TITLE + "」失败了",
      taskIds: ["subagent:a", "subagent:b"],
      mergeable: false,
      focus: true,
    });
    const w = mount(TopNotice, { attachTo: document.body });

    const body = w.find(".uf-notice__body");
    expect(body.element.tagName).toBe("BUTTON");
    // 完整读屏文案（可见文本会省略，但 aria 永远完整）
    expect(body.attributes("aria-label")).toContain(LONG_TITLE);
    expect(body.attributes("aria-label")).toContain("定位");

    await body.trigger("click");
    await flushPromises();
    // 交由 C 的导航服务一次处理整组任务（不再自己展开左栏 / 逐条 focusTask）
    expect(navigation.openTaskNoticeTargets).toHaveBeenCalledTimes(1);
    expect(navigation.openTaskNoticeTargets.mock.calls[0][0]).toEqual([
      "subagent:a",
      "subagent:b",
    ]);
    expect(notifications.hasActive).toBe(false);
    w.unmount();
  });

  it("带激活动作的通知（初始设置未完成）：点击执行回调并关闭本条", async () => {
    const { notifications } = setup();
    const onActivate = vi.fn();
    notifications.enqueue({
      key: "setup-incomplete:v1",
      kind: "setup_incomplete",
      text: "初始设置尚未完成",
      onActivate,
    });
    const w = mount(TopNotice, { attachTo: document.body });
    expect(w.text()).toContain("初始设置尚未完成");
    // setup_incomplete 不合并：只有一条、文案不被改写
    expect(notifications.notices).toHaveLength(1);
    expect(notifications.notices[0].text).toBe("初始设置尚未完成");

    await w.find(".uf-notice__body").trigger("click");
    expect(onActivate).toHaveBeenCalledTimes(1);
    expect(notifications.hasActive).toBe(false);
    w.unmount();
  });

  it("悬停暂停、移开恢复剩余时长；5s 到点自动收起", async () => {
    const { notifications } = setup();
    notifications.enqueue({
      key: "task-failed:p",
      kind: "task_failed",
      text: "「任务」失败了",
      taskIds: ["subagent:p"],
      mergeable: false,
      focus: true,
    });
    const w = mount(TopNotice, { attachTo: document.body });
    const root = w.find(".uf-notice");
    await root.trigger("mouseenter");
    expect(notifications.pausedKeys).toContain("task-failed:p");

    vi.advanceTimersByTime(NOTICE_VISIBLE_MS - 1000);
    expect(notifications.hasActive).toBe(true);

    // 暂停期间倒计时冻结：剩余时长是「暂停那一刻的剩余」
    await root.trigger("mouseleave");
    vi.advanceTimersByTime(NOTICE_VISIBLE_MS - 200);
    expect(notifications.hasActive).toBe(true);
    vi.advanceTimersByTime(300);
    expect(notifications.hasActive).toBe(false);
    w.unmount();
  });

  it("出现时不抢焦点；关闭按钮只关本次通知", async () => {
    const { notifications } = setup();
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    notifications.enqueue({
      key: "task-failed:focus",
      kind: "task_failed",
      text: "「任务」失败了",
      taskIds: ["subagent:focus"],
      mergeable: false,
      focus: true,
    });
    const w = mount(TopNotice, { attachTo: document.body });
    expect(document.activeElement).toBe(input);

    await w.find(".uf-notice__close").trigger("click");
    expect(notifications.hasActive).toBe(false);
    expect(document.activeElement).toBe(input);
    w.unmount();
  });
});