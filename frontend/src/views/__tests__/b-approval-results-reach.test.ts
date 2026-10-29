/**
 * B / F05：收起态也能从 HUD「任务」进入「审批结果与历史」（与 A 的只读面板集成）。
 *
 * E 的基线与中间态结论：互动页收起对话后，输入区五图标入口行不可见；
 * 若审批结果只放在 ComposerEntries，收起态就 0 入口（违反「两个聊天页、0 待办仍可达、常驻可达」）。
 * 这里的验收口径：
 * - 对话收起时 HUD「任务」入口常驻可点，打开任务面板后切到「审批结果与历史」分区；
 * - 该分区挂的是 A 的自包含只读面板（零 props），真实读取审批历史（mock 服务），不复制面板逻辑；
 * - 审批结果与任务操作分区展示：切到结果分区时任务/待确认操作不再混在同一列；
 * - 面板关闭后再次打开回到任务分区（不做跨会话记忆）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import InteractiveView from "../../views/InteractiveView.vue";
import { resetServices, stubServices } from "./acceptance-i2v2-page-harness";
import { useInteractiveStore } from "../../stores/interactive";
import * as lifecycle from "../../services/approvalLifecycle";

const STUBS = {
  BoardCanvas: { template: '<div data-stub="canvas"><slot name="overlay" :scale="1" :width="1200" :height="800" /></div>' },
  BoardToolbar: { template: '<div data-stub="toolbar" />' },
  ConversationModeSwitch: { template: '<div data-stub="mode-switch" />' },
  MessageStream: { template: '<div data-stub="stream" />' },
  Composer: { template: '<div data-stub="composer" />' },
} as const;

let wrapper: VueWrapper | null = null;

function mountPage(): VueWrapper {
  const pinia = createPinia();
  setActivePinia(pinia);
  wrapper = mount(InteractiveView, {
    attachTo: document.body,
    global: { plugins: [pinia], stubs: STUBS as never },
  }) as unknown as VueWrapper;
  return wrapper;
}

async function settle(): Promise<void> {
  await flushPromises();
  await wrapper!.vm.$nextTick();
  await flushPromises();
}

beforeEach(() => {
  resetServices();
  vi.spyOn(lifecycle, "getApprovalHistory").mockResolvedValue([
    { approval_id: "appr_hist_a", outcome: "executed", summary: "已执行：写入 2 个文件" },
    { approval_id: "appr_hist_b", outcome: "failed", summary: "执行失败：目标文件被占用" },
  ] as never);
  vi.spyOn(lifecycle, "getPendingApprovals").mockResolvedValue([] as never);
  localStorage.clear();
  document.body.innerHTML = "";
});

afterEach(() => {
  try {
    wrapper?.unmount();
  } catch {
    /* 已经卸载 */
  }
  wrapper = null;
  vi.restoreAllMocks();
  resetServices();
  document.body.innerHTML = "";
});

describe("收起态：HUD 任务 → 审批结果与历史", () => {
  it("对话收起时任务入口常驻；切到「审批结果与历史」能看到真实历史，且与任务操作分开", async () => {
    stubServices();
    mountPage();
    await settle();
    const store = useInteractiveStore();
    expect(store.chatOpen, "前提：对话处于收起态").toBe(false);
    expect(store.tasksOpen).toBe(false);

    const entry = wrapper!.find('[data-im="tasks-entry"]');
    expect(entry.exists(), "收起态没有 HUD 任务入口（审批结果就没有常驻可达路径）").toBe(true);
    await entry.trigger("click");
    await settle();
    expect(store.tasksOpen).toBe(true);
    expect(wrapper!.find('[data-im="task-center"]').exists()).toBe(true);
    // 默认是任务分区
    expect(wrapper!.find('[data-im="task-tab-tasks"]').attributes("aria-selected")).toBe("true");
    expect(wrapper!.find('[data-im="task-approval-results"]').exists()).toBe(false);

    await wrapper!.find('[data-im="task-tab-approval-results"]').trigger("click");
    await settle();
    const results = wrapper!.find('[data-im="task-approval-results"]');
    expect(results.exists(), "切到审批结果分区后面板没有渲染").toBe(true);
    expect(results.find('[data-test="approval-results-panel"]').exists(), "没有挂 A 的只读审批结果面板").toBe(true);
    expect(results.text()).toContain("已执行：写入 2 个文件");
    expect(results.text()).toContain("执行失败：目标文件被占用");
    // 分区隔离：任务 / 待确认操作不在结果分区里
    expect(wrapper!.find('[data-im="task-pending"]').exists()).toBe(false);
    expect(wrapper!.find('[data-im="task-batch-approve"]').exists()).toBe(false);
    expect(wrapper!.find('[data-im="task-tab-approval-results"]').attributes("aria-selected")).toBe("true");
  });

  it("关闭再打开回到任务分区（分区状态不跨会话记忆，也不隐藏任务入口）", async () => {
    stubServices();
    mountPage();
    await settle();
    await wrapper!.find('[data-im="tasks-entry"]').trigger("click");
    await settle();
    await wrapper!.find('[data-im="task-tab-approval-results"]').trigger("click");
    await settle();
    expect(wrapper!.find('[data-im="task-approval-results"]').exists()).toBe(true);

    await wrapper!.find('[data-im="task-close"]').trigger("click");
    await settle();
    expect(wrapper!.find('[data-im="task-center"]').exists()).toBe(false);

    await wrapper!.find('[data-im="tasks-entry"]').trigger("click");
    await settle();
    expect(wrapper!.find('[data-im="task-tab-tasks"]').attributes("aria-selected")).toBe("true");
    expect(wrapper!.find('[data-im="task-approval-results"]').exists()).toBe(false);
  });

  it("历史读取失败时结果分区如实说明并可重试（不当作没有历史）", async () => {
    vi.restoreAllMocks();
    vi.spyOn(lifecycle, "getPendingApprovals").mockResolvedValue([] as never);
    vi.spyOn(lifecycle, "getApprovalHistory").mockRejectedValue(new Error("HTTP 500：服务端内部错误"));
    stubServices();
    mountPage();
    await settle();
    await wrapper!.find('[data-im="tasks-entry"]').trigger("click");
    await settle();
    await wrapper!.find('[data-im="task-tab-approval-results"]').trigger("click");
    await settle();
    const results = wrapper!.find('[data-im="task-approval-results"]');
    expect(results.text()).toContain("读取审批历史失败");
    expect(results.find('[data-test="approval-history-retry"]').exists()).toBe(true);
    expect(results.find('[data-test="approval-results-empty"]').exists()).toBe(false);
  });
});
