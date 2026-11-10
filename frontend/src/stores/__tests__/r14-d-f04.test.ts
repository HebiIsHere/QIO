/**
 * R14-D · F04-frontend：恢复事项独立于活动轮次，成功由权威结果收口。
 *
 * 旧缺陷（本轮要修）：
 * - 恢复入口（可重试回答）只挂在活动轮次的 TOPIC_PHASE 阶段上；TURN_END / 新一轮 /
 *   clearTopicApplicationsForTurn / resetTopicPhase 把未完成事项一起清掉 → 用户再也点不到重试；
 * - 重试成功后旧的失败提示没有收口，界面上仍像「失败着」。
 *
 * 本轮契约（docs/topic-review-20261110-contract.md §3.4 / §3.6）：
 * - 待恢复事项来自持久恢复事项查询（GET /api/recovery/records，kind=topic_answer_recovery）
 *   与既有重试入口（POST .../retry-answer）；关闭/断连/TURN_END/刷新/重启不删除未完成事项；
 * - 活动轮次状态（stage）/ 申请状态（审批队列）/ 历史恢复事项（topicRecoveryItems）分离；
 * - 成功由权威结果（retry-answer answered=true）关闭对应记录、移除旧失败提示、补读唯一真实答案；
 * - 恢复旧轮次不激活它、不重挂当前轮、不跳当前导航、不误解锁别的申请；
 * - 刷新数据与事件重放按身份/版本合并：已完成不被迟到失败/旧快照覆盖。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";
import { useApprovalsStore } from "../approvals";
import { useUnifiedApprovalsStore } from "../unifiedApprovals";
import { api } from "../../services/api";
import { retryTopicAnswer, topicRetryResultFromBody } from "../../services/approvalLifecycle";
import UnifiedApprovalLayer from "../../components/conversation/UnifiedApprovalLayer.vue";
import type { AgentEvent } from "../../services/events";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
}));

vi.mock("../../services/api", () => ({
  request: mocks.request,
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "t1",
      topic_name: "当前话题",
      anchor_fragment: null,
      messages: [],
    })),
    loadHistory: vi.fn(async () => ({ messages: [], anchor: null })),
    sendTurn: vi.fn(async () => ({ ok: true, accepted: true, turn_id: "turn_x" })),
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

vi.mock("../../services/approvalLifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/approvalLifecycle")>();
  return {
    ...actual,
    getApprovalHistory: vi.fn(async () => []),
    retryTopicAnswer: vi.fn(async () => ({ ok: true, code: "", message: "" })),
  };
});

let seq = 0;
function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: `r14d-f04-${type}-${seq}`, ts: "", data } as AgentEvent;
}

type Store = ReturnType<typeof useSessionStore>;
type EventStore = ReturnType<typeof useEventStore>;

function setup() {
  setActivePinia(createPinia());
  return {
    session: useSessionStore(),
    events: useEventStore(),
    approvals: useApprovalsStore(),
    unified: useUnifiedApprovalsStore(),
  };
}

function startTurn(session: Store, events: EventStore, turnId = "turn_1") {
  session.setAnchor("t1", null, "当前话题", null, false);
  events.route(ev("TURN_START", { turn_id: turnId }));
}

function topicPhase(events: EventStore, data: Record<string, unknown>) {
  events.route(ev("TOPIC_PHASE", data));
}

function recoveryFailure(events: EventStore, opts: { recordId?: string; turnId?: string; message?: string } = {}) {
  topicPhase(events, {
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
  });
}

/** B 定稿的持久恢复记录形状（GET /api/recovery/records）。 */
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
  document.body.innerHTML = "";
  mocks.request.mockReset();
  mocks.request.mockResolvedValue({ ok: true, approvals: [], items: [] });
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
  retryMock().mockResolvedValue({ ok: true, code: "", message: "", answered: true });
});

