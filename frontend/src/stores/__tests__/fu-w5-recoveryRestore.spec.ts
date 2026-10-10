/**
 * `stores/restore.ts` 的一处新接线（A01 + A03）：**唯一恢复入口**里一并拉收件箱。
 *
 * 守三件事：
 * 1. 每一次恢复都顺带拉一次收件箱（不新增第二个恢复入口）；
 * 2. 专用接口失败时，`/api/runtime/state` 的 `orphaned_turns` 仍然看得见 ——
 *    孤儿记录一旦不可见，那条消息就永久消失；
 * 3. 按「已解决 id 集合」过滤：稍旧的快照不得复活已经处理掉的记录。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const getRuntimeState = vi.fn();

vi.mock("../../services/events", () => ({
  connectEvents: () => ({ onopen: null, onerror: null, close: () => {} }),
  publishTestEvent: vi.fn(async () => undefined),
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
    getRuntimeState: (...a: unknown[]) => getRuntimeState(...a),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
    listDevAuthorizations: vi.fn(async () => ({ authorizations: [] })),
  },
}));

const fetchRecoveryRecords = vi.fn();
const ignoreRecovery = vi.fn();
const continueRecovery = vi.fn();
const repairOrphan = vi.fn();
const requeueDerived = vi.fn();

vi.mock("../../services/recoveryApi", () => ({
  fetchRecoveryRecords: (...a: unknown[]) => fetchRecoveryRecords(...a),
  continueRecovery: (...a: unknown[]) => continueRecovery(...a),
  repairOrphan: (...a: unknown[]) => repairOrphan(...a),
  ignoreRecovery: (...a: unknown[]) => ignoreRecovery(...a),
  requeueDerived: (...a: unknown[]) => requeueDerived(...a),
  isRecoveryConflict: (e: unknown) => (e as { status?: number })?.status === 409,
}));

import { useSessionStore } from "../session";
import { restoreRuntimeState } from "../restore";

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

function runtimeState(orphans: unknown[] = []) {
  return {
    instance_id: "inst_1",
    revision: 7,
    turn_queue: {
      instance_id: "inst_1",
      revision: 7,
      running: null,
      queued: [],
      cancelled: [],
    },
    approvals: [],
    tasks: [],
    tools: [],
    narratives: [],
    interrupted_approvals: [],
    interrupted_turns: [],
    orphaned_turns: orphans,
  };
}

describe("恢复入口一并加载收件箱", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchRecoveryRecords.mockResolvedValue({ records: [], total: 0, shown: 0, truncated: false });
    getRuntimeState.mockResolvedValue(runtimeState());
  });

  it("一次恢复 = 一次快照 + 一次收件箱（不新增第二个入口）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();

    const outcome = await restoreRuntimeState("resync");

    expect(outcome.ok).toBe(true);
    expect(getRuntimeState).toHaveBeenCalledTimes(1);
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(1);
    expect(session.resyncState).toBe("normal");
  });

  it("专用接口失败时，快照里的孤儿仍然进入同一个清单（可操作）", async () => {
    fetchRecoveryRecords.mockRejectedValue(new Error("network down"));
    getRuntimeState.mockResolvedValue(runtimeState([ORPHAN]));
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();

    const outcome = await restoreRuntimeState("reconnect");

    expect(outcome.ok).toBe(true); // 一个补充请求失败不该让整轮恢复失败
    expect(session.recoveryRecords.map((r) => r.record_id)).toEqual(["turn_orphan"]);
    expect(session.recoveryRecords[0]?.actions.some((a) => a.enabled)).toBe(true);
    // 失败原因留在屏幕上（界面据此说明这一份可能不完整）
    expect(session.recoveryError).toContain("network down");
  });

  it("收件箱记录与快照孤儿并成一份清单，且不重复", async () => {
    fetchRecoveryRecords.mockResolvedValue({
      records: [
        {
          record_id: "turn_orphan",
          kind: "user_turn",
          state_class: "orphaned_claim",
          status: "interrupted",
          message: "被抢占过但没有后继的那条",
          topic_id: "topic_1",
          reason: "queued_at_restart",
          created_at: "2026-10-01T10:00:00+00:00",
          updated_at: "2026-10-01T10:05:00+00:00",
          owner_instance_id: null,
          owner_state: "none",
          owner_note: "这条记录的重发关系没有写成",
          claim_generation: null,
          attempts: null,
          last_error: null,
          actions: [
            { id: "repair", label: "修好这条记录", enabled: true, reason: "" },
          ],
        },
      ],
      total: 3,
      shown: 1,
      truncated: true,
    });
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();

    await restoreRuntimeState("connect");

    expect(session.recoveryRecords).toHaveLength(1);
    // 服务端说还有没显示完的：总数不能被本地合并改小
    expect(session.recoveryTotal).toBe(3);
  });

  it("旧快照不复活已处理记录：第二轮的孤儿不会再回来", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    ignoreRecovery.mockResolvedValue({ ok: true, ignored: true });

    getRuntimeState.mockResolvedValue(runtimeState([ORPHAN]));
    await restoreRuntimeState("connect");
    expect(session.recoveryRecords).toHaveLength(1);

    await session.ignoreRecovery("turn_orphan");
    expect(session.recoveryRecords).toHaveLength(0);

    // 后端这边的快照还是旧的（孤儿仍在）：界面不许复活它
    await restoreRuntimeState("resync");
    expect(session.recoveryRecords).toHaveLength(0);
  });
});
