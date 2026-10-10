/**
 * F08 回归（界面层）：旧入口 InterruptedTurnEntry 与新入口 RecoveryInbox
 * 必须共用一份恢复状态 —— 同一条记录不在两边重复出现，处理掉之后两边都不残留，
 * 处理掉最后一条不出现虚假的「还有 N 条未显示」。
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
import InterruptedTurnEntry from "../InterruptedTurnEntry.vue";
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
  it("收件箱覆盖的记录只出现一次；在新入口忽略后旧入口不残留", async () => {
    const { pinia, session } = setup();
    session.applyInterruptedState([], [TURN_A], []);
    recoveryFns.fetchRecoveryRecords.mockResolvedValue(listing([record("turn_a")], { total: 1 }));
    await session.loadRecoveryInbox();

    const inbox = mount(RecoveryInbox, { global: { plugins: [pinia] } });
    const entry = mount(InterruptedTurnEntry, { global: { plugins: [pinia] } });
    await flushPromises();

    // 记录只由新入口提供：旧入口不再重复一条同样的操作入口
    expect(inbox.findAll(".item")).toHaveLength(1);
    expect(entry.find(".interrupted-turns").exists()).toBe(false);

    await inbox.find("button.btn-ignore").trigger("click");
    await flushPromises();

    expect(inbox.find(".item").exists()).toBe(false);
    expect(entry.find(".interrupted-turns").exists()).toBe(false);
    expect(session.interruptedTurns).toEqual([]);
    expect(session.recoveryRecords).toEqual([]);
    expect(session.recoveryTotal).toBe(0);

    // 旧快照回来也不复活
    session.applyInterruptedState([], [TURN_A], []);
    await flushPromises();
    expect(entry.find(".interrupted-turns").exists()).toBe(false);

    inbox.unmount();
    entry.unmount();
  });

  it("收件箱拉不到时旧入口兜底显示；在旧入口忽略后收件箱也不残留", async () => {
    const { pinia, session } = setup();
    recoveryFns.fetchRecoveryRecords.mockRejectedValue(new Error("network down"));
    session.applyInterruptedState([], [TURN_A], []);
    await session.loadRecoveryInbox();

    const inbox = mount(RecoveryInbox, { global: { plugins: [pinia] } });
    const entry = mount(InterruptedTurnEntry, { global: { plugins: [pinia] } });
    await flushPromises();

    // 专用接口挂了：孤儿 / 中断消息仍然由旧入口看得见，不能消失
    expect(entry.find(".interrupted-turns").exists()).toBe(true);
    expect(inbox.find(".item").exists()).toBe(false);

    await entry.find(".entry").trigger("click");
    await entry.find("button.btn-dismiss").trigger("click");
    await flushPromises();

    expect(session.interruptedTurns).toEqual([]);
    expect(session.recoveryRecords).toEqual([]);
    // 旧入口不再有可操作的那一条（只留下「已忽略」的结果说明）
    expect(entry.find(".entry").exists()).toBe(false);
    expect(entry.find(".notice").text()).toContain("已忽略");
    expect(inbox.find(".item").exists()).toBe(false);

    inbox.unmount();
    entry.unmount();
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
