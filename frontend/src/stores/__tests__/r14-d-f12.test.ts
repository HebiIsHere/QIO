/**
 * R14-D · F12：草稿跟随只消费「本次话题操作的权威成功结果」。
 *
 * 旧缺陷（本轮要修）：armTopicDraftFollow 用 targetTopicId=null 通配「下一次任意导航」，
 * 于是无关手动导航 / 重复事件 / 乱序事件都会把等待期间的草稿搬走（create 尤其明显）。
 *
 * 本轮契约（docs/topic-review-20261110-contract.md §3.6 + Lead 补充的 ANCHOR 事件契约）：
 * - 跟随身份绑定 operation_id + param_version + turn_id + 原话题/片段 + 草稿版本；
 * - 目标未知（create）只保留待关联，删除 targetTopicId=null 通配；
 * - 只有 origin=topic_operation 且 operation_id（及 param_version）匹配的 ANCHOR 才迁移一次；
 * - 无关手动 ANCHOR（无 operation_id）、旧/重复/乱序 ANCHOR 不迁移、不提前消费；
 * - 迁移前复核原草稿仍待迁移且版本未被用户新编辑覆盖；
 * - 操作失效/拒绝/失败/TURN_END 收口清除跟随（不被迟到成功事件带走）；
 * - 目标已有草稿沿用既有保护（登记为被让位草稿），不静默覆盖；
 * - 草稿始终由用户主动发送，迁移不自动发送。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";
import { useUnifiedApprovalsStore } from "../unifiedApprovals";
import { draftStorageKey, readDraft, writeDraft } from "../../interactive/drafts";
import { api } from "../../services/api";
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
    getRecoveryRecords: vi.fn(async () => ({
      records: [],
      total: 0,
      shown: 0,
      truncated: false,
    })),
  },
}));

let seq = 0;
function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: `r14d-${type}-${seq}`, ts: "", data } as AgentEvent;
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
  return { session, events };
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
  fromFragmentId?: string | null;
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
    from_fragment_id: opts.fromFragmentId ?? null,
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
  events.route(
    ev("APPROVAL_REQUIRED", {
      approval: { approval_id: approvalId, kind, payload, turn_id: turnId },
      phase: "waiting",
    }),
  );
  return approvalId;
}

function approvalResult(
  events: EventStore,
  approvalId: string,
  phase: string,
  extra: Record<string, unknown> = {},
) {
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

/** 权威成功结果：话题工具真实执行成功后的 ANCHOR（Lead 冻结的事件契约）。 */
function authoritativeAnchor(
  events: EventStore,
  opts: {
    topicId: string;
    name?: string;
    operationId?: string;
    /** 审批稳定身份（默认与本次申请的 approval id 一致 = 同一次确认）。 */
    approvalId?: string | null;
    paramVersion?: number | null;
    turnId?: string;
    seq?: number;
    navVersion?: number;
  },
) {
  events.route(
    ev("ANCHOR", {
      topic_id: opts.topicId,
      topic_name: opts.name ?? "目标话题",
      fragment_id: null,
      fragment_title: null,
      historic: false,
      origin: "topic_operation",
      operation_id: opts.operationId ?? "op-1",
      approval_id: opts.approvalId === undefined ? "ap-topic-1" : opts.approvalId,
      param_version: opts.paramVersion === undefined ? 1 : opts.paramVersion,
      turn_id: opts.turnId ?? "turn_1",
      nav_version: opts.navVersion ?? 7,
      seq: opts.seq ?? 5,
    }),
  );
}

/** 普通手动导航：origin=manual、operation_id=null（不得消费跟随）。 */
function manualAnchor(events: EventStore, topicId: string, name = "另一个话题") {
  events.route(
    ev("ANCHOR", {
      topic_id: topicId,
      topic_name: name,
      fragment_id: null,
      fragment_title: null,
      historic: false,
      origin: "manual",
      operation_id: null,
      param_version: null,
      turn_id: null,
      nav_version: 9,
      seq: 20,
    }),
  );
}

function waitingDraft(session: Store, text: string) {
  session.draft = text;
  session.flushDraft();
}

