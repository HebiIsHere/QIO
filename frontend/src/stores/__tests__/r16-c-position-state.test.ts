/**
 * R16-C · 前端整份位置状态接受、接续提示与草稿跟随（契约 §7 + §14.2/14.3/14.5/14.8）。
 *
 * 本文件只通过真实 Pinia store / 事件路由 / 本机草稿存储 / 真实 api 模块（写请求序列化）
 * 观察用户可见行为，覆盖：
 * - acceptPositionState 唯一入口（position_identity + position_version 整份排序、旧整份拒绝、
 *   同版本同内容幂等、同版本内容冲突记录并触发一次权威恢复、身份变化快照重建基线）；
 * - 事件 ANCHOR / runtime 快照 / 导航 HTTP 响应都走同一入口；旧后端无 position_version 走
 *   legacy_nav_order 兼容路径；
 * - 自动分段 / 内部细化按 position_version 推进片段（I03）；
 * - 旧快照三件套（位置/历史标记/pending）整份拒绝（I02/I06，回归 B）；
 * - 重连快照先到：等待草稿不离开输入框、不被覆盖，跟随仍武装，操作结果到达后迁移一次（I13）；
 * - 草稿跟随独立幂等消费身份：重复结果零副作用、无关手动导航不消费、被替代结果不搬并清除、
 *   目标已有草稿保护、始终不自动发送；
 * - 手动导航单飞串行 + 显式 requestId/baseNavVersion（每次真实动作新 UUID、网络重试复用）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";
import { draftStorageKey, readDraft, writeDraft } from "../../interactive/drafts";
import { api } from "../../services/api";
import type { AgentEvent } from "../../services/events";

vi.mock("../../services/api", () => ({
  api: {
    getRuntimeState: vi.fn(async () => ({
      instance_id: "inst-1",
      revision: 1,
      turn_queue: { instance_id: "inst-1", revision: 1, running: null, queued: [], cancelled: [] },
      approvals: [],
      tasks: [],
      tools: [],
    })),
    getSessionContext: vi.fn(async () => ({
      topic_id: "t1",
      topic_name: "话题一",
      anchor_fragment: null,
      messages: [],
    })),
    sendTurn: vi.fn(async () => ({
      ok: true,
      accepted: true,
      turn_id: "turn_9",
      status: "accepted",
      topic_id: null,
    })),
    cancelTurn: vi.fn(async () => ({ ok: true, cancelled: true })),
    cancelContinuation: vi.fn(async () => ({ ok: true, cancelled: true })),
    setAnchor: vi.fn(async () => ({ ok: true })),
    continueFromHistory: vi.fn(async () => ({ ok: true })),
    respondTopicApproval: vi.fn(async () => ({ ok: true })),
    respondApproval: vi.fn(async () => ({ ok: true })),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
    listDevAuthorizations: vi.fn(async () => ({ items: [] })),
    getRecoveryRecords: vi.fn(async () => ({ records: [], total: 0, shown: 0, truncated: false })),
  },
}));

vi.mock("../../services/backend", () => ({
  resolveBackend: async () => ({ base: "http://api.test", token: "test-token" }),
  authHeaders: () => ({ Authorization: "Bearer test-token" }),
  resetBackend: () => {},
}));

type Session = ReturnType<typeof useSessionStore>;
type EventStore = ReturnType<typeof useEventStore>;

let eventSeq = 0;

function ev(type: string, data: Record<string, unknown>): AgentEvent {
  eventSeq += 1;
  return { type, id: `r16c-${type}-${eventSeq}`, ts: "", data } as AgentEvent;
}

function setup(): { session: Session; events: EventStore } {
  setActivePinia(createPinia());
  const session = useSessionStore();
  const events = useEventStore();
  /**
   * 历史重载必须回显当前权威话题（真实后端与 ANCHOR 同源）。否则一次合法的
   * 位置采用会被随后的 loadHistory 用固定话题盖回去，测试就不是在测接受规则了。
   */
  vi.mocked(api.getSessionContext).mockImplementation(async () => {
    const currentTopic = session.currentTopicId ?? "t1";
    const fragmentId = session.anchorFragmentId;
    return {
      topic_id: currentTopic,
      topic_name: "话题",
      anchor_fragment: fragmentId
        ? { id: fragmentId, title: null, historic: session.anchorHistoric }
        : null,
      messages: [],
    } as never;
  });
  return { session, events };
}

