/**
 * 收件箱 store（A01 + A03）：用户可操作、成功就地更新、失败可重试。
 *
 * 守的是任务书里最容易被做成「只加类型、只加方法」的那几件事：
 * 1. 五个动作**真的**打到接口上，且只有服务端确认成功才改本地状态（不做乐观移除）；
 * 2. 成功后**就地**更新本地状态，不需要刷新页面；
 * 3. 失败保留可重试状态，并把可读原因写给界面；
 * 4. 同一时间只有一个动作在飞（重复点击不重复提交）；
 * 5. 旧快照（`/api/runtime/state` 的 orphaned_turns）不得复活已处理记录。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const fetchRecoveryRecords = vi.fn();
const continueRecovery = vi.fn();
const repairOrphan = vi.fn();
const ignoreRecovery = vi.fn();
const requeueDerived = vi.fn();

vi.mock("../../services/recoveryApi", () => ({
  fetchRecoveryRecords: (...a: unknown[]) => fetchRecoveryRecords(...a),
  continueRecovery: (...a: unknown[]) => continueRecovery(...a),
  repairOrphan: (...a: unknown[]) => repairOrphan(...a),
  ignoreRecovery: (...a: unknown[]) => ignoreRecovery(...a),
  requeueDerived: (...a: unknown[]) => requeueDerived(...a),
  isRecoveryConflict: (e: unknown) => (e as { status?: number })?.status === 409,
}));

// api.ts 只在这个用例里被间接读到（dev 任务等），给一份最小替身
vi.mock("../../services/api", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly path: string,
      detail: string,
    ) {
      super(detail);
    }
  },
  api: {
    getSessionContext: vi.fn(async () => ({ topic_id: "", messages: [] })),
    getRuntimeState: vi.fn(async () => ({
      instance_id: "i",
      revision: 1,
      turn_queue: { instance_id: "i", revision: 1, running: null, queued: [], cancelled: [] },
      approvals: [],
      tasks: [],
    })),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
  },
}));

import { useSessionStore } from "../session";
import type { RecoveryRecordView } from "../../services/recoveryApi";

function readyRecord(overrides: Partial<RecoveryRecordView> = {}): RecoveryRecordView {
  return {
    record_id: "turn_a",
    kind: "user_turn",
    state_class: "ready",
    status: "interrupted",
    message: "帮我把发布闸门跑一遍",
    topic_id: "topic_1",
    reason: "queued_at_restart",
    created_at: "2026-10-02T10:00:00+00:00",
    updated_at: "2026-10-02T10:05:00+00:00",
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

function orphanedRecord(): RecoveryRecordView {
  return readyRecord({
    record_id: "turn_orphan",
    state_class: "orphaned_claim",
    owner_note: "这条记录的重发关系没有写成，需要先修好",
    actions: [
      { id: "repair", label: "修好这条记录", enabled: true, reason: "" },
      {
        id: "continue",
        label: "继续发送这条",
        enabled: false,
        reason: "需要先修好这条记录的重发关系",
      },
    ],
  });
}

function derivedRecord(): RecoveryRecordView {
  return readyRecord({
    record_id: "task_1",
    kind: "derived_task",
    state_class: "derived_stale",
    status: "running",
    message: null,
    attempts: 2,
    last_error: "上一次执行超时",
    claim_generation: 3,
    owner_state: "dead",
    actions: [{ id: "requeue", label: "重新排队", enabled: true, reason: "" }],
  });
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return useSessionStore();
}

beforeEach(() => {
  vi.clearAllMocks();
  fetchRecoveryRecords.mockResolvedValue({ records: [], total: 0, shown: 0, truncated: false });
});

describe("加载收件箱", () => {
  it("拉一次就落地（含 total，供「还有 N 条未显示」用）", async () => {
    fetchRecoveryRecords.mockResolvedValue({
      records: [readyRecord()],
      total: 5,
      shown: 1,
      truncated: true,
    });
    const session = setup();

    await session.loadRecoveryInbox();

    expect(session.recoveryRecords.map((r) => r.record_id)).toEqual(["turn_a"]);
    expect(session.recoveryTotal).toBe(5);
    expect(session.recoveryLoading).toBe(false);
    expect(session.recoveryError).toBe("");
  });

  it("单飞：并发调用只发一个请求", async () => {
    let release: (v: unknown) => void = () => {};
    fetchRecoveryRecords.mockImplementationOnce(
      () => new Promise((resolve) => { release = resolve; }) as never,
    );
    const session = setup();

    const first = session.loadRecoveryInbox();
    const second = session.loadRecoveryInbox();
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(1);

    release({ records: [readyRecord()], total: 1, shown: 1, truncated: false });
    await Promise.all([first, second]);
    expect(session.recoveryRecords).toHaveLength(1);
  });

  it("拉不到 ≠ 没有：保留已显示的记录 + 可重试说明，绝不擦成空清单", async () => {
    fetchRecoveryRecords.mockResolvedValue({
      records: [readyRecord()],
      total: 1,
      shown: 1,
      truncated: false,
    });
    const session = setup();
    await session.loadRecoveryInbox();

    fetchRecoveryRecords.mockRejectedValueOnce(new Error("network down"));
    await session.loadRecoveryInbox();

    expect(session.recoveryRecords).toHaveLength(1);
    expect(session.recoveryError).toContain("network down");
    expect(session.recoveryError).toContain("可以重试");
  });
});

describe("继续 / 修复 / 忽略 / 重新排队：真的落到接口并就地更新", () => {
  it("继续成功 → 就地移除，不靠刷新", async () => {
    fetchRecoveryRecords.mockResolvedValue({
      records: [readyRecord()],
      total: 1,
      shown: 1,
      truncated: false,
    });
    continueRecovery.mockResolvedValue({ ok: true, record_id: "turn_a", turn_id: "turn_new", status: "queued" });
    const session = setup();
    await session.loadRecoveryInbox();

    const res = await session.continueRecovery("turn_a");

    expect(continueRecovery).toHaveBeenCalledWith("turn_a", {
      expected_class: "ready",
      expected_status: "interrupted",
    });
    expect(res.ok).toBe(true);
    expect(session.recoveryRecords).toHaveLength(0);
    expect(session.recoveryError).toContain("重新排队");
    // 没有重新拉清单：确实靠就地更新
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(1);
  });

  it("修复孤儿成功 → 就地变成「可继续 / 可忽略」，不需要刷新", async () => {
    fetchRecoveryRecords.mockResolvedValue({
      records: [orphanedRecord()],
      total: 1,
      shown: 1,
      truncated: false,
    });
    repairOrphan.mockResolvedValue({ ok: true, repaired: true, record_id: "turn_orphan" });
    const session = setup();
    await session.loadRecoveryInbox();

    const res = await session.repairOrphan("turn_orphan");

    expect(repairOrphan).toHaveBeenCalledWith("turn_orphan", "orphaned_claim");
    expect(res.ok).toBe(true);
    const record = session.recoveryRecords[0];
    expect(record?.state_class).toBe("ready");
    expect(record?.actions.map((a) => a.id)).toEqual(["continue", "ignore"]);
    expect(record?.actions.every((a) => a.enabled)).toBe(true);
    expect(record?.owner_note).toBe("");
  });

  it("忽略成功 → 就地移除，并说明原文仍然保留", async () => {
    fetchRecoveryRecords.mockResolvedValue({
      records: [readyRecord()],
      total: 1,
      shown: 1,
      truncated: false,
    });
    ignoreRecovery.mockResolvedValue({ ok: true, ignored: true });
    const session = setup();
    await session.loadRecoveryInbox();

    const res = await session.ignoreRecovery("turn_a");

    expect(ignoreRecovery).toHaveBeenCalledWith("turn_a", "ready");
    expect(res.ok).toBe(true);
    expect(session.recoveryRecords).toHaveLength(0);
    expect(session.recoveryError).toContain("原文仍然保留");
  });

  it("重新排队成功 → 就地标成 pending，且不重置 attempts / last_error", async () => {
    fetchRecoveryRecords.mockResolvedValue({
      records: [derivedRecord()],
      total: 1,
      shown: 1,
      truncated: false,
    });
    requeueDerived.mockResolvedValue({ ok: true, state: "pending" });
    const session = setup();
    await session.loadRecoveryInbox();

    const res = await session.requeueDerived("task_1");

    expect(requeueDerived).toHaveBeenCalledWith("task_1", {
      expected_state: "running",
      expected_generation: 3,
    });
    expect(res.ok).toBe(true);
    const record = session.recoveryRecords[0];
    expect(record?.status).toBe("pending");
    expect(record?.attempts).toBe(2);
    expect(record?.last_error).toBe("上一次执行超时");
  });

  it("失败 → 记录留在清单里（可重试），原因可读、不静默", async () => {
    fetchRecoveryRecords.mockResolvedValue({
      records: [readyRecord()],
      total: 1,
      shown: 1,
      truncated: false,
    });
    continueRecovery.mockRejectedValue(new Error("fetch failed"));
    const session = setup();
    await session.loadRecoveryInbox();

    const res = await session.continueRecovery("turn_a");

    expect(res.ok).toBe(false);
    expect(res.message).toContain("fetch failed");
    expect(session.recoveryRecords).toHaveLength(1);
    expect(session.recoveryError).toContain("可以重试");
  });

  it("409 → 如实转述服务端原因、重新对齐，不假装成功也不本地遮掉记录", async () => {
    fetchRecoveryRecords.mockResolvedValue({
      records: [readyRecord()],
      total: 1,
      shown: 1,
      truncated: false,
    });
    // 后端的 409 原因可能不是「已经被处理过」（也可能是状态已变化 / 原写入者还在运行 /
    // 需要先修复）。界面必须**原样转述**，不能自己编一个理由。
    continueRecovery.mockRejectedValue(
      Object.assign(new Error("上次的写入者现在还在运行，不能接管"), { status: 409 }),
    );
    const session = setup();
    await session.loadRecoveryInbox();

    const res = await session.continueRecovery("turn_a");

    expect(res.ok).toBe(false);
    expect(res.message).toContain("上次的写入者现在还在运行");
    // 409 不等于「已经处理过」：这条记录仍然可操作，不得被本地遮掉
    expect(session.recoveryRecords.map((r) => r.record_id)).toContain("turn_a");
    // 409 之后重新问服务端要真相
    await vi.waitFor(() => expect(fetchRecoveryRecords).toHaveBeenCalledTimes(2));
  });

  it("同一条只允许一个请求在飞：重复点击不发第二次", async () => {
    fetchRecoveryRecords.mockResolvedValue({
      records: [readyRecord(), readyRecord({ record_id: "turn_b" })],
      total: 2,
      shown: 2,
      truncated: false,
    });
    let release: (v: unknown) => void = () => {};
    continueRecovery.mockImplementationOnce(
      () => new Promise((resolve) => { release = resolve; }) as never,
    );
    const session = setup();
    await session.loadRecoveryInbox();

    const first = session.continueRecovery("turn_a");
    const second = await session.continueRecovery("turn_b");

    expect(continueRecovery).toHaveBeenCalledTimes(1);
    expect(second.ok).toBe(false);
    expect(second.message).toContain("还在提交中");

    release({ ok: true, record_id: "turn_a", turn_id: "turn_new", status: "queued" });
    await first;
    expect(session.recoveryRecords.map((r) => r.record_id)).toEqual(["turn_b"]);
  });
});

describe("与 /api/runtime/state 的孤儿记录并入同一清单", () => {
  const ORPHAN = {
    turn_id: "turn_orphan",
    message: "被抢占过但没有后继的那条",
    topic_id: "topic_1",
    status: "interrupted",
    reason: "queued_at_restart",
    reason_text: "这条消息当时还在排队，进程退出后没有开始执行",
    created_at: "2026-10-01T10:00:00+00:00",
    ended_at: "2026-10-01T10:05:00+00:00",
  };

  it("专用接口失败也能看见快照里的孤儿（并且给一个真的能点的「修好」）", async () => {
    fetchRecoveryRecords.mockRejectedValue(new Error("network down"));
    const session = setup();

    session.applyInterruptedState([], [], [ORPHAN] as never);

    const record = session.recoveryRecords.find((r) => r.record_id === "turn_orphan");
    expect(record).toBeDefined();
    expect(record?.state_class).toBe("orphaned_claim");
    expect(record?.message).toBe("被抢占过但没有后继的那条");
    const repair = record?.actions.find((a) => a.id === "repair");
    expect(repair?.enabled).toBe(true);
    const cont = record?.actions.find((a) => a.id === "continue");
    expect(cont?.enabled).toBe(false);
    expect(cont?.reason).toBeTruthy();
  });

  it("去重：同一条两边都有时只出现一次，并保留收件箱记录（它带 actions）", async () => {
    fetchRecoveryRecords.mockResolvedValue({
      records: [readyRecord({ record_id: "turn_orphan", owner_note: "收件箱给的说法" })],
      total: 1,
      shown: 1,
      truncated: false,
    });
    const session = setup();

    await session.loadRecoveryInbox();
    session.applyInterruptedState([], [], [ORPHAN] as never);

    const hits = session.recoveryRecords.filter((r) => r.record_id === "turn_orphan");
    expect(hits).toHaveLength(1);
    expect(hits[0]?.owner_note).toBe("收件箱给的说法");
  });

  it("旧快照不得复活已处理记录", async () => {
    fetchRecoveryRecords.mockResolvedValue({
      records: [readyRecord({ record_id: "turn_orphan" })],
      total: 1,
      shown: 1,
      truncated: false,
    });
    ignoreRecovery.mockResolvedValue({ ok: true, ignored: true });
    const session = setup();
    session.applyInterruptedState([], [], [ORPHAN] as never);
    await session.loadRecoveryInbox();
    expect(session.recoveryRecords).toHaveLength(1);

    await session.ignoreRecovery("turn_orphan");
    expect(session.recoveryRecords).toHaveLength(0);

    // 稍旧的快照又回来了：不许把它复活成点不动的假待办
    session.applyInterruptedState([], [], [ORPHAN] as never);
    expect(session.recoveryRecords).toHaveLength(0);
  });

  it("修好之后快照里的那一份也不再回退成「孤儿」", async () => {
    fetchRecoveryRecords.mockRejectedValue(new Error("network down"));
    repairOrphan.mockResolvedValue({ ok: true, repaired: true, record_id: "turn_orphan" });
    const session = setup();
    session.applyInterruptedState([], [], [ORPHAN] as never);

    await session.repairOrphan("turn_orphan");
    expect(session.recoveryRecords[0]?.state_class).toBe("ready");
  });
});

describe("界面文案来源（不猜状态）", () => {
  it("「为什么动不了」优先用服务端给的 owner_note", async () => {
    const { recoveryReasonOf, recoveryActionLabel } = await import("../session");
    const record = readyRecord({ owner_note: "无法确认上次的写入者是否已停止" });
    expect(recoveryReasonOf(record)).toBe("无法确认上次的写入者是否已停止");
    expect(recoveryActionLabel("continue")).toBe("继续发送这条");
    expect(recoveryActionLabel("repair", "修好这条记录")).toBe("修好这条记录");
  });

  it("`owner_state: unknown` 不会被说成「已停止」", async () => {
    const { recoveryReasonOf } = await import("../session");
    const record = readyRecord({ owner_state: "unknown", owner_note: "" });
    expect(recoveryReasonOf(record)).not.toContain("已停止");
  });

  it("汇总只说数量，不替用户做决定", async () => {
    const session = setup();
    session.recoveryRecords = [readyRecord(), readyRecord({ record_id: "turn_b" })];
    expect(session.recoveryInboxSummary()).toContain("2");
    expect(session.recoveryInboxSummary()).toContain("需要你决定");
    expect(session.recoveryInboxSummary()).not.toContain("自动");
  });
});
