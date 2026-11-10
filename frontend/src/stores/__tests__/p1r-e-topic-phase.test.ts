/**
 * P1R-E：前端状态层定向测试（话题阶段 / 申请失效 / 旧事件丢弃 / 发送限制 / 幂等 / 恢复）。
 *
 * 只钉行为，不测实现细节：
 * 1) TOPIC_PHASE 阶段派生：stage + recovery（recordId/message/retryable），未知阶段忽略；
 * 2) 旧事件丢弃：别的轮、别的 operation、阶段回退、轮结束后迟到的事件都不改当前位置；
 * 3) 409 topic_application_stale / APPROVAL_RESULT decision=stale → 申请 invalidated：
 *    保留在队列里说明原因、原确认不可执行、不写拒绝范围、不自动重弹、不误解锁另一条；
 * 4) 正文门：等待/执行阶段不展示正式正文，只有 answerable 之后才展示；旧后端（无 TOPIC_PHASE）行为不变；
 * 5) 草稿与发送限制：等待期间可写草稿、本对话发送受限（来源标注 topic_application）、
 *    确认后草稿跟随目标但不自动发送、失效/拒绝后限制解除且草稿留在原话题；
 * 6) 幂等：双击/重放只提交一次，第二次返回既有结果；retryAnswer 单飞；
 * 7) 操作身份字段：operationId / paramVersion / baseNavVersion / conflict / sourceTopicApprovalId。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";
import { useApprovalsStore } from "../approvals";
import { useUnifiedApprovalsStore } from "../unifiedApprovals";
import { draftStorageKey, readDraft, writeDraft } from "../../interactive/drafts";
import { api } from "../../services/api";
import { retryTopicAnswer } from "../../services/approvalLifecycle";
import type { AgentEvent } from "../../services/events";

vi.mock("../../services/api", () => ({
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
  return { type, id: `p1r-e-${type}-${seq}`, ts: "", data } as AgentEvent;
}

function chatKey(topicId: string): string {
  return draftStorageKey("chat", topicId);
}
function storedText(topicId: string): string | null {
  return readDraft(chatKey(topicId))?.text ?? null;
}

type Store = ReturnType<typeof useSessionStore>;
type EventStore = ReturnType<typeof useEventStore>;

function setup() {
  setActivePinia(createPinia());
  const session = useSessionStore();
  const events = useEventStore();
  const unified = useUnifiedApprovalsStore();
  const approvals = useApprovalsStore();
  return { session, events, unified, approvals };
}

function startTurn(session: Store, events: EventStore) {
  session.setAnchor("t1", null, "当前话题", null, false);
  events.route(ev("TURN_START", { turn_id: "turn_1" }));
}

interface RequiredOptions {
  approvalId?: string;
  kind?: "topic_switch" | "topic_create";
  turnId?: string;
  targetTopicId?: string | null;
  newName?: string;
  operationId?: string | null;
  paramVersion?: number | null;
  baseNavVersion?: number | null;
  conflict?: Record<string, unknown> | null;
}

function topicRequired(events: EventStore, opts: RequiredOptions = {}): string {
  const approvalId = opts.approvalId ?? "ap-topic-1";
  const kind = opts.kind ?? "topic_switch";
  const turnId = opts.turnId ?? "turn_1";
  const payload: Record<string, unknown> = {
    action: kind === "topic_create" ? "create" : "switch",
    turn_id: turnId,
    from_topic_id: "t1",
    from_topic_name: "当前话题",
    reason: "这段内容更像目标话题",
  };
  if (opts.operationId !== null) payload.operation_id = opts.operationId ?? "op-1";
  if (opts.paramVersion !== null) payload.param_version = opts.paramVersion ?? 1;
  if (opts.baseNavVersion !== null) payload.base_nav_version = opts.baseNavVersion ?? 1;
  if (kind === "topic_create") {
    payload.new_name = opts.newName ?? "新话题";
  } else {
    payload.target_topic_id = opts.targetTopicId ?? "t2";
    payload.target_topic_name = "目标话题";
  }
  if (opts.conflict) payload.conflict = opts.conflict;
  events.route(
    ev("APPROVAL_REQUIRED", {
      approval: { approval_id: approvalId, kind, payload, turn_id: turnId },
      phase: "waiting",
    }),
  );
  return approvalId;
}

function approvalResult(events: EventStore, approvalId: string, phase: string, extra: Record<string, unknown> = {}) {
  events.route(
    ev("APPROVAL_RESULT", {
      approval_id: approvalId,
      phase,
      decision: phase === "rejected" ? "rejected" : "approved",
      turn_id: "turn_1",
      ...extra,
    }),
  );
}

/**
 * 权威成功结果（F12）：话题工具真实执行成功后的 ANCHOR（带 operation 身份）。
 * 草稿跟随只消费这种事件；手动导航不迁移、不提前消费。
 */
