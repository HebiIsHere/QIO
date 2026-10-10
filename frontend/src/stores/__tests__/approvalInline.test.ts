/**
 * 内联审批的门控与幂等（契约 §1.5 + Lead 裁决 3）：
 *
 * * 内联卡显示时，全局 ApprovalEntry 不再对**同一个 approval_id** 显示按钮；
 * * respondById 按 approval_id 幂等：重复点击只发一次请求，也绝不误伤下一项；
 * * 非当前轮 / 没有绑定轮的审批仍走全局入口。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import ApprovalEntry from "../../components/ApprovalEntry.vue";
import { useApprovalsStore } from "../approvals";

const { respondApproval } = vi.hoisted(() => ({ respondApproval: vi.fn() }));

vi.mock("../../services/api", () => ({
  api: { respondApproval: (...args: unknown[]) => respondApproval(...args) },
  ApiError: class ApiError extends Error {},
}));

beforeEach(() => {
  respondApproval.mockReset();
});

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { pinia, approvals: useApprovalsStore() };
}

describe("同一时刻只允许一套按钮（按 approval_id 门控）", () => {
  it("内联声明生效时，全局入口不再显示按钮；释放后恢复", async () => {
    const { pinia, approvals } = setup();
    approvals.enqueue("ap_1", "tool_execution", {}, { autoOpen: false });
    const w = mount(ApprovalEntry, { global: { plugins: [pinia] } });
    await nextTick();
    expect(w.find(".approval-entry").exists()).toBe(true);

    approvals.claimInline("ap_1");
    await nextTick();
    expect(w.find(".approval-entry").exists()).toBe(false);

    approvals.releaseInline("ap_1");
    await nextTick();
    expect(w.find(".approval-entry").exists()).toBe(true);
    w.unmount();
  });

  it("内联声明只对同一条审批生效（不是「有审批就藏」）", async () => {
    const { pinia, approvals } = setup();
    approvals.enqueue("ap_1", "tool_execution", {}, { autoOpen: false });
    approvals.claimInline("ap_other");
    const w = mount(ApprovalEntry, { global: { plugins: [pinia] } });
    await nextTick();
    expect(w.find(".approval-entry").exists()).toBe(true);
    w.unmount();
  });
});

describe("respondById：按 id 幂等，不误伤下一项", () => {
  it("重复点击只发一次请求", async () => {
    const { approvals } = setup();
    let release!: (value: unknown) => void;
    respondApproval.mockImplementation(
      () => new Promise((resolve) => { release = resolve; }),
    );
    approvals.enqueue("ap_1", "tool_execution", {}, { autoOpen: false });

    const first = approvals.respondById("ap_1", "approved");
    const second = approvals.respondById("ap_1", "approved");
    expect(respondApproval).toHaveBeenCalledTimes(1);

    release({ ok: true });
    await first;
    await second;
    expect(approvals.queue).toHaveLength(0);
  });

  it("只应答指定 id：队列里的下一项不动", async () => {
    const { approvals } = setup();
    respondApproval.mockResolvedValue({ ok: true });
    approvals.enqueue("ap_1", "tool_execution", {}, { autoOpen: false });
    approvals.enqueue("ap_2", "tool_execution", {}, { autoOpen: false });

    await approvals.respondById("ap_2", "rejected");

    expect(respondApproval).toHaveBeenCalledWith("ap_2", "rejected", undefined, expect.anything());
    expect(approvals.queue.map((a) => a.approval_id)).toEqual(["ap_1"]);
  });

  it("已经不存在的 id：不发请求（绝不会打到别人身上）", async () => {
    const { approvals } = setup();
    await approvals.respondById("ap_gone", "approved");
    expect(respondApproval).not.toHaveBeenCalled();
  });
});