/** §3.1 position wire（snake_case 全套；测试可覆盖任意字段）。 */
function wire(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    topic_id: "t1",
    topic_name: "话题一",
    fragment_id: null,
    fragment_title: null,
    historic: false,
    pending_intent_id: null,
    pending_intent_version: null,
    pending_source_fragment_id: null,
    pending_source_title: null,
    nav_version: 1,
    position_version: 1,
    position_identity: "db-1",
    ...overrides,
  };
}

function legacyWire(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  const record = wire(overrides);
  delete record.position_version;
  delete record.position_identity;
  return record;
}

function accept(
  session: Session,
  input: Record<string, unknown>,
  source: "anchor" | "snapshot" | "command" = "anchor",
) {
  return session.acceptPositionState(input, source);
}

function positionOf(session: Session) {
  return {
    topicId: session.currentTopicId,
    fragmentId: session.anchorFragmentId,
    historic: session.anchorHistoric,
  };
}

function pendingOf(session: Session) {
  return session.pendingContinuation;
}

function factsOf(session: Session): Record<string, unknown> | null {
  return (session as unknown as { _positionFacts: Record<string, unknown> | null })._positionFacts;
}

function conflictOf(session: Session): Record<string, unknown> | null {
  return (session as unknown as { _positionStateConflict: Record<string, unknown> | null })
    ._positionStateConflict;
}

function consumedOf(session: Session): string[] {
  return (session as unknown as { _draftFollowConsumed: string[] })._draftFollowConsumed;
}

function followOf(session: Session): Record<string, unknown> | null {
  return (session as unknown as { _topicDraftFollow: Record<string, unknown> | null })
    ._topicDraftFollow;
}

function chatKey(topicId: string): string {
  return draftStorageKey("chat", topicId);
}

function storedText(topicId: string): string | null {
  return readDraft(chatKey(topicId))?.text ?? null;
}

function writeWaitingDraft(session: Session, text: string): void {
  session.draft = text;
  session.flushDraft();
}

function historyContext(topicId: string, fragmentId: string | null = null, historic = false): void {
  vi.mocked(api.getSessionContext).mockResolvedValue({
    topic_id: topicId,
    topic_name: "话题",
    anchor_fragment: fragmentId ? { id: fragmentId, title: null, historic } : null,
    messages: [],
  } as never);
}

/** 真实审批顺序：开轮 → 话题申请 → 等待期间写草稿 → 确认（武装跟随）。 */
function armFollowWithWaitingDraft(
  session: Session,
  events: EventStore,
  opts: { draftText?: string; operationId?: string; approvalId?: string } = {},
): void {
  const operationId = opts.operationId ?? "op-1";
  const approvalId = opts.approvalId ?? "ap-1";
  // 应用启动已接受的基线（真实顺序：先有 runtime 快照，再有审批/导航）。
  accept(session, wire({ nav_version: 1, position_version: 1 }));
  events.route(ev("TURN_START", { turn_id: "turn_1", revision: 1, instance_id: "inst-1" }));
  events.route(
    ev("APPROVAL_REQUIRED", {
      approval: {
        approval_id: approvalId,
        kind: "topic_switch",
        payload: {
          action: "switch",
          turn_id: "turn_1",
          operation_id: operationId,
          param_version: 1,
          base_nav_version: 1,
          target_topic_id: "t2",
          from_topic_id: "t1",
          from_fragment_id: null,
        },
        turn_id: "turn_1",
      },
      phase: "waiting",
    }),
  );
  writeWaitingDraft(session, opts.draftText ?? "等待期间写的话");
  events.route(
    ev("APPROVAL_RESULT", {
      approval_id: approvalId,
      phase: "approved",
      decision: "approved",
      turn_id: "turn_1",
    }),
  );
  expect(followOf(session)).not.toBeNull();
}

/** 本次操作的权威成功结果（新后端：整份 position wire + 操作身份）。 */
function operationResult(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    ...wire({ topic_id: "t2", nav_version: 8, position_version: 8 }),
    origin: "topic_operation",
    operation_id: "op-1",
    approval_id: "ap-1",
    param_version: 1,
    turn_id: "turn_1",
    ...overrides,
  };
}

beforeEach(() => {
  localStorage.clear();
  eventSeq = 0;
  vi.clearAllMocks();
  historyContext("t1");
});

afterEach(() => {
  vi.useRealTimers();
});

