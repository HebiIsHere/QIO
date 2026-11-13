/**
 * D 组定向用例（问题 16）：执行记录是阻挡背景的浮层，必须有完整键盘操作。
 *
 * 验证：打开后焦点移入层内；Tab 限制在层内（不跑到后面页面）；
 * Esc 走共用 Esc 层级栈按当前层关闭且只关一次；关闭后焦点回原入口，
 * 入口已不存在时退回仍在的可见入口；背景板面的键盘/点击不会被误触。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import ExecutionRecordPanel from "../ExecutionRecordPanel.vue";
import { registerEscapeLayer } from "../../../composables/useEscapeLayer";
import type { IntentView } from "../../../interactive/intentWorkbench";

const mocks = vi.hoisted(() => ({
  fetchLedger: vi.fn(),
  fetchIntentResults: vi.fn(),
}));

vi.mock("../../../services/interactive", () => ({
  fetchLedger: mocks.fetchLedger,
  fetchIntentResults: mocks.fetchIntentResults,
}));

const intent: IntentView = {
  intent_id: "intent_1",
  board_id: "board_1",
  version: 3,
  status: "failed",
  title: "整理方案对比",
  goal: "把两份方案放到组里",
  retry: {
    series_id: "series_1",
    intent_id: "intent_1",
    status: "stopped",
    attempts: 1,
    limit: 3,
    last_error: "timeout",
    next_action: "manual",
  },
};

function mountPanel() {
  return mount(ExecutionRecordPanel, {
    props: { intent, boardId: "board_1", open: true },
    attachTo: document.body,
  });
}

function pressKey(target: EventTarget, key: string, init: KeyboardEventInit = {}): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    key,
    bubbles: true,
    cancelable: true,
    ...init,
  });
  target.dispatchEvent(event);
  return event;
}

beforeEach(() => {
  mocks.fetchLedger.mockReset();
  mocks.fetchIntentResults.mockReset();
  mocks.fetchLedger.mockResolvedValue({
    available: true,
    operations: [
      { operation_id: "op_1", intent_id: "intent_1", action: "board_update_card", status: "failed" },
    ],
  });
  mocks.fetchIntentResults.mockResolvedValue({
    available: true,
    results: [{ result_id: "res_1", intent_id: "intent_1", title: "对比结果", content: "正文" }],
  });
});

afterEach(() => {
  document.body.innerHTML = "";
});

describe("执行记录浮层的键盘完整操作", () => {
  it("打开后焦点移入层内；Tab/Shift+Tab 留在层内；Esc 只发一次关闭", async () => {
    const trigger = document.createElement("button");
    trigger.textContent = "打开执行记录";
    document.body.appendChild(trigger);
    trigger.focus();
    expect(document.activeElement).toBe(trigger);

    const wrapper = mountPanel();
    await flushPromises();
    await flushPromises();

    const panel = wrapper.get('[data-im="execution-record"]').element as HTMLElement;
    expect(panel.contains(document.activeElement)).toBe(true);
    // 面板容器自己先接住焦点（读屏会念出对话框名称），不是把焦点留在后面页面。
    expect(document.activeElement).toBe(panel);

    const focusables = [...panel.querySelectorAll<HTMLElement>("button:not([disabled])")];
    expect(focusables.length).toBeGreaterThan(0);
    const last = focusables[focusables.length - 1];
    last.focus();
    const tabEvent = pressKey(last, "Tab");
    expect(tabEvent.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(focusables[0]);
    const shiftTab = pressKey(focusables[0], "Tab", { shiftKey: true });
    expect(shiftTab.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(last);

    pressKey(document.body, "Escape");
    pressKey(document.body, "Escape");
    expect(wrapper.emitted("close")).toHaveLength(1);

    // 父级真正关闭：焦点回到打开前的入口。
    await wrapper.setProps({ open: false });
    await flushPromises();
    expect(document.activeElement).toBe(trigger);
    wrapper.unmount();
  });

  it("背景页面不再收到 Delete / Ctrl+Z 这类板面快捷键", async () => {
    const background = vi.fn();
    window.addEventListener("keydown", background);
    const trigger = document.createElement("button");
    document.body.appendChild(trigger);
    trigger.focus();
    const wrapper = mountPanel();
    await flushPromises();
    await flushPromises();

    const closeButton = wrapper.get('[data-im="execution-record-close"]').element as HTMLElement;
    closeButton.focus();
    const del = pressKey(closeButton, "Delete");
    expect(del.defaultPrevented).toBe(true);
    expect(background).not.toHaveBeenCalled();
    const undo = pressKey(closeButton, "z", { ctrlKey: true });
    expect(undo.defaultPrevented).toBe(true);
    expect(background).not.toHaveBeenCalled();
    // 普通字母键不拦截（否则会像把整层键盘吞掉）
    pressKey(closeButton, "a");
    expect(background).toHaveBeenCalledTimes(1);

    window.removeEventListener("keydown", background);
    await wrapper.setProps({ open: false });
    wrapper.unmount();
  });

  it("背景遮罩挡住点击：点遮罩只关闭本层，不会落到后面的板面", async () => {
    const wrapper = mountPanel();
    await flushPromises();
    const scrim = wrapper.find('[data-im="execution-record-scrim"]');
    expect(scrim.exists()).toBe(true);
    await scrim.trigger("click");
    expect(wrapper.emitted("close")).toHaveLength(1);
    wrapper.unmount();
  });

  it("嵌套控件先关内层：后注册的内层处理 Esc 时执行记录不关闭", async () => {
    const wrapper = mountPanel();
    await flushPromises();
    const inner = vi.fn(() => true);
    const dispose = registerEscapeLayer({ id: "d-test-inner-layer", onEscape: inner });
    pressKey(document.body, "Escape");
    expect(inner).toHaveBeenCalledTimes(1);
    expect(wrapper.emitted("close") ?? []).toHaveLength(0);
    dispose();
    pressKey(document.body, "Escape");
    expect(wrapper.emitted("close")).toHaveLength(1);
    wrapper.unmount();
  });

  it("标题栏（含关闭按钮）固定在浮层顶部，正文在独立滚动容器里（#91）", async () => {
    const wrapper = mountPanel();
    await flushPromises();
    const panel = wrapper.get('[data-im="execution-record"]');
    const head = wrapper.get(".erp-head");
    const body = wrapper.get('[data-im="execution-record-body"]');

    // 标题栏是浮层的第一个直接子节点：滚动正文时它不会跟着滚走。
    expect(panel.element.firstElementChild).toBe(head.element);
    expect(head.element.parentElement).toBe(panel.element);
    const closeButton = wrapper.get('[data-im="execution-record-close"]');
    expect(head.element.contains(closeButton.element)).toBe(true);
    expect(body.element.contains(closeButton.element)).toBe(false);

    // 正文（含所有分区与诊断）都在自己的滚动容器里，与标题栏是兄弟节点。
    expect(body.element.parentElement).toBe(panel.element);
    expect(body.element.contains(wrapper.get(".erp-title").element)).toBe(true);
    expect(body.element.contains(wrapper.get('[data-im="execution-record-basis"]').element)).toBe(true);
    expect(body.element.contains(wrapper.get('[data-im="execution-record-diagnostics"]').element)).toBe(true);
    expect(head.element.contains(wrapper.get('[data-im="execution-record-basis"]').element)).toBe(false);
    wrapper.unmount();
  });

  it("打开它的入口已被移除时，关闭后焦点退回仍在的可见入口", async () => {
    const tasksEntry = document.createElement("button");
    tasksEntry.setAttribute("data-im", "tasks-entry");
    tasksEntry.textContent = "任务";
    document.body.appendChild(tasksEntry);
    const trigger = document.createElement("button");
    trigger.textContent = "执行记录";
    document.body.appendChild(trigger);
    trigger.focus();

    const wrapper = mountPanel();
    await flushPromises();
    await flushPromises();
    trigger.remove();
    await wrapper.setProps({ open: false });
    await flushPromises();
    expect(document.activeElement).toBe(tasksEntry);
    wrapper.unmount();
  });
});