function anchor(events: EventStore, topicId: string, name = "目标话题") {
  events.route(
    ev("ANCHOR", {
      topic_id: topicId,
      topic_name: name,
      fragment_id: null,
      fragment_title: null,
      historic: false,
      origin: "topic_operation",
      operation_id: "op-1",
      param_version: 1,
      turn_id: "turn_1",
      nav_version: 7,
      seq: 5,
    }),
  );
}

function phase(events: EventStore, data: Record<string, unknown>) {
  events.route(ev("TOPIC_PHASE", data));
}

function waitingDraft(session: Store, text: string) {
  session.draft = text;
  session.flushDraft();
}

function topicItem(unified: ReturnType<typeof useUnifiedApprovalsStore>) {
  return unified.items.find((item) => item.kind === "topic") ?? null;
}

beforeEach(() => {
  localStorage.clear();
  seq = 0;
  vi.mocked(api.respondTopicApproval).mockClear();
  vi.mocked(api.respondTopicApproval).mockResolvedValue({ ok: true });
  vi.mocked(api.sendTurn).mockClear();
  vi.mocked(retryTopicAnswer).mockClear();
  vi.mocked(retryTopicAnswer).mockResolvedValue({ ok: true, code: "", message: "" });
});

/* ───────────────────────────── 1. 阶段派生 ───────────────────────────── */

describe("TOPIC_PHASE：阶段与恢复信息派生", () => {
  it("按顺序推进阶段，stage 反映最新阶段；统一队列按 operation+turn 匹配到同一条申请", () => {
    const { session, events, unified } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1" });
    const item = topicItem(unified);
    expect(item?.topic?.approvalId).toBe(id);
    expect(session.stage).toBeNull();

    for (const stage of ["decision_open", "awaiting_confirmation", "executing", "context_preparing"]) {
      phase(events, { phase: stage, operation_id: "op-1", approval_id: id, turn_id: "turn_1" });
      expect(session.stage).toBe(stage);
      expect(unified.topicStageFor(itemOrNull(item))).toBe(stage);
    }
    phase(events, { phase: "answerable", operation_id: "op-1", approval_id: id, turn_id: "turn_1" });
    expect(session.stage).toBe("answerable");
    expect(session.recovery).toBeNull();
    expect(unified.topicRecoveryFor(itemOrNull(item))).toBeNull();
  });

  it("recoverable_failure 派生真实恢复信息；重试推进后恢复信息清空", () => {
    const { session, events } = setup();
    startTurn(session, events);
    phase(events, { phase: "executing", operation_id: "op-1", turn_id: "turn_1" });
    phase(events, {
      phase: "recoverable_failure",
      operation_id: "op-1",
      approval_id: "ap-topic-1",
      turn_id: "turn_1",
      recovery: {
        record_id: "rec-1",
        kind: "topic_answer_recovery",
        failed_stage: "context_prepare",
        message: "目标话题的上下文没有准备好",
        retryable: true,
      },
    });
    expect(session.stage).toBe("recoverable_failure");
    expect(session.recovery).toEqual({
      recordId: "rec-1",
      message: "目标话题的上下文没有准备好",
      retryable: true,
    });

    phase(events, { phase: "context_preparing", operation_id: "op-1", turn_id: "turn_1" });
    phase(events, { phase: "answerable", operation_id: "op-1", turn_id: "turn_1" });
    expect(session.stage).toBe("answerable");
    expect(session.recovery).toBeNull();
  });

  it("未知阶段不改状态（不猜、不伪造）", () => {
    const { session, events } = setup();
    startTurn(session, events);
    phase(events, { phase: "deciding", operation_id: "op-1", turn_id: "turn_1" });
    expect(session.stage).toBeNull();
    phase(events, { phase: "answerable", operation_id: "op-1", turn_id: "turn_1" });
    phase(events, { phase: "", operation_id: "op-1", turn_id: "turn_1" });
    expect(session.stage).toBe("answerable");
  });
});

