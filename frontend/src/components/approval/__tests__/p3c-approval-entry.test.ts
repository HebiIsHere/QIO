/**
 * 统一审批层：暂缓后不反复自动弹 + 唯一挂载守卫（Phase 3-C / 契约 §6.4）。
 *
 * 注意：本文件**不**挂载 UnifiedApprovalEntry（该旧兼容入口在 A-CP1 后由 C 删除）。
 * 聊天页的入口由 ComposerEntries 的 ⑤ 提供；非聊天路由由 NonChatApprovalEntry 提供，
 * 两者都打开同一个 UnifiedApprovalModal —— 层本身不再渲染入口，避免第二套按钮。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import UnifiedApprovalLayer from "../../conversation/UnifiedApprovalLayer.vue";
import { useApprovalsStore } from "../../../stores/approvals";
import { useUnifiedApprovalsStore } from "../../../stores/unifiedApprovals";

let wrappers: VueWrapper[] = [];

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
  });

  it("两个层同时挂载会如实警告（唯一挂载约束不靠自觉）", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    wrappers.push(mount(UnifiedApprovalLayer, { props: { showMemory: false } }));
    wrappers.push(mount(UnifiedApprovalLayer, { props: { showMemory: false } }));
    await flushPromises();
    expect(warn.mock.calls.some((call) => String(call[0]).includes("统一审批层被重复挂载"))).toBe(true);
  });

  it("层本身不渲染第二套入口（入口只在 ComposerEntries / NonChatApprovalEntry）", () => {
    const layer = readFileSync(
      resolve(process.cwd(), "src/components/conversation/UnifiedApprovalLayer.vue"),
      "utf8",
    );
    expect(layer).not.toContain("UnifiedApprovalEntry");
    expect(layer).not.toContain("unified-approval-entry");
    expect(layer).toContain("<UnifiedApprovalModal />");
  });
});