describe("F04 · 恢复事项不随轮次结束消失", () => {
  it("TOPIC_PHASE.recoverable_failure → TURN_END：活动阶段收口，但恢复入口与事实仍在", () => {
    const { session, events } = setup();
    startTurn(session, events);
    recoveryFailure(events, { recordId: "rec-1" });
    expect(session.stage).toBe("recoverable_failure");
    expect(session.recovery?.recordId).toBe("rec-1");

    events.route(ev("TURN_END", { turn_id: "turn_1", status: "failed" }));

    // 活动轮次状态收口（不再属于当前轮）
    expect(session.stage).toBeNull();
    expect(session.topicStageTurnId).toBeNull();
    // 但未完成事项仍在：入口不消失、仍可重试
    expect(session.recovery?.recordId).toBe("rec-1");
    expect(session.recovery?.retryable).toBe(true);
    expect(session.recoveryItems.map((item) => item.recordId)).toContain("rec-1");
  });

  it("clearTopicApplicationsForTurn / resetTopicPhase / 新一轮 TURN_START 都不删除未完成事项", async () => {
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
    expect(session.recovery?.recordId).toBe("rec-1");
  });

  it("刷新/重启（新 pinia + 持久清单）：未完成事项仍可见、仍能重试；旧轮次不被激活", async () => {
    setActivePinia(createPinia());
    let session = useSessionStore();
    await loadListing(session, [recoveryRecord()]);
    expect(session.recoveryItems).toHaveLength(1);

    // 关闭重开：同一份本机状态（新 pinia）→ 恢复入口来自持久查询
    setActivePinia(createPinia());
    session = useSessionStore();
    expect(session.recoveryItems).toHaveLength(0);
    await loadListing(session, [recoveryRecord()]);
    expect(session.recoveryItems).toHaveLength(1);
    expect(session.activeTurnId).toBeNull();

    session.history = { status: "ready", error: null };
    const before = vi.mocked(api.getSessionContext).mock.calls.length;
    const result = await session.retryTopicAnswer("rec-1");
    expect(result.ok).toBe(true);
    expect(retryMock()).toHaveBeenCalledWith("rec-1");
    // 恢复旧轮次不激活它、不重挂当前轮
    expect(session.activeTurnId).toBeNull();
    expect(session.topicStageTurnId).toBeNull();
    // 成功：补读唯一真实答案（既有历史读取通道）
    expect(vi.mocked(api.getSessionContext).mock.calls.length).toBe(before + 1);
  });
});

describe("F04 · 成功由权威结果收口", () => {
  it("重试成功：关闭对应记录、移除旧失败提示、补读答案；迟到旧快照/事件不复活", async () => {
    const { session, events } = setup();
    startTurn(session, events);
    recoveryFailure(events, { recordId: "rec-1" });
    await loadListing(session, [recoveryRecord()]);
    session.history = { status: "ready", error: null };
    retryMock().mockResolvedValueOnce({
      ok: true,
      code: "",
      message: "已按原问题重新生成回答。",
      answered: true,
      answerId: "ans-1",
    });

    const result = await session.retryTopicAnswer("rec-1");
    expect(result.ok).toBe(true);
    expect(session.recovery).toBeNull();
    expect(session.recoveryItems).toHaveLength(0);
    // 旧失败提示不再成立
    expect(session.recoveryError).not.toContain("没有准备好");

    // 迟到 / 重放的旧快照仍说这条 pending：不得复活（本地已 resolved）
    await loadListing(session, [recoveryRecord()]);
    expect(session.recoveryItems).toHaveLength(0);
    // 重放失败事件（同一轮已结束）也不得复活
    recoveryFailure(events, { recordId: "rec-1" });
    expect(session.recoveryItems).toHaveLength(0);
    expect(session.recovery).toBeNull();
  });

  it("只关闭这一条：别的恢复事项与别的申请都不受影响（不误解锁）", async () => {
    const { session, events, unified } = setup();
    startTurn(session, events);
    // 另一条持久恢复事项
    await loadListing(session, [
      recoveryRecord(),
      recoveryRecord({ record_id: "rec-2", turn_id: "turn_old", message: "另一条失败" }),
    ]);
    expect(session.recoveryItems).toHaveLength(2);

    retryMock().mockResolvedValueOnce({ ok: true, code: "", message: "", answered: true });
    await session.retryTopicAnswer("rec-1");

    expect(session.recoveryItems.map((item) => item.recordId)).toEqual(["rec-2"]);
    expect(session.recovery?.recordId).toBe("rec-2");

    // 另一条待决定的话题申请仍保持等待（发送限制不被误解锁）
    events.route(
      ev("APPROVAL_REQUIRED", {
        approval: {
          approval_id: "ap-live",
          kind: "topic_switch",
          turn_id: "turn_1",
          payload: {
            action: "switch",
            turn_id: "turn_1",
            from_topic_id: "t1",
            target_topic_id: "t3",
            operation_id: "op-live",
            param_version: 1,
          },
        },
        phase: "waiting",
      }),
    );
    expect(session.topicSendLock?.approvalId).toBe("ap-live");
    expect(unified.items.some((item) => item.kind === "topic")).toBe(true);
  });

  it("失败 / 处理中：不关闭记录、不伪造成功", async () => {
    const { session, events } = setup();
    startTurn(session, events);
    await loadListing(session, [recoveryRecord()]);

    retryMock().mockResolvedValueOnce({
      ok: false,
      code: "topic_application_stale",
      message: "目标位置已经变化，恢复不会覆盖你的新选择。",
    });
    const failed = await session.retryTopicAnswer("rec-1");
    expect(failed.ok).toBe(false);
    expect(session.recoveryItems).toHaveLength(1);

    // 并发生成中（后端 202 recovery_in_progress）：记录保留，不当成成功
    retryMock().mockResolvedValueOnce({
      ok: false,
      code: "recovery_in_progress",
      message: "这条恢复正在处理中，请稍后刷新",
      inProgress: true,
    });
    const busy = await session.retryTopicAnswer("rec-1");
    expect(busy.ok).toBe(false);
    expect(busy.code).toBe("recovery_in_progress");
    expect(session.recoveryItems).toHaveLength(1);
  });

  it("单飞：同一条记录的重复点击只发一次请求", async () => {
    const { session, events } = setup();
    startTurn(session, events);
    await loadListing(session, [recoveryRecord()]);
    const gate: { release?: (value: unknown) => void } = {};
    retryMock().mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          gate.release = resolve as (value: unknown) => void;
        }) as never,
    );
    const first = session.retryTopicAnswer("rec-1");
    const second = await session.retryTopicAnswer("rec-1");
    expect(second.ok).toBe(false);
    expect(second.code).toBe("recovery_in_progress");
    gate.release?.({ ok: true, code: "", message: "", answered: true });
    expect((await first).ok).toBe(true);
    expect(retryMock()).toHaveBeenCalledTimes(1);
  });
});

