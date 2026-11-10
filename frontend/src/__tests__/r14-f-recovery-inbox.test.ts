/**
 * R14-F 独立回归（F04-frontend）：恢复事项独立于活动轮次，成功由权威结果收口。
 *
 * 真实 session/event stores + 本机状态；网络仅 mock 两个权威通道：
 * GET /api/recovery/records（持久清单）与 POST .../retry-answer（重试回答）。
 * 只钉用户可见行为，不复用实现者测试：
 * - TURN_END / 新一轮 / clearTopicApplicationsForTurn / resetTopicPhase 不清掉未完成事项；
 * - 刷新（新 store）后从持久清单仍可见、仍可重试，旧轮次不被激活；
 * - 重试成功（answered=true）关闭记录、移除旧失败提示、补读唯一真实答案，
 *   迟到/重放的失败事件不得复活已完成记录；
 * - 处理中（in_progress）不是成功：记录保留、不重复提交。
 *
 * 对应契约：docs/topic-review-20261110-contract.md §3.4 / §3.6。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../stores/session";
import { useEventStore } from "../stores/events";
import { api } from "../services/api";
import { retryTopicAnswer } from "../services/approvalLifecycle";
import type { AgentEvent } from "../services/events";

vi.mock("../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "t1",
      topic_name: "当前话题",
      anchor_fragment: null,
      messages: [],
    })),
    loadHistory: vi.fn(async () => ({ messages: [], anchor: null })),
    sendTurn: vi.fn(async () => ({
      ok: true,
      accepted: true,
      turn_id: "turn_9",
      status: "accepted",
      topic_id: null,
    })),
    cancelTurn: vi.fn(async () => ({ ok: true, cancelled: true })),
    respondTopicApproval: vi.fn(async () => ({ ok: true })),
    respondApproval: vi.fn(async () => ({ ok: true })),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
    listDevAuthorizations: vi.fn(async () => ({ items: [] })),
    getRecoveryRecords: vi.fn(async () => ({
      records: [],
      total: 0,
      shown: 0,
      truncated: false,
    })),
    getRuntimeState: vi.fn(async () => ({})),
    getApprovalsPending: vi.fn(async () => ({ approvals: [] })),
  },
}));

vi.mock("../services/approvalLifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../services/approvalLifecycle")>();
  return {
    ...actual,
    getApprovalHistory: vi.fn(async () => []),
    retryTopicAnswer: vi.fn(async () => ({
      ok: true,
      answered: true,
      code: "",
      message: "",
    })),
  };
});

let seq = 0;

function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: `r14f-rec-${type}-${seq}`, ts: "", data } as AgentEvent;
}

type Store = ReturnType<typeof useSessionStore>;
type EventStore = ReturnType<typeof useEventStore>;

function setup() {
  setActivePinia(createPinia());
  return { session: useSessionStore(), events: useEventStore() };
}

function startTurn(session: Store, events: EventStore, turnId = "turn_1") {
  session.setAnchor("t1", null, "当前话题", null, false);
  events.route(ev("TURN_START", { turn_id: turnId }));
}

function recoveryFailure(
  events: EventStore,
  opts: { recordId?: string; turnId?: string; message?: string } = {},
) {
  events.route(
    ev("TOPIC_PHASE", {
      phase: "recoverable_failure",
      operation_id: "op-1",
      turn_id: opts.turnId ?? "turn_1",
      recovery: {
        record_id: opts.recordId ?? "rec-1",
        kind: "topic_answer_recovery",
        failed_stage: "context_prepare",
        message: opts.message ?? "目标话题的上下文没有准备好",
        retryable: true,
      },
    }),
  );
}

function recoveryRecord(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    record_id: "rec-1",
    kind: "topic_answer_recovery",
    state_class: "answer_pending",
    status: "pending",
    message: "目标话题的上下文没有准备好",
    topic_id: "t2",
    reason: "context_prepare",
    failed_stage: "context_prepare",
    turn_id: "turn_1",
    from_topic_id: "t1",
    fragment_id: null,
    operation_id: "op-1",
    param_version: 1,
    base_nav_version: 1,
    recovery_nav_version: 2,
    answer_id: null,
    attempt_count: 1,
    attempts: 1,
    created_at: "2026-11-10T12:00:00Z",
    updated_at: "2026-11-10T12:00:00Z",
    owner_instance_id: null,
    owner_state: "none",
    owner_note: "",
    claim_generation: null,
    last_error: null,
    confirmed_stopped: false,
    retryable: true,
    actions: [{ id: "retry_answer", label: "重试回答", enabled: true, reason: "" }],
    ...overrides,
  };
}

async function loadListing(session: Store, records: Record<string, unknown>[]) {
  vi.mocked(api.getRecoveryRecords).mockResolvedValueOnce({
    records,
    total: records.length,
    shown: records.length,
    truncated: false,
  });
  await session.loadRecoveryInbox();
}

function retryMock(): ReturnType<typeof vi.mocked<typeof retryTopicAnswer>> {
  return vi.mocked(retryTopicAnswer);
}

beforeEach(() => {
  localStorage.clear();
  seq = 0;
  vi.mocked(api.getRecoveryRecords).mockReset();
  vi.mocked(api.getRecoveryRecords).mockResolvedValue({
    records: [],
    total: 0,
    shown: 0,
    truncated: false,
  });
  vi.mocked(api.getSessionContext).mockClear();
  vi.mocked(api.sendTurn).mockClear();
  retryMock().mockReset();
  retryMock().mockResolvedValue({
    ok: true,
    answered: true,
    code: "",
    message: "",
  });
});

describe("F04 · 恢复事项不随活动轮次结束而消失", () => {
  it("TOPIC_PHASE.recoverable_failure → TURN_END：阶段收口，但恢复入口与事实仍在", () => {
    const { session, events } = setup();
    startTurn(session, events);
    recoveryFailure(events, { recordId: "rec-1" });
    expect(session.stage).toBe("recoverable_failure");
    expect(session.recovery?.recordId).toBe("rec-1");

    events.route(ev("TURN_END", { turn_id: "turn_1", status: "failed" }));

    expect(session.stage).toBeNull();
    expect(session.recovery?.recordId).toBe("rec-1");
    expect(session.recovery?.retryable).toBe(true);
    expect(session.recoveryItems.map((item) => item.recordId)).toContain("rec-1");
  });

  it("resetTopicPhase / clearTopicApplicationsForTurn / 新一轮 TURN_START 都不删除未完成事项", async () => {
    const { session, events } = setup();
    startTurn(session, events);
    await loadListing(session, [recoveryRecord()]);
    expect(session.recoveryItems).toHaveLength(1);

    session.resetTopicPhase();
    expect(session.recoveryItems).toHaveLength(1);
    session.clearTopicApplicationsForTurn();
    expect(session.recoveryItems).toHaveLength(1);
    events.route(ev("TURN_START", { turn_id: "turn_2" }));
    expect(session.recoveryItems).toHaveLength(1);
  });
});

describe("F04 · 刷新 / 重启后仍可见并可重试", () => {
  it("新 store + 持久清单：未完成事项仍可见、旧轮次不被激活、重试走权威入口", async () => {
    setActivePinia(createPinia());
    let session = useSessionStore();
    await loadListing(session, [recoveryRecord()]);
    expect(session.recoveryItems).toHaveLength(1);

    // 关闭重开：同一份本机状态（新 pinia），恢复入口来自持久查询。
    setActivePinia(createPinia());
    session = useSessionStore();
    expect(session.recoveryItems).toHaveLength(0);
    await loadListing(session, [recoveryRecord()]);
    expect(session.recoveryItems).toHaveLength(1);
    expect(session.activeTurnId).toBeNull();

    const result = await session.retryTopicAnswer("rec-1");
    expect(result.ok).toBe(true);
    expect(retryMock()).toHaveBeenCalledWith("rec-1");
    expect(session.activeTurnId).toBeNull();
    expect(session.topicStageTurnId).toBeNull();
  });

  it("权威清单里已 answered 的记录不进入待办入口（旧快照不得复活）", async () => {
    const session = useSessionStore();
    await loadListing(session, [
      recoveryRecord({ status: "answered", state_class: "done", retryable: false }),
    ]);
    expect(session.recoveryItems).toHaveLength(0);
  });
});

describe("F04 · 成功由权威结果收口（唯一真实答案）", () => {
  it("重试成功（answered=true）：关闭记录、移除失败提示、补读历史；迟到失败事件不复活", async () => {
    const { session, events } = setup();
    startTurn(session, events);
    await loadListing(session, [recoveryRecord()]);
    recoveryFailure(events, { recordId: "rec-1" });
    expect(session.recoveryItems).toHaveLength(1);
    // 历史已加载完成（真实界面进入话题后的状态）：成功后允许用既有刷新入口补读。
    session.history = { status: "ready", error: null };

    const before = vi.mocked(api.getSessionContext).mock.calls.length;
    const result = await session.retryTopicAnswer("rec-1");

    expect(result.ok).toBe(true);
    expect(result.answered).toBe(true);
    expect(session.recoveryItems).toHaveLength(0);
    expect(session.recovery).toBeNull();
    // 成功后补读唯一真实答案（既有历史读取通道）：不伪造本地消息。
    expect(vi.mocked(api.getSessionContext).mock.calls.length).toBe(before + 1);
    expect(api.sendTurn).not.toHaveBeenCalled();

    // 迟到/重放的同一条失败事件（同一 record_id）不得把已完成记录复活。
    recoveryFailure(events, { recordId: "rec-1" });
    expect(session.recoveryItems).toHaveLength(0);
    expect(session.recovery).toBeNull();
  });

  it("处理中（in_progress）不是成功：记录保留、不重复提交", async () => {
    const { session, events } = setup();
    startTurn(session, events);
    await loadListing(session, [recoveryRecord()]);
    retryMock().mockResolvedValueOnce({
      ok: false,
      inProgress: true,
      code: "recovery_in_progress",
      message: "这条恢复正在处理中（已有执行者领取租约），请稍后刷新。",
    });

    const first = await session.retryTopicAnswer("rec-1");
    expect(first.ok).toBe(false);
    expect(first.inProgress).toBe(true);
    expect(session.recoveryItems).toHaveLength(1);

    // 再次点击仍走权威入口一次（本地单飞在请求结束后解除），记录不被删除。
    retryMock().mockResolvedValueOnce({
      ok: false,
      inProgress: true,
      code: "recovery_in_progress",
      message: "仍在处理中",
    });
    await session.retryTopicAnswer("rec-1");
    expect(session.recoveryItems).toHaveLength(1);
  });
});
