/**
 * R19-B：保存失败后通知前端刷新恢复清单（R19 契约 §2.2 (b) / §5 B2 / B4）。
 *
 * 缺口（基线 d1f8376d）：后端保存失败后既没有事件，前端也没有事件分支 ——
 * 已打开且清单为空的页面只有在刷新 / 重连后才看得见「重试回答」。
 *
 * 这里守四条行为：
 * 1. 事件类型进入 EVENT_TYPES，dispatch 收到后复用既有 loadRecoveryInbox 刷新；
 * 2. 事件到达时已有清单请求在途：**不并发第二个快照请求**，完成后补拉一轮，
 *    新记录最终可见；
 * 3. 在途期间的重复事件（SSE 重放）只登记一次补拉，不产生坏状态；
 * 4. 普通并发 loadRecoveryInbox 的既有单飞语义保持不变（补拉只由通知入口登记）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const fetchRecoveryRecords = vi.fn();

vi.mock("../../services/recoveryApi", () => ({
  fetchRecoveryRecords: (...a: unknown[]) => fetchRecoveryRecords(...a),
  continueRecovery: vi.fn(),
  repairOrphan: vi.fn(),
  ignoreRecovery: vi.fn(),
  requeueDerived: vi.fn(),
  confirmStopped: vi.fn(),
  isRecoveryConflict: (e: unknown) => (e as { status?: number })?.status === 409,
}));

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
    listDevAuthorizations: vi.fn(async () => ({ authorizations: [] })),
  },
}));

import { useSessionStore } from "../session";
import { useEventStore } from "../events";
import { EVENT_TYPES } from "../../services/events";
import type { RecoveryRecordView } from "../../services/recoveryApi";

function record(recordId: string): RecoveryRecordView {
  return {
    record_id: recordId,
    kind: "topic_answer_recovery",
    state_class: "answer_pending",
    status: "pending",
    message: "普通回答已生成但保存失败，可从此处重试补存",
    topic_id: "topic_1",
    reason: "answer",
    created_at: "2026-11-11T10:00:00+00:00",
    updated_at: "2026-11-11T10:00:05+00:00",
    owner_instance_id: null,
    owner_state: "none",
    owner_note: "",
    claim_generation: null,
    attempts: null,
    last_error: null,
    actions: [{ id: "retry_answer", label: "重试回答", enabled: true, reason: "" }],
  };
}

function listing(records: RecoveryRecordView[], extra: Record<string, unknown> = {}) {
  return { records, total: records.length, shown: records.length, truncated: false, ...extra };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { session: useSessionStore(), events: useEventStore() };
}

const NEW_RECORD_ID = "trec_r19_b_new";

function inboxChangedEvent(id = "evt_r19b") {
  return {
    type: "RECOVERY_INBOX_CHANGED",
    id,
    ts: "2026-11-11T10:00:00+00:00",
    data: {
      record_id: NEW_RECORD_ID,
      turn_id: "turn_1",
      topic_id: "topic_1",
      kind: "topic_answer_recovery",
      failed_stage: "answer",
      action: "retry_answer",
    },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  fetchRecoveryRecords.mockResolvedValue(listing([]));
});

describe("R19-B 事件类型与刷新入口", () => {
  it("EVENT_TYPES 包含 RECOVERY_INBOX_CHANGED（否则 EventSource 根本收不到该事件）", () => {
    expect(EVENT_TYPES).toContain("RECOVERY_INBOX_CHANGED");
  });

  it("空闲时收到事件 → 复用 loadRecoveryInbox 刷新，新记录可见", async () => {
    const { session, events } = setup();
    fetchRecoveryRecords.mockResolvedValue(listing([record(NEW_RECORD_ID)], { total: 1 }));

    events.dispatch(inboxChangedEvent());

    await vi.waitFor(() =>
      expect(session.recoveryRecords.map((r) => r.record_id)).toEqual([NEW_RECORD_ID]),
    );
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(1);
    expect(session.recoveryLoading).toBe(false);
    expect(session.recoveryError).toBe("");
  });
});

describe("R19-B 在途请求的完成后补拉", () => {
  it("事件到达时已有请求在途 → 不并发第二个快照请求，完成后补拉一轮使新记录最终可见", async () => {
    const d1 = deferred<ReturnType<typeof listing>>();
    const d2 = deferred<ReturnType<typeof listing>>();
    fetchRecoveryRecords
      .mockImplementationOnce(() => d1.promise)
      .mockImplementationOnce(() => d2.promise);
    const { session, events } = setup();

    const first = session.loadRecoveryInbox();
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(1);

    // 在途：只登记「完成后补拉一轮」，绝不并发第二个清单请求
    events.dispatch(inboxChangedEvent());
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(1);

    d1.resolve(listing([]));
    await vi.waitFor(() => expect(fetchRecoveryRecords).toHaveBeenCalledTimes(2));

    d2.resolve(listing([record(NEW_RECORD_ID)], { total: 1 }));
    await vi.waitFor(() =>
      expect(session.recoveryRecords.map((r) => r.record_id)).toEqual([NEW_RECORD_ID]),
    );
    await first;
    expect(session.recoveryLoading).toBe(false);
    expect(session.recoveryError).toBe("");
  });

  it("在途期间重复事件（SSE 重放）只登记一次补拉，不产生坏状态", async () => {
    const d1 = deferred<ReturnType<typeof listing>>();
    const d2 = deferred<ReturnType<typeof listing>>();
    fetchRecoveryRecords
      .mockImplementationOnce(() => d1.promise)
      .mockImplementationOnce(() => d2.promise);
    const { session, events } = setup();

    const first = session.loadRecoveryInbox();
    for (let i = 0; i < 3; i += 1) events.dispatch(inboxChangedEvent(`evt_replay_${i}`));
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(1);

    d1.resolve(listing([]));
    await vi.waitFor(() => expect(fetchRecoveryRecords).toHaveBeenCalledTimes(2));

    d2.resolve(listing([record(NEW_RECORD_ID)]));
    await vi.waitFor(() => expect(session.recoveryLoading).toBe(false));
    await first;
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(2);
    expect(session.recoveryRecords.map((r) => r.record_id)).toEqual([NEW_RECORD_ID]);

    // 空闲后重复事件仍然幂等：只是再拉一次权威清单，最终状态由后端决定
    const d3 = deferred<ReturnType<typeof listing>>();
    fetchRecoveryRecords.mockImplementationOnce(() => d3.promise);
    events.dispatch(inboxChangedEvent("evt_after_idle"));
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(3);
    d3.resolve(listing([record(NEW_RECORD_ID)]));
    await vi.waitFor(() => expect(session.recoveryLoading).toBe(false));
    expect(session.recoveryRecords.map((r) => r.record_id)).toEqual([NEW_RECORD_ID]);
  });
});

describe("R19-B 与既有单飞 / 实例竞态语义不回归", () => {
  it("普通并发 loadRecoveryInbox 仍单飞：不因补拉逻辑额外发请求", async () => {
    const d1 = deferred<ReturnType<typeof listing>>();
    fetchRecoveryRecords.mockImplementationOnce(() => d1.promise);
    const { session } = setup();

    const first = session.loadRecoveryInbox();
    const second = session.loadRecoveryInbox();
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(1);

    d1.resolve(listing([record("trec_plain")]));
    await Promise.all([first, second]);
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(1);
    expect(session.recoveryRecords.map((r) => r.record_id)).toEqual(["trec_plain"]);
    expect(session.recoveryLoading).toBe(false);
  });

  it("切换实例后在飞旧请求不被新事件复用；事件为当前实例单独拉一份，旧响应整段丢弃", async () => {
    const dOld = deferred<ReturnType<typeof listing>>();
    const dNew = deferred<ReturnType<typeof listing>>();
    fetchRecoveryRecords
      .mockImplementationOnce(() => dOld.promise)
      .mockImplementationOnce(() => dNew.promise);
    const { session, events } = setup();
    session.adoptInstance("old-inst");

    const oldLoad = session.loadRecoveryInbox();
    session.adoptInstance("new-inst");
    events.dispatch(inboxChangedEvent());
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(2);

    dNew.resolve(listing([record("trec_new_inst")]));
    await vi.waitFor(() =>
      expect(session.recoveryRecords.map((r) => r.record_id)).toEqual(["trec_new_inst"]),
    );

    dOld.resolve(listing([record("trec_old_inst")], { total: 9 }));
    await oldLoad;
    expect(session.recoveryRecords.map((r) => r.record_id)).toEqual(["trec_new_inst"]);
    expect(session.recoveryTotal).toBe(1);
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(2);
  });
});
