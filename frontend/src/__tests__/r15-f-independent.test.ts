/**
 * R15 F 独立验收（R2）：旧位置事件必须整条拒绝，不能只保护草稿。
 *
 * 角色 F：独立复现 + 交叉评审。本文件只通过真实 Pinia store + 事件路由观察
 * 用户可见行为，不复用 C 的测试代码；对应契约 §4.2(c) / §4.3 / §4.4。
 *
 * 覆盖：
 * 1. 手动导航到新位置后，旧操作成功事件（更低 nav_version / 更低 seq）→ 位置、
 *    待接续提示、草稿三者都不动；
 * 2. 草稿跟随已消费 / 没有草稿 / 跟随从未武装时，旧事件同样整条拒绝；
 * 3. 同话题乱序；同版本只允许合法细化（不完整事件不得抹掉片段、topic 冲突拒绝）；
 * 4. 失效操作的迟到成功事件（即使版本更新）不得重新导航；
 * 5. 权威快照（/api/runtime/state anchor）与实例变化：旧实例事件拒绝、新实例
 *    事件接受、seq 不跨实例比较；
 * 6. 既有语义不倒退：目标已有草稿保护、确认后跟随真实目标、不自动发送。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../stores/session";
import { useEventStore } from "../stores/events";
import { draftStorageKey, readDraft, writeDraft } from "../interactive/drafts";
import { api } from "../services/api";
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
      turn_id: "turn_1",
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

let eventSeq = 0;

function ev(type: string, data: Record<string, unknown>): AgentEvent {
  eventSeq += 1;
  return { type, id: "r15f-" + type + "-" + eventSeq, ts: "", data } as AgentEvent;
}

type Store = ReturnType<typeof useSessionStore>;
type EventStore = ReturnType<typeof useEventStore>;

function setup(): { session: Store; events: EventStore } {
  setActivePinia(createPinia());
  return { session: useSessionStore(), events: useEventStore() };
}

function chatKey(topicId: string): string {
  return draftStorageKey("chat", topicId);
}

function storedText(topicId: string): string | null {
  return readDraft(chatKey(topicId))?.text ?? null;
}

function position(session: Store): { topic: string | null; fragment: string | null; historic: boolean } {
  return {
    topic: session.currentTopicId,
    fragment: session.anchorFragmentId,
    historic: session.anchorHistoric,
  };
}

function positionFacts(session: Store): Record<string, unknown> | null {
  return (session as unknown as { _positionFacts: Record<string, unknown> | null })
    ._positionFacts;
}

function waitingDraft(session: Store, text: string): void {
  session.draft = text;
  session.flushDraft();
}

function followOf(session: Store): Record<string, unknown> | null {
  return (session as unknown as { _topicDraftFollow: Record<string, unknown> | null })
    ._topicDraftFollow;
}

function startTurn(session: Store, events: EventStore, turnId = "turn_1"): void {
  session.adoptInstance("inst-A");
  session.setAnchor("t1", null, "当前话题", null, false);
  events.route(ev("TURN_START", { turn_id: turnId, revision: 1, instance_id: "inst-A" }));
}

interface AnchorOptions {
  topicId: string;
  fragmentId?: string | null;
  name?: string;
  fragmentTitle?: string | null;
  historic?: boolean;
  origin?: "topic_operation" | "manual";
  operationId?: string | null;
  approvalId?: string | null;
  paramVersion?: number | null;
  turnId?: string | null;
  navVersion?: number | null;
  seq?: number | null;
  instanceId?: string | null;
  pendingIntentId?: string | null;
  pendingSourceFragmentId?: string | null;
  pendingSourceTitle?: string | null;
}

function anchorEvent(events: EventStore, opts: AnchorOptions): void {
  events.route(
    ev("ANCHOR", {
      topic_id: opts.topicId,
      topic_name: opts.name ?? "话题",
      fragment_id: opts.fragmentId ?? null,
      fragment_title: opts.fragmentTitle ?? null,
      historic: opts.historic ?? false,
      origin: opts.origin ?? "manual",
      operation_id: opts.operationId ?? null,
      approval_id: opts.approvalId ?? null,
      param_version: opts.paramVersion ?? null,
      turn_id: opts.turnId ?? null,
      nav_version: opts.navVersion ?? null,
      seq: opts.seq ?? null,
      instance_id: opts.instanceId ?? "inst-A",
      pending_intent_id: opts.pendingIntentId ?? null,
      pending_source_fragment_id: opts.pendingSourceFragmentId ?? null,
      pending_source_title: opts.pendingSourceTitle ?? null,
    }),
  );
}

interface RequiredOptions {
  approvalId?: string;
  operationId?: string;
  targetTopicId?: string;
  kind?: "topic_switch" | "topic_create";
}

function topicRequired(events: EventStore, opts: RequiredOptions = {}): string {
  const approvalId = opts.approvalId ?? "ap-r15f-1";
  const kind = opts.kind ?? "topic_switch";
  const payload: Record<string, unknown> = {
    action: kind === "topic_create" ? "create" : "switch",
    turn_id: "turn_1",
    from_topic_id: "t1",
    from_topic_name: "当前话题",
    from_fragment_id: null,
    reason: "判断属于目标话题",
    operation_id: opts.operationId ?? "op-r15f",
    param_version: 1,
    base_nav_version: 1,
  };
  if (kind === "topic_create") payload.new_name = "新话题";
  else {
    payload.target_topic_id = opts.targetTopicId ?? "t2";
    payload.target_topic_name = "目标话题";
  }
  events.route(
    ev("APPROVAL_REQUIRED", {
      approval: { approval_id: approvalId, kind, payload, turn_id: "turn_1" },
      phase: "waiting",
    }),
  );
  return approvalId;
}

function approvalResult(events: EventStore, approvalId: string, phase: string, extra: Record<string, unknown> = {}): void {
  events.route(
    ev("APPROVAL_RESULT", {
      approval_id: approvalId,
      phase,
      decision: phase === "approved" ? "approved" : "rejected",
      turn_id: "turn_1",
      ...extra,
    }),
  );
}

function authoritative(events: EventStore, opts: Omit<AnchorOptions, "origin">): void {
  anchorEvent(events, {
    ...opts,
    origin: "topic_operation",
    operationId: opts.operationId === undefined ? "op-r15f" : opts.operationId,
    turnId: opts.turnId === undefined ? "turn_1" : opts.turnId,
    paramVersion: opts.paramVersion === undefined ? 1 : opts.paramVersion,
  });
}

beforeEach(() => {
  localStorage.clear();
  eventSeq = 0;
  vi.mocked(api.sendTurn).mockClear();
});

// ---------------------------------------------------------------------------
// 1. 旧位置事件整条拒绝（位置 / 待接续 / 草稿）
// ---------------------------------------------------------------------------

describe("R2 · 旧操作成功事件不得改变位置（不只保护草稿）", () => {
  it("手动导航到新位置后，迟到的旧操作成功事件（nav 更低）不跳回旧目标", () => {
    const { session, events } = setup();
    startTurn(session, events);
    anchorEvent(events, { topicId: "t1", name: "当前话题", navVersion: 1, seq: 1 });

    // 本轮话题操作等待确认：武装一次性草稿跟随，等待期间允许写草稿。
    const approvalId = topicRequired(events, { operationId: "op-old", targetTopicId: "t2" });
    waitingDraft(session, "等待期间写的话");
    approvalResult(events, approvalId, "approved");
    expect(followOf(session)).not.toBeNull();

    // 用户自己手动切到新位置（真实新导航，版本更高）。
    anchorEvent(events, {
      topicId: "manual-new",
      name: "手动新话题",
      navVersion: 9,
      seq: 20,
      instanceId: "inst-A",
    });
    expect(position(session).topic).toBe("manual-new");
    expect(position(session).fragment).toBeNull();

    // 旧操作的权威成功事件迟到：版本 8 / 序号 19（都更旧），按契约必须整条拒绝。
    authoritative(events, {
      topicId: "t2",
      name: "目标话题二",
      operationId: "op-old",
      approvalId,
      navVersion: 8,
      seq: 19,
      instanceId: "inst-A",
      pendingIntentId: "intent-old",
      pendingSourceFragmentId: "fragment-old",
      pendingSourceTitle: "旧来源",
    });

    expect(position(session).topic, "旧事件不得把当前位置跳回旧目标").toBe("manual-new");
    expect(position(session).fragment).toBeNull();
    expect(session.pendingContinuation, "旧事件不得覆盖待接续提示").toBeNull();
    expect(session.draft).toBe("");
    expect(storedText("t1"), "等待期间的草稿必须留在原话题").toBe("等待期间写的话");
    expect(storedText("t2"), "旧事件不得把草稿迁移到旧目标").toBeNull();
    expect(followOf(session), "被拒绝的事件不得消费跟随").not.toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("跟随已经消费后重放旧事件：位置与草稿都不动", () => {
    const { session, events } = setup();
    startTurn(session, events);
    anchorEvent(events, { topicId: "t1", navVersion: 1, seq: 1 });
    const approvalId = topicRequired(events, { operationId: "op-done", targetTopicId: "t2" });
    waitingDraft(session, "确认后跟随的草稿");
    approvalResult(events, approvalId, "approved");
    authoritative(events, { topicId: "t2", operationId: "op-done", approvalId, navVersion: 2, seq: 2 });
    expect(position(session).topic).toBe("t2");
    expect(storedText("t2")).toBe("确认后跟随的草稿");
    expect(followOf(session)).toBeNull();

    // 用户在新话题重新写稿。
    waitingDraft(session, "切过去之后新写的字");

    // 重连/乱序重放：另一条旧操作的成功事件（版本 1 / 序号 1），位置必须不动。
    authoritative(events, {
      topicId: "t-old",
      operationId: "op-stale",
      approvalId: "ap-stale",
      navVersion: 1,
      seq: 1,
    });
    expect(position(session).topic).toBe("t2");
    expect(position(session).fragment).toBeNull();
    expect(session.draft).toBe("切过去之后新写的字");
    expect(storedText("t2")).toBe("切过去之后新写的字");
    expect(storedText("t-old")).toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("没有草稿、跟随从未武装时旧事件同样被拒绝（接受不依赖草稿跟随）", () => {
    const { session, events } = setup();
    startTurn(session, events);
    anchorEvent(events, { topicId: "t1", navVersion: 4, seq: 40 });
    expect(followOf(session)).toBeNull();
    expect(session.draft).toBe("");

    // 手动导航后再收到旧事件（更旧版本 / 更旧序号）。
    anchorEvent(events, { topicId: "t9", name: "最新位置", navVersion: 5, seq: 41 });
    anchorEvent(events, { topicId: "t-old", navVersion: 3, seq: 39 });
    expect(position(session).topic, "没有草稿也绝不能因旧事件改变位置").toBe("t9");
    expect(session.pendingContinuation).toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("同话题乱序：旧版本拒绝、缺片段事件不得抹掉已知片段、同版本 topic 冲突拒绝", () => {
    const { session, events } = setup();
    startTurn(session, events);
    anchorEvent(events, { topicId: "tA", fragmentId: "f2", navVersion: 10, seq: 30 });
    expect(position(session).fragment).toBe("f2");

    // 真正的新事件：版本更高，接受。
    anchorEvent(events, { topicId: "tA", fragmentId: "f3", navVersion: 11, seq: 31 });
    expect(position(session).fragment).toBe("f3");

    // 旧版本事件（同话题不同片段）：拒绝。
    anchorEvent(events, { topicId: "tA", fragmentId: "f2", navVersion: 10, seq: 30 });
    expect(position(session).fragment, "旧版本事件不得把位置挪回旧片段").toBe("f3");

    // 同版本但缺片段：不得用不完整事件抹掉已知真实片段。
    anchorEvent(events, { topicId: "tA", fragmentId: null, navVersion: 11 });
    expect(position(session).fragment, "同版本缺片段事件必须拒绝（ambiguous_same_version）").toBe("f3");

    // 同版本但 topic 冲突：拒绝。
    anchorEvent(events, { topicId: "tB", fragmentId: null, navVersion: 11 });
    expect(position(session).topic, "同版本 topic 冲突必须拒绝").toBe("tA");

    // 同话题、事实片段为空 → 同版本合法补充片段信息：接受。
    anchorEvent(events, { topicId: "tC", fragmentId: null, navVersion: 12 });
    expect(position(session).topic).toBe("tC");
    anchorEvent(events, { topicId: "tC", fragmentId: "f4", navVersion: 12 });
    expect(position(session).fragment, "同版本合法细化必须接受").toBe("f4");
    expect(position(session).topic).toBe("tC");
  });

  it("失效操作的迟到成功事件即使版本更新也必须整条拒绝", () => {
    const { session, events } = setup();
    startTurn(session, events);
    anchorEvent(events, { topicId: "t1", navVersion: 1, seq: 1 });
    const approvalId = topicRequired(events, { operationId: "op-invalid", targetTopicId: "t2" });

    // 后端判定失效（stale）：操作进入终态，登记为「失效操作」。
    events.route(
      ev("APPROVAL_RESULT", {
        approval_id: approvalId,
        phase: "invalidated",
        decision: "stale",
        code: "topic_application_stale",
        operation_id: "op-invalid",
        turn_id: "turn_1",
      }),
    );

    // 失效操作迟到的“成功”ANCHOR：版本甚至比当前更新（5 > 1），仍必须拒绝。
    authoritative(events, {
      topicId: "t2",
      operationId: "op-invalid",
      approvalId,
      navVersion: 5,
      seq: 2,
      pendingIntentId: "intent-should-not-apply",
      pendingSourceTitle: "失效来源",
    });
    expect(position(session).topic, "失效操作迟到事件不得重新导航").toBe("t1");
    expect(position(session).fragment).toBeNull();
    expect(session.pendingContinuation).toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("旧轮迟到事件按轮次关联拒绝（stale_turn）", () => {
    const { session, events } = setup();
    startTurn(session, events, "turn_2");
    anchorEvent(events, { topicId: "t1", navVersion: 1, seq: 1 });
    // turn_1 已经结束的旧轮：即使版本更新也不得改当前位置。
    authoritative(events, {
      topicId: "t-old-turn",
      turnId: "turn_1",
      navVersion: 5,
      seq: 2,
    });
    expect(position(session).topic).toBe("t1");
  });
});

// ---------------------------------------------------------------------------
// 2. 权威快照 / 实例变化 / seq 归零
// ---------------------------------------------------------------------------

const snapshotAnchor = (
  topicId: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> => ({
  topic_id: topicId,
  topic_name: "快照话题",
  fragment_id: null,
  fragment_title: null,
  historic: false,
  nav_version: 10,
  pending_intent_id: null,
  pending_intent_version: null,
  pending_source_fragment_id: null,
  pending_source_title: null,
  ...overrides,
});

describe("R2 · 权威快照与实例变化", () => {
  it("权威快照无条件采用位置与待接续；旧实例事件拒绝、新实例事件接受且 seq 归零", () => {
    const { session, events } = setup();
    session.adoptInstance("inst-A");
    const applied = session.applyAuthoritativePositionSnapshot(
      snapshotAnchor("tA", {
        pending_intent_id: "intent-snap",
        pending_intent_version: 3,
        pending_source_fragment_id: "f-snap",
        pending_source_title: "快照来源",
      }),
      "inst-A",
    );
    expect(applied.applied).toBe(true);
    expect(position(session).topic).toBe("tA");
    expect(session.pendingContinuation?.intentId).toBe("intent-snap");
    expect(session.pendingContinuation?.sourceTitle).toBe("快照来源");
    const factsA = positionFacts(session);
    expect(factsA?.navVersion).toBe(10);
    expect(factsA?.instanceId).toBe("inst-A");

    // 服务重启：新实例 B 的权威快照（nav 版本可能归零/缺失）。
    session.adoptInstance("inst-B");
    const appliedB = session.applyAuthoritativePositionSnapshot(
      snapshotAnchor("tB", { nav_version: null }),
      "inst-B",
    );
    expect(appliedB.applied).toBe(true);
    expect(position(session).topic).toBe("tB");
    expect(positionFacts(session)?.instanceId).toBe("inst-B");

    // 旧实例 A 的迟到事件（没有 nav 版本，seq 500）：整条拒绝。
    anchorEvent(events, {
      topicId: "t-old-instance",
      navVersion: null,
      seq: 500,
      instanceId: "inst-A",
    });
    expect(position(session).topic, "旧实例的迟到事件必须拒绝").toBe("tB");

    // 新实例 B 的事件接受（seq 从 1 重新开始，不与旧实例的 500 比较）。
    anchorEvent(events, {
      topicId: "tB2",
      navVersion: null,
      seq: 1,
      instanceId: "inst-B",
    });
    expect(position(session).topic, "新实例事件必须接受（seq 归零是正常基线）").toBe("tB2");

    // 同实例内 seq 仍然单调：更小/相等的 seq 拒绝。
    anchorEvent(events, { topicId: "tB3", navVersion: null, seq: 1, instanceId: "inst-B" });
    expect(position(session).topic).toBe("tB2");
    anchorEvent(events, { topicId: "tB4", navVersion: null, seq: 2, instanceId: "inst-B" });
    expect(position(session).topic).toBe("tB4");
  });

  it("本地已有更新实时位置时，较旧权威快照不得倒退位置（pending 仍以快照为准）", () => {
    const { session, events } = setup();
    startTurn(session, events);
    anchorEvent(events, { topicId: "t2", fragmentId: "f2", navVersion: 9, seq: 20 });

    const applied = session.applyAuthoritativePositionSnapshot(
      snapshotAnchor("t1", { nav_version: 5, pending_intent_id: "intent-newer" }),
      "inst-A",
    );
    expect(applied.keptNewerLocal).toBe(true);
    expect(position(session).topic, "较旧快照不得把本地更新的实时位置倒退").toBe("t2");
    expect(position(session).fragment).toBe("f2");
    expect(session.pendingContinuation?.intentId, "快照仍是权威的 pending 来源").toBe(
      "intent-newer",
    );
    expect(positionFacts(session)?.navVersion).toBe(9);
  });

  it("events.applyRuntimeState 应用 /api/runtime/state 的 anchor 字段", () => {
    const { session, events } = setup();
    session.adoptInstance("inst-A");
    events.applyRuntimeState({
      approvals: [],
      tasks: [],
      instance_id: "inst-A",
      anchor: snapshotAnchor("t-runtime", {
        fragment_id: "f-runtime",
        fragment_title: "运行时快照片段",
        pending_intent_id: "intent-runtime",
        pending_source_title: "运行时来源",
      }),
    } as unknown as Parameters<EventStore["applyRuntimeState"]>[0]);
    expect(position(session).topic).toBe("t-runtime");
    expect(position(session).fragment).toBe("f-runtime");
    expect(session.pendingContinuation?.intentId).toBe("intent-runtime");
    expect(positionFacts(session)?.navVersion).toBe(10);
  });
});

// ---------------------------------------------------------------------------
// 3. 冻结接口（供 E/F 断言）
// ---------------------------------------------------------------------------

describe("R2 · 冻结接口", () => {
  it("acceptTopicPositionEvent 返回 accepted/reason/migrated，并先做操作关联拒绝", () => {
    const { session, events } = setup();
    startTurn(session, events);
    anchorEvent(events, { topicId: "t1", navVersion: 7, seq: 7 });

    expect(typeof session.acceptTopicPositionEvent).toBe("function");
    const stale = session.acceptTopicPositionEvent({
      topicId: "t-old",
      fragmentId: null,
      historic: false,
      navVersion: 6,
      seq: 6,
      origin: "manual",
    });
    expect(stale.accepted).toBe(false);
    expect(stale.reason).toBe("stale_nav_version");
    expect(position(session).topic).toBe("t1");

    const fresh = session.acceptTopicPositionEvent({
      topicId: "t-new",
      fragmentId: null,
      historic: false,
      navVersion: 8,
      seq: 8,
      origin: "manual",
    });
    expect(fresh.accepted).toBe(true);
    expect(fresh.migrated).toBe(false);
    expect(position(session).topic).toBe("t-new");
  });
});

// ---------------------------------------------------------------------------
// 4. 既有语义不倒退（目标已有草稿保护 / 跟随真实目标 / 不自动发送）
// ---------------------------------------------------------------------------

describe("R2 · 既有草稿语义不倒退", () => {
  it("目标话题本来就有草稿：不静默覆盖（让位登记）、跟随真实目标、不自动发送", () => {
    const { session, events } = setup();
    startTurn(session, events);
    anchorEvent(events, { topicId: "t1", navVersion: 1, seq: 1 });
    expect(writeDraft(chatKey("t2"), "目标话题原有的草稿", 1).ok).toBe(true);
    const approvalId = topicRequired(events, { operationId: "op-displace", targetTopicId: "t2" });
    waitingDraft(session, "等待期间写的话");
    approvalResult(events, approvalId, "approved");

    authoritative(events, {
      topicId: "t2",
      operationId: "op-displace",
      approvalId,
      navVersion: 2,
      seq: 2,
    });
    expect(position(session).topic).toBe("t2");
    expect(session.draft, "当前编辑的仍是等待期间那份").toBe("等待期间写的话");
    const displaced = session.displacedDraftsForTopic("t2");
    expect(displaced.some((record) => record.text === "目标话题原有的草稿")).toBe(true);
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("普通手动导航不消费跟随：等待草稿留在原话题（F12 既有语义）", () => {
    const { session, events } = setup();
    startTurn(session, events);
    anchorEvent(events, { topicId: "t1", navVersion: 1, seq: 1 });
    const approvalId = topicRequired(events, { operationId: "op-wait", targetTopicId: "t2" });
    waitingDraft(session, "还在等待确认");
    approvalResult(events, approvalId, "approved");

    anchorEvent(events, { topicId: "t-other", name: "无关话题", navVersion: 2, seq: 2 });
    expect(position(session).topic).toBe("t-other");
    expect(followOf(session), "手动导航不得消费本次操作的跟随").not.toBeNull();
    expect(storedText("t1")).toBe("还在等待确认");
    expect(storedText("t2")).toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });
});
