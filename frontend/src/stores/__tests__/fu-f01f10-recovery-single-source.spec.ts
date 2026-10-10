/**
 * F08 回归：**旧入口（InterruptedTurnEntry）与新入口（RecoveryInbox）必须共用一份恢复状态**。
 *
 * 原缺陷：
 * * 同一条 interrupted 消息可以同时出现在 `interruptedTurns` 与 `recoveryRecords` 两个集合里，
 *   两个入口对同一条记录重复提供操作；
 * * 在新入口忽略只更新新的 resolved 集合，应用旧快照又会让旧入口复活该消息；
 * * 成功移除最后一条记录后 `recoveryTotal` 仍是 1，界面显示虚假的「还有 N 条未显示」。
 *
 * 这里断言：
 * 1. 收件箱拉回来后，同一条记录不再由旧入口重复提供；旧入口在收件箱拉不到时仍然兜底；
 * 2. 任一入口处理后，另一入口不残留；旧快照不复活；
 * 3. 处理掉最后一条后没有虚假的「未显示」提示；真实多页 total 仍然准确；
 * 4. 「修好」不是「处理掉」：记录与 total 都保持。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const apiFns = vi.hoisted(() => ({
  getSessionContext: vi.fn(async () => ({ topic_id: "", messages: [] })),
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

import { useSessionStore } from "../session";
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

function record(recordId: string, overrides: Partial<RecoveryRecordView> = {}): RecoveryRecordView {
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
    ...overrides,
  };
}

function listing(records: RecoveryRecordView[], extra: Record<string, unknown> = {}) {
  return { records, total: records.length, shown: records.length, truncated: false, ...extra };
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return useSessionStore();
}

beforeEach(() => {
  vi.clearAllMocks();
  apiFns.resendInterruptedTurn.mockResolvedValue({
    ok: true,
    recovered_turn_id: "turn_a",
    turn_id: "turn_new",
    status: "queued",
  });
  apiFns.dismissInterruptedTurn.mockResolvedValue({ ok: true, dismissed: "turn_a" });
  recoveryFns.fetchRecoveryRecords.mockResolvedValue(listing([]));
  recoveryFns.continueRecovery.mockResolvedValue({
    ok: true,
    record_id: "turn_a",
    turn_id: "turn_new",
    status: "queued",
  });
  recoveryFns.ignoreRecovery.mockResolvedValue({ ok: true, ignored: true });
  recoveryFns.repairOrphan.mockResolvedValue({ ok: true, repaired: true, record_id: "turn_x" });
});

describe("F08 单一恢复状态来源", () => {
  it("收件箱拉回来后同一条不再由旧入口重复提供；收件箱拉不到时旧入口兜底", async () => {
    const session = setup();

    // 快照先到、收件箱还没拉到：旧入口兜底显示
    session.applyInterruptedState([], [TURN_A], []);
    expect(session.interruptedTurns.map((t) => t.turn_id)).toEqual(["turn_a"]);

    // 收件箱拉回同一条：新入口独家提供，旧入口不再重复
    recoveryFns.fetchRecoveryRecords.mockResolvedValue(listing([record("turn_a")], { total: 5 }));
    await session.loadRecoveryInbox();

    expect(session.recoveryRecords.map((r) => r.record_id)).toEqual(["turn_a"]);
    expect(session.recoveryTotal).toBe(5);
    expect(session.interruptedTurns).toEqual([]);
  });

  it("在新入口忽略后，应用旧快照也不会让旧入口复活", async () => {
    const session = setup();
    session.applyInterruptedState([], [TURN_A], []);
    recoveryFns.fetchRecoveryRecords.mockResolvedValue(listing([record("turn_a")], { total: 1 }));
    await session.loadRecoveryInbox();
    expect(session.interruptedTurns).toEqual([]);

    const res = await session.ignoreRecovery("turn_a");
    expect(res.ok).toBe(true);
    expect(session.recoveryRecords).toEqual([]);
    expect(session.recoveryTotal).toBe(0);

    // 旧快照又来一遍：两个入口都不得复活它
    session.applyInterruptedState([], [TURN_A], []);
    expect(session.interruptedTurns).toEqual([]);
    expect(session.recoveryRecords).toEqual([]);
  });

  it("从旧入口忽略后，收件箱同一条也不残留（列表 + total 一起收敛）", async () => {
    const session = setup();
    // 旧入口已经持有这条（快照给的）
    session.applyInterruptedState([], [TURN_A], []);
    expect(session.interruptedTurns.map((t) => t.turn_id)).toEqual(["turn_a"]);
    // 收件箱也持有同一条（模拟缺陷留下的「两个集合都有」状态，不触发派生去重）
    session.recoveryRecords = [record("turn_a")];
    session.recoveryTotal = 1;

    const res = await session.dismissInterruptedTurn("turn_a");

    expect(res.ok).toBe(true);
    expect(session.interruptedTurns).toEqual([]);
    expect(session.recoveryRecords).toEqual([]);
    expect(session.recoveryTotal).toBe(0);

    // 旧快照不许复活
    session.applyInterruptedState([], [TURN_A], []);
    expect(session.interruptedTurns).toEqual([]);
  });

  it("从旧入口继续成功后同样同步收件箱与 total", async () => {
    const session = setup();
    session.applyInterruptedState([], [TURN_A], []);
    session.recoveryRecords = [record("turn_a")];
    session.recoveryTotal = 4;

    const res = await session.resumeInterruptedTurn("turn_a");

    expect(res.ok).toBe(true);
    expect(session.interruptedTurns).toEqual([]);
    expect(session.recoveryRecords).toEqual([]);
    // 有依据地减一：不重读清单，也不把未知数量猜成 0
    expect(session.recoveryTotal).toBe(3);
  });

  it("处理掉最后一条后没有虚假的「还有 N 条未显示」", async () => {
    const session = setup();
    recoveryFns.fetchRecoveryRecords.mockResolvedValue(listing([record("turn_a")], { total: 1 }));
    await session.loadRecoveryInbox();
    expect(session.recoveryTotal).toBe(1);

    const res = await session.continueRecovery("turn_a");
    expect(res.ok).toBe(true);
    expect(session.recoveryRecords).toEqual([]);
    expect(session.recoveryTotal).toBe(0);
    // 界面上的 hiddenCount = max(0, total - count) 因此是 0：不会出现「还有 1 条未显示」
    expect(Math.max(0, session.recoveryTotal - session.recoveryRecords.length)).toBe(0);
  });

  it("真实多页 total 仍然准确：只减掉刚处理掉的那一条，截断提示保留", async () => {
    const session = setup();
    recoveryFns.fetchRecoveryRecords.mockResolvedValue(
      listing([record("turn_a"), record("turn_b")], { total: 7, shown: 2, truncated: true }),
    );
    await session.loadRecoveryInbox();
    expect(session.recoveryTotal).toBe(7);

    const res = await session.ignoreRecovery("turn_a");
    expect(res.ok).toBe(true);
    expect(session.recoveryRecords.map((r) => r.record_id)).toEqual(["turn_b"]);
    expect(session.recoveryTotal).toBe(6);
    // 还有 5 条确实没显示：截断提示不能被改小成 0
    expect(Math.max(0, session.recoveryTotal - session.recoveryRecords.length)).toBe(5);

    // 服务端给的权威 total 会重新覆盖本地推断
    recoveryFns.fetchRecoveryRecords.mockResolvedValue(
      listing([record("turn_b")], { total: 4, shown: 1, truncated: true }),
    );
    await session.loadRecoveryInbox();
    expect(session.recoveryTotal).toBe(4);
  });

  it("「修好」不是「处理掉」：记录与 total 都保留", async () => {
    const session = setup();
    const orphan = record("turn_x", {
      state_class: "orphaned_claim",
      actions: [
        { id: "repair", label: "修好这条记录", enabled: true, reason: "" },
      ],
    });
    recoveryFns.fetchRecoveryRecords.mockResolvedValue(listing([orphan], { total: 2 }));
    await session.loadRecoveryInbox();

    const res = await session.repairOrphan("turn_x");

    expect(res.ok).toBe(true);
    expect(session.recoveryRecords.map((r) => r.record_id)).toEqual(["turn_x"]);
    expect(session.recoveryRecords[0]?.state_class).toBe("ready");
    expect(session.recoveryTotal).toBe(2);
  });
});