describe("R16-C · 整份位置状态接受（I06 / §14.3）", () => {
  it("无本地事实 → baseline 采用整份；位置与 pending 同源可见", () => {
    const { session } = setup();
    const result = accept(
      session,
      wire({
        topic_id: "t1",
        fragment_id: "f1",
        fragment_title: "片段一",
        historic: true,
        pending_intent_id: "intent-1",
        pending_intent_version: 2,
        pending_source_fragment_id: "f1",
        pending_source_title: "片段一",
        nav_version: 3,
        position_version: 4,
      }),
    );
    expect(result).toEqual({
      accepted: true,
      reason: "baseline",
      idempotent: false,
      conflicted: false,
      adopted: true,
    });
    expect(positionOf(session)).toEqual({ topicId: "t1", fragmentId: "f1", historic: true });
    expect(pendingOf(session)).toEqual({ intentId: "intent-1", sourceTitle: "片段一" });
    expect(factsOf(session)).toMatchObject({
      topicId: "t1",
      fragmentId: "f1",
      historic: true,
      navVersion: 3,
      positionVersion: 4,
      positionIdentity: "db-1",
      pendingIntentId: "intent-1",
      pendingSourceFragmentId: "f1",
    });
  });

  it("position_version 更大 → 整份采用（new_position_state，非幂等）", () => {
    const { session } = setup();
    accept(session, wire({ nav_version: 3, position_version: 4 }));
    const result = accept(
      session,
      wire({
        topic_id: "t1",
        fragment_id: "f2",
        fragment_title: "片段二",
        historic: false,
        pending_intent_id: "intent-2",
        pending_intent_version: 1,
        pending_source_title: "片段二",
        nav_version: 5,
        position_version: 6,
      }),
    );
    expect(result).toMatchObject({ accepted: true, reason: "new_position_state", adopted: true });
    expect(positionOf(session)).toEqual({ topicId: "t1", fragmentId: "f2", historic: false });
    expect(pendingOf(session)).toEqual({ intentId: "intent-2", sourceTitle: "片段二" });
  });

  it("position_version 更旧 → 整份拒绝（位置/pending/草稿都不动）", () => {
    const { session } = setup();
    accept(
      session,
      wire({ nav_version: 9, position_version: 9, pending_intent_id: "intent-new", pending_source_title: "新" }),
    );
    writeWaitingDraft(session, "不该被旧状态动到的草稿");
    const before = positionOf(session);

    const result = accept(
      session,
      wire({
        topic_id: "t-old",
        fragment_id: "f-old",
        historic: true,
        nav_version: 8,
        position_version: 8,
        pending_intent_id: "intent-old",
        pending_source_title: "旧",
      }),
    );
    expect(result).toEqual({
      accepted: false,
      reason: "stale_position_state",
      idempotent: false,
      conflicted: false,
      adopted: false,
    });
    expect(positionOf(session)).toEqual(before);
    expect(pendingOf(session)).toEqual({ intentId: "intent-new", sourceTitle: "新" });
    expect(session.draft).toBe("不该被旧状态动到的草稿");
    expect(factsOf(session)).toMatchObject({ topicId: "t1", positionVersion: 9 });
  });

  it("同版本同内容 → 幂等接受、零副作用（不重载历史、不迁移草稿）", () => {
    const { session } = setup();
    accept(session, wire({ nav_version: 5, position_version: 5 }));
    const factsBefore = { ...(factsOf(session) ?? {}) };
    writeWaitingDraft(session, "幂等不改的草稿");
    vi.mocked(api.getSessionContext).mockClear();

    const result = accept(session, wire({ nav_version: 5, position_version: 5 }), "command");
    expect(result).toEqual({
      accepted: true,
      reason: "idempotent",
      idempotent: true,
      conflicted: false,
      adopted: false,
    });
    expect(factsOf(session)).toEqual(factsBefore);
    expect(session.draft).toBe("幂等不改的草稿");
    expect(conflictOf(session)).toBeNull();
    expect(api.getSessionContext).not.toHaveBeenCalled();
  });

  it("同版本内容冲突 → 不合并、记录 _positionStateConflict 并触发一次权威恢复（same_version_conflict）", async () => {
    const { session } = setup();
    accept(
      session,
      wire({ nav_version: 5, position_version: 5, pending_intent_id: "intent-a", pending_source_title: "A" }),
    );
    vi.mocked(api.getRuntimeState).mockClear();

    const result = accept(
      session,
      wire({
        topic_id: "t2",
        fragment_id: "f9",
        historic: true,
        nav_version: 5,
        position_version: 5,
        pending_intent_id: "intent-b",
        pending_source_title: "B",
      }),
    );
    expect(result).toEqual({
      accepted: false,
      reason: "same_version_conflict",
      idempotent: false,
      conflicted: true,
      adopted: false,
    });
    // 不部分合并：位置与 pending 都保持本地版本。
    expect(positionOf(session)).toEqual({ topicId: "t1", fragmentId: null, historic: false });
    expect(pendingOf(session)).toEqual({ intentId: "intent-a", sourceTitle: "A" });
    const conflict = conflictOf(session);
    expect(conflict).toMatchObject({ source: "anchor" });
    expect((conflict?.local as Record<string, unknown>)?.topicId).toBe("t1");
    expect((conflict?.incoming as Record<string, unknown>)?.topicId).toBe("t2");
    // 触发一次权威恢复。
    expect(api.getRuntimeState).toHaveBeenCalledTimes(1);
    await Promise.resolve();
    await Promise.resolve();
  });

  it("旧实例事件不能绕过检查（identity 不同 + nav_version 相同 → stale_position_identity）", () => {
    const { session } = setup();
    accept(session, wire({ nav_version: 12, position_version: 7, position_identity: "db-new" }));
    vi.mocked(api.getRuntimeState).mockClear();

    const result = accept(
      session,
      wire({
        topic_id: "t-old",
        nav_version: 12,
        position_version: 7,
        position_identity: "db-old",
        pending_intent_id: "intent-old",
      }),
    );
    expect(result).toMatchObject({
      accepted: false,
      reason: "stale_position_identity",
      adopted: false,
    });
    expect(positionOf(session)).toEqual({ topicId: "t1", fragmentId: null, historic: false });
    expect(pendingOf(session)).toBeNull();
    expect(api.getRuntimeState).toHaveBeenCalledTimes(1);
  });

  it("身份变化的权威快照 → 重建基线整份采用（new_position_identity），随后旧身份事件被拒（I07）", () => {
    const { session } = setup();
    accept(session, wire({ nav_version: 7, position_version: 5, position_identity: "db-old" }));

    const snapshotResult = accept(
      session,
      wire({
        topic_id: "t-restored",
        fragment_id: "f-r",
        fragment_title: "恢复片段",
        nav_version: 0,
        position_version: 0,
        position_identity: "db-new",
      }),
      "snapshot",
    );
    expect(snapshotResult).toMatchObject({
      accepted: true,
      reason: "new_position_identity",
      adopted: true,
    });
    expect(positionOf(session)).toEqual({
      topicId: "t-restored",
      fragmentId: "f-r",
      historic: false,
    });
    expect(factsOf(session)).toMatchObject({ positionIdentity: "db-new", positionVersion: 0 });

    // 旧实例（旧数据库身份）的事件即使 position_version 更大，也不得绕过身份检查。
    const stale = accept(
      session,
      wire({
        topic_id: "t-old",
        nav_version: 20,
        position_version: 99,
        position_identity: "db-old",
      }),
    );
    expect(stale).toMatchObject({ accepted: false, reason: "stale_position_identity" });
    expect(positionOf(session)).toEqual({
      topicId: "t-restored",
      fragmentId: "f-r",
      historic: false,
    });
  });

  it("ANCHOR 同时带扁平旧字段与嵌套 position 时以嵌套整份状态为准（I02 同源）", () => {
    const { session, events } = setup();
    accept(session, wire({ nav_version: 3, position_version: 3 }));
    events.route(
      ev("ANCHOR", {
        // 扁平字段故意落后 / 与嵌套冲突：接受入口必须用同源的整份 position。
        topic_id: "t-flat-stale",
        historic: false,
        nav_version: 2,
        position_version: 9,
        position_identity: "db-1",
        position: wire({
          topic_id: "t2",
          fragment_id: "f2",
          fragment_title: "片段二",
          historic: true,
          nav_version: 9,
          position_version: 9,
          pending_intent_id: "intent-9",
          pending_source_title: "片段二",
        }),
      }),
    );
    expect(positionOf(session)).toEqual({ topicId: "t2", fragmentId: "f2", historic: true });
    expect(pendingOf(session)).toEqual({ intentId: "intent-9", sourceTitle: "片段二" });
    expect(factsOf(session)).toMatchObject({ positionVersion: 9 });
  });

  it("旧后端（无 position_version）ANCHOR 走 legacy_nav_order 兼容路径", () => {
    const { session } = setup();
    const result = accept(session, legacyWire({ topic_id: "t1", nav_version: 3 }), "anchor");
    expect(result).toMatchObject({
      accepted: true,
      reason: "legacy_nav_order",
      adopted: true,
    });
    expect(positionOf(session)).toEqual({ topicId: "t1", fragmentId: null, historic: false });
  });

  it("no_input / no_topic 明确拒绝", () => {
    const { session } = setup();
    expect(accept(session, null as unknown as Record<string, unknown>).reason).toBe("no_input");
    expect(accept(session, wire({ topic_id: null })).reason).toBe("no_topic");
  });
});

