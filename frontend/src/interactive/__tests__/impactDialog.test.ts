/**
 * 影响确认框的接线测试（子智能体 D）：证明中央确认框真的按契约工作。
 *
 * 覆盖：
 * - 打开时焦点落在框内第一个按钮（默认「取消」）；
 * - Tab / Shift+Tab 焦点限制在框内；
 * - Escape = 取消（改动不生效、任务继续）；
 * - 关闭后焦点回到触发点；
 * - 「继续」走 store.confirmImpact（改动生效 + 相关任务暂停），不把演示状态说成真实执行成功。
 */
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ImpactConfirmDialog from "../../components/interactive/ImpactConfirmDialog.vue";
import { useInteractiveStore } from "../../stores/interactive";

let wrapper: VueWrapper | null = null;

function mountDialog(): { store: ReturnType<typeof useInteractiveStore>; wrapper: VueWrapper } {
  const store = useInteractiveStore();
  // 这两个动作会去访问服务端；组件测试只关心「接线是否正确」，所以在这里替换掉，
  // 避免测试环境因为连不上后端而出现未处理的网络错误。
  vi.spyOn(store, "cancelImpact").mockImplementation(async () => {
    store.pendingImpact = null;
  });
  vi.spyOn(store, "confirmImpact").mockResolvedValue({ outcome: "saved", paused: [] });
  store.pendingImpact = {
    previewRev: 0,
    stateVersion: 0,
    affected: [
      {
        intentId: "i1",
        title: "整理两份材料",
        materials: ["文件「a.pdf」"],
        consequence: "继续保存会让这项任务暂停并保留当前进度；取消则不改动板面，任务继续。",
      },
    ],
  };
  wrapper = mount(ImpactConfirmDialog, { attachTo: document.body, global: { stubs: { Teleport: true } } });
  return { store, wrapper };
}

beforeEach(() => {
  setActivePinia(createPinia());
  document.body.innerHTML = "";
  try {
    localStorage.clear();
  } catch {
    // 忽略
  }
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  vi.restoreAllMocks();
});

describe("影响确认框", () => {
  it("打开时焦点在框内第一个按钮（默认是「取消」）", async () => {
    const { wrapper: w } = mountDialog();
    await w.vm.$nextTick();
    const first = w.find('[data-im="impact-cancel"]').element as HTMLButtonElement;
    expect(document.activeElement).toBe(first);
    const dialog = w.find('[data-im="impact-dialog"]').element as HTMLElement;
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it("说明受影响的对象、任务与后果，并写明改动还没有生效", () => {
    const { wrapper: w } = mountDialog();
    const text = w.text();
    expect(text).toContain("整理两份材料");
    expect(text).toContain("a.pdf");
    expect(text).toContain("暂停");
    expect(text).toContain("改动还没有生效");
    expect(text).toContain("这不是对任务的重新审批");
  });

  it("Tab 焦点限制在框内：从最后一个按钮回到第一个", async () => {
    const { wrapper: w } = mountDialog();
    await w.vm.$nextTick();
    const first = w.find('[data-im="impact-cancel"]').element as HTMLButtonElement;
    const last = w.find('[data-im="impact-continue"]').element as HTMLButtonElement;
    last.focus();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    expect(document.activeElement).toBe(first);
    first.focus();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true }));
    expect(document.activeElement).toBe(last);
  });

  it("Escape = 取消：改动不生效，框关闭", async () => {
    const { store, wrapper: w } = mountDialog();
    const spy = vi.mocked(store.cancelImpact);
    await w.vm.$nextTick();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(spy).toHaveBeenCalledTimes(1);
    expect(store.pendingImpact).toBeNull();
    await w.vm.$nextTick();
    expect(w.find('[data-im="impact-dialog"]').exists()).toBe(false);
  });

  it("取消按钮 = 取消（不改动板面）", async () => {
    const { store, wrapper: w } = mountDialog();
    const spy = vi.mocked(store.cancelImpact);
    await w.find('[data-im="impact-cancel"]').trigger("click");
    expect(spy).toHaveBeenCalledTimes(1);
    expect(store.pendingImpact).toBeNull();
  });

  it("继续按钮 = 改动生效并暂停相关任务（走 store.confirmImpact）", async () => {
    const { store, wrapper: w } = mountDialog();
    const spy = vi.mocked(store.confirmImpact);
    /**
     * 本轮（N2）后 store.confirmImpact 返回**真实结果** ImpactConfirmResult（不再是 void），
     * 且真实 store 在确认时会清掉 pendingImpact（改动已进入保存）。
     * 界面只在返回 saved 时才说「已保存生效」，真实结果放在结果面板里（Teleport）。
     */
    spy.mockImplementation(async () => {
      store.pendingImpact = null;
      return { outcome: "saved", paused: [] };
    });
    await w.find('[data-im="impact-continue"]').trigger("click");
    expect(spy).toHaveBeenCalledTimes(1);
    await w.vm.$nextTick();
    const panel = w.find('[data-im="impact-outcome"]');
    expect(panel.exists()).toBe(true);
    expect(panel.attributes("data-outcome")).toBe("saved");
    expect(panel.text()).toContain("改动已保存生效");
  });

  it("关闭后焦点回到触发点", async () => {
    const trigger = document.createElement("button");
    trigger.textContent = "触发点";
    document.body.appendChild(trigger);
    trigger.focus();
    expect(document.activeElement).toBe(trigger);

    const { store, wrapper: w } = mountDialog();
    await w.vm.$nextTick();
    expect(document.activeElement).not.toBe(trigger); // 打开时焦点已经进框

    store.pendingImpact = null;
    await w.vm.$nextTick();
    await w.vm.$nextTick();
    expect(document.activeElement).toBe(trigger);
    trigger.remove();
  });

  it("撤回还有需要决定的部分时，用同一个框说明剩余改动与具体影响", () => {
    const store = useInteractiveStore();
    store.pendingImpact = null;
    store.intents = [
      {
        id: "i9",
        boardId: "board_t",
        submissionId: null,
        title: "会失败的任务",
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
        createdAt: "2026-10-07T00:00:00.000Z",
        updatedAt: "2026-10-07T00:00:00.000Z",
        revert: {
          reverted: ["卡片 c1"],
          kept: [],
          pendingDecision: [{ id: "c2", reason: "撤回会影响别的工作", impact: "它是某条关系的一端" }],
          reasonText: "任务失败，已撤回 1 项",
        },
      },
    ];
    const w = mount(ImpactConfirmDialog, { attachTo: document.body });
    wrapper = w;
    const text = w.text();
    expect(text).toContain("还有改动没有撤回");
    expect(text).toContain("它是某条关系的一端");
    expect(w.find('[data-im="impact-dialog"]').attributes("data-impact-mode")).toBe("revert");
  });
});