function followOf(session: Store): Record<string, unknown> | null {
  return (session as unknown as { _topicDraftFollow: Record<string, unknown> | null })
    ._topicDraftFollow;
}

beforeEach(() => {
  localStorage.clear();
  seq = 0;
  vi.mocked(api.sendTurn).mockClear();
  vi.mocked(api.getSessionContext).mockClear();
});

describe("F12 · 无关手动导航不迁移、不提前消费", () => {
  it("switch：手动 ANCHOR（origin=manual）不搬等待草稿，跟随仍待权威结果", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "等待期间写的话");
    approvalResult(events, id, "approved");
    expect(followOf(session)).not.toBeNull();

    manualAnchor(events, "t2", "目标话题");

    expect(session.currentTopicId).toBe("t2");
    // 草稿没有被搬走：仍在原话题的草稿键上，输入框是目标话题自己的（空）草稿
    expect(session.draft).toBe("");
    expect(storedText("t1")).toBe("等待期间写的话");
    expect(storedText("t2")).toBeNull();
    // 跟随没有被手动导航消费：仍等本次操作的权威结果
    expect(followOf(session)).not.toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("create（目标未知）：手动 ANCHOR 不再通配迁移（旧实现会搬走）", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { kind: "topic_create", operationId: "op-1", newName: "新话题" });
    waitingDraft(session, "新建后还要发的话");
    approvalResult(events, id, "approved");

    manualAnchor(events, "t-new", "新话题");

    expect(session.currentTopicId).toBe("t-new");
    expect(session.draft).toBe("");
    expect(storedText("t1")).toBe("新建后还要发的话");
    expect(storedText("t-new")).toBeNull();
    expect(followOf(session)).not.toBeNull();
  });

  it("operation 不匹配的 ANCHOR 不迁移、不消费；之后的匹配结果仍能迁移", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "只跟 op-1");
    approvalResult(events, id, "approved");

    // 旧操作的迟到成功结果（锚点仍指当前话题）：operation 不匹配 → 不迁移、不消费
    authoritativeAnchor(events, { topicId: "t1", operationId: "op-old", seq: 4 });
    expect(session.currentTopicId).toBe("t1");
    expect(session.draft).toBe("只跟 op-1");
    expect(storedText("t1")).toBe("只跟 op-1");
    expect(followOf(session)).not.toBeNull();

    // 本次操作的权威结果：迁移一次
    authoritativeAnchor(events, { topicId: "t2", operationId: "op-1", seq: 5 });
    expect(session.currentTopicId).toBe("t2");
    expect(session.draft).toBe("只跟 op-1");
    expect(storedText("t2")).toBe("只跟 op-1");
    expect(storedText("t1")).toBeNull();
    expect(followOf(session)).toBeNull();
  });
});