describe("R16-C · 同源完整状态与旧快照三件套（I02 / I03 / 回归 A、B、C）", () => {
  it("自动分段 / 内部细化按 position_version 推进片段并重载历史（I03）", () => {
    const { session } = setup();
    accept(
      session,
      wire({
        topic_id: "t1",
        fragment_id: "f-open",
        historic: false,
        nav_version: 4,
        position_version: 4,
      }),
    );
    vi.mocked(api.getSessionContext).mockClear();
    historyContext("t1", "f-sealed", true);

    // auto_segment：开放片段被容量封存 → 同话题片段变化（真实状态变化 +1 版本）。
    const segmented = accept(
      session,
      wire({
        topic_id: "t1",
        fragment_id: "f-sealed",
        fragment_title: "已封存片段",
        historic: true,
        nav_version: 4,
        position_version: 5,
      }),
    );
    expect(segmented).toMatchObject({
      accepted: true,
      reason: "new_position_state",
      adopted: true,
    });
    expect(positionOf(session)).toEqual({ topicId: "t1", fragmentId: "f-sealed", historic: true });
    expect(api.getSessionContext).toHaveBeenCalledTimes(1);
  });

  it("较新位置/pending 已接受后，旧快照三者（位置/历史标记/pending）整份拒绝（回归 B）", () => {
    const { session } = setup();
    accept(session, wire({ nav_version: 3, position_version: 3 }));
    accept(
      session,
      wire({
        topic_id: "t2",
        fragment_id: "f-new",
        fragment_title: "新位置",
        historic: false,
        pending_intent_id: "intent-new",
        pending_intent_version: 2,
        pending_source_title: "新接续",
        nav_version: 9,
        position_version: 9,
      }),
      "anchor",
    );
    historyContext("t2", "f-new", false);

    const staleSnapshot = accept(
      session,
      wire({
        topic_id: "t-old",
        fragment_id: "f-old",
        fragment_title: "旧位置",
        historic: true,
        pending_intent_id: "intent-old",
        pending_intent_version: 1,
        pending_source_title: "旧接续",
        nav_version: 8,
        position_version: 8,
      }),
      "snapshot",
    );
    expect(staleSnapshot).toMatchObject({ accepted: false, reason: "stale_position_state" });
    expect(positionOf(session)).toEqual({ topicId: "t2", fragmentId: "f-new", historic: false });
    expect(pendingOf(session)).toEqual({ intentId: "intent-new", sourceTitle: "新接续" });
  });

  it("重连快照走同一入口：applyRuntimeState 的较旧 anchor 整份拒绝，pending 不被部分覆盖（I02）", () => {
    const { session, events } = setup();
    accept(
      session,
      wire({
        nav_version: 9,
        position_version: 9,
        pending_intent_id: "intent-new",
        pending_source_title: "新接续",
        pending_source_fragment_id: "f-new",
      }),
    );
    events.applyRuntimeState({
      instance_id: "inst-1",
      approvals: [],
      tasks: [],
      anchor: wire({
        topic_id: "t-old",
        fragment_id: "f-old",
        historic: true,
        pending_intent_id: "intent-old",
        pending_source_title: "旧接续",
        nav_version: 8,
        position_version: 8,
      }),
    });
    expect(positionOf(session)).toEqual({ topicId: "t1", fragmentId: null, historic: false });
    expect(pendingOf(session)).toEqual({ intentId: "intent-new", sourceTitle: "新接续" });
    expect(factsOf(session)).toMatchObject({ positionVersion: 9 });
  });

  it("runtime 快照的新版 anchor 走同一入口整份采用（位置与 pending 同源）", () => {
    const { session, events } = setup();
    accept(session, wire({ nav_version: 1, position_version: 1 }));
    events.applyRuntimeState({
      instance_id: "inst-1",
      approvals: [],
      tasks: [],
      anchor: wire({
        topic_id: "t2",
        fragment_id: "f2",
        fragment_title: "片段二",
        historic: true,
        nav_version: 7,
        position_version: 7,
        pending_intent_id: "intent-7",
        pending_source_title: "片段二",
      }),
    });
    expect(positionOf(session)).toEqual({ topicId: "t2", fragmentId: "f2", historic: true });
    expect(pendingOf(session)).toEqual({ intentId: "intent-7", sourceTitle: "片段二" });
    expect(factsOf(session)).toMatchObject({
      positionVersion: 7,
      source: "snapshot",
    });
  });

  it("事件 ANCHOR 走同一入口：较旧事件整条拒绝，较新事件整份采用", () => {
    const { session, events } = setup();
    accept(session, wire({ nav_version: 8, position_version: 8 }));
    events.route(
      ev("ANCHOR", {
        ...wire({ topic_id: "t-old", nav_version: 7, position_version: 7 }),
        pending_intent_id: "intent-old",
      }),
    );
    expect(positionOf(session)).toEqual({ topicId: "t1", fragmentId: null, historic: false });
    expect(pendingOf(session)).toBeNull();

    events.route(
      ev("ANCHOR", {
        ...wire({
          topic_id: "t2",
          fragment_id: "f2",
          fragment_title: "片段二",
          historic: true,
          nav_version: 10,
          position_version: 11,
          pending_intent_id: "intent-2",
          pending_source_title: "片段二",
        }),
      }),
    );
    expect(positionOf(session)).toEqual({ topicId: "t2", fragmentId: "f2", historic: true });
    expect(pendingOf(session)).toEqual({ intentId: "intent-2", sourceTitle: "片段二" });
  });
});

