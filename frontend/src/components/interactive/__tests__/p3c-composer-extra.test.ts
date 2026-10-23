/**
 * Phase 3-C：输入区事项行（ComposerEntries）的额外入口。
 *
 * 验收口径（契约 §6.4）：
 * - 额外事项与内置入口共用**同一个展开状态**：同一时刻最多展开一个普通事项；
 * - 审批入口不展开面板（直接打开统一弹窗），并收起其它事项；
 * - 收起不删除任何记录；Esc 收起当前面板；
 * - 不传 extraEntries 时，对话页行为一字不变（既有五入口测试继续覆盖）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

vi.mock("../../../services/api", () => ({
  api: {
    cancelTurn: vi.fn(async () => ({ ok: true, cancelled: true })),
    cancelContinuation: vi.fn(async () => ({ ok: true, cancelled: true })),
  },
}));

import ComposerEntries from "../../conversation/ComposerEntries.vue";
import { useApprovalsStore } from "../../../stores/approvals";
import { useUnifiedApprovalsStore } from "../../../stores/unifiedApprovals";

let wrappers: VueWrapper[] = [];

function freshPinia() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

function mountEntries(props: Record<string, unknown> = {}, pinia = freshPinia()): VueWrapper {
  const w = mount(ComposerEntries, {
    props,
    global: { plugins: [pinia] },
    slots: {
      "extra-panel": '<div class="stub-extra-panel" :data-stub-id="params.id">额外面板：{{ params.id }}</div>',
    },
  });
  wrappers.push(w);
  return w;
}

beforeEach(() => {
  localStorage.clear();
});

function cleanup(): void {
  for (const w of wrappers) {
    try { w.unmount(); } catch { /* 已卸载 */ }
  }
  wrappers = [];
}

describe("额外事项入口：与内置入口共用单一展开", () => {
  it("额外入口出现在同一条事项行；点开渲染对应面板，再点收起", async () => {
    const w = mountEntries({
      context: "interactive",
      extraEntries: [
        { id: "retry", title: "重试与收尾", hint: "自动/手动重试", count: 2, tone: "warn" },
        { id: "recovery", title: "恢复", count: 1 },
      ],
    });
    await nextTick();
    expect(w.get(".entry-row").attributes("aria-label")).toBe("输入区事项入口");
    const extraButtons = w.findAll('[data-extra="1"]');
    expect(extraButtons).toHaveLength(2);
    expect(extraButtons[0].attributes("data-entry")).toBe("retry");
    expect(extraButtons[0].text()).toContain("2");

    await extraButtons[0].trigger("click");
    expect(w.find('[data-extra-entry="retry"]').exists()).toBe(true);
    const openedEvents = w.emitted("extra-open") as unknown[][];
    expect(openedEvents[openedEvents.length - 1]).toEqual(["retry"]);

    await extraButtons[0].trigger("click");
    expect(w.find('[data-extra-entry="retry"]').exists()).toBe(false);
    const closedEvents = w.emitted("extra-close") as unknown[][];
    expect(closedEvents[closedEvents.length - 1]).toEqual(["retry"]);
  });

  it("打开另一个额外事项会收起当前（最多展开一个）", async () => {
    const w = mountEntries({
      extraEntries: [
        { id: "retry", title: "重试" },
        { id: "recovery", title: "恢复" },
      ],
    });
    await nextTick();
    await w.get('[data-entry="retry"]').trigger("click");
    expect(w.find('[data-extra-entry="retry"]').exists()).toBe(true);
    await w.get('[data-entry="recovery"]').trigger("click");
    expect(w.find('[data-extra-entry="retry"]').exists()).toBe(false);
    expect(w.find('[data-extra-entry="recovery"]').exists()).toBe(true);
  });

  it("审批入口：不展开额外面板，直接打开统一弹窗并收起当前事项", async () => {
    const pinia = freshPinia();
    const approvals = useApprovalsStore();
    const unified = useUnifiedApprovalsStore();
    approvals.enqueue("ap_1", "tool_execution", { description: "删除临时目录" }, { autoOpen: false });

    const w = mountEntries({ extraEntries: [{ id: "retry", title: "重试" }] }, pinia);
    await nextTick();
    await flushPromises();
    await w.get('[data-entry="retry"]').trigger("click");
    expect(w.find('[data-extra-entry="retry"]').exists()).toBe(true);

    const approvalBtn = w.get('[data-entry="approvals"]');
    await approvalBtn.trigger("click");
    expect(unified.visible).toBe(true);
    expect(w.find('[data-extra-entry="retry"]').exists()).toBe(false);
  });

  it("Esc 收起当前额外面板（不删除任何记录）", async () => {
    const w = mountEntries({ extraEntries: [{ id: "retry", title: "重试" }] });
    await nextTick();
    await w.get('[data-entry="retry"]').trigger("click");
    expect(w.find('[data-extra-entry="retry"]').exists()).toBe(true);
    await w.get(".composer-entries").trigger("keydown", { key: "Escape" });
    expect(w.find('[data-extra-entry="retry"]').exists()).toBe(false);
  });

  it("隐藏的额外入口不占位；不传 extraEntries 时没有额外按钮", async () => {
    const hidden = mountEntries({ extraEntries: [{ id: "x", title: "隐藏", visible: false }] });
    await nextTick();
    expect(hidden.find(".entry-row").exists()).toBe(false);
    cleanup();

    const plain = mountEntries();
    await nextTick();
    expect(plain.findAll('[data-extra="1"]')).toHaveLength(0);
  });
});