describe("F12 · 只有本次操作的权威成功结果才迁移一次", () => {
  it("switch：权威 ANCHOR 迁移草稿到真实目标，不自动发送", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "确认后还要发的话");
    approvalResult(events, id, "approved");

    authoritativeAnchor(events, { topicId: "t2", operationId: "op-1", paramVersion: 1, seq: 5 });

    expect(session.currentTopicId).toBe("t2");
    expect(session.draft).toBe("确认后还要发的话");
    expect(storedText("t2")).toBe("确认后还要发的话");
    expect(storedText("t1")).toBeNull();
    expect(followOf(session)).toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("create：目标由权威结果给出（未知 → 真实目标），只迁移一次", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { kind: "topic_create", operationId: "op-1", newName: "新话题" });
    waitingDraft(session, "新建后还要发的话");
    approvalResult(events, id, "approved");

    authoritativeAnchor(events, { topicId: "t-new", name: "新话题", operationId: "op-1", seq: 5 });

    expect(session.currentTopicId).toBe("t-new");
    expect(session.draft).toBe("新建后还要发的话");
    expect(storedText("t-new")).toBe("新建后还要发的话");
    expect(storedText("t1")).toBeNull();
    expect(followOf(session)).toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("改名后确认（param_version 推进，但 approval_id 不变）：仍按同一次确认迁移一次", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, {
      kind: "topic_create",
      operationId: "op-rename",
      paramVersion: 1,
      newName: "新话题",
    });
    waitingDraft(session, "改名后也要跟过去的话");
    approvalResult(events, id, "approved");

    // 用户在弹窗里把名称改成「改名后的话题」→ 执行版本 v2；approval_id 仍是同一次确认
    authoritativeAnchor(events, {
      topicId: "t-renamed",
      name: "改名后的话题",
      operationId: "op-rename",
      approvalId: id,
      paramVersion: 2,
      seq: 5,
    });

    expect(session.currentTopicId).toBe("t-renamed");
    expect(session.draft).toBe("改名后也要跟过去的话");
    expect(storedText("t-renamed")).toBe("改名后也要跟过去的话");
    expect(storedText("t1")).toBeNull();
    expect(followOf(session)).toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("approval_id 不匹配（同一 operation 的另一次确认）：不迁移、不消费", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-ap", targetTopicId: "t2" });
    waitingDraft(session, "只跟这次确认");
    approvalResult(events, id, "approved");

    // 另一次确认的迟到结果（锚点仍指当前话题）：不迁移、不消费
    authoritativeAnchor(events, {
      topicId: "t1",
      operationId: "op-ap",
      approvalId: "ap-other",
      seq: 5,
    });
    expect(session.currentTopicId).toBe("t1");
    expect(session.draft).toBe("只跟这次确认");
    expect(storedText("t2")).toBeNull();
    expect(followOf(session)).not.toBeNull();

    // 本次确认的权威结果仍能迁移
    authoritativeAnchor(events, {
      topicId: "t2",
      operationId: "op-ap",
      approvalId: id,
      seq: 6,
    });
    expect(session.draft).toBe("只跟这次确认");
    expect(storedText("t2")).toBe("只跟这次确认");
  });

  it("重复/重放权威 ANCHOR：不重复搬草稿，不覆盖用户之后写的新稿", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "搬一次");
    approvalResult(events, id, "approved");
    authoritativeAnchor(events, { topicId: "t2", operationId: "op-1", seq: 5 });
    expect(session.draft).toBe("搬一次");

    // 用户在新话题里继续写
    session.draft = "切换之后新写的字";
    session.flushDraft();
    // 重连重放同一条权威结果（同 operation / 同 seq）
    authoritativeAnchor(events, { topicId: "t2", operationId: "op-1", seq: 5 });
    expect(session.draft).toBe("切换之后新写的字");
    expect(storedText("t2")).toBe("切换之后新写的字");
    expect(storedText("t1")).toBeNull();
  });
});

describe("F12 · 迁移前复核草稿版本（用户新编辑不覆盖）", () => {
  it("等待期间用户又改了稿（版本变化）：不迁移，草稿留在原话题", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "旧稿");
    approvalResult(events, id, "approved");
    // 批准之后用户继续编辑：这是新版本，不得被静默搬走
    session.draft = "批准后又改的新稿";
    session.flushDraft();

    authoritativeAnchor(events, { topicId: "t2", operationId: "op-1", seq: 5 });

    expect(session.currentTopicId).toBe("t2");
    expect(session.draft).toBe("");
    expect(storedText("t1")).toBe("批准后又改的新稿");
    expect(storedText("t2")).toBeNull();
    expect(followOf(session)).toBeNull();
  });

  it("草稿已被发送清空：不迁移空内容、不伪造草稿", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "已经发出去的话");
    approvalResult(events, id, "approved");
    session.draft = "";
    session.flushDraft();

    authoritativeAnchor(events, { topicId: "t2", operationId: "op-1", seq: 5 });

    expect(session.currentTopicId).toBe("t2");
    expect(session.draft).toBe("");
    expect(storedText("t2")).toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });
});

