/**
 * 独立验证 v2（契约 B4 / D3）：审批作废后界面**立刻**收敛。
 *
 * 要求是「不依赖刷新、不依赖超时」：`APPROVAL_RESULT` 事件一到，弹窗与入口条都要立刻消失。
 * 所以这里的断言全部是**同步**的 —— 事件分发之后马上检查 store 状态，中间没有 await、
 * 没有 fake timers、没有 flushPromises。只要实现变成「等超时」「等下一次轮询」，
 * 这些用例就会红。
 *
 * 这是验证方自己的文件（`approvalInvalidation.verify.test.ts`），
 * 不覆盖 approval-dev 的 `approvalInvalidation.test.ts`。
 *
 * 弹窗 / 入口条与 store 的对应关系（见 `frontend/src/components/ApprovalModal.vue`
 * 与 `ApprovalEntry.vue`）：
 *   * 弹窗渲染条件 ≈ `current !== null && visible`；
 *   * 入口条渲染条件 ≈ `pendingCount > 0 && !visible`。
 * 两条路径都覆盖，才能真正说「卡片自己消失」。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useApprovalsStore } from "../approvals";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";

vi.mock("../../services/api", () => ({
  api: {
    getRuntimeState: vi.fn(async () => ({})),
    respondApproval: vi.fn(async () => ({ ok: true })),
  },
}));

function resultEvent(approvalId: string, decision = "cancelled", reason = "task_abandoned") {
  return {
    type: "APPROVAL_RESULT",
    instance_id: "inst_verify",
    revision: 7,
    ts: "2026-10-04T00:00:00.000Z",
    data: { approval_id: approvalId, decision, reason },
  } as never;
}

beforeEach(() => {
  setActivePinia(createPinia());
});

describe("审批作废 → 卡片立刻消失（契约 B4/D3）", () => {
  it("弹窗形态：事件一到就消失，不等刷新也不等超时", () => {
    const approvals = useApprovalsStore();
    const events = useEventStore();
    approvals.enqueue("appr_1", "credential_grant", { workspace: "ws_1" });
    expect(approvals.pendingCount).toBe(1);
    expect(approvals.visible).toBe(true);
    expect(approvals.current?.approval_id).toBe("appr_1");

    events.dispatch(resultEvent("appr_1"));

    // 同步断言：这里没有任何 await / 定时器
    expect(approvals.pendingCount).toBe(0);
    expect(approvals.visible).toBe(false);
    expect(approvals.current).toBeNull();
  });

  it("入口条形态（「稍后处理」或 RESYNC 恢复出来的）：同样立刻消失", () => {
    const approvals = useApprovalsStore();
    const events = useEventStore();
    // RESYNC 恢复出来的审批 autoOpen=false：只亮入口条，不抢焦点
    approvals.enqueue("appr_2", "credential_grant", { workspace: "ws_2" }, { autoOpen: false });
    expect(approvals.pendingCount).toBe(1);
    expect(approvals.visible).toBe(false); // 入口条形态

    events.dispatch(resultEvent("appr_2"));

    expect(approvals.pendingCount).toBe(0);
    expect(approvals.visible).toBe(false);
    expect(approvals.current).toBeNull();
  });

  it("只收敛被作废的那一条，别的待审批不受影响", () => {
    const approvals = useApprovalsStore();
    const events = useEventStore();
    approvals.enqueue("appr_a", "credential_grant", { workspace: "ws_a" });
    approvals.enqueue("appr_b", "tool_execution", { workspace: "ws_b" });

    events.dispatch(resultEvent("appr_a"));

    expect(approvals.pendingCount).toBe(1);
    expect(approvals.current?.approval_id).toBe("appr_b");
    expect(approvals.queue.some((item) => item.approval_id === "appr_a")).toBe(false);
  });

  it("幂等：重复事件与未知 id 都不报错、也不会误删别的条目", () => {
    const approvals = useApprovalsStore();
    const events = useEventStore();
    approvals.enqueue("appr_c", "credential_grant", { workspace: "ws_c" });

    events.dispatch(resultEvent("appr_unknown"));
    expect(approvals.pendingCount).toBe(1);
    events.dispatch(resultEvent("appr_c"));
    events.dispatch(resultEvent("appr_c")); // 重复
    expect(approvals.pendingCount).toBe(0);
  });

  it("「继续开发」那一类待办也一起被清掉（同一 id 的会话状态）", () => {
    const approvals = useApprovalsStore();
    const session = useSessionStore();
    const events = useEventStore();
    approvals.enqueue("appr_d", "tool_execution", { workspace: "ws_d" });
    session.pendingContinue = { id: "appr_d" } as never;

    events.dispatch(resultEvent("appr_d"));

    expect(approvals.pendingCount).toBe(0);
    expect(session.pendingContinue).toBeNull();
  });
});
