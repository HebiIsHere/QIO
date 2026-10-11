/**
 * C 本轮「决定界面」的反例测试（N2 真实结果文案 / N3 待决定撤回项的继续与取消）。
 *
 * 这些用例都从**真实操作结果**出发，而不是从按钮处理函数完成出发：
 * - N2：保存被拒后重新说明时，界面不许同时宣称「已保存 / 已暂停」；
 *   只有 confirmImpact 返回 saved 才说生效，失败要给出真实原因与可达的重试入口；
 * - N3：「继续」必须真的调 continueRevertDecision（并只带这次展示的 decisionIds），
 *   「取消」/Escape 只结束本次提示（dismissRevertDecision），绝不误执行撤回；
 *   失败要有真实原因与重试；关闭后能按任务身份再次查看；多任务按 intentId 处理。
 *
 * 依赖 Lead 在 store 里落地的接口（c347d48 起）：confirmImpact 返回 ImpactConfirmResult、
 * continueRevertDecision / dismissRevertDecision / reopenRevertDecision / revertDecisionDismissed /
 * revertDecisionStateFor / cancelRecovery。这里用 vi.spyOn 局部替身，避免测试环境真的发请求。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ImpactConfirmDialog from "../ImpactConfirmDialog.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import type { Intent, ImpactConfirmResult } from "../../../interactive/types";

let wrapper: VueWrapper | null = null;

function makeIntent(id: string, overrides: Partial<Intent> = {}): Intent {
  return {
    id,
    boardId: "board_t",
    submissionId: null,
    title: "任务 " + id,
    summary: "",
    status: "failed",
    preview: { cards: [], groups: [], links: [] },
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 1, total: 2, text: "" },
    reason: "",
    demo: true,
    createdAt: "2026-10-08T00:00:00.000Z",
    updatedAt: "2026-10-08T00:00:00.000Z",
    ...overrides,
  };
}

function mountDialog(): { store: ReturnType<typeof useInteractiveStore>; w: VueWrapper } {
  const store = useInteractiveStore();
  wrapper = mount(ImpactConfirmDialog, { attachTo: document.body, global: { stubs: { Teleport: true } } });
  return { store, w: wrapper };
}

function panelText(w: VueWrapper): string {
  const panel = w.find('[data-im="impact-outcome"]');
  return panel.exists() ? panel.text() : "";
}

/** Teleport 里的「还有撤回没有决定」入口 */
function dismissedEntry(w: VueWrapper) {
  return w.find('[data-im="impact-dismissed"]');
}

beforeEach(() => {
  setActivePinia(createPinia());
  document.body.innerHTML = "";
  try {
    localStorage.clear();
  } catch {
    /* 忽略 */
  }
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  vi.restoreAllMocks();
});

