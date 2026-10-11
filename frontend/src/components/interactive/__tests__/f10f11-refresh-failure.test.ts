/**
 * 【审计 8fa8452 · F10 / F11 界面】成功提交被刷新失败改判、任务清单读取失败被显示成消失。
 *
 * 这两条缺陷都必须**在界面上**看得见、且给出正确的下一步：
 * - F10：提交已成功、页面刷新失败 → 说「提交成功，页面刷新失败」+ 只触发读取的「重试刷新」；
 *   没有回执 → 说「提交结果未知」，不诱导直接重复提交。
 * - F11：清单刷新失败 → 仍然列出最后一次已知任务（不是「任务消失」），标明刷新失败并给重试。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import SubmitCluster from "../SubmitCluster.vue";
import IntentStatusPopover from "../IntentStatusPopover.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import * as board from "../../../interactive/board";
import type { Intent } from "../../../interactive/types";

vi.mock("../../../services/interactive", () => ({
  fetchBoardState: vi.fn(async () => ({ pending: { expressions: [] } })),
  fetchVisibleRange: vi.fn(async () => ({ visibleRange: null })),
  fetchIntents: vi.fn(),
  saveDrafts: vi.fn(),
  saveBoardState: vi.fn(),
  submitBoard: vi.fn(),
  checkMaterialImpact: vi.fn(),
}));

const mounted: VueWrapper[] = [];

function track(wrapper: VueWrapper): VueWrapper {
  mounted.push(wrapper);
  return wrapper;
}

function runningIntent(): Intent {
  return {
    id: "intent_1",
    boardId: "board_t",
    submissionId: null,
    title: "整理两份材料",
    summary: "",
    status: "running",
    preview: { cards: [], groups: [], links: [] },
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 0, text: "" },
    reason: "",
    demo: true,
    createdAt: "t",
    updatedAt: "t",
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  localStorage.clear();
});

afterEach(() => {
  while (mounted.length) mounted.pop()?.unmount();
  document.body.innerHTML = "";
  vi.clearAllMocks();
});

describe("F10 提交区：提交成功、页面刷新失败", () => {
  it("默认区说清「提交成功，页面刷新失败」，并提供只触发读取的重试", async () => {
    const store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    store.submitStatus = "succeeded";
    store.submitRefreshState = "error";
    store.submitRefreshError = "网络中断：读取板面失败";
    const retry = vi.spyOn(store, "retrySubmitRefresh").mockResolvedValue({ ok: true });
    const wrapper = track(mount(SubmitCluster));

    const notice = wrapper.find('[data-im="submit-refresh-failed"]');
    expect(notice.exists(), "提交成功、刷新失败时必须有专门说明（F10）").toBe(true);
    expect(notice.text()).toContain("提交成功，页面刷新失败");
    expect(notice.text(), "真实原因要可见").toContain("网络中断");

    const button = wrapper.find('[data-im="submit-refresh-retry"]');
    expect(button.exists()).toBe(true);
    expect(button.text()).toContain("重试刷新");
    await button.trigger("click");
    expect(retry, "重试刷新必须走 store.retrySubmitRefresh（只读取）").toHaveBeenCalledTimes(1);
    retry.mockRestore();
  });

  it("没有回执时单独说明「提交结果未知」，不把它显示成提交失败", async () => {
    const store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    store.submitStatus = "unknown";
    store.submitResultUnknown = true;
    store.submitError = "提交结果未知：没有拿到服务端回执";
    const retry = vi.spyOn(store, "retrySubmitRefresh").mockResolvedValue({ ok: true });
    const submitAttempt = vi.spyOn(store, "submit").mockResolvedValue(null);
    const wrapper = track(mount(SubmitCluster));

    expect(wrapper.find('[data-im="submit-failure"]').exists(), "未知不是失败").toBe(false);
    const unknown = wrapper.find('[data-im="submit-unknown"]');
    expect(unknown.exists()).toBe(true);
    expect(unknown.text()).toContain("提交结果未知");
    expect(unknown.text(), "文案不许暗示用户再提交一次").toContain("不要直接重复提交");
    expect(wrapper.find('[data-im="submit-status"]').text()).toContain("提交结果未知");

    // F10 要求：未知时也要有**只触发读取**的查证入口，而不是只有一个提示
    const check = wrapper.find('[data-im="submit-unknown-refresh"]');
    expect(check.exists(), "提交结果未知时缺少只读的查证 / 刷新入口（F10）").toBe(true);
    await check.trigger("click");
    expect(retry, "未知状态的查证入口必须只触发读取").toHaveBeenCalledTimes(1);
    expect(submitAttempt, "未知状态绝不允许自动重复提交").not.toHaveBeenCalled();
    retry.mockRestore();
    submitAttempt.mockRestore();
  });
});

describe("F11 任务浮层：清单读取失败", () => {
  it("读取失败时保留已知任务、标明刷新失败并给重试入口", async () => {
    const store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    store.tasksOpen = true;
    store.intents = [runningIntent()];
    store.intentsLoadState = "error";
    store.intentsLoadError = "网络断了";
    const retry = vi.spyOn(store, "loadIntents").mockResolvedValue(true);
    const wrapper = track(mount(IntentStatusPopover, { attachTo: document.body }));
    await wrapper.vm.$nextTick();

    const notice = wrapper.find('[data-im="tasks-refresh-failed"]');
    expect(notice.exists(), "读取失败必须在任务浮层里可见（F11）").toBe(true);
    expect(notice.text()).toContain("网络断了");
    expect(wrapper.findAll('[data-im="task-item"]').length, "已知任务不许因为读取失败而消失").toBe(1);

    const button = wrapper.find('[data-im="tasks-refresh-retry"]');
    expect(button.exists()).toBe(true);
    await button.trigger("click");
    expect(retry, "重试必须真的重新读取任务清单").toHaveBeenCalled();
    retry.mockRestore();
  });

  it("读取失败导致清单为空时，明确说「这里不是没有任务」", async () => {
    const store = useInteractiveStore();
    store.board = board.emptyState("board_t");
    store.tasksOpen = true;
    store.intents = [];
    store.intentsLoadState = "error";
    store.intentsLoadError = "网络断了";
    const wrapper = track(mount(IntentStatusPopover, { attachTo: document.body }));
    await wrapper.vm.$nextTick();
    expect(wrapper.text()).toContain("不是「没有任务」");
  });
});