/* ───────────────────── 2. 旧事件丢弃（不改当前位置） ───────────────────── */

describe("旧事件/乱序事件：不改当前位置", () => {
  it("别的轮、别的 operation、阶段回退、answerable 之后迟到的事件全部丢弃", () => {
    const { session, events } = setup();
    startTurn(session, events);
    phase(events, { phase: "context_preparing", operation_id: "op-1", turn_id: "turn_1" });
    expect(session.stage).toBe("context_preparing");

    // 别的轮
    phase(events, { phase: "answerable", operation_id: "op-1", turn_id: "turn_other" });
    expect(session.stage).toBe("context_preparing");
    // 别的 operation
    phase(events, { phase: "answerable", operation_id: "op-2", turn_id: "turn_1" });
    expect(session.stage).toBe("context_preparing");
    // 阶段回退（旧事件）
    phase(events, { phase: "executing", operation_id: "op-1", turn_id: "turn_1" });
    expect(session.stage).toBe("context_preparing");
    // 正常推进
    phase(events, { phase: "answerable", operation_id: "op-1", turn_id: "turn_1" });
    expect(session.stage).toBe("answerable");
    // ANSWERABLE 之后迟到的更早阶段
    phase(events, { phase: "executing", operation_id: "op-1", turn_id: "turn_1" });
    expect(session.stage).toBe("answerable");
    // 重复同一条事件幂等
    phase(events, { phase: "answerable", operation_id: "op-1", turn_id: "turn_1" });
    expect(session.stage).toBe("answerable");
  });

  it("旧 operation 的结局事件不消费：不解除等待、不搬草稿、不影响后续真实结果", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "旧操作事件不该搬我");

    events.route(
      ev("APPROVAL_RESULT", {
        approval_id: id,
        phase: "approved",
        decision: "approved",
        operation_id: "op-old",
        turn_id: "turn_1",
      }),
    );
    expect(session.topicApprovalPending).toBe(true);
    expect(session._topicDraftFollow).toBeNull();
    expect(session.draft).toBe("旧操作事件不该搬我");

    // 同一 approval_id 的真实操作结果仍按正常路径生效
    approvalResult(events, id, "rejected");
    expect(session.topicApprovalPending).toBe(false);
    expect(session.draft).toBe("旧操作事件不该搬我");
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("轮结束之后迟到的阶段事件不改状态", () => {
    const { session, events } = setup();
    startTurn(session, events);
    phase(events, { phase: "awaiting_confirmation", operation_id: "op-1", turn_id: "turn_1" });
    events.route(ev("TURN_END", { turn_id: "turn_1", status: "completed" }));
    expect(session.stage).toBeNull();
    phase(events, { phase: "answerable", operation_id: "op-1", turn_id: "turn_1" });
    expect(session.stage).toBeNull();
  });
});

/* ───────────────────────── 3. 正文门（可回答判据） ───────────────────────── */