describe("N2：状态与文案只依据真实操作结果", () => {
  it("保存被拒后重新说明：说明里不许再同时出现「已保存 / 已暂停」", async () => {
    const { store, w } = mountDialog();
    store.pendingImpact = {
      previewRev: 2,
      stateVersion: 7,
      affected: [
        { intentId: "i1", title: "整理材料", materials: ["文件 a.pdf"], consequence: "继续保存会让它暂停并保留进度" },
      ],
      note: "影响范围已经重新核对：下面是当前候选的真实影响，确认后才会保存生效。",
    };
    await w.vm.$nextTick();
    const text = w.text();
    expect(text).toContain("整理材料");
    expect(text).toContain("改动还没有生效");
    // 关键反例：确认还没有发生，不许出现任何「已经保存 / 已经暂停」的事实声明
    expect(w.find('[data-im="impact-notice"]').exists()).toBe(false);
    expect(text).not.toContain("已确认：改动已经保存生效");
    expect(text).not.toContain("受影响的任务已暂停");
    expect(store.pendingImpact).not.toBeNull();
  });

  it("确认时板面又变了（结果 superseded）：不宣称成功，并等新的说明", async () => {
    const { store, w } = mountDialog();
    store.pendingImpact = {
      previewRev: 3,
      stateVersion: 7,
      affected: [{ intentId: "i1", title: "任务 A", materials: [], consequence: "会暂停" }],
    };
    store.confirmImpact = vi.fn(async () => {
      store.pendingImpact = null;
      return { outcome: "superseded", reason: "等待确认期间板面又有改动，已重新核实这次改动的影响" } as ImpactConfirmResult;
    });
    await w.vm.$nextTick();
    await w.find('[data-im="impact-continue"]').trigger("click");
    await w.vm.$nextTick();
    const text = panelText(w) + w.text();
    expect(text).toContain("板面又有改动");
    expect(text).not.toContain("已经保存生效");
    expect(text).not.toContain("已暂停");
  });

  it("检查失败：显示真实原因，并给可达的重试入口（重新核实并保存）", async () => {
    const { store, w } = mountDialog();
    const saveNow = vi.fn(async () => undefined);
    store.saveNow = saveNow as unknown as typeof store.saveNow;
    store.pendingImpact = {
      previewRev: 4,
      stateVersion: 7,
      affected: [{ intentId: "i1", title: "任务 A", materials: [], consequence: "会暂停" }],
    };
    store.confirmImpact = vi.fn(async () => {
      store.pendingImpact = null;
      return { outcome: "check_failed", reason: "这次保存前的影响预判没有完成，保存已暂停（服务端没有给出可用的影响确认记录）" } as ImpactConfirmResult;
    });
    await w.vm.$nextTick();
    await w.find('[data-im="impact-continue"]').trigger("click");
    await w.vm.$nextTick();
    const text = panelText(w);
    expect(text).toContain("服务端没有给出可用的影响确认记录");
    expect(text).not.toContain("已经保存");
    const retry = w.find('[data-im="impact-retry"]');
    expect(retry.exists()).toBe(true);
    await retry.trigger("click");
    expect(saveNow).toHaveBeenCalledTimes(1);
  });

  it("保存失败：真实原因不截断，重试可用", async () => {
    const { store, w } = mountDialog();
    const longReason =
      "服务端在写入这次板面改动时出错：事务回滚，候选与勾选都还保留着；请稍后重试，或先复制一份材料再改（这段原文故意很长，用来验证不被截断）";
    const saveNow = vi.fn(async () => undefined);
    store.saveNow = saveNow as unknown as typeof store.saveNow;
    store.pendingImpact = {
      previewRev: 5,
      stateVersion: 7,
      affected: [{ intentId: "i1", title: "任务 A", materials: [], consequence: "会暂停" }],
    };
    store.confirmImpact = vi.fn(async () => {
      store.pendingImpact = null;
      return { outcome: "save_failed", reason: longReason } as ImpactConfirmResult;
    });
    await w.vm.$nextTick();
    await w.find('[data-im="impact-continue"]').trigger("click");
    await w.vm.$nextTick();
    const panel = w.find('[data-im="impact-outcome"]');
    expect(panel.exists()).toBe(true);
    expect(panel.attributes("data-outcome")).toBe("save_failed");
    expect(panelText(w)).toContain(longReason);
    expect(panelText(w)).not.toContain("已经保存生效");
    await w.find('[data-im="impact-retry"]').trigger("click");
    expect(saveNow).toHaveBeenCalledTimes(1);
  });

  it("真正保存成功：只有在返回 saved 时才说生效，并列出真正被暂停的任务", async () => {
    const { store, w } = mountDialog();
    store.pendingImpact = {
      previewRev: 6,
      stateVersion: 7,
      affected: [{ intentId: "i1", title: "任务 A", materials: [], consequence: "会暂停" }],
    };
    store.confirmImpact = vi.fn(async () => {
      store.pendingImpact = null;
      return {
        outcome: "saved",
        paused: [makeIntent("i1", { title: "任务 A", status: "paused" })],
      } as ImpactConfirmResult;
    });
    await w.vm.$nextTick();
    await w.find('[data-im="impact-continue"]').trigger("click");
    await w.vm.$nextTick();
    const panel = w.find('[data-im="impact-outcome"]');
    expect(panel.exists()).toBe(true);
    expect(panel.attributes("data-outcome")).toBe("saved");
    expect(panelText(w)).toContain("改动已保存生效");
    expect(panelText(w)).toContain("任务 A");
    expect(panelText(w)).toContain("已暂停");
  });

  it("仍待确认：如实说还没有保存，不许把「还需要确认」说成成功", async () => {
    const { store, w } = mountDialog();
    store.pendingImpact = {
      previewRev: 7,
      stateVersion: 7,
      affected: [{ intentId: "i1", title: "任务 A", materials: [], consequence: "会暂停" }],
    };
    store.confirmImpact = vi.fn(async () => ({ outcome: "needs_confirm", reason: "受影响的任务与刚才的说明不同" } as ImpactConfirmResult));
    await w.vm.$nextTick();
    await w.find('[data-im="impact-continue"]').trigger("click");
    await w.vm.$nextTick();
    const text = panelText(w);
    expect(text).toContain("还没有保存");
    expect(text).toContain("受影响的任务与刚才的说明不同");
    expect(text).not.toContain("已经保存生效");
  });
});

