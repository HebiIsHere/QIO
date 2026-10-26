/**
 * FU2-C 红用例 3（组件）：旧审批中断项只读展示 + 回到前台补读。
 *
 * 先红后绿（基线 373fda40 上应失败）：
 * 1. 恢复出的 legacy 中断项（legacy=true + status=interrupted_legacy，state 为空）只读显示
 *    「未执行 / 中断待核对」，**不提供**允许 / 拒绝 / 确认按钮；
 * 2. legacy 普通等待项不渲染伪造的 v2 状态行，批准走 /respond；
 * 3. 窗口回到前台（visibilitychange）→ 主动补读 pending + history。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({
  respondApproval: vi.fn(),
  request: vi.fn(),
  decideApproval: vi.fn(),
}));

vi.mock("../../../services/api", () => ({
  request: mocks.request,
  api: { respondApproval: mocks.respondApproval },
}));

vi.mock("../../../services/approvalLifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/approvalLifecycle")>();
  return { ...actual, decideApproval: mocks.decideApproval };
});

import UnifiedApprovalModal from "../UnifiedApprovalModal.vue";
import UnifiedApprovalLayer from "../../conversation/UnifiedApprovalLayer.vue";
import { useApprovalsStore } from "../../../stores/approvals";
import { useUnifiedApprovalsStore } from "../../../stores/unifiedApprovals";

const LEGACY_PAYLOAD: Record<string, unknown> = {
  name: "sub",
  tool_type: "subagent",
  description: "创建一个子 agent 型工具",
  subagent_budget: { max_iterations: 4, max_tokens: 8000, output_limit_chars: 700 },
};

let wrappers: VueWrapper[] = [];

function setup() {
  localStorage.clear();
  setActivePinia(createPinia());
  return { approvals: useApprovalsStore(), unified: useUnifiedApprovalsStore() };
}

beforeEach(() => {
  for (const fn of Object.values(mocks)) fn.mockReset();
  mocks.request.mockResolvedValue({ items: [], total: 0 });
  mocks.respondApproval.mockResolvedValue({ ok: true });
});

afterEach(() => {
  for (const w of wrappers) {
    try { w.unmount(); } catch { /* 已卸载 */ }
  }
  wrappers = [];
  vi.unstubAllGlobals();
});

describe("legacy 中断项：只读，不给可点击的允许/拒绝", () => {
  it("显示「未执行 / 中断待核对」，只有「知道了」，且不显示伪造状态机", async () => {
    const { approvals, unified } = setup();
    approvals.enqueue("ap_legacy", "tool_create", LEGACY_PAYLOAD, {
      autoOpen: true,
      meta: { legacy: true, status: "interrupted_legacy", label: "创建子 agent" },
    });
    unified.open("tool:ap_legacy");
    const w = mount(UnifiedApprovalModal);
    wrappers.push(w);
    await flushPromises();

    const text = w.text();
    expect(text).toContain("未执行");
    expect(text).toContain("中断待核对");
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(false);
    expect(w.find('[data-test="unified-approval-reject"]').exists()).toBe(false);
    expect(w.find('[data-test="unified-approval-confirm-next"]').exists()).toBe(false);
    expect(w.find('[data-test="unified-approval-legacy-ack"]').exists()).toBe(true);

    await w.find('[data-test="unified-approval-legacy-ack"]').trigger("click");
    expect(approvals.queue).toHaveLength(0);
  });

  it("legacy 普通等待项：批准只走 /respond，不渲染伪造的 v2 状态行", async () => {
    const { approvals, unified } = setup();
    approvals.enqueue("ap_legacy", "tool_create", LEGACY_PAYLOAD, {
      autoOpen: true,
      meta: { legacy: true },
    });
    unified.open("tool:ap_legacy");
    const w = mount(UnifiedApprovalModal);
    wrappers.push(w);
    await flushPromises();

    expect(w.find('[data-test="unified-approval-state"]').exists()).toBe(false);
    await w.find('[data-test="unified-approval-allow"]').trigger("click");
    await flushPromises();
    expect(mocks.respondApproval).toHaveBeenCalledWith("ap_legacy", "approved", undefined, expect.anything());
    expect(mocks.decideApproval).not.toHaveBeenCalled();
  });
});

describe("回到前台：主动补读 pending + history", () => {
  it("visibilitychange(visible) → 补读两个接口（读取失败在面板可见）", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.includes("/api/approvals/pending")
        ? { approvals: [] }
        : url.includes("/api/approvals/history")
          ? { history: [] }
          : { items: [], total: 0 };
      return { ok: true, status: 200, json: async () => body, text: async () => "" };
    });
    vi.stubGlobal("fetch", fetchMock);
    setActivePinia(createPinia());
    const w = mount(UnifiedApprovalLayer, { props: { showMemory: false } });
    wrappers.push(w);
    await flushPromises();
    await nextTick();
    fetchMock.mockClear();

    document.dispatchEvent(new Event("visibilitychange"));
    await flushPromises();

    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls.some((url) => url.includes("/api/approvals/pending"))).toBe(true);
    expect(urls.some((url) => url.includes("/api/approvals/history"))).toBe(true);
  });
});