describe("正式正文：只有 answerable 之后才展示", () => {
  it("等待/执行阶段到达的正式正文不进入回答流，answerable 之后正常展示", () => {
    const { session, events } = setup();
    startTurn(session, events);
    phase(events, { phase: "awaiting_confirmation", operation_id: "op-1", turn_id: "turn_1" });
    events.route(ev("ASSISTANT", { content: "不该出现的正式回答", turn_id: "turn_1", interim: false }));
    expect(session.messages.some((m) => m.role === "assistant" && m.content.includes("不该出现"))).toBe(false);

    phase(events, { phase: "answerable", operation_id: "op-1", turn_id: "turn_1" });
    events.route(ev("ASSISTANT", { content: "这是正式回答", turn_id: "turn_1", interim: false }));
    expect(session.messages.some((m) => m.role === "assistant" && m.content.includes("这是正式回答"))).toBe(true);
  });

  it("标准顺序（B 修复后）：先 answerable 再正式正文 → 立即展示且只展示一次", () => {
    const { session, events } = setup();
    startTurn(session, events);
    phase(events, { phase: "awaiting_confirmation", operation_id: "op-1", turn_id: "turn_1" });
    phase(events, { phase: "answerable", operation_id: "op-1", turn_id: "turn_1" });
    events.route(
      ev("ASSISTANT", {
        content: "标准顺序的最终回答",
        turn_id: "turn_1",
        delta_id: "d-std",
        seq: 1,
        interim: false,
        streaming: false,
      }),
    );
    const shown = session.messages.filter(
      (m) => m.role === "assistant" && m.content.includes("标准顺序的最终回答"),
    );
    expect(shown).toHaveLength(1);
  });

  it("契约原文顺序（容错）：正文先到、answerable 后到 → 先不展示，最终原样回放一次", () => {
    const { session, events } = setup();
    startTurn(session, events);
    phase(events, { phase: "awaiting_confirmation", operation_id: "op-1", turn_id: "turn_1" });
    // B 修复前的顺序：gated 正式正文先发，answerable 随后
    events.route(
      ev("ASSISTANT", {
        content: "先到的最终回答",
        turn_id: "turn_1",
        delta_id: "d-late",
        seq: 1,
        interim: false,
        streaming: false,
      }),
    );
    expect(
      session.messages.some((m) => m.role === "assistant" && m.content.includes("先到的最终回答")),
    ).toBe(false);
    // 同一 delta 的重复累计快照：暂存期也只保留一份
    events.route(
      ev("ASSISTANT", {
        content: "先到的最终回答",
        turn_id: "turn_1",
        delta_id: "d-late",
        seq: 1,
        interim: false,
        streaming: false,
      }),
    );
    expect(
      session.messages.some((m) => m.role === "assistant" && m.content.includes("先到的最终回答")),
    ).toBe(false);

    phase(events, { phase: "answerable", operation_id: "op-1", turn_id: "turn_1" });
    const shown = session.messages.filter(
      (m) => m.role === "assistant" && m.content.includes("先到的最终回答"),
    );
    expect(shown).toHaveLength(1);
    // 回放后不残留：后续重复事件不重复展示
    phase(events, { phase: "answerable", operation_id: "op-1", turn_id: "turn_1" });
    expect(
      session.messages.filter((m) => m.role === "assistant" && m.content.includes("先到的最终回答")),
    ).toHaveLength(1);
  });

  it("旧后端没有 TOPIC_PHASE：正文照旧展示（不隐藏既有事件充数）", () => {
    const { session, events } = setup();
    startTurn(session, events);
    events.route(ev("ASSISTANT", { content: "旧后端的回答", turn_id: "turn_1", interim: false }));
    expect(session.stage).toBeNull();
    expect(session.messages.some((m) => m.role === "assistant" && m.content.includes("旧后端的回答"))).toBe(true);
  });
});

/* ───────────────────────── 4. 409 失效 ───────────────────────── */

