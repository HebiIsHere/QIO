/**
 * A · TopNotice 组件层：hover/focus 分别暂停（F06）、成员过滤后显示文案与读屏文案跟随剩余成员（F03）。
 *
 * 修复前失败根因：组件只按「进入/离开」各调一次无 reason 的 pause/resume，
 * 鼠标离开就恢复（键盘仍在通知内）；文案从整条通知的最后一个成员文本取。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import TopNotice from "../TopNotice.vue";
import {
  NOTICE_MERGE_WINDOW_MS,
  noticeDisplayText,
  useNotificationsStore,
} from "../../../stores/notifications";

function setup() {
  setActivePinia(createPinia());
  return { notifications: useNotificationsStore() };
}

/** 派发带 relatedTarget 的 focusout（jsdom 的 Event 上直接定义属性，等价真实 FocusEvent）。 */
function dispatchFocusOut(target: Element, related: Node | null): void {
  const event = new Event("focusout", { bubbles: true });
  Object.defineProperty(event, "relatedTarget", { value: related });
  target.dispatchEvent(event);
}

function showNotice(
  notifications: ReturnType<typeof useNotificationsStore>,
  key = "task-failed:p",
) {
  notifications.enqueue({
    key,
    kind: "task_failed",
    text: "「任务」失败了",
    taskIds: ["subagent:p"],
    mergeable: false,
    focus: true,
  });
}

describe("F06 hover 与 focus 暂停原因分开（组件层）", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
  });

  it("鼠标离开但键盘焦点仍在通知内：不恢复倒计时；焦点真正离开才继续", async () => {
    const { notifications } = setup();
    showNotice(notifications);
    const key = notifications.activeKey!;
    const w = mount(TopNotice, { attachTo: document.body });
    const root = w.find(".uf-notice");

    await root.trigger("mouseenter");
    await root.trigger("focusin");
    expect(notifications.pausedKeys).toContain(key);

    // 鼠标离开：键盘焦点仍在这一条通知里
    await root.trigger("mouseleave");
    expect(notifications.pausedKeys).toContain(key);
    vi.advanceTimersByTime(4000);
    expect(notifications.hasActive).toBe(true);

    // 焦点在内部按钮之间移动：不解除聚焦暂停
    dispatchFocusOut(root.element, w.find(".uf-notice__close").element);
    expect(notifications.pausedKeys).toContain(key);
    vi.advanceTimersByTime(4000);
    expect(notifications.hasActive).toBe(true);

    // 焦点真正离开通知：按剩余时长继续
    dispatchFocusOut(root.element, document.body);
    expect(notifications.pausedKeys).not.toContain(key);
    vi.advanceTimersByTime(4900);
    expect(notifications.hasActive).toBe(true);
    vi.advanceTimersByTime(200);
    expect(notifications.hasActive).toBe(false);
    w.unmount();
  });

  it("焦点离开时鼠标仍在通知上：继续暂停", async () => {
    const { notifications } = setup();
    showNotice(notifications);
    const key = notifications.activeKey!;
    const w = mount(TopNotice, { attachTo: document.body });
    const root = w.find(".uf-notice");

    await root.trigger("mouseenter");
    await root.trigger("focusin");
    dispatchFocusOut(root.element, document.body);
    expect(notifications.pausedKeys).toContain(key);
    vi.advanceTimersByTime(6000);
    expect(notifications.hasActive).toBe(true);

    await root.trigger("mouseleave");
    vi.advanceTimersByTime(4900);
    expect(notifications.hasActive).toBe(true);
    vi.advanceTimersByTime(200);
    expect(notifications.hasActive).toBe(false);
    w.unmount();
  });

  it("关闭按钮只关本次并清理暂停原因；组件卸载也不留下暂停原因", async () => {
    const { notifications } = setup();
    showNotice(notifications);
    const key = notifications.activeKey!;
    const w = mount(TopNotice, { attachTo: document.body });
    await w.find(".uf-notice").trigger("mouseenter");
    expect(notifications.pausedKeys).toContain(key);
    await w.find(".uf-notice__close").trigger("click");
    expect(notifications.hasActive).toBe(false);
    expect(notifications.pausedKeys).not.toContain(key);
    w.unmount();

    // 卸载场景：暂停中的通知在组件消失后不能永远挂着（新事件身份，旧的一条已收口）
    showNotice(notifications, "task-failed:unmount");
    const second = mount(TopNotice, { attachTo: document.body });
    await second.find(".uf-notice").trigger("mouseenter");
    expect(notifications.pausedKeys).toContain(notifications.activeKey);
    second.unmount();
    expect(notifications.pausedKeys).not.toContain(notifications.activeKey);
  });
});

describe("F03 显示文案/读屏文案跟随剩余成员（组件层）", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
  });

  it("合并通知中 B 在显示前恢复：可见文案与 aria-label 都是 A，不再出现 B", () => {
    const { notifications } = setup();
    let recoveredB = false;
    notifications.enqueue({
      key: "ev-a",
      kind: "task_failed",
      text: "「整理资料」失败了",
      taskIds: ["subagent:a"],
      mergeable: true,
      focus: true,
    });
    notifications.enqueue({
      key: "ev-b",
      kind: "task_failed",
      text: "「编译报告」失败了",
      taskIds: ["subagent:b"],
      mergeable: true,
      focus: true,
      stillRelevant: () => !recoveredB,
    });
    recoveredB = true;
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);
    expect(noticeDisplayText(notifications.active)).toBe("「整理资料」失败了");

    const w = mount(TopNotice, { attachTo: document.body });
    expect(w.find(".uf-notice__line").text()).toBe("「整理资料」失败了");
    const aria = w.find(".uf-notice__body").attributes("aria-label") ?? "";
    expect(aria).toContain("整理资料");
    expect(aria).not.toContain("编译报告");
    expect(w.text()).not.toContain("编译报告");
    w.unmount();
  });

  it("两条真实失败合并：可见「2 项任务失败了」，读屏文案同数量", async () => {
    const { notifications } = setup();
    notifications.enqueue({
      key: "ev-a", kind: "task_failed", text: "「整理资料」失败了",
      taskIds: ["subagent:a"], mergeable: true, focus: true,
    });
    notifications.enqueue({
      key: "ev-b", kind: "task_failed", text: "「编译报告」失败了",
      taskIds: ["subagent:b"], mergeable: true, focus: true,
    });
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);
    const w = mount(TopNotice, { attachTo: document.body });
    expect(w.find(".uf-notice__line").text()).toBe("2 项任务失败了");
    const label = w.find(".uf-notice__body").attributes("aria-label") ?? "";
    expect(label).toContain("2 项任务失败了");
    w.unmount();
  });
});