describe("N3：待决定撤回项的继续 / 取消", () => {
  function seedRevert(store: ReturnType<typeof useInteractiveStore>, intentId = "i9"): void {
    store.pendingImpact = null;
    store.intents = [
      makeIntent(intentId, {
        title: "会失败的任务",
        revert: {
          reverted: ["卡片 c1"],
          kept: [],
          pendingDecision: [
            { id: "c2", reason: "撤回会影响别的工作", impact: "它是某条关系的一端" },
            { id: "c3", reason: "也被引用", impact: "组内还有成员" },
          ],
          reasonText: "任务失败，已撤回 1 项",
        },
      }),
    ];
  }

  it("继续：真的执行这次明确展示的剩余处理，并且只带这次展示的 decisionIds", async () => {
    const { store, w } = mountDialog();
    seedRevert(store);
    const calls: { id: string; ids?: string[] }[] = [];
    store.continueRevertDecision = vi.fn(async (id: string, ids?: string[]) => {
      calls.push({ id, ids });
      return { ok: true, intent: store.intents[0] };
    });
    await w.vm.$nextTick();
    await w.find('[data-im="impact-continue"]').trigger("click");
    await w.vm.$nextTick();
    expect(calls.length).toBe(1);
    expect(calls[0].id).toBe("i9");
    expect(calls[0].ids).toEqual(["c2", "c3"]);
  });

  it("取消：只结束本次提示，不执行撤回（不再调用只清 materialPaused 的旧动作）", async () => {
    const { store, w } = mountDialog();
    seedRevert(store);
    const dismissMaterial = vi.fn();
    store.dismissMaterialPaused = dismissMaterial;
    const dismiss = vi.fn();
    store.dismissRevertDecision = dismiss;
    const cont = vi.fn(async () => ({ ok: true }));
    store.continueRevertDecision = cont;
    await w.vm.$nextTick();
    await w.find('[data-im="impact-cancel"]').trigger("click");
    expect(dismiss).toHaveBeenCalledWith("i9");
    expect(cont).not.toHaveBeenCalled();
    expect(dismissMaterial).not.toHaveBeenCalled();
  });

  it("Escape：与取消同一条路径（结束提示、保留板面、不执行撤回）", async () => {
    const { store, w } = mountDialog();
    seedRevert(store);
    const dismiss = vi.fn();
    store.dismissRevertDecision = dismiss;
    const cont = vi.fn(async () => ({ ok: true }));
    store.continueRevertDecision = cont;
    await w.vm.$nextTick();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(dismiss).toHaveBeenCalledWith("i9");
    expect(cont).not.toHaveBeenCalled();
  });

  it("关闭后不再自动打断；可以按任务身份主动再次查看", async () => {
    const { store, w } = mountDialog();
    seedRevert(store);
    await w.vm.$nextTick();
    expect(w.find('[data-im="impact-dialog"]').exists()).toBe(true);
    store.revertDecisionDismissed = { i9: true };
    await w.vm.$nextTick();
    expect(w.find('[data-im="impact-dialog"]').exists()).toBe(false);
    // 「任务列表更新」不是重新打断的理由：只有用户主动再次查看才放出来
    store.intents = [...store.intents];
    await w.vm.$nextTick();
    expect(w.find('[data-im="impact-dialog"]').exists()).toBe(false);

    const reopened: string[] = [];
    store.reopenRevertDecision = vi.fn((id: string) => {
      reopened.push(id);
      store.revertDecisionDismissed = {};
    });
    const remaining = dismissedEntry(w);
    expect(remaining.exists()).toBe(true);
    await remaining.find('button[data-im="impact-dismissed-reopen"]').trigger("click");
    expect(reopened).toEqual(["i9"]);
    await w.vm.$nextTick();
    expect(w.find('[data-im="impact-dialog"]').exists()).toBe(true);
  });

  it("失败：显示服务端真实原因，并提供可用的重试入口", async () => {
    const { store, w } = mountDialog();
    seedRevert(store);
    const results = [
      { ok: false, reason: "撤回前重新核对发现这些内容已经被改动，需要你重新说明后再处理" },
      { ok: true, intent: store.intents[0] },
    ];
    let call = 0;
    store.continueRevertDecision = vi.fn(async () => results[call++]);
    await w.vm.$nextTick();
    await w.find('[data-im="impact-continue"]').trigger("click");
    await w.vm.$nextTick();
    const text = w.text();
    expect(text).toContain("撤回前重新核对发现这些内容已经被改动");
    const retry = w.find('[data-im="impact-continue"]');
    expect(retry.exists()).toBe(true);
    await retry.trigger("click");
    expect(store.continueRevertDecision).toHaveBeenCalledTimes(2);
  });

  it("多任务按身份处理：只对用户这次要处理的那个 intentId 生效", async () => {
    const { store, w } = mountDialog();
    store.pendingImpact = null;
    store.intents = [
      makeIntent("i9", {
        title: "任务九",
        revert: { reverted: [], kept: [], pendingDecision: [{ id: "d9", reason: "九的原因", impact: "九的影响" }], reasonText: "九" },
      }),
      makeIntent("i10", {
        title: "任务十",
        revert: { reverted: [], kept: [], pendingDecision: [{ id: "d10", reason: "十的原因", impact: "十的影响" }], reasonText: "十" },
      }),
    ];
    const calls: { id: string; ids?: string[] }[] = [];
    store.continueRevertDecision = vi.fn(async (id: string, ids?: string[]) => {
      calls.push({ id, ids });
      store.intents = store.intents.filter((item) => item.id !== id);
      return { ok: true };
    });
    await w.vm.$nextTick();
    const blocks = w.findAll('[data-im="impact-item"]');
    expect(blocks.length).toBe(2);
    const second = blocks[1];
    await second.find('[data-im="impact-item-continue"]').trigger("click");
    expect(calls).toEqual([{ id: "i10", ids: ["d10"] }]);
  });

  it("请求进行中：按钮显示进行态并禁用，避免重复执行", async () => {
    const { store, w } = mountDialog();
    seedRevert(store);
    store.revertDecisionStateFor = vi.fn(() => ({ status: "pending" as const, error: null }));
    store.continueRevertDecision = vi.fn(async () => ({ ok: true }));
    await w.vm.$nextTick();
    const button = w.find('[data-im="impact-continue"]');
    expect(button.attributes("disabled")).toBeDefined();
    await button.trigger("click");
    expect(store.continueRevertDecision).not.toHaveBeenCalled();
  });
});