describe("申请失效：409 topic_application_stale", () => {
  it("标记 invalidated：保留说明、原确认不可执行、不写拒绝范围、不自动重弹", async () => {
    const { session, events, unified, approvals } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1" });
    const item = topicItem(unified);
    expect(item).not.toBeNull();

    vi.mocked(api.respondTopicApproval).mockRejectedValueOnce(
      Object.assign(new Error("stale"), {
        status: 409,
        body: { detail: { code: "topic_application_stale", message: "本轮绑定的话题已变化" } },
      }),
    );
    const ok = await unified.decideTopic(itemOrThrow(item), { decision: "approve" });
    expect(ok).toBe(false);
    expect(api.respondTopicApproval).toHaveBeenCalledTimes(1);
    // 提交的是批准，没有顺手写拒绝范围
    const call = vi.mocked(api.respondTopicApproval).mock.calls[0];
    expect(call[1]).toBe("approved");
    expect((call[2] as { scope?: unknown } | undefined)?.scope).toBeUndefined();

    const after = approvals.queue.find((entry) => entry.approval_id === id);
    expect(after?.invalidated).toBe(true);
    expect(after?.invalidatedCode).toBe("topic_application_stale");
    expect(after?.invalidatedReason).toContain("失效");
    const itemAfter = topicItem(unified);
    expect(itemAfter?.invalidated).toBe(true);
    expect(itemAfter?.topic?.invalidated).toBe(true);
    expect(itemAfter?.topic?.invalidatedReason).toContain("失效");
    expect(unified.isTopicDecisionDisabled(itemOrNull(itemAfter))).toBe(true);
    // 不自动重弹
    expect(unified.maybeAutoOpen()).toBe(false);
    // 失效后再次点确认：不发请求
    await unified.decideTopic(itemOrThrow(itemAfter), { decision: "reject" });
    expect(api.respondTopicApproval).toHaveBeenCalledTimes(1);
  });

  it("APPROVAL_RESULT decision=stale：只失效该条，不误解锁另一条申请", () => {
    const { session, events, unified } = setup();
    startTurn(session, events);
    topicRequired(events, { approvalId: "ap-1", operationId: "op-1", turnId: "turn_1" });
    events.route(ev("TURN_START", { turn_id: "turn_2" }));
    topicRequired(events, { approvalId: "ap-2", operationId: "op-2", turnId: "turn_2" });

    events.route(
      ev("APPROVAL_RESULT", {
        approval_id: "ap-1",
        decision: "stale",
        message: "这条申请已经失效",
        turn_id: "turn_1",
      }),
    );

    const ap1 = unified.items.find((item) => item.topic?.approvalId === "ap-1");
    const ap2 = unified.items.find((item) => item.topic?.approvalId === "ap-2");
    expect(ap1?.invalidated).toBe(true);
    expect(ap1?.invalidatedReason).toContain("失效");
    expect(ap2?.invalidated).toBe(false);
    expect(ap2?.approval?.stale).not.toBe(true);
    expect(session.stage).toBeNull();
  });

  it("失效项不因服务端 pending 快照里没有它而被静默删除（快照收敛后说明仍在）", async () => {
    const { session, events, unified, approvals } = setup();
    startTurn(session, events);
    topicRequired(events, { operationId: "op-1" });
    const item = topicItem(unified);
    vi.mocked(api.respondTopicApproval).mockRejectedValueOnce(
      Object.assign(new Error("stale"), {
        status: 409,
        body: { detail: { code: "topic_application_stale", message: "本轮绑定的话题已变化" } },
      }),
    );
    await unified.decideTopic(itemOrThrow(item), { decision: "approve" });

    // 服务端 pending 快照里已经没有这条（失效后不再等待）——收敛也不能删掉说明
    approvals.reconcile([]);
    approvals.reconcileSince([], Number.MAX_SAFE_INTEGER);
    const after = topicItem(unified);
    expect(after?.invalidated).toBe(true);
    expect(after?.invalidatedReason).toContain("失效");
    expect(unified.isTopicDecisionDisabled(itemOrNull(after))).toBe(true);
  });

  it("失效收口：acknowledgeInvalidatedTopic 不发请求、不写拒绝范围、保留改过的名字", async () => {
    const { session, events, unified, approvals } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1" });
    const item = topicItem(unified);
    vi.mocked(api.respondTopicApproval).mockRejectedValueOnce(
      Object.assign(new Error("stale"), {
        status: 409,
        body: { detail: { code: "topic_application_stale", message: "本轮绑定的话题已变化" } },
      }),
    );
    await unified.decideTopic(itemOrThrow(item), { decision: "approve" });
    const invalidated = topicItem(unified);
    unified.updateTopicDraft(invalidated!.key, "用户改过的名字");

    unified.acknowledgeInvalidatedTopic(itemOrThrow(invalidated));
    expect(approvals.queue.some((entry) => entry.approval_id === id)).toBe(false);
    // 只发生过用户主动提交的那一次，收口本身不发任何请求
    expect(api.respondTopicApproval).toHaveBeenCalledTimes(1);
    // 改过的名字保留
    expect(unified.topicDrafts[invalidated!.key]).toBe("用户改过的名字");
    expect(unified.maybeAutoOpen()).toBe(false);
  });
});

/* ───────────────────────── 5. retryAnswer ───────────────────────── */