describe("F12 · 失效 / 拒绝 / 失败 / 轮结束收口", () => {
  it("失效后迟到的权威 ANCHOR 不带草稿（失效按失效收口）", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "失效也不搬");
    approvalResult(events, id, "approved");
    approvalResult(events, id, "invalidated", {
      decision: "stale",
      code: "topic_application_stale",
      message: "这条话题申请已经失效",
    });
    expect(followOf(session)).toBeNull();

    authoritativeAnchor(events, { topicId: "t2", operationId: "op-1", seq: 5 });
    expect(session.draft).toBe("");
    expect(storedText("t1")).toBe("失效也不搬");
    expect(storedText("t2")).toBeNull();
  });

  it("拒绝后草稿留原话题；迟到的权威成功事件也不搬", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "拒绝留在这里");
    approvalResult(events, id, "approved");
    approvalResult(events, id, "rejected");
    expect(followOf(session)).toBeNull();

    authoritativeAnchor(events, { topicId: "t2", operationId: "op-1", seq: 5 });
    expect(session.currentTopicId).toBe("t2");
    expect(storedText("t1")).toBe("拒绝留在这里");
    expect(storedText("t2")).toBeNull();
  });

  it("TURN_END 时仍 armed 的跟随按「未确认成功」收口：不猜目标、草稿留原话题", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "这一轮没切成");
    approvalResult(events, id, "approved");
    events.route(ev("TURN_END", { turn_id: "turn_1", status: "completed" }));
    expect(followOf(session)).toBeNull();

    authoritativeAnchor(events, { topicId: "t2", operationId: "op-1", seq: 5 });
    expect(session.currentTopicId).toBe("t2");
    expect(storedText("t1")).toBe("这一轮没切成");
    expect(storedText("t2")).toBeNull();
  });

  it("TOPIC_PHASE recoverable_failure：按失败收口跟随（迟到成功事件不再带走）", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "失败不搬");
    approvalResult(events, id, "approved");

    events.route(
      ev("TOPIC_PHASE", {
        phase: "recoverable_failure",
        operation_id: "op-1",
        turn_id: "turn_1",
        recovery: { record_id: "rec-1", message: "上下文准备失败", retryable: true },
      }),
    );
    expect(followOf(session)).toBeNull();

    authoritativeAnchor(events, { topicId: "t2", operationId: "op-1", seq: 5 });
    expect(storedText("t1")).toBe("失败不搬");
    expect(storedText("t2")).toBeNull();
  });
});

describe("F12 · 目标已有草稿保护", () => {
  it("目标话题已有草稿：不静默覆盖，原稿登记为可恢复的被让位草稿", () => {
    const { session, events } = setup();
    startTurn(session, events);
    expect(writeDraft(chatKey("t2"), "目标话题原有的草稿", 1).ok).toBe(true);
    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "等待期间写的话");
    approvalResult(events, id, "approved");

    authoritativeAnchor(events, { topicId: "t2", operationId: "op-1", seq: 5 });

    expect(session.draft).toBe("等待期间写的话");
    const displaced = session.displacedDraftsForTopic("t2");
    expect(displaced.some((record) => record.text === "目标话题原有的草稿")).toBe(true);
    expect(displaced.every((record) => record.text !== "等待期间写的话")).toBe(true);

    session.draft = "";
    session.flushDraft();
    expect(session.restoreDisplacedDraft(displaced[0]?.id).ok).toBe(true);
    expect(session.draft).toBe("目标话题原有的草稿");
    expect(api.sendTurn).not.toHaveBeenCalled();
  });
});

describe("F12 · 等待期间发送限制（发送按钮与快捷发送）", () => {
  it("等待话题选择时本对话发送受限；决定后解除；草稿可写不自动发送", async () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, { operationId: "op-1", targetTopicId: "t2" });
    expect(session.topicSendLock?.source).toBe("topic_application");
    waitingDraft(session, "等待时能写");
    expect(api.sendTurn).not.toHaveBeenCalled();

    // 真实顺序：用户在统一弹窗里点了「转到并回答」→ 申请出队 → 执行结果事件
    const unified = useUnifiedApprovalsStore();
    const item = unified.items.find((entry) => entry.kind === "topic");
    expect(await unified.decideTopic(item!, { decision: "approve" })).toBe(true);
    approvalResult(events, id, "approved");
    expect(session.topicSendLock).toBeNull();
    authoritativeAnchor(events, { topicId: "t2", operationId: "op-1", seq: 5 });
    expect(session.draft).toBe("等待时能写");
    expect(api.sendTurn).not.toHaveBeenCalled();
  });
});
