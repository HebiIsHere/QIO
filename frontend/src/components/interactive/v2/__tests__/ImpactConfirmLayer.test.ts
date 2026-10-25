/**
 * 旧 ImpactConfirmDialog 的有效能力迁移到 v2/ImpactConfirmLayer —— 定向行为测试。
 *
 * 迁移清单（COMMON §5 B-CP1）：
 * - pendingDecision：每项决定的状态（原因 / 影响）在真实页面上可见；
 * - 剩余撤回 pendingReverts / dismissedReverts：失败或取消后未处理的项自动提示，结束提示后仍可「再次查看」；
 * - 明确 decisionIds：只提交这次界面展示的项，不顺带处理没看到的项；
 * - 关闭不撤回：取消只结束提示，不执行任何撤回；
 * - 失败重试：服务端真实原因可见、按钮恢复可点；
 * - 部分完成反馈：处理完展示项但仍有剩余时说清楚还剩多少；
 * - 与统一审批共用一个阻塞面：审批弹窗打开时让位。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import ImpactConfirmLayer from "../ImpactConfirmLayer.vue";
import { useInteractiveStore } from "../../../../stores/interactive";
import { useUnifiedApprovalsStore } from "../../../../stores/unifiedApprovals";
import type { Intent } from "../../../../interactive/types";

function pending(id: string, reason: string, impact = "会影响其他工作") {
  return { id, reason, impact };
}

function intentWithRevert(
  id: string,
  decisions: { id: string; reason: string; impact: string }[],
  status = "failed",
): Intent {
  return {
    id,
    boardId: "board_t",
    submissionId: null,
    title: "任务 " + id,
    summary: "",
    status,
    preview: null,
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 1, text: "" },
    reason: "",
    demo: false,
    createdAt: "2026-10-25T00:00:00.000Z",
    updatedAt: "2026-10-25T00:00:00.000Z",
    revert: {
      reverted: [],
      kept: [],
      pendingDecision: decisions,
      reasonText: "已经撤回不受影响的部分",
    },
  } as unknown as Intent;
}

let wrapper: VueWrapper | null = null;

function mountLayer() {
  const pinia = createPinia();
  setActivePinia(pinia);
  wrapper = mount(ImpactConfirmLayer, { global: { plugins: [pinia] }, attachTo: document.body });
  return { wrapper, store: useInteractiveStore(), unified: useUnifiedApprovalsStore() };
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
});

describe("剩余撤回：真实展示 pendingDecision（每项决定状态）", () => {
  it("任务失败/取消后仍留着待决定项：自动出现确认框，逐项写原因与影响", async () => {
    const { wrapper: w, store } = mountLayer();
    store.intents = [
      intentWithRevert("i1", [pending("d1", "会改写任务 A 依赖的材料"), pending("d2", "会删除任务 B 的结果卡")]),
    ];
    await nextTick();

    const dialog = w.get('[data-im="impact-confirm"]');
    expect(dialog.attributes("data-impact-mode")).toBe("revert");
    expect(dialog.attributes("role")).toBe("alertdialog");
    expect(w.findAll('[data-im="impact-revert-item"]')).toHaveLength(1);
    expect(w.findAll('[data-im="impact-revert-pending-item"]')).toHaveLength(2);
    const text = dialog.text();
    expect(text).toContain("会改写任务 A 依赖的材料");
    expect(text).toContain("会删除任务 B 的结果卡");
    expect(text).toContain("不会顺带处理你没有看到的项");
    expect(text).toContain("已经撤回不受影响的部分");
  });

  it("已结束提示的任务不再自动打断，但可在「再次查看」里重新打开", async () => {
    const { wrapper: w, store } = mountLayer();
    store.intents = [intentWithRevert("i1", [pending("d1", "会改写材料")])];
    await nextTick();
    await w.get('[data-im="impact-confirm-cancel"]').trigger("click");
    await flushPromises();

    expect(w.find('[data-im="impact-confirm"]').exists()).toBe(false);
    const dismissed = w.get('[data-im="impact-revert-dismissed"]');
    expect(dismissed.text()).toContain("还有撤回没有决定");
    expect(dismissed.text()).toContain("任务 i1");

    await w.get('[data-im="impact-revert-reopen"]').trigger("click");
    await nextTick();
    expect(w.find('[data-im="impact-confirm"]').exists()).toBe(true);
    expect(w.find('[data-im="impact-revert-dismissed"]').exists()).toBe(false);
  });
});

describe("剩余撤回：明确 decisionIds / 关闭不撤回 / 失败重试 / 部分完成", () => {
  it("「继续」只提交这次界面明确展示的 decisionIds（不顺带处理没看到的项）", async () => {
    const { wrapper: w, store } = mountLayer();
    store.intents = [
      intentWithRevert("i1", [pending("d1", "会改写材料 A")]),
      intentWithRevert("i2", [pending("d2", "会改写材料 B")]),
    ];
    await nextTick();
    const spy = vi.spyOn(store, "continueRevertDecision").mockResolvedValue({ ok: true } as never);
    const buttons = w.findAll('[data-im="impact-revert-continue"]');
    expect(buttons).toHaveLength(2);
    await buttons[0].trigger("click");
    await flushPromises();

    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenCalledWith("i1", ["d1"]);
    // 另一个任务的决定项没有被顺带处理
    expect(spy).not.toHaveBeenCalledWith("i2", expect.anything());
  });

  it("关闭不撤回：取消只结束本次提示，不调用任何撤回接口", async () => {
    const { wrapper: w, store } = mountLayer();
    store.intents = [intentWithRevert("i1", [pending("d1", "会改写材料")])];
    await nextTick();
    const spy = vi.spyOn(store, "continueRevertDecision").mockResolvedValue({ ok: true } as never);
    await w.get('[data-im="impact-confirm-cancel"]').trigger("click");
    await flushPromises();

    expect(spy).not.toHaveBeenCalled();
    expect(store.revertDecisionDismissed["i1"]).toBe(true);
    expect(store.intents[0].revert?.pendingDecision).toHaveLength(1);
  });

  it("失败重试：服务端真实原因可见、按钮恢复可点，再次点击真的重试", async () => {
    const { wrapper: w, store } = mountLayer();
    store.intents = [intentWithRevert("i1", [pending("d1", "会改写材料")])];
    await nextTick();
    const spy = vi
      .spyOn(store, "continueRevertDecision")
      .mockResolvedValue({ ok: false, reason: "服务端拒绝：内容已经变化" } as never);

    const button = w.get('[data-im="impact-revert-continue"]');
    await button.trigger("click");
    await flushPromises();

    expect(w.get('[data-im="impact-revert-item-error"]').text()).toContain("服务端拒绝：内容已经变化");
    const again = w.get('[data-im="impact-revert-continue"]');
    expect(again.attributes("disabled")).toBeUndefined();
    await again.trigger("click");
    await flushPromises();
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("部分完成：处理完展示项但仍有剩余 → 说明还剩多少；全部处理完 → 结果活过对话框关闭", async () => {
    const { wrapper: w, store } = mountLayer();
    store.intents = [intentWithRevert("i1", [pending("d1", "会改写材料 A"), pending("d2", "会改写材料 B")])];
    await nextTick();
    const spy = vi.spyOn(store, "continueRevertDecision");
    // 服务端逐项摘要：请求了两项，其中一项因为之后又有变化需要重新决定
    spy.mockImplementationOnce(async () => {
      store.intents = [intentWithRevert("i1", [pending("d2", "会改写材料 B")])];
      return {
        ok: true,
        decision: {
          requested: ["d1", "d2"],
          processed: ["d1"],
          reconfirmed: [],
          skipped: [],
          remaining: ["d2"],
          gone: [],
        },
      } as never;
    });
    spy.mockImplementationOnce(async () => {
      store.intents = [];
      return {
        ok: true,
        decision: {
          requested: ["d2"],
          processed: ["d2"],
          reconfirmed: [],
          skipped: [],
          remaining: [],
          gone: [],
        },
      } as never;
    });

    await w.get('[data-im="impact-revert-continue"]').trigger("click");
    await flushPromises();
    // 框还开着：部分完成说明在框内，且按服务端 processed / remaining 说，不按展示条数猜
    expect(w.find('[data-im="impact-confirm"]').exists()).toBe(true);
    expect(w.get('[data-im="impact-revert-notice"]').text()).toContain("已按你的决定撤回 1 项");
    expect(w.get('[data-im="impact-revert-notice"]').text()).toContain("还有 1 项需要你决定");

    await w.get('[data-im="impact-revert-continue"]').trigger("click");
    await flushPromises();
    // 全部处理完：框关闭，真实结果留在页面上
    expect(w.find('[data-im="impact-confirm"]').exists()).toBe(false);
    const outcome = w.get('[data-im="impact-confirm-outcome"]');
    expect(outcome.attributes("data-outcome")).toBe("revert_done");
    expect(outcome.text()).toContain("已按你的决定撤回 1 项");
    expect(outcome.text()).toContain("剩下的改动已经处理完");
    expect(spy).toHaveBeenCalledTimes(2);
  });
});

describe("键盘与阻塞面规则", () => {
  it("Escape 在撤回模式 = 关闭提示（不撤回）；打开时焦点落在第一个按钮", async () => {
    const { wrapper: w, store } = mountLayer();
    store.intents = [intentWithRevert("i1", [pending("d1", "会改写材料")])];
    await nextTick();
    await flushPromises();
    const cancel = w.get('[data-im="impact-confirm-cancel"]');
    expect(document.activeElement).toBe(cancel.element);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    expect(w.find('[data-im="impact-confirm"]').exists()).toBe(false);
    expect(store.revertDecisionDismissed["i1"]).toBe(true);
  });

  it("Escape 在影响确认模式 = 取消本次改动（沿用原有语义）", async () => {
    const { wrapper: w, store } = mountLayer();
    store.pendingImpact = {
      affected: [{ intentId: "i1", title: "整理材料", materials: ["a.pdf"], consequence: "会暂停" }],
      previewRev: 1,
      stateVersion: 0,
      checkId: "chk",
    } as never;
    await nextTick();
    await flushPromises();
    expect(w.get('[data-im="impact-confirm"]').attributes("data-impact-mode")).toBe("impact");
    const spy = vi.spyOn(store, "cancelImpact").mockResolvedValue(undefined as never);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("统一审批弹窗打开时让位：撤回确认也不叠第二个阻塞面，关闭后自动回来", async () => {
    const { wrapper: w, store, unified } = mountLayer();
    store.intents = [intentWithRevert("i1", [pending("d1", "会改写材料")])];
    unified.visible = true;
    await nextTick();
    expect(w.find('[data-im="impact-confirm"]').exists()).toBe(false);
    expect(w.get('[data-im="impact-confirm-deferred"]').text()).toContain("撤回");

    unified.visible = false;
    await nextTick();
    expect(w.find('[data-im="impact-confirm"]').exists()).toBe(true);
  });
});