describe("恢复入口：retryTopicAnswer", () => {
  it("成功：调用一次恢复通道并给出可见结果", async () => {
    const { session, events, unified } = setup();
    startTurn(session, events);
    phase(events, {
      phase: "recoverable_failure",
      operation_id: "op-1",
      turn_id: "turn_1",
      recovery: { record_id: "rec-1", message: "上下文准备失败", retryable: true },
    });
    vi.mocked(retryTopicAnswer).mockResolvedValueOnce({ ok: true, code: "", message: "已经重新开始回答" });

    const ok = await unified.retryTopicAnswer("rec-1");
    expect(ok).toBe(true);
    expect(retryTopicAnswer).toHaveBeenCalledTimes(1);
    expect(retryTopicAnswer).toHaveBeenCalledWith("rec-1");
    expect(unified.lastNotice.length).toBeGreaterThan(0);
  });

  it("失败：如实说明不可用，不假装成功", async () => {
    const { session, events, unified } = setup();
    startTurn(session, events);
    phase(events, {
      phase: "recoverable_failure",
      operation_id: "op-1",
      turn_id: "turn_1",
      recovery: { record_id: "rec-2", message: "上下文准备失败", retryable: true },
    });
    vi.mocked(retryTopicAnswer).mockResolvedValueOnce({
      ok: false,
      code: "not_found",
      message: "恢复入口暂不可用",
    });
    const ok = await unified.retryTopicAnswer("rec-2");
    expect(ok).toBe(false);
    expect(unified.submitError).toContain("恢复入口暂不可用");
    expect(retryTopicAnswer).toHaveBeenCalledTimes(1);
  });

  it("恢复成功后用既有历史读取把后端持久化的回答补读回对话；失败不补读", async () => {
    const { session, events, unified } = setup();
    startTurn(session, events);
    phase(events, {
      phase: "recoverable_failure",
      operation_id: "op-1",
      turn_id: "turn_1",
      recovery: { record_id: "rec-backfill", message: "上下文准备失败", retryable: true },
    });
    // 失败轮已经结束（真实场景里用户随后点重试），并且当前话题的历史已经载入过
    events.route(ev("TURN_END", { turn_id: "turn_1", status: "failed" }));
    session.history = { status: "ready", error: null };
    const before = vi.mocked(api.getSessionContext).mock.calls.length;

    vi.mocked(retryTopicAnswer).mockResolvedValueOnce({ ok: true, code: "", message: "" });
    expect(await unified.retryTopicAnswer("rec-backfill")).toBe(true);
    // 成功 → 触发一次既有历史补读（回答已在后端持久化，前端没有事件）
    expect(vi.mocked(api.getSessionContext).mock.calls.length).toBe(before + 1);

    // 失败 → 不补读：另一条（尚未成功的）恢复事项走同一入口，失败原因如实返回
    const afterSuccess = vi.mocked(api.getSessionContext).mock.calls.length;
    session.topicRecovery = { recordId: "rec-fail", message: "上下文准备失败", retryable: true };
    vi.mocked(retryTopicAnswer).mockResolvedValueOnce({
      ok: false,
      code: "not_found",
      message: "恢复入口暂不可用",
    });
    expect(await unified.retryTopicAnswer("rec-fail")).toBe(false);
    expect(vi.mocked(api.getSessionContext).mock.calls.length).toBe(afterSuccess);
    expect(unified.submitError).toContain("恢复入口暂不可用");
  });

  it("重复点击：请求在飞时不再发第二次", async () => {
    const { session, events, unified } = setup();
    startTurn(session, events);
    // F04：恢复入口来自真实恢复事项（持久/事件种子），不能凭任意 recordId 直接重试
    phase(events, {
      phase: "recoverable_failure",
      operation_id: "op-1",
      turn_id: "turn_1",
      recovery: { record_id: "rec-3", message: "上下文准备失败", retryable: true },
    });
    const gate: { release?: (value: { ok: boolean; code: string; message: string }) => void } = {};
    vi.mocked(retryTopicAnswer).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          gate.release = resolve;
        }),
    );
    const first = unified.retryTopicAnswer("rec-3");
    const second = unified.retryTopicAnswer("rec-3");
    expect(await second).toBe(false);
    gate.release?.({ ok: true, code: "", message: "" });
    expect(await first).toBe(true);
    expect(retryTopicAnswer).toHaveBeenCalledTimes(1);
  });
});

/* ───────────────────── 6. 发送限制与草稿四类行为 ───────────────────── */

