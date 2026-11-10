/**
 * R1—R4 跨模块集成验证（E 角色独立用例）· 前端侧。
 *
 * 对应契约：docs/topic-review-r1r4-contract-20261111.md §7（跨模块集成）、
 * §4.2(c) 前端 session 接口、§4.3 位置事件接受规则。
 *
 * 只验证**跨模块边界**，不重复 C 的模块内用例：
 * - R2：迟到 / 旧位置 ANCHOR 不得让前端跳回；接受规则不依赖草稿跟随；
 * - R1+R2：后端 stale 拒绝后，即便旧操作 ANCHOR 迟到也不得改变用户新位置；
 * - R3+R2：A→B 后重放 A 的旧位置事件不得改变前端已到的 B；
 * - R4+R2：恢复旧问题不抢占新轮次、不改当前导航与草稿；
 * - ⑤：位置只由 ANCHOR / 权威快照更新，不引入第二套状态权威；
 * - ④：正常切换 / 拒绝后回答的草稿跟随回归（组合层；详细用例跑既有测试）。
 *
 * 真实 Pinia store + 真实事件路由（events.route）+ 真实本机草稿存储；
 * 模型与网络全部 mock（不联网、不写真实应用数据库）；无 sleep。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../stores/session";
import { useEventStore } from "../stores/events";
import { draftStorageKey, readDraft } from "../interactive/drafts";
import { api } from "../services/api";
import { retryTopicAnswer } from "../services/approvalLifecycle";
import type { AgentEvent } from "../services/events";

/**
 * 真实 services/api.ts 没有导出 api.loadHistory（loadHistory 是 store 方法，
 * 内部走 getSessionContext）。vi.mock 里造出来的字段不能用于 vi.mocked(api.x)
 * 的类型检查，所以用 vi.hoisted 的局部 holder 在 mock 工厂里复用并直接断言它。
 */
const { loadHistoryMock } = vi.hoisted(() => ({
  loadHistoryMock: vi.fn(async () => ({ messages: [], anchor: null })),
}));

vi.mock("../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "t1",
      topic_name: "当前话题",
      anchor_fragment: null,
      messages: [],
    })),
    loadHistory: loadHistoryMock,
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

vi.mock("../services/approvalLifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../services/approvalLifecycle")>();
  return {
    ...actual,
    retryTopicAnswer: vi.fn(async () => ({
      ok: false,
      code: "recovery_in_progress",
      message: "这条恢复正在处理中，请稍后刷新。",
      answered: false,
      inProgress: true,
    })),
  };
});

let seq = 0;

function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: `r15e-${type}-${seq}`, ts: "", data } as AgentEvent;
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

function waitingDraft(session: Store, text: string) {
  session.draft = text;
  session.flushDraft();
}

interface AnchorOptions {
  topicId: string;
  name?: string;
  fragmentId?: string | null;
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
  pendingSourceTitle?: string | null;
}

function anchor(events: EventStore, opts: AnchorOptions) {
  const origin = opts.origin ?? "manual";
  events.route(
    ev("ANCHOR", {
      topic_id: opts.topicId,
      topic_name: opts.name ?? "话题",
      fragment_id: opts.fragmentId ?? null,
      fragment_title: opts.fragmentTitle ?? null,
      historic: opts.historic ?? false,
      origin,
      operation_id: opts.operationId ?? null,
      approval_id: opts.approvalId ?? null,
      param_version: opts.paramVersion ?? null,
      turn_id: opts.turnId ?? (origin === "topic_operation" ? "turn_1" : null),
      nav_version: opts.navVersion ?? null,
      seq: opts.seq ?? null,
      instance_id: opts.instanceId ?? "inst-1",
      pending_intent_id: opts.pendingIntentId ?? null,
      pending_source_title: opts.pendingSourceTitle ?? null,
    }),
  );
}