describe("N2：取消后回读失败时如实说明", () => {
  it("回读没落地就不许说「已撤回」，要显示真实原因与重试", async () => {
    const { store, w } = mountDialog();
    store.pendingImpact = {
      previewRev: 8,
      stateVersion: 7,
      affected: [{ intentId: "i1", title: "任务 A", materials: [], consequence: "会暂停" }],
    };
    const retryCancel = vi.fn(async () => ({ ok: true }));
    store.retryCancelRecovery = retryCancel;
    store.cancelImpact = vi.fn(async () => {
      // 真实 store 在 cancelImpact 内部维护 cancelRecovery：回读失败 → active=true + 真实原因
      store.cancelRecovery = { boardId: "board_t", active: true, reason: "读回服务器板面失败：网络不可用" };
    });
    await w.vm.$nextTick();
    await w.find('[data-im="impact-cancel"]').trigger("click");
    await w.vm.$nextTick();
    const text = w.text() + panelText(w);
    expect(text).toContain("读回服务器板面失败：网络不可用");
    expect(text).not.toContain("改动没有生效");
    const retry = w.find('[data-im="impact-cancel-retry"]');
    expect(retry.exists()).toBe(true);
    await retry.trigger("click");
    expect(retryCancel).toHaveBeenCalledTimes(1);
  });
});