describe("发送限制：只限制本对话的发送动作，不锁死应用", () => {
  it("等待期间：本对话发送限制生效（来源=本申请），草稿可写但不自动发送", () => {
    const { session, events } = setup();
    startTurn(session, events);
    expect(session.topicSendLock).toBeNull();
    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });

    expect(session.topicSendLock).not.toBeNull();
    expect(session.topicSendLock?.source).toBe("topic_application");
    expect(session.topicSendLock?.approvalId).toBe(id);
    expect(session.topicSendLock?.operationId).toBe("op-1");

    waitingDraft(session, "等待期间写的话");
    expect(storedText("t1")).toBe("等待期间写的话");
    expect(api.sendTurn).not.toHaveBeenCalled();

    approvalResult(events, id, "rejected");
    expect(session.topicSendLock).toBeNull();
    expect(session.draft).toBe("等待期间写的话");
    expect(storedText("t1")).toBe("等待期间写的话");
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("别的轮次的申请不锁本对话", () => {
    const { session, events } = setup();
    startTurn(session, events);
    topicRequired(events, { approvalId: "ap-other", turnId: "turn_other", operationId: "op-x" });
    expect(session.topicSendLock).toBeNull();
  });

  it("确认后：限制解除，草稿留在输入框并跟随真实目标，不自动发送", async () => {
    const { session, events, unified } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "确认后还要发的话");

    // 真实顺序：用户点了「转到并回答」→ 提交成功（申请出队）→ 后端执行结果事件
    const item = topicItem(unified);
    expect(await unified.decideTopic(itemOrThrow(item), { decision: "approve" })).toBe(true);
    approvalResult(events, id, "approved");
    expect(session.topicSendLock).toBeNull();
    anchor(events, "t2");

    expect(session.currentTopicId).toBe("t2");
    expect(session.draft).toBe("确认后还要发的话");
    expect(storedText("t2")).toBe("确认后还要发的话");
    expect(storedText("t1")).toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("失效后：限制解除、草稿留在原话题，用户可以在当前话题继续写", async () => {
    const { session, events, unified } = setup();
    startTurn(session, events);
    topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "失效后继续在这里问");
    const item = topicItem(unified);
    vi.mocked(api.respondTopicApproval).mockRejectedValueOnce(
      Object.assign(new Error("stale"), {
        status: 409,
        body: { detail: { code: "topic_application_stale", message: "本轮绑定的话题已变化" } },
      }),
    );
    await unified.decideTopic(itemOrThrow(item), { decision: "approve" });

    expect(session.topicSendLock).toBeNull();
    expect(session.currentTopicId).toBe("t1");
    expect(session.draft).toBe("失效后继续在这里问");
    expect(storedText("t1")).toBe("失效后继续在这里问");
    expect(api.sendTurn).not.toHaveBeenCalled();
  });
});

/* ─────────────────── 6b. 草稿另外两类（不覆盖 / 不通配） ─────────────────── */

describe("草稿：目标已有草稿不覆盖；跟随不通配", () => {
  it("目标话题已有草稿：跟过去的新稿留在输入框，原稿登记为可恢复的被让位草稿", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const stored = writeDraft(chatKey("t2"), "目标话题原有的草稿", 1);
    expect(stored.ok).toBe(true);

    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "等待期间写的话");
    approvalResult(events, id, "approved");
    anchor(events, "t2");

    // 不静默覆盖：输入框仍是这次的新稿
    expect(session.draft).toBe("等待期间写的话");
    const displaced = session.displacedDraftsForTopic("t2");
    expect(displaced.some((record) => record.text === "目标话题原有的草稿")).toBe(true);
    expect(displaced.every((record) => record.text !== "等待期间写的话")).toBe(true);

    // 原稿可恢复：清空输入框后能取回
    session.draft = "";
    session.flushDraft();
    expect(session.restoreDisplacedDraft(displaced[0]?.id).ok).toBe(true);
    expect(session.draft).toBe("目标话题原有的草稿");
  });

  it("跟随只作用于这次确认的目标：之后的普通导航不带走草稿", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "只跟这一次");
    approvalResult(events, id, "approved");
    anchor(events, "t2");
    expect(session.draft).toBe("只跟这一次");

    // 之后的普通导航（不是本次申请的目标）：草稿按话题各存各的，不跟过去
    session.setAnchor("t3", null, "另一个话题", null, false);
    expect(session.draft).toBe("");
    expect(storedText("t2")).toBe("只跟这一次");
  });
});

/* ───────────────────────── 7. 重复确认幂等 ───────────────────────── */

