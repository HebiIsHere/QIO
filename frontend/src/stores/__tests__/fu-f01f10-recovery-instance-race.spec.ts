/**
 * F07 回归：**切换后端实例后必须丢弃旧的恢复清单请求**。
 *
 * 原缺陷：`loadRecoveryInbox()` 只有一个布尔单飞标记，没有请求实例 / 代次保护。
 * 旧实例的请求挂起时 `adoptInstance('new')`，旧请求返回后仍会把
 * `old-instance-record` 写进新实例的 recoveryRecords —— 界面上出现的是已经死掉的
 * 那个后端实例的记录；反过来，旧请求还会把新请求的 loading 提前解锁（或把新实例的
 * 读取挡在单飞锁外）。
 *
 * 这里按既有 `_devTasksSeq` 模式校验「代次 + 实例」：
 * 1. 旧请求迟到 + 新请求先返回：records / total 只认新实例；
 * 2. 旧请求失败迟到：错误不写到新实例上；
 * 3. 旧请求不得清除新请求的 loading；
 * 4. 实例快速连续切换多次：只由当前实例最后一次请求决定；
 * 5. 同一实例内并发仍然单飞（不回归既有行为）。
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
import type { RecoveryRecordView } from "../../services/recoveryApi";

function record(recordId: string): RecoveryRecordView {
  return {
    record_id: recordId,
    kind: "user_turn",
    state_class: "ready",
    status: "interrupted",
    message: `消息 ${recordId}`,
    topic_id: "topic_1",
    reason: "queued_at_restart",
    created_at: "2026-10-10T10:00:00+00:00",
    updated_at: "2026-10-10T10:05:00+00:00",
    owner_instance_id: null,
    owner_state: "none",
    owner_note: "",
    claim_generation: null,
    attempts: null,
    last_error: null,
    actions: [{ id: "continue", label: "继续发送这条", enabled: true, reason: "" }],
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
  return useSessionStore();
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("F07 切换实例：旧实例的收件箱请求必须丢弃", () => {
  it("旧请求迟到 + 新请求先返回：records / total 只认新实例", async () => {
    const session = setup();
    session.adoptInstance("old-inst");

    const dOld = deferred<ReturnType<typeof listing>>();
    const dNew = deferred<ReturnType<typeof listing>>();
    fetchRecoveryRecords
      .mockImplementationOnce(() => dOld.promise)
      .mockImplementationOnce(() => dNew.promise);

    const pOld = session.loadRecoveryInbox();
    expect(session.recoveryLoading).toBe(true);

    // 旧请求还挂着就换了实例 → 新实例的读取必须能继续，不被旧请求的单飞锁挡住
    session.adoptInstance("new-inst");
    const pNew = session.loadRecoveryInbox();
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(2);

    // 新实例先返回
    dNew.resolve(listing([record("new-instance-record")], { total: 1 }));
    await pNew;
    expect(session.recoveryRecords.map((r) => r.record_id)).toEqual(["new-instance-record"]);
    expect(session.recoveryTotal).toBe(1);
    expect(session.recoveryLoading).toBe(false);
    expect(session.recoveryError).toBe("");

    // 旧实例的响应最后才回来：整段丢弃，一个字段都不许写
    dOld.resolve(listing([record("old-instance-record")], { total: 9 }));
    await pOld;
    expect(session.recoveryRecords.map((r) => r.record_id)).toEqual(["new-instance-record"]);
    expect(session.recoveryTotal).toBe(1);
    expect(session.recoveryError).toBe("");
    expect(session.recoveryLoading).toBe(false);
  });

  it("旧请求失败迟到：错误不能写到新实例的清单上", async () => {
    const session = setup();
    session.adoptInstance("old-inst");

    const dOld = deferred<ReturnType<typeof listing>>();
    const dNew = deferred<ReturnType<typeof listing>>();
    fetchRecoveryRecords
      .mockImplementationOnce(() => dOld.promise)
      .mockImplementationOnce(() => dNew.promise);

    const pOld = session.loadRecoveryInbox();
    session.adoptInstance("new-inst");
    const pNew = session.loadRecoveryInbox();

    dNew.resolve(listing([record("new-instance-record")]));
    await pNew;
    expect(session.recoveryError).toBe("");

    dOld.reject(new Error("旧实例的网络炸了"));
    await pOld;
    expect(session.recoveryError).toBe("");
    expect(session.recoveryRecords.map((r) => r.record_id)).toEqual(["new-instance-record"]);
  });

  it("旧请求不得清除新请求的 loading", async () => {
    const session = setup();
    session.adoptInstance("old-inst");

    const dOld = deferred<ReturnType<typeof listing>>();
    const dNew = deferred<ReturnType<typeof listing>>();
    fetchRecoveryRecords
      .mockImplementationOnce(() => dOld.promise)
      .mockImplementationOnce(() => dNew.promise);

    const pOld = session.loadRecoveryInbox();
    session.adoptInstance("new-inst");
    const pNew = session.loadRecoveryInbox();
    expect(session.recoveryLoading).toBe(true);

    // 旧实例先回来：它没资格解锁新实例那一次请求的 loading
    dOld.resolve(listing([record("old-instance-record")]));
    await pOld;
    expect(session.recoveryLoading).toBe(true);
    expect(session.recoveryRecords).toEqual([]);

    dNew.resolve(listing([record("new-instance-record")]));
    await pNew;
    expect(session.recoveryLoading).toBe(false);
    expect(session.recoveryRecords.map((r) => r.record_id)).toEqual(["new-instance-record"]);
  });

  it("实例快速连续切换多次：只由当前实例最后一次请求决定", async () => {
    const session = setup();
    session.adoptInstance("i1");

    const d1 = deferred<ReturnType<typeof listing>>();
    const d2 = deferred<ReturnType<typeof listing>>();
    const d3 = deferred<ReturnType<typeof listing>>();
    fetchRecoveryRecords
      .mockImplementationOnce(() => d1.promise)
      .mockImplementationOnce(() => d2.promise)
      .mockImplementationOnce(() => d3.promise);

    const p1 = session.loadRecoveryInbox();
    session.adoptInstance("i2");
    const p2 = session.loadRecoveryInbox();
    session.adoptInstance("i3");
    const p3 = session.loadRecoveryInbox();
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(3);

    // 当前实例（i3）先返回，两个旧实例的响应乱序迟到
    d3.resolve(listing([record("r3")], { total: 3 }));
    await p3;
    d2.resolve(listing([record("r2")], { total: 2 }));
    await p2;
    d1.resolve(listing([record("r1")], { total: 1 }));
    await p1;

    expect(session.recoveryRecords.map((r) => r.record_id)).toEqual(["r3"]);
    expect(session.recoveryTotal).toBe(3);
    expect(session.recoveryError).toBe("");
    expect(session.recoveryLoading).toBe(false);
  });

  it("同一实例内并发仍然是单飞（不回归既有行为）", async () => {
    const session = setup();
    session.adoptInstance("inst-1");

    const d1 = deferred<ReturnType<typeof listing>>();
    fetchRecoveryRecords.mockImplementationOnce(() => d1.promise);

    const p1 = session.loadRecoveryInbox();
    const p2 = session.loadRecoveryInbox();
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(1);

    d1.resolve(listing([record("r1")]));
    await Promise.all([p1, p2]);
    expect(session.recoveryRecords.map((r) => r.record_id)).toEqual(["r1"]);
    expect(session.recoveryLoading).toBe(false);
  });
});
