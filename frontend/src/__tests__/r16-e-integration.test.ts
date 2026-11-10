/**
 * R16 · E：前端真实接线集成（快照先到 + 权威操作结果后到；草稿跟随一次；旧实例事件）。
 *
 * 真实 Pinia session/events store + 真实本机草稿存储；api 与 restoreRuntimeState 为 mock
 *（不联网、不写真实应用数据）。时序用真实事件顺序，不用 sleep。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../stores/session";
import { useEventStore } from "../stores/events";
import { draftStorageKey, readDraft } from "../interactive/drafts";
import { api } from "../services/api";
import type { AgentEvent } from "../services/events";

const { restoreSpy } = vi.hoisted(() => ({
  restoreSpy: vi.fn(async () => ({ restored: true })),
}));

vi.mock("../stores/restore", () => ({
  restoreRuntimeState: restoreSpy,
}));

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
    getRuntimeState: vi.fn(async () => ({ instance_id: "inst-1", anchor: null })),
  },
}));

let seq = 0;

function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: "r16e-i-" + type + "-" + seq, ts: "", data } as AgentEvent;
}

function position(overrides: Record<string, unknown> = {}): Record<string, unknown> {
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
    nav_version: 3,
    position_version: 11,
    position_identity: "db-i",
    ...overrides,
  };
}

function anchor(events: ReturnType<typeof useEventStore>, payload: Record<string, unknown>) {
  const p = position((payload.position as Record<string, unknown> | undefined) ?? undefined);
  const flat: Record<string, unknown> = {
    topic_id: p.topic_id,
    topic_name: p.topic_name,
    fragment_id: p.fragment_id,
    fragment_title: p.fragment_title,
    historic: p.historic,
    pending_intent_id: p.pending_intent_id,
    pending_intent_version: p.pending_intent_version,
    pending_source_fragment_id: p.pending_source_fragment_id,
    pending_source_title: p.pending_source_title,
    origin: "manual",
    operation_id: null,
    approval_id: null,
    param_version: null,
    turn_id: null,
    seq: null,
    instance_id: "inst-1",
    position: p,
    position_version: p.position_version,
    position_identity: p.position_identity,
    command_id: null,
    ...payload,
  };
  events.route(ev("ANCHOR", flat));
}

function setup() {
  setActivePinia(createPinia());
  return { session: useSessionStore(), events: useEventStore() };
}

function armTopicFollow(events: ReturnType<typeof useEventStore>, opts: {
  approvalId?: string;
  operationId?: string;
  targetTopicId?: string;
  paramVersion?: number;
} = {}) {
  const approvalId = opts.approvalId ?? "ap-1";
  const operationId = opts.operationId ?? "op-1";
  const targetTopicId = opts.targetTopicId ?? "t2";
  events.route(
    ev("APPROVAL_REQUIRED", {
      approval: {
        approval_id: approvalId,
        kind: "topic_switch",
        turn_id: "turn_1",
        payload: {
          action: "switch",
          turn_id: "turn_1",
          from_topic_id: "t1",
          from_topic_name: "当前话题",
          from_fragment_id: null,
          reason: "这段内容更像目标话题",
          operation_id: operationId,
          param_version: opts.paramVersion ?? 1,
          base_nav_version: 1,
          target_topic_id: targetTopicId,
          target_topic_name: "目标话题",
        },
      },
      phase: "waiting",
    }),
  );
  return approvalId;
}

function approve(events: ReturnType<typeof useEventStore>, approvalId: string) {
  events.route(
    ev("APPROVAL_RESULT", {
      approval_id: approvalId,
      phase: "approved",
      decision: "approved",
      turn_id: "turn_1",
    }),
  );
}

beforeEach(() => {
  localStorage.clear();
  seq = 0;
  vi.clearAllMocks();
});

describe("R16 ④：快照先到、权威操作结果后到（§7.3）", () => {
  it("快照采用位置不清掉已武装跟随/等待草稿；结果到达后迁移一次、不自动发送", async () => {
    const { session, events } = setup();
    session.setAnchor("t1", null, "当前话题", null, false);
    events.route(ev("TURN_START", { turn_id: "turn_1" }));
    const approvalId = armTopicFollow(events);
    session.draft = "确认之后要发的话";
    session.flushDraft();
    approve(events, approvalId);

    // 重连快照先到：位置前进到 v12（同一 DB 身份），不得丢等待草稿、不得覆盖新编辑
    const snapshot = session.acceptPositionState(
      position({ topic_id: "t1", position_version: 12 }),
      "snapshot",
    );
    expect(snapshot.accepted).toBe(true);
    expect(session.draft).toBe("确认之后要发的话");
    expect(readDraft(draftStorageKey("chat", "t1"))?.text).toBe("确认之后要发的话");

    // 权威操作结果后到（ANCHOR origin=topic_operation，身份匹配）
    anchor(events, {
      origin: "topic_operation",
      operation_id: "op-1",
      approval_id: approvalId,
      param_version: 1,
      topic_id: "t2",
      topic_name: "目标话题",
      position: position({ topic_id: "t2", position_version: 13 }),
      instance_id: "inst-1",
      seq: 2,
    });
    expect(session.currentTopicId).toBe("t2");
    expect(session.draft).toBe("确认之后要发的话");
    expect(readDraft(draftStorageKey("chat", "t2"))?.text).toBe("确认之后要发的话");
    expect(session._draftFollowConsumed.length).toBeGreaterThan(0);
    expect(session._draftFollowConsumed.join(",")).toContain("op-1");
    expect(vi.mocked(api.sendTurn)).not.toHaveBeenCalled();

    // 重复结果：命中已消费集合 → 零副作用（不重复迁移、不自动发送）
    const consumedBefore = [...session._draftFollowConsumed];
    anchor(events, {
      origin: "topic_operation",
      operation_id: "op-1",
      approval_id: approvalId,
      param_version: 1,
      topic_id: "t2",
      topic_name: "目标话题",
      position: position({ topic_id: "t2", position_version: 13 }),
      instance_id: "inst-1",
      seq: 3,
    });
    expect(session._draftFollowConsumed).toEqual(consumedBefore);
    expect(session.draft).toBe("确认之后要发的话");
    expect(vi.mocked(api.sendTurn)).not.toHaveBeenCalled();
  });

  it("同版本同内容幂等接受时，匹配操作身份的草稿跟随仍可消费一次（§14.8）", () => {
    const { session, events } = setup();
    session.setAnchor("t1", null, "当前话题", null, false);
    events.route(ev("TURN_START", { turn_id: "turn_1" }));
    const approvalId = armTopicFollow(events);
    session.draft = "同版本幂等也要跟随一次";
    session.flushDraft();
    approve(events, approvalId);

    // 位置先通过快照到达结果版本 v12（t2）；随后同版本同内容 ANCHOR 到达
    const snapshot = session.acceptPositionState(
      position({ topic_id: "t2", position_version: 12 }),
      "snapshot",
    );
    expect(snapshot.accepted).toBe(true);
    const consumedBefore = session._draftFollowConsumed.length;
    anchor(events, {
      origin: "topic_operation",
      operation_id: "op-1",
      approval_id: approvalId,
      param_version: 1,
      topic_id: "t2",
      topic_name: "目标话题",
      position: position({ topic_id: "t2", position_version: 12 }),
      instance_id: "inst-1",
      seq: 4,
    });
    expect(session.currentTopicId).toBe("t2");
    expect(readDraft(draftStorageKey("chat", "t2"))?.text).toBe("同版本幂等也要跟随一次");
    expect(session._draftFollowConsumed.length).toBe(consumedBefore + 1);
    expect(vi.mocked(api.sendTurn)).not.toHaveBeenCalled();
  });
});

describe("R16 ⑤：冲突恢复 single-flight 与旧实例隔离", () => {
  it("连续同版本冲突只触发一次权威恢复（不并发、不风暴）", async () => {
    const { session } = setup();
    session.acceptPositionState(position({ position_version: 20 }), "snapshot");
    session.acceptPositionState(
      position({ topic_id: "t2", position_version: 20, pending_intent_id: "i2" }),
      "anchor",
    );
    session.acceptPositionState(
      position({ topic_id: "t3", position_version: 20, pending_intent_id: "i3" }),
      "anchor",
    );
    await Promise.resolve();
    expect(restoreSpy).toHaveBeenCalledTimes(1);
    expect(session._positionFacts?.topicId).toBe("t1");
  });

  it("旧实例/旧版本事件不得覆盖已接受位置，也不迁移草稿", () => {
    const { session, events } = setup();
    session.setAnchor("t1", null, "当前话题", null, false);
    events.route(ev("TURN_START", { turn_id: "turn_1" }));
    session.acceptPositionState(
      position({ topic_id: "t-current", position_version: 30 }),
      "snapshot",
    );
    session.draft = "当前草稿";
    session.flushDraft();
    anchor(events, {
      topic_id: "t-old",
      position: position({ topic_id: "t-old", position_version: 5 }),
      instance_id: "inst-old",
      seq: 1,
    });
    expect(session._positionFacts?.topicId).toBe("t-current");
    expect(session.currentTopicId).toBe("t-current");
    expect(session.draft).toBe("当前草稿");
    expect(readDraft(draftStorageKey("chat", "t-old"))).toBeNull();
  });
});