describe("重复确认幂等：双击 / 重放不重复提交", () => {
  it("同一申请第二次提交返回既有成功结果，不再发请求", async () => {
    const { session, events, unified } = setup();
    startTurn(session, events);
    topicRequired(events, { operationId: "op-1" });
    const item = topicItem(unified);
    const first = await unified.decideTopic(itemOrThrow(item), { decision: "approve" });
    expect(first).toBe(true);
    expect(api.respondTopicApproval).toHaveBeenCalledTimes(1);
    expect(topicItem(unified)).toBeNull();

    const second = await unified.decideTopic(itemOrThrow(item), { decision: "approve" });
    expect(second).toBe(true);
    expect(api.respondTopicApproval).toHaveBeenCalledTimes(1);
    expect(unified.lastNotice).toContain("已经");
  });

  it("请求在飞时双击只提交一次", async () => {
    const { session, events, unified } = setup();
    startTurn(session, events);
    topicRequired(events, { operationId: "op-1" });
    const item = topicItem(unified);
    const gate: { release?: (value: { ok: boolean }) => void } = {};
    vi.mocked(api.respondTopicApproval).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          gate.release = resolve;
        }),
    );
    const first = unified.decideTopic(itemOrThrow(item), { decision: "approve" });
    const second = await unified.decideTopic(itemOrThrow(item), { decision: "approve" });
    expect(second).toBe(false);
    gate.release?.({ ok: true });
    expect(await first).toBe(true);
    expect(api.respondTopicApproval).toHaveBeenCalledTimes(1);
  });

  it("重复 APPROVAL_REQUIRED 不重复入队", () => {
    const { session, events, unified } = setup();
    startTurn(session, events);
    topicRequired(events, { approvalId: "ap-1", operationId: "op-1" });
    topicRequired(events, { approvalId: "ap-1", operationId: "op-1" });
    expect(unified.items.filter((item) => item.kind === "topic")).toHaveLength(1);
  });
});

/* ─────────────────── 8. 操作身份 / 冲突 / 旧后端字段 ─────────────────── */

describe("操作身份字段派生", () => {
  it("operationId / paramVersion / baseNavVersion / sourceTopicApprovalId 从 payload 结构化读取", () => {
    const { session, events, unified } = setup();
    startTurn(session, events);
    topicRequired(events, { operationId: "op-42", paramVersion: 3, baseNavVersion: 7 });
    const item = topicItem(unified);
    expect(item?.topic?.operationId).toBe("op-42");
    expect(item?.topic?.paramVersion).toBe(3);
    expect(item?.topic?.baseNavVersion).toBe(7);
    expect(item?.operationId).toBe("op-42");
    expect(item?.sourceTopicApprovalId).toBe("");
    expect(item?.topic?.conflict).toBeNull();
    expect(item?.invalidated).toBe(false);
  });

  it("相似话题冲突按 payload.conflict 派生（缺失就是 null，不猜）", () => {
    const { session, events, unified } = setup();
    startTurn(session, events);
    topicRequired(events, {
      approvalId: "ap-c",
      kind: "topic_create",
      newName: "原神",
      conflict: {
        status: "conflict_pending",
        existing_topic_id: "t9",
        existing_topic_name: "原神",
      },
    });
    const item = topicItem(unified);
    expect(item?.topic?.conflict).toEqual({
      status: "conflict_pending",
      existingTopicId: "t9",
      existingTopicName: "原神",
    });
  });

  it("旧后端 payload 没有新字段：全部为 null/空，不伪造", () => {
    const { session, events, unified } = setup();
    startTurn(session, events);
    topicRequired(events, { operationId: null, paramVersion: null, baseNavVersion: null });
    const item = topicItem(unified);
    expect(item?.topic?.operationId).toBe("");
    expect(item?.topic?.paramVersion).toBeNull();
    expect(item?.topic?.baseNavVersion).toBeNull();
    expect(session.stage).toBeNull();
    expect(unified.topicStageFor(itemOrNull(item))).toBeNull();
    expect(unified.topicRecoveryFor(itemOrNull(item))).toBeNull();
  });
});

/* 让测试读起来是行为而不是类型体操：类型守卫。 */
function itemOrNull<T>(value: T | null): T {
  if (value === null) throw new Error("话题申请项不存在");
  return value;
}
function itemOrThrow<T>(value: T | null): T {
  if (value === null) throw new Error("话题申请项不存在");
  return value;
}