describe("F04 · 刷新/重放按身份/版本合并", () => {
  it("清单里已 answered / 已完成的记录：不进入待恢复入口", async () => {
    const { session } = setup();
    await loadListing(session, [
      recoveryRecord({ record_id: "rec-done", status: "answered", state_class: "answered" }),
    ]);
    expect(session.recoveryItems).toHaveLength(0);
    expect(session.recovery).toBeNull();
  });

  it("同一 record 的迟到旧版本（updated_at 更旧）不覆盖本地较新的失败事实", async () => {
    const { session } = setup();
    await loadListing(session, [
      recoveryRecord({ updated_at: "2026-11-10T12:00:05Z", attempt_count: 2, attempts: 2, message: "较新的失败原因" }),
    ]);
    expect(session.recovery?.message).toBe("较新的失败原因");
    await loadListing(session, [
      recoveryRecord({ updated_at: "2026-11-10T12:00:00Z", attempt_count: 1, attempts: 1, message: "更旧的失败原因" }),
    ]);
    expect(session.recoveryItems).toHaveLength(1);
    expect(session.recovery?.message).toBe("较新的失败原因");
  });

  it("answer_in_progress（有租约正在生成）：记录可见但不可重复重试", async () => {
    const { session } = setup();
    await loadListing(session, [
      recoveryRecord({
        state_class: "answer_in_progress",
        retryable: false,
        actions: [{ id: "retry_answer", label: "重试回答", enabled: false, reason: "正在生成回答，请稍后刷新" }],
      }),
    ]);
    expect(session.recoveryItems).toHaveLength(1);
    expect(session.recovery?.retryable).toBe(false);
    const result = await session.retryTopicAnswer("rec-1");
    expect(result.ok).toBe(false);
    expect(result.code).toBe("not_retryable");
    expect(retryMock()).not.toHaveBeenCalled();
  });
});