describe("R16-C · 草稿跟随（独立幂等消费身份，§7.3 / §14.8）", () => {
  it("本次操作权威结果到达：迁移一次、跟随清除、消费身份登记、不自动发送", () => {
    const { session, events } = setup();
    armFollowWithWaitingDraft(session, events);
    vi.mocked(api.sendTurn).mockClear();

    const result = accept(session, operationResult(), "anchor");
    expect(result).toMatchObject({ accepted: true, reason: "new_position_state" });
    expect(positionOf(session)).toEqual({ topicId: "t2", fragmentId: null, historic: false });
    expect(session.draft).toBe("等待期间写的话");
    expect(storedText("t2")).toBe("等待期间写的话");
    expect(storedText("t1")).toBeNull();
    expect(followOf(session)).toBeNull();
    expect(consumedOf(session)).toContain("follow:op-1:1");
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("重复结果命中已消费集合 → 零副作用（不重复搬运、不重复登记）", () => {
    const { session, events } = setup();
    armFollowWithWaitingDraft(session, events);
    accept(session, operationResult(), "anchor");
    const draftAfter = session.draft;
    const consumedAfter = [...consumedOf(session)];

    const replay = accept(session, operationResult(), "command");
    expect(replay).toMatchObject({ accepted: true, reason: "idempotent", idempotent: true });
    expect(session.draft).toBe(draftAfter);
    expect(consumedOf(session)).toEqual(consumedAfter);
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("无关手动导航不消费跟随；被更新选择替代的旧操作结果不搬草稿并清除跟随", () => {
    const { session, events } = setup();
    armFollowWithWaitingDraft(session, events);

    // 用户手动导航到新位置（新入口，无关本次操作）：跟随不得被消费。
    const manual = accept(
      session,
      wire({ topic_id: "t-manual", nav_version: 20, position_version: 20 }),
      "anchor",
    );
    expect(manual).toMatchObject({ accepted: true, reason: "new_position_state" });
    expect(followOf(session)).not.toBeNull();
    expect(consumedOf(session)).toEqual([]);
    expect(storedText("t2")).toBeNull();

    // 旧操作结果（position_version 旧于已接受状态）：不搬草稿并清除跟随。
    const stale = accept(
      session,
      operationResult({ nav_version: 19, position_version: 19 }),
      "anchor",
    );
    expect(stale).toMatchObject({ accepted: false, reason: "stale_position_state" });
    expect(followOf(session)).toBeNull();
    expect(consumedOf(session)).toContain("follow:op-1:1");
    expect(storedText("t2")).toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("目标已有草稿：不静默覆盖（登记为被让位草稿）、不自动发送", () => {
    const { session, events } = setup();
    expect(writeDraft(chatKey("t2"), "目标话题原有的草稿", 1).ok).toBe(true);
    armFollowWithWaitingDraft(session, events);
    vi.mocked(api.sendTurn).mockClear();

    accept(session, operationResult(), "anchor");
    expect(session.draft).toBe("等待期间写的话");
    expect(storedText("t2")).toBe("等待期间写的话");
    expect(storedText("t1")).toBeNull();
    const displaced = session.displacedDraftsForTopic("t2");
    expect(displaced.some((record) => record.text === "目标话题原有的草稿")).toBe(true);
    expect(displaced.every((record) => record.text !== "等待期间写的话")).toBe(true);
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("重连快照先到：等待草稿不离开输入框、不被覆盖；结果到达后迁移一次（I13 回归）", () => {
    const { session, events } = setup();
    armFollowWithWaitingDraft(session, events);
    expect(session.draft).toBe("等待期间写的话");

    // 断线重连：快照先到（本次操作已生效的真实位置，但还没有操作结果事件）。
    const snapshot = accept(
      session,
      wire({
        topic_id: "t2",
        fragment_id: null,
        historic: false,
        nav_version: 8,
        position_version: 8,
      }),
      "snapshot",
    );
    expect(snapshot).toMatchObject({ accepted: true, reason: "new_position_state", adopted: true });
    // 快照采用位置时不得按普通导航清掉等待草稿；跟随仍武装。
    expect(session.draft).toBe("等待期间写的话");
    expect(followOf(session)).not.toBeNull();
    expect(consumedOf(session)).toEqual([]);
    expect(storedText("t1")).toBe("等待期间写的话");

    // 权威操作结果后到（位置已同版本同内容）：幂等接受，但草稿按操作身份迁移一次。
    const result = accept(session, operationResult(), "command");
    expect(result).toMatchObject({ accepted: true, reason: "idempotent", idempotent: true });
    expect(session.draft).toBe("等待期间写的话");
    expect(storedText("t2")).toBe("等待期间写的话");
    expect(storedText("t1")).toBeNull();
    expect(followOf(session)).toBeNull();
    expect(consumedOf(session)).toContain("follow:op-1:1");
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("重连快照不覆盖等待期间的新编辑（新编辑保存到原话题，不被位置恢复清掉）", () => {
    const { session, events } = setup();
    armFollowWithWaitingDraft(session, events);
    // 用户在等待期间又改了草稿（这是必须受保护的新编辑）。
    writeWaitingDraft(session, "等待期间的新编辑");

    accept(
      session,
      wire({ topic_id: "t2", nav_version: 8, position_version: 8 }),
      "snapshot",
    );
    expect(session.draft).toBe("等待期间的新编辑");
    expect(storedText("t1")).toBe("等待期间的新编辑");
    expect(followOf(session)).not.toBeNull();
  });

  it("APPROVAL_RESULT(executed) 携带的 position 走接受入口并迁移草稿一次", () => {
    const { session, events } = setup();
    armFollowWithWaitingDraft(session, events);

    events.route(
      ev("APPROVAL_RESULT", {
        approval_id: "ap-1",
        phase: "executed",
        decision: "approved",
        turn_id: "turn_1",
        operation_id: "op-1",
        param_version: 1,
        position: wire({ topic_id: "t2", nav_version: 8, position_version: 8 }),
      }),
    );

    expect(positionOf(session)).toEqual({ topicId: "t2", fragmentId: null, historic: false });
    expect(session.draft).toBe("等待期间写的话");
    expect(storedText("t2")).toBe("等待期间写的话");
    expect(storedText("t1")).toBeNull();
    expect(followOf(session)).toBeNull();
    expect(consumedOf(session)).toContain("follow:op-1:1");
  });
});

describe("R16-C · 手动导航：显式身份 + 单飞串行（§14.2）", () => {
  function deferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (error: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  }

  it("单飞串行：后点排队，拿到前一次接受后的新 base_nav_version 再发", async () => {
    const { session } = setup();
    accept(session, wire({ nav_version: 5, position_version: 5 }));
    const first = deferred<Record<string, unknown>>();
    vi.mocked(api.setAnchor).mockReturnValueOnce(first.promise as never);
    vi.mocked(api.setAnchor).mockResolvedValueOnce({
      ok: true,
      changed: true,
      replay: false,
      position: wire({ topic_id: "tB", nav_version: 9, position_version: 9 }),
    } as never);

    const p1 = session.manualNavigation({ kind: "enter_topic", topicId: "tA" });
    const p2 = session.manualNavigation({ kind: "enter_topic", topicId: "tB" });
    await Promise.resolve();
    expect(api.setAnchor).toHaveBeenCalledTimes(1);
    const firstOptions = vi.mocked(api.setAnchor).mock.calls[0][2] as {
      requestId: string;
      baseNavVersion: number;
    };
    expect(firstOptions.baseNavVersion).toBe(5);
    expect(typeof firstOptions.requestId).toBe("string");
    expect(firstOptions.requestId.length).toBeGreaterThan(0);

    first.resolve({
      ok: true,
      changed: true,
      replay: false,
      position: wire({ topic_id: "tA", nav_version: 8, position_version: 8 }),
    });
    const r1 = await p1;
    expect(r1.ok).toBe(true);

    const r2 = await p2;
    expect(r2.ok).toBe(true);
    expect(api.setAnchor).toHaveBeenCalledTimes(2);
    const secondOptions = vi.mocked(api.setAnchor).mock.calls[1][2] as {
      requestId: string;
      baseNavVersion: number;
    };
    // 排队动作拿到前一次接受后的新 base。
    expect(secondOptions.baseNavVersion).toBe(8);
    expect(secondOptions.requestId).not.toBe(firstOptions.requestId);
    expect(positionOf(session)).toEqual({ topicId: "tB", fragmentId: null, historic: false });
  });

  it("网络失败后重试同一动作：复用同一 request_id 与 base_nav_version", async () => {
    const { session } = setup();
    accept(session, wire({ nav_version: 5, position_version: 5 }));
    vi.mocked(api.setAnchor).mockRejectedValueOnce(new Error("fetch failed"));
    vi.mocked(api.setAnchor).mockResolvedValueOnce({
      ok: true,
      changed: true,
      replay: false,
      position: wire({ topic_id: "tA", nav_version: 8, position_version: 8 }),
    } as never);

    const first = await session.manualNavigation({ kind: "enter_topic", topicId: "tA" });
    expect(first.ok).toBe(false);
    const firstOptions = vi.mocked(api.setAnchor).mock.calls[0][2] as {
      requestId: string;
      baseNavVersion: number;
    };

    const retry = await session.manualNavigation({ kind: "enter_topic", topicId: "tA" });
    expect(retry.ok).toBe(true);
    const retryOptions = vi.mocked(api.setAnchor).mock.calls[1][2] as {
      requestId: string;
      baseNavVersion: number;
    };
    expect(retryOptions.requestId).toBe(firstOptions.requestId);
    expect(retryOptions.baseNavVersion).toBe(firstOptions.baseNavVersion);
  });

  it("409 navigation_command_stale：返回结构化失败、不改位置、触发一次权威恢复", async () => {
    const { session } = setup();
    accept(session, wire({ nav_version: 5, position_version: 5 }));
    vi.mocked(api.setAnchor).mockRejectedValueOnce({
      status: 409,
      body: { code: "navigation_command_stale" },
      message: "POST /api/anchor -> 409: stale",
    } as never);
    vi.mocked(api.getRuntimeState).mockClear();

    const result = await session.manualNavigation({ kind: "enter_topic", topicId: "tA" });
    expect(result.ok).toBe(false);
    expect(result.code).toBe("navigation_command_stale");
    expect(positionOf(session)).toEqual({ topicId: "t1", fragmentId: null, historic: false });
    expect(api.getRuntimeState).toHaveBeenCalledTimes(1);
  });

  it("取消旧接续选择也带显式身份与 base_nav_version", async () => {
    const { session } = setup();
    accept(session, wire({ nav_version: 5, position_version: 5 }));
    session.setPendingContinuation({ intentId: "intent-a", sourceTitle: "历史片段 A" });
    vi.mocked(api.cancelContinuation).mockClear();

    const ok = await session.cancelPendingContinuation();
    expect(ok).toBe(true);
    expect(api.cancelContinuation).toHaveBeenCalledTimes(1);
    const options = vi.mocked(api.cancelContinuation).mock.calls[0][0] as {
      requestId: string;
      baseNavVersion: number;
    };
    expect(options.baseNavVersion).toBe(5);
    expect(typeof options.requestId).toBe("string");
    expect(session.pendingContinuation).toBeNull();
  });
});

describe("R16-C · api 写请求携带 request_id / base_nav_version（§14.2）", () => {
  it("setAnchor / continueFromHistory 的请求体包含显式身份与依据", async () => {
    const actual = await vi.importActual<typeof import("../../services/api")>("../../services/api");
    const calls: { url: string; init: RequestInit }[] = [];
    globalThis.fetch = vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return {
        ok: true,
        status: 200,
        text: async () => "{}",
        json: async () => ({ ok: true, position: null }),
      } as unknown as Response;
    }) as unknown as typeof fetch;

    await actual.api.setAnchor("t1", null, { requestId: "req-anchor-1", baseNavVersion: 7 });
    await actual.api.continueFromHistory("t1", "f2", {
      requestId: "req-continue-1",
      baseNavVersion: 8,
    });
    await actual.api.cancelContinuation({ requestId: "req-cancel-1", baseNavVersion: 9 });

    expect(calls).toHaveLength(3);
    expect(JSON.parse(String(calls[0].init.body))).toMatchObject({
      topic_id: "t1",
      fragment_id: null,
      request_id: "req-anchor-1",
      base_nav_version: 7,
    });
    expect(JSON.parse(String(calls[1].init.body))).toMatchObject({
      topic_id: "t1",
      fragment_id: "f2",
      continue_from_history: true,
      request_id: "req-continue-1",
      base_nav_version: 8,
    });
    expect(JSON.parse(String(calls[2].init.body))).toMatchObject({
      request_id: "req-cancel-1",
      base_nav_version: 9,
    });
  });
});