interface RequiredOptions {
  approvalId?: string;
  kind?: "topic_switch" | "topic_create";
  operationId?: string;
  paramVersion?: number;
  targetTopicId?: string;
  newName?: string;
  turnId?: string;
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
    from_fragment_id: null,
    reason: "这段内容更像目标话题",
    operation_id: opts.operationId ?? "op-1",
    param_version: opts.paramVersion ?? 1,
    base_nav_version: 1,
  };
  if (kind === "topic_create") payload.new_name = opts.newName ?? "新话题";
  else {
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

function approvalResult(events: EventStore, approvalId: string, phase: string) {
  events.route(
    ev("APPROVAL_RESULT", {
      approval_id: approvalId,
      phase,
      decision: phase === "approved" ? "approved" : "rejected",
      turn_id: "turn_1",
    }),
  );
}

beforeEach(() => {
  localStorage.clear();
  seq = 0;
  vi.clearAllMocks();
});

describe("R2 ①：迟到的旧操作 ANCHOR 不得让前端跳回", () => {
  it("用户新位置 v10/seq21 之后收到旧操作成功事件 v8/seq19：位置、待接续、草稿都不动", () => {
    const { session, events } = setup();
    startTurn(session, events);

    // 用户手动导航到新位置（v9 / seq20）
    anchor(events, {
      topicId: "manual-new",
      name: "新位置",
      navVersion: 9,
      seq: 20,
    });
    expect(session.currentTopicId).toBe("manual-new");

    // 用户随后改选历史（真实接续登记事件的形状：同话题/历史片段/pending，v10/seq21）
    anchor(events, {
      topicId: "t1",
      name: "用户新选的话题",
      fragmentId: "f-hist",
      fragmentTitle: "用户新选的历史",
      historic: true,
      origin: "manual",
      navVersion: 10,
      seq: 21,
      pendingIntentId: "intent-new",
      pendingSourceTitle: "用户新选的历史",
    });
    expect(session.currentTopicId).toBe("t1");
    expect(session.anchorFragmentId).toBe("f-hist");
    expect(session.pendingContinuation?.intentId).toBe("intent-new");

    // 旧依据下的话题操作成功事件迟到（v8 / seq19）——必须整条丢弃
    anchor(events, {
      topicId: "t-old-op",
      name: "旧操作目标",
      fragmentId: "f-old",
      fragmentTitle: "旧历史",
      historic: false,
      origin: "topic_operation",
      operationId: "op-old",
      approvalId: "ap-old",
      paramVersion: 1,
      navVersion: 8,
      seq: 19,
    });

    expect(session.currentTopicId).toBe("t1");
    expect(session.anchorFragmentId).toBe("f-hist");
    expect(session.anchorHistoric).toBe(true);
    expect(session.pendingContinuation?.intentId).toBe("intent-new");
  });

  it("失效操作的迟到成功事件：即使 nav_version 更新也整条拒绝（操作关联拒绝优先）", () => {
    const { session, events } = setup();
    startTurn(session, events);
    anchor(events, { topicId: "manual-new", navVersion: 9, seq: 20 });
    // 操作已进入失效集合（拒绝 / 失效 / 被新导航替代）
    session.rememberInvalidTopicOperation("op-dead");
    anchor(events, {
      topicId: "t-old-op",
      origin: "topic_operation",
      operationId: "op-dead",
      navVersion: 99,
      seq: 99,
    });
    expect(session.currentTopicId).toBe("manual-new");
  });
});

describe("R3+R2 ②：A→B 后重放 A 的旧位置事件不得改变前端 B", () => {
  it("重放 A（v7/seq10）后位置仍在 B，B 的草稿不被迁移", () => {
    const { session, events } = setup();
    session.setAnchor("t-a", "f-a", "话题A", { id: "f-a", title: "A历史" }, true);
    anchor(events, {
      topicId: "t-a",
      name: "话题A",
      fragmentId: "f-a",
      fragmentTitle: "A历史",
      historic: true,
      navVersion: 7,
      seq: 10,
    });
    anchor(events, {
      topicId: "t-b",
      name: "话题B",
      fragmentId: "f-b",
      fragmentTitle: "B历史",
      historic: true,
      navVersion: 8,
      seq: 11,
    });
    expect(session.currentTopicId).toBe("t-b");

    waitingDraft(session, "在 B 里写的草稿");

    // 后端重放 A 的旧请求即使产生事件（旧 v7/seq10），前端也必须拒绝
    anchor(events, {
      topicId: "t-a",
      name: "话题A",
      fragmentId: "f-a",
      fragmentTitle: "A历史",
      historic: true,
      origin: "topic_operation",
      operationId: "op-a-replayed",
      navVersion: 7,
      seq: 10,
    });

    expect(session.currentTopicId).toBe("t-b");
    expect(session.anchorFragmentId).toBe("f-b");
    expect(session.draft).toBe("在 B 里写的草稿");
    expect(readDraft(draftStorageKey("chat", "t-b"))?.text).toBe("在 B 里写的草稿");
    expect(readDraft(draftStorageKey("chat", "t-a"))).toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });
});

describe("R2 §4.3：位置接受规则独立于草稿跟随", () => {
  it("无草稿、未武装跟随时：旧版本 / 同版本冲突 / 缺字段事件同样被拒绝", () => {
    const { session, events } = setup();
    // 基线事实：t1 / 无片段 / v5
    anchor(events, { topicId: "t1", navVersion: 5, seq: 3 });
    expect(session._topicDraftFollow).toBeNull();

    // 同版本合法细化：接受
    const refine = session.acceptTopicPositionEvent({
      topicId: "t1",
      fragmentId: "frag-1",
      historic: false,
      navVersion: 5,
      seq: 4,
      instanceId: "inst-1",
    });
    expect(refine.accepted).toBe(true);
    expect(refine.reason).toBe("fragment_refinement");
    expect(session.anchorFragmentId).toBe("frag-1");

    // 旧版本：拒绝
    const stale = session.acceptTopicPositionEvent({
      topicId: "t1",
      fragmentId: "frag-0",
      historic: false,
      navVersion: 4,
      seq: 9,
    });
    expect(stale.accepted).toBe(false);
    expect(stale.reason).toBe("stale_nav_version");

    // 同版本话题冲突：拒绝
    const conflict = session.acceptTopicPositionEvent({
      topicId: "t2",
      fragmentId: null,
      historic: false,
      navVersion: 5,
      seq: 10,
    });
    expect(conflict.accepted).toBe(false);
    expect(conflict.reason).toBe("topic_conflict_same_version");

    // 同版本但缺片段字段：不得抹掉已知真实片段
    const ambiguous = session.acceptTopicPositionEvent({
      topicId: "t1",
      fragmentId: null,
      historic: false,
      navVersion: 5,
      seq: 11,
    });
    expect(ambiguous.accepted).toBe(false);
    expect(ambiguous.reason).toBe("ambiguous_same_version");

    // 旧轮迟到的结果：即使版本更新也拒绝
    events.route(ev("TURN_START", { turn_id: "turn_now" }));
    const staleTurn = session.acceptTopicPositionEvent({
      origin: "topic_operation",
      operationId: "op-x",
      turnId: "turn_old",
      topicId: "t9",
      navVersion: 100,
      seq: 12,
    });
    expect(staleTurn.accepted).toBe(false);
    expect(staleTurn.reason).toBe("stale_turn");

    // 位置没有因为任何拒绝而改变
    expect(session.currentTopicId).toBe("t1");
    expect(session.anchorFragmentId).toBe("frag-1");
  });
});

describe("R4+R2 ③：恢复旧问题不抢占新轮次、不改导航与草稿", () => {
  it("新轮次进行中重试旧恢复：活动轮次 / 位置 / 草稿 / 记录都不变，不自动发送", async () => {
    const { session, events } = setup();
    // 旧轮失败 → 持久恢复事项种子（真实事件入口）
    events.route(ev("TURN_START", { turn_id: "turn_old" }));
    events.route(
      ev("TOPIC_PHASE", {
        phase: "recoverable_failure",
        turn_id: "turn_old",
        operation_id: "op-old",
        failed_stage: "context_prepare",
        recovery: { record_id: "rec-old", message: "回答准备失败" },
      }),
    );
    events.route(ev("TURN_END", { turn_id: "turn_old" }));
    expect(session.topicRecoveryItems.some((item) => item.recordId === "rec-old")).toBe(true);

    // 新轮次开始；用户已经在新位置并写了草稿
    session.setAnchor("t-new", "f-new", "新话题", { id: "f-new", title: "新位置" }, false);
    events.route(ev("TURN_START", { turn_id: "turn_new" }));
    waitingDraft(session, "新轮次进行中写的草稿");
    const loadCallsBefore = loadHistoryMock.mock.calls.length;

    vi.mocked(retryTopicAnswer).mockResolvedValueOnce({
      ok: false,
      code: "recovery_in_progress",
      message: "这条恢复正在处理中，请稍后刷新。",
      answered: false,
      inProgress: true,
    });
    const out = await session.retryTopicAnswer("rec-old");

    expect(out.inProgress).toBe(true);
    expect(out.ok).toBe(false);
    // 不抢占新轮次
    expect(session.activeTurnId).toBe("turn_new");
    // 不改当前导航与草稿
    expect(session.currentTopicId).toBe("t-new");
    expect(session.anchorFragmentId).toBe("f-new");
    expect(session.draft).toBe("新轮次进行中写的草稿");
    expect(readDraft(draftStorageKey("chat", "t-new"))?.text).toBe("新轮次进行中写的草稿");
    // 不重复发送、不伪造成功、不关闭记录、不重读历史
    expect(api.sendTurn).not.toHaveBeenCalled();
    expect(session.topicRecoveryItems.some((item) => item.recordId === "rec-old")).toBe(true);
    expect(loadHistoryMock.mock.calls.length).toBe(loadCallsBefore);
  });
});

describe("⑤ 无第二套状态权威：位置只由 ANCHOR / 权威快照更新", () => {
  it("轮次 / 审批 / 草稿等流程不改位置；权威快照补位置；旧实例迟到事件被拒绝", () => {
    const { session, events } = setup();
    startTurn(session, events);
    // 其他流程都不改位置：审批登记 + 草稿 + 轮次结束
    topicRequired(events, { approvalId: "ap-1", operationId: "op-1", targetTopicId: "t2" });
    waitingDraft(session, "草稿");
    events.route(ev("TURN_END", { turn_id: "turn_1" }));
    expect(session.currentTopicId).toBe("t1");

    // 服务器权威快照（/api/runtime/state.anchor）是补权威，不是第二套来源
    const applied = session.applyAuthoritativePositionSnapshot(
      {
        topic_id: "t-snap",
        topic_name: "快照话题",
        fragment_id: "f-snap",
        fragment_title: "快照片段",
        historic: false,
        nav_version: 12,
        pending_intent_id: "intent-1",
        pending_intent_version: 2,
        pending_source_fragment_id: "f-src",
        pending_source_title: "来源",
      },
      "inst-2",
    );
    expect(applied.applied).toBe(true);
    expect(session.currentTopicId).toBe("t-snap");
    expect(session.anchorFragmentId).toBe("f-snap");
    expect(session.pendingContinuation?.intentId).toBe("intent-1");

    // 旧实例的迟到位置事件：拒绝（版本更旧 + 实例不同）
    anchor(events, {
      topicId: "t-old",
      navVersion: 3,
      seq: 1,
      instanceId: "inst-1",
    });
    expect(session.currentTopicId).toBe("t-snap");
    expect(session.anchorFragmentId).toBe("f-snap");

    // 新实例的更新位置事件：接受（nav_version 主序）
    anchor(events, {
      topicId: "t-snap2",
      name: "快照后新位置",
      navVersion: 13,
      seq: 1,
      instanceId: "inst-2",
    });
    expect(session.currentTopicId).toBe("t-snap2");
  });
});

describe("④ 受影响集合回归（组合层）：正常切换的草稿跟随", () => {
  it("确认后跟随真实目标、不自动发送；拒绝后草稿留在原话题", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, {
      approvalId: "ap-ok",
      operationId: "op-ok",
      targetTopicId: "t2",
    });
    waitingDraft(session, "确认之后要发的话");
    approvalResult(events, id, "approved");
    anchor(events, {
      topicId: "t2",
      name: "目标话题",
      origin: "topic_operation",
      operationId: "op-ok",
      approvalId: id,
      paramVersion: 1,
      navVersion: 7,
      seq: 5,
    });
    expect(session.currentTopicId).toBe("t2");
    expect(session.draft).toBe("确认之后要发的话");
    expect(readDraft(draftStorageKey("chat", "t2"))?.text).toBe("确认之后要发的话");
    expect(api.sendTurn).not.toHaveBeenCalled();
  });
});
