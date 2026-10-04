/**
 * 放弃开发时被作废的确认，必须在**收到 `APPROVAL_RESULT` 的那一刻**从界面消失：
 * 不刷新页面、不等任何超时、也不需要用户再点一次「知道了」。
 *
 * 这一轮修的后端缺口：lifecycle 创建 `credential_grant` 审批时载荷没有任务标识，
 * `ApprovalService.invalidate_for_task()` 找不到它 —— 后端已经放弃任务了，界面上
 * 那张凭据确认卡却还挂着等人点「允许」。后端补齐载荷（`workspace`）之后，
 * 事件会真的发到前端；这里先把**前端这一侧**的行为锁住：
 *
 * 1. 事件按 `approval_id` 收敛（`stores/events.ts` → `approvals.resolve(id)`）；
 * 2. 队列只剩一项时少一项而不是清空；队列空了 `deferred` 复位；
 * 3. 弹窗与入口条随之消失，且不依赖刷新。
 *
 * 全部走真实 store + 真实事件路由（`useEventStore().route()` 就是生产里 SSE 的
 * 落地路径），只有 API 层是替身：用例同时断言**前端没有替用户做任何决定**
 * （没有发出 approve/reject 请求）。
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { nextTick } from "vue";
import ApprovalModal from "../../components/ApprovalModal.vue";
import ApprovalEntry from "../../components/ApprovalEntry.vue";
import { useApprovalsStore } from "../approvals";
import { useEventStore } from "../events";
import type { AgentEvent } from "../../services/events";
import { useSessionStore } from "../session";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: { respondApproval: vi.fn(async () => ({ ok: true })) },
}));

const respond = api.respondApproval as unknown as ReturnType<typeof vi.fn>;

function setup(): {
  pinia: Pinia;
  approvals: ReturnType<typeof useApprovalsStore>;
  events: ReturnType<typeof useEventStore>;
} {
  const pinia = createPinia();
  setActivePinia(pinia);
  useSessionStore(); // 事件路由要用到会话（审批归属/继续条）
  return { pinia, approvals: useApprovalsStore(), events: useEventStore() };
}

/** 后端放弃任务后发出的那一条事件（`approval.py::_publish_result` 的形状）。 */
function routeCancelled(events: ReturnType<typeof useEventStore>, approvalId: string) {
  events.route({
    type: "APPROVAL_RESULT",
    id: "ev_1",
    ts: "",
    data: {
      approval_id: approvalId,
      decision: "cancelled",
      reason: "task_abandoned",
      turn_id: "turn_main",
    },
  } as unknown as AgentEvent);
}

beforeEach(() => {
  vi.clearAllMocks();
  respond.mockResolvedValue({ ok: true });
  document.body.innerHTML = "";
});

afterEach(() => {
  delete document.documentElement.dataset.motion;
});

describe("作废事件（APPROVAL_RESULT{cancelled}）让界面立即收敛", () => {
  it("只移除被作废的那一项：其余待办不受影响，队列空了 deferred 复位", () => {
    const { approvals, events } = setup();
    approvals.enqueue(
      "appr_cred",
      "credential_grant",
      { key_id: "weather-key", tool_name: "weather_fetch", workspace: "ws_1" },
      { autoOpen: false },
    );
    approvals.enqueue("appr_create", "tool_create", { name: "t" }, { autoOpen: false });
    expect(approvals.pendingCount).toBe(2);
    expect(approvals.visible).toBe(false); // 用户按过「稍后处理」

    routeCancelled(events, "appr_cred");

    // 同步收敛：事件处理完的这一拍就已经生效，中间没有任何 await
    expect(approvals.queue.map((a) => a.approval_id)).toEqual(["appr_create"]);
    expect(approvals.pendingCount).toBe(1);
    expect(approvals.deferred).toBe(true); // 还有别的待办，用户的选择保留

    routeCancelled(events, "appr_create");

    expect(approvals.queue).toEqual([]);
    expect(approvals.deferred).toBe(false); // 队列空了必须复位，否则下次审批会被静默藏起来
    expect(approvals.visible).toBe(false);
    // 前端没有替用户做任何决定：作废是后端的事实，不是「点了拒绝」
    expect(respond).not.toHaveBeenCalled();
  });

  it("作废别的审批不影响本项（按 id 收敛是幂等的）", () => {
    const { approvals, events } = setup();
    approvals.enqueue("appr_cred", "credential_grant", { workspace: "ws_1" });

    routeCancelled(events, "appr_not_mine");
    routeCancelled(events, "");

    expect(approvals.queue.map((a) => a.approval_id)).toEqual(["appr_cred"]);
    expect(approvals.current?.approval_id).toBe("appr_cred");
  });

  it("弹窗与入口条随之消失：不等超时、不刷新（减少动画时当场卸载 DOM）", async () => {
    // 减少动画：退出动画被跳过，卸载时机完全由状态决定 —— 这样断言的就是
    // 「状态收敛」本身，而不是那个 150ms 的淡出。
    document.documentElement.dataset.motion = "reduced";
    const { pinia, approvals, events } = setup();
    const entry = mount(ApprovalEntry, { attachTo: document.body, global: { plugins: [pinia] } });
    const modal = mount(ApprovalModal, { attachTo: document.body, global: { plugins: [pinia] } });

    approvals.enqueue(
      "appr_cred",
      "credential_grant",
      { key_id: "weather-key", tool_name: "weather_fetch", workspace: "ws_1" },
      { autoOpen: true },
    );
    await flushPromises();
    expect(modal.find(".modal-mask").exists()).toBe(true);
    expect(entry.find(".approval-entry").exists()).toBe(false);

    routeCancelled(events, "appr_cred");
    await nextTick();

    expect(approvals.pendingCount).toBe(0);
    expect(modal.find(".modal-mask").exists()).toBe(false);
    expect(entry.find(".approval-entry").exists()).toBe(false);
    expect(respond).not.toHaveBeenCalled();
  });

  it("「稍后处理」收起的弹窗：作废后入口条立即消失，不留下看不见的待办", async () => {
    document.documentElement.dataset.motion = "reduced";
    const { pinia, approvals, events } = setup();
    const entry = mount(ApprovalEntry, { attachTo: document.body, global: { plugins: [pinia] } });
    const modal = mount(ApprovalModal, { attachTo: document.body, global: { plugins: [pinia] } });

    approvals.enqueue(
      "appr_cred",
      "credential_grant",
      { workspace: "ws_1" },
      { autoOpen: false },
    );
    await flushPromises();
    expect(entry.find(".approval-entry").exists()).toBe(true);
    expect(modal.find(".modal-mask").exists()).toBe(false);

    routeCancelled(events, "appr_cred");
    await nextTick();

    expect(entry.find(".approval-entry").exists()).toBe(false);
    expect(approvals.pendingCount).toBe(0);
  });

  it("默认动画下也只需一个 150ms 淡出：动画结束就从 DOM 移除，无需刷新", async () => {
    const { pinia, approvals, events } = setup();
    const modal = mount(ApprovalModal, { attachTo: document.body, global: { plugins: [pinia] } });
    approvals.enqueue("appr_cred", "credential_grant", { workspace: "ws_1" });
    await flushPromises();
    expect(modal.find(".modal-mask").exists()).toBe(true);

    routeCancelled(events, "appr_cred");
    await nextTick();

    // 状态已经收敛（遮罩进入退出动画，只是为了好看，不是「还没消失」）
    expect(approvals.visible).toBe(false);
    await new Promise((r) => setTimeout(r, 220));
    await nextTick();
    expect(modal.find(".modal-mask").exists()).toBe(false);
  });
});