describe("F04 · 权威清单刷新与竞态", () => {
  it("刷新后权威清单不再包含（已在别处完成）：入口关闭，重放的旧失败事件不复活", async () => {
    const { session, events } = setup();
    startTurn(session, events);
    recoveryFailure(events, { recordId: "rec-gone" });
    expect(session.recovery?.recordId).toBe("rec-gone");

    // 下一次权威清单里已经没有它（后端已 answered）→ 关闭入口并记入 resolved
    await loadListing(session, []);
    expect(session.recoveryItems).toHaveLength(0);
    expect(session.recovery).toBeNull();

    // 迟到的失败事件重放：不得复活
    recoveryFailure(events, { recordId: "rec-gone" });
    expect(session.recoveryItems).toHaveLength(0);
    expect(session.recovery).toBeNull();
  });

  it("清单读取期间新到的失败事件：不被这次（更旧的）清单误删", async () => {
    const { session, events } = setup();
    startTurn(session, events);
    const gate: { release?: (value: unknown) => void } = {};
    vi.mocked(api.getRecoveryRecords).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          gate.release = resolve as (value: unknown) => void;
        }) as never,
    );
    const loading = session.loadRecoveryInbox();
    // 读取在飞时产生新的失败事实（后端记录已落库，但这份响应里没有它）
    recoveryFailure(events, { recordId: "rec-race" });
    gate.release?.({
      records: [],
      total: 0,
      shown: 0,
      truncated: false,
    });
    await loading;

    expect(session.recoveryItems.map((item) => item.recordId)).toContain("rec-race");
    expect(session.recovery?.recordId).toBe("rec-race");
  });

  it("清单读取失败：保留既有入口（拉不到 ≠ 没有）", async () => {
    const { session, events } = setup();
    startTurn(session, events);
    recoveryFailure(events, { recordId: "rec-keep" });
    vi.mocked(api.getRecoveryRecords).mockRejectedValueOnce(new Error("网络中断"));
    await session.loadRecoveryInbox();
    expect(session.recoveryItems.map((item) => item.recordId)).toContain("rec-keep");
    expect(session.recoveryError).toContain("没能读取");
  });
});

describe("F04 · 用户可见入口（统一层提示块）", () => {
  it("TURN_END 之后仍显示恢复提示，点重试走唯一 store 入口；成功后提示消失", async () => {
    const { session, events } = setup();
    startTurn(session, events);
    recoveryFailure(events, { recordId: "rec-1" });
    events.route(ev("TURN_END", { turn_id: "turn_1", status: "failed" }));

    const spy = vi.spyOn(session, "retryTopicAnswer").mockResolvedValue({
      ok: true,
      code: "",
      message: "已按原问题重新生成回答。",
      answered: true,
    });
    const w = mount(UnifiedApprovalLayer, { props: { showMemory: false } });
    await flushPromises();
    const notice = w.find('[data-test="topic-stage-notice"]');
    expect(notice.exists()).toBe(true);
    expect(notice.attributes("data-stage")).toBe("recoverable_failure");
    expect(notice.text()).toContain("没有准备好");

    await w.find('[data-test="topic-stage-retry"]').trigger("click");
    await flushPromises();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy.mock.calls[0][0]).toBe("rec-1");
    w.unmount();
  });
});

describe("F04 · retry-answer 响应解析（approvalLifecycle）", () => {
  it("answered=true 才是权威成功；202 recovery_in_progress 是处理中，不是成功", () => {
    const ok = topicRetryResultFromBody(
      {
        ok: true,
        answered: true,
        record_id: "rec-1",
        turn_id: "turn_1",
        topic_id: "t2",
        answer_id: "ans-1",
        answer: "真实回答",
      },
      200,
    );
    expect(ok.ok).toBe(true);
    expect(ok.answered).toBe(true);
    expect(ok.answerId).toBe("ans-1");

    const busy = topicRetryResultFromBody(
      {
        ok: true,
        answered: false,
        in_progress: true,
        code: "recovery_in_progress",
        record_id: "rec-1",
        message: "这条恢复正在处理中，请稍后刷新",
      },
      202,
    );
    expect(busy.ok).toBe(false);
    expect(busy.code).toBe("recovery_in_progress");
    expect(busy.inProgress).toBe(true);
  });

  it("409/404/503 结构化失败如实返回 code（不包装成成功）", () => {
    const stale = topicRetryResultFromBody(
      { ok: false, conflict: true, code: "topic_application_stale", message: "位置已变化" },
      409,
    );
    expect(stale.ok).toBe(false);
    expect(stale.code).toBe("topic_application_stale");

    const missing = topicRetryResultFromBody({ ok: false, code: "recovery_not_found", message: "没有这条恢复记录" }, 404);
    expect(missing.ok).toBe(false);
    expect(missing.code).toBe("recovery_not_found");
  });
});