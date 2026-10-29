/**
 * F08 回归（界面层）：新旧两个恢复入口必须共用一份恢复状态 ——
 * 收件箱（RecoveryInbox）与「发送异常与草稿」面板（SendIssuesPanel，入口①）里
 * 的「上次没有执行的消息」是同一批 interrupted 记录：同一条不在两边重复出现，
 * 处理掉之后两边都不残留，处理掉最后一条不出现虚假的「还有 N 条未显示」。
 *
 * 2026-10-26（合并后清理 · C1）：旧的常驻入口 InterruptedTurnEntry.vue 已删除，
 * 它的有效能力（继续 / 忽略 / 全部忽略 / 结果说明 / 收件箱拉不到时的兜底）由
 * components/conversation/SendIssuesPanel.vue 承接。本文件按新的真实入口断言，
 * 负向断言（不重复入口、不复活、不虚假剩余条数）一条不少。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const apiFns = vi.hoisted(() => ({
  getRuntimeState: vi.fn(),
  getDevTasks: vi.fn(async () => ({ tasks: [] })),
  listDevAuthorizations: vi.fn(async () => ({ authorizations: [] })),
  resendInterruptedTurn: vi.fn(),
  dismissInterruptedTurn: vi.fn(),
}));

const recoveryFns = vi.hoisted(() => ({
  fetchRecoveryRecords: vi.fn(),
  continueRecovery: vi.fn(),
  repairOrphan: vi.fn(),
  ignoreRecovery: vi.fn(),
  requeueDerived: vi.fn(),
}));

vi.mock("../../services/api", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly path: string,
      detail: string,
    ) {
      super(detail);
      this.name = "ApiError";
    }
  },
  api: apiFns,
}));

vi.mock("../../services/recoveryApi", () => ({
  fetchRecoveryRecords: (...a: unknown[]) => recoveryFns.fetchRecoveryRecords(...a),
  continueRecovery: (...a: unknown[]) => recoveryFns.continueRecovery(...a),
  repairOrphan: (...a: unknown[]) => recoveryFns.repairOrphan(...a),
  ignoreRecovery: (...a: unknown[]) => recoveryFns.ignoreRecovery(...a),
  requeueDerived: (...a: unknown[]) => recoveryFns.requeueDerived(...a),
  isRecoveryConflict: (e: unknown) => (e as { status?: number })?.status === 409,
}));

import RecoveryInbox from "../RecoveryInbox.vue";
import SendIssuesPanel from "../conversation/SendIssuesPanel.vue";
import { useSessionStore } from "../../stores/session";
import type { RecoveryRecordView } from "../../services/recoveryApi";
import type { InterruptedTurn } from "../../services/api";

const TURN_A: InterruptedTurn = {
  turn_id: "turn_a",
  message: "帮我把发布闸门跑一遍",
  topic_id: "topic_1",
  status: "interrupted",
  reason: "queued_at_restart",
  reason_text: "这条消息当时还在排队，进程退出后没有开始执行",
  created_at: "2026-10-10T10:00:00+00:00",
  ended_at: "2026-10-10T10:05:00+00:00",
};

function record(recordId: string): RecoveryRecordView {
  return {
    record_id: recordId,
    kind: "user_turn",
    state_class: "ready",
    status: "interrupted",
    message: "帮我把发布闸门跑一遍",
    topic_id: "topic_1",
    reason: "queued_at_restart",
    created_at: "2026-10-10T10:00:00+00:00",
    updated_at: "2026-10-10T10:05:00+00:00",
    owner_instance_id: null,
    owner_state: "none",
    owner_note: "这条消息在上次退出时没有开始执行",
    claim_generation: null,
    attempts: null,
    last_error: null,
    actions: [
      { id: "continue", label: "继续发送这条", enabled: true, reason: "" },
      { id: "ignore", label: "忽略", enabled: true, reason: "" },
    ],
  };
}

function listing(records: RecoveryRecordView[], extra: Record<string, unknown> = {}) {
  return { records, total: records.length, shown: records.length, truncated: false, ...extra };
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { pinia, session: useSessionStore() };
}

beforeEach(() => {
  vi.clearAllMocks();
  document.body.innerHTML = "";
  apiFns.dismissInterruptedTurn.mockResolvedValue({ ok: true, dismissed: "turn_a" });
  apiFns.resendInterruptedTurn.mockResolvedValue({
    ok: true,
    recovered_turn_id: "turn_a",
    turn_id: "turn_new",
    status: "queued",
  });
  recoveryFns.fetchRecoveryRecords.mockResolvedValue(listing([]));
  recoveryFns.ignoreRecovery.mockResolvedValue({ ok: true, ignored: true });
  recoveryFns.continueRecovery.mockResolvedValue({
    ok: true,
    record_id: "turn_a",
    turn_id: "turn_new",
    status: "queued",
  });
});

describe("F08 两个恢复入口共用一份状态", () => {
  it("收件箱覆盖的记录只出现一次；发送异常面板不重复同一条", async () => {
    const { pinia, session } = setup();
    session.applyInterruptedState([], [TURN_A], []);
    recoveryFns.fetchRecoveryRecords.mockResolvedValue(listing([record("turn_a")], { total: 1 }));
    await session.loadRecoveryInbox();

    const inbox = mount(RecoveryInbox, { global: { plugins: [pinia] } });
    const issues = mount(SendIssuesPanel, { global: { plugins: [pinia] } });
    await flushPromises();

    // 记录只由收件箱提供：发送异常面板不再重复一条同样的操作入口
    expect(inbox.findAll(".item")).toHaveLength(1);
    expect(issues.find('[data-section="interrupted"]').exists()).toBe(false);

    await inbox.find("button.btn-ignore").trigger("click");
    await flushPromises();

    expect(inbox.find(".item").exists()).toBe(false);
    expect(issues.find('[data-section="interrupted"]').exists()).toBe(false);
    expect(session.interruptedTurns).toEqual([]);
    expect(session.recoveryRecords).toEqual([]);
    expect(session.recoveryTotal).toBe(0);

    // 旧快照回来也不复活
    session.applyInterruptedState([], [TURN_A], []);
    await flushPromises();
    expect(issues.find('[data-section="interrupted"]').exists()).toBe(false);

    inbox.unmount();
    issues.unmount();
  });

  it("收件箱接口拉不到时，发送异常面板兜底显示中断消息；处理后面板与收件箱都不残留", async () => {
    const { pinia, session } = setup();
    recoveryFns.fetchRecoveryRecords.mockRejectedValue(new Error("network down"));
    session.applyInterruptedState([], [TURN_A], []);
    await session.loadRecoveryInbox();

    const inbox = mount(RecoveryInbox, { global: { plugins: [pinia] } });
    const issues = mount(SendIssuesPanel, { global: { plugins: [pinia] } });
    await flushPromises();

    // 专用收件箱接口挂了：中断消息仍然由发送异常面板看得见，不能消失
    const section = issues.find('[data-section="interrupted"]');
    expect(section.exists()).toBe(true);
    expect(section.text()).toContain("帮我把发布闸门跑一遍");
    expect(inbox.find(".item").exists()).toBe(false);

    await section.find("button.qio-btn.mini.quiet").trigger("click");
    await flushPromises();

    expect(session.interruptedTurns).toEqual([]);
    expect(session.recoveryRecords).toEqual([]);
    // 列表清空之后结果说明仍然留在屏幕上
    expect(issues.find(".notice").text()).toContain("已忽略");
    expect(issues.find('[data-section="interrupted"]').exists()).toBe(false);
    expect(inbox.find(".item").exists()).toBe(false);

    inbox.unmount();
    issues.unmount();
  });

  it("处理掉最后一条后，界面上不再有虚假的「还有 N 条未显示」", async () => {
    const { pinia, session } = setup();
    recoveryFns.fetchRecoveryRecords.mockResolvedValue(listing([record("turn_a")], { total: 1 }));
    await session.loadRecoveryInbox();

    const inbox = mount(RecoveryInbox, { global: { plugins: [pinia] } });
    await flushPromises();
    expect(inbox.find(".truncated").exists()).toBe(false);

    await inbox.find("button.btn-ignore").trigger("click");
    await flushPromises();

    expect(session.recoveryTotal).toBe(0);
    // 没有虚假的「还有 N 条未显示」
    expect(inbox.find(".truncated").exists()).toBe(false);

    inbox.unmount();
  });

  it("真实多页截断仍然如实提示剩余条数", async () => {
    const { pinia, session } = setup();
    recoveryFns.fetchRecoveryRecords.mockResolvedValue(
      listing([record("turn_a"), record("turn_b")], { total: 7, shown: 2, truncated: true }),
    );
    await session.loadRecoveryInbox();

    const inbox = mount(RecoveryInbox, { global: { plugins: [pinia] } });
    await flushPromises();
    expect(inbox.find(".truncated").text()).toContain("还有 5 条未显示");

    await inbox.findAll("button.btn-ignore")[0]!.trigger("click");
    await flushPromises();

    // 处理掉一条之后：真实 total 只减一，截断提示仍然真实
    expect(session.recoveryTotal).toBe(6);
    expect(inbox.find(".truncated").text()).toContain("还有 5 条未显示");

    inbox.unmount();
  });
});
