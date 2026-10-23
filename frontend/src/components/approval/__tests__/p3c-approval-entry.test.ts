/**
 * Phase 3-C：统一审批入口与唯一挂载（契约 §6.4）。
 *
 * 验收口径：
 * - 互动页只有**一个**审批入口（旧 ApprovalEntry/Modal 在聊天路由已由 App 门控让位，
 *   本文件不重复测 App；这里测统一入口自身）；
 * - 入口可手动找回：暂缓 / 关闭只影响自动弹出，入口始终可用且能再次打开；
 * - 暂缓后不反复自动弹：轮询、重渲染、重新挂载都不会把同一项再弹出来；
 * - inline / floating 两种呈现都不伪造数量（工具 + 知识分别计数）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import UnifiedApprovalEntry from "../UnifiedApprovalEntry.vue";
import UnifiedApprovalLayer from "../../conversation/UnifiedApprovalLayer.vue";
import { useApprovalsStore } from "../../../stores/approvals";
import { useUnifiedApprovalsStore } from "../../../stores/unifiedApprovals";

let wrappers: VueWrapper[] = [];

function mountEntry(props: Record<string, unknown> = {}): VueWrapper {
  const w = mount(UnifiedApprovalEntry, { props });
  wrappers.push(w);
  return w;
}

function stubFetch(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ items: [], total: 0, maintenance_available: true }),
      text: async () => "",
    })),
  );
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  stubFetch();
});

afterEach(() => {
  for (const w of wrappers) {
    try { w.unmount(); } catch { /* 已卸载 */ }
  }
  wrappers = [];
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("统一审批入口：唯一、可手动找回", () => {
  it("没有待办时不渲染；有真实待办才出现，数量来自统一队列（工具 + 知识分别计数）", async () => {
    const w = mountEntry();
    expect(w.find('[data-im="unified-approval-entry"]').exists()).toBe(false);

    const approvals = useApprovalsStore();
    approvals.enqueue("ap_1", "tool_execution", { description: "删除临时目录" }, { autoOpen: false });
    approvals.enqueue("ap_2", "credential_grant", { description: "读取凭据" }, { autoOpen: false });
    await flushPromises();
    await nextTick();

    const button = w.get('[data-im="unified-approval-entry"]');
    expect(button.attributes("aria-label")).toContain("2");
    expect(button.text()).toContain("2");
    expect(button.attributes("data-variant")).toBe("floating");
  });

  it("弹窗正在显示时不重复出现（同一时刻只有一个入口）", async () => {
    const approvals = useApprovalsStore();
    const unified = useUnifiedApprovalsStore();
    approvals.enqueue("ap_1", "tool_execution", { description: "删除临时目录" }, { autoOpen: false });
    await flushPromises();
    const w = mountEntry();
    expect(w.find('[data-im="unified-approval-entry"]').exists()).toBe(true);

    unified.open();
    await nextTick();
    expect(w.find('[data-im="unified-approval-entry"]').exists()).toBe(false);
  });

  it("关闭（暂缓）后入口仍然可点，点一次就打开一次（手动找回）", async () => {
    const approvals = useApprovalsStore();
    const unified = useUnifiedApprovalsStore();
    approvals.enqueue("ap_1", "tool_execution", { description: "删除临时目录" }, { autoOpen: false });
    await flushPromises();
    const w = mountEntry();
    unified.open();
    await nextTick();
    unified.close();
    await nextTick();
    expect(unified.visible).toBe(false);

    const button = w.find('[data-im="unified-approval-entry"]');
    expect(button.exists()).toBe(true);
    await button.trigger("click");
    expect(unified.visible).toBe(true);
  });

  it("inline 变体参与布局（不固定定位），用于输入区事项行；opened 事件只报一次", async () => {
    const approvals = useApprovalsStore();
    approvals.enqueue("ap_1", "tool_execution", { description: "删除临时目录" }, { autoOpen: false });
    await flushPromises();
    const w = mountEntry({ variant: "inline", compact: true, withMark: false });
    const button = w.get('[data-im="unified-approval-entry"]');
    expect(button.attributes("data-variant")).toBe("inline");
    expect(button.classes()).toContain("is-inline");
    expect(button.classes()).not.toContain("is-floating");
    expect(button.text()).toContain("审批 1");
    await button.trigger("click");
    expect(w.emitted("opened")).toHaveLength(1);
  });
});

describe("统一审批层：暂缓后不反复自动弹 + 唯一挂载守卫", () => {
  it("自动弹出一次后关闭；再次触发自动弹出、重渲染都不会把同一项再弹出来", async () => {
    const approvals = useApprovalsStore();
    const unified = useUnifiedApprovalsStore();
    const w = mount(UnifiedApprovalLayer, { props: { showMemory: false } });
    wrappers.push(w);
    await flushPromises();

    approvals.enqueue("ap_auto", "tool_execution", { description: "后台写入" }, { autoOpen: true });
    await flushPromises();
    await nextTick();
    expect(unified.visible, "没有自动弹出").toBe(true);

    unified.close();
    await flushPromises();
    expect(unified.visible).toBe(false);

    // 模拟轮询 / 重渲染 / 再次触发：同一项已暂缓，不允许再自动弹
    const exposed = w.vm as unknown as { retryAutoOpen: () => void };
    exposed.retryAutoOpen();
    exposed.retryAutoOpen();
    await flushPromises();
    expect(unified.visible).toBe(false);

    // 重新挂载一个层（页面切换）也不会绕过暂缓规则
    const w2 = mount(UnifiedApprovalLayer, { props: { showMemory: false } });
    wrappers.push(w2);
    await flushPromises();
    expect(unified.visible).toBe(false);

    // 但手动入口仍能找回
    const entry = mountEntry();
    await entry.get('[data-im="unified-approval-entry"]').trigger("click");
    expect(unified.visible).toBe(true);
  });

  it("两个层同时挂载会如实警告（唯一挂载约束不靠自觉）", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    wrappers.push(mount(UnifiedApprovalLayer, { props: { showMemory: false } }));
    wrappers.push(mount(UnifiedApprovalLayer, { props: { showMemory: false } }));
    await flushPromises();
    expect(warn.mock.calls.some((call) => String(call[0]).includes("统一审批层被重复挂载"))).toBe(true);
  });
});