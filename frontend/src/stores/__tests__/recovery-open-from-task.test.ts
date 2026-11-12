/**
 * #78 任务栏「去处理」→ 恢复窗口的接线（store 侧）+ 文案 8。
 *
 * 约定（共同契约 §4.1/§4.7）：点击 open_recovery 时由任务栏发
 * `qio:open-recovery`（detail.recordId），对话页负责打开恢复窗口并滚动聚焦同一条；
 * 目标已经不在清单里时由这里给真实反馈。文案 8：成功不再写背景说明，失败才提示。
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

import { useConversationTasksStore } from "../conversationTasks";
import { useSessionStore } from "../session";
import type { RecoveryRecordView } from "../../services/recoveryApi";
import type { TaskFeedItem } from "../../conversation/contracts";

function record(recordId: string): RecoveryRecordView {
  return {
    record_id: recordId,
    kind: "user_turn",
    state_class: "ready",
    status: "interrupted",
    message: "上次没有执行完的消息",
    topic_id: "topic_1",
    reason: "queued_at_restart",
    created_at: "2026-11-12T10:00:00+00:00",
    updated_at: "2026-11-12T10:00:05+00:00",
    owner_instance_id: null,
    owner_state: "none",
    owner_note: "",
    claim_generation: null,
    attempts: null,
    last_error: null,
    actions: [{ id: "continue", label: "继续发送这条", enabled: true, reason: "" }],
  };
}

function recoveryTask(recordId: string, id = "recovery:" + recordId): TaskFeedItem {
  return {
    id,
    source: "recovery",
    sourceId: recordId,
    title: "上次没有执行完的消息",
    status: "waiting_user",
    rawStatus: "interrupted",
    origin: "topic",
    topicId: "topic_1",
    topicTitle: null,
    executor: null,
    summary: "",
    queuedAt: "2026-11-12T10:00:00+00:00",
    startedAt: null,
    finishedAt: null,
    durationMs: null,
    requiresAttention: true,
    acknowledged: false,
    output: { truncated: false, preview: "", hint: "", readMore: false },
    actions: [{ kind: "open_recovery", label: "去处理", target: recordId, supported: true }],
  };
}

let received: unknown[] = [];
function onOpen(event: Event) {
  received.push((event as CustomEvent).detail);
}

beforeEach(() => {
  localStorage.clear();
  received = [];
  vi.clearAllMocks();
  fetchRecoveryRecords.mockResolvedValue({ records: [], total: 0, shown: 0, truncated: false });
  window.addEventListener("qio:open-recovery", onOpen);
});

function teardown() {
  window.removeEventListener("qio:open-recovery", onOpen);
}

describe("#78 任务栏去处理 → 恢复窗口", () => {
  it("记录还在：发 qio:open-recovery（detail.recordId），且不再写成功后的背景说明（文案 8）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    session.recoveryRecords = [record("rec-1")];
    const tasks = useConversationTasksStore();
    const item = recoveryTask("rec-1");

    await tasks.dispatchAction(item, "open_recovery", "rec-1");
    expect(received).toEqual([{ recordId: "rec-1" }]);
    expect(tasks.noticeFor(item.id), "成功后不该再写背景说明").toBe("");
    teardown();
  });

  it("目标已消失：不发事件，并给出真实反馈（不是静默无反应）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    session.recoveryRecords = [];
    const tasks = useConversationTasksStore();
    const item = recoveryTask("rec-gone");

    await tasks.dispatchAction(item, "open_recovery", "rec-gone");
    expect(received).toEqual([]);
    expect(tasks.noticeFor(item.id)).toContain("不在清单里");
    teardown();
  });
});
