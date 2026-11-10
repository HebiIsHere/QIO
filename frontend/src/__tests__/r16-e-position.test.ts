/**
 * R16 · E：前端完整位置状态整份接受的真实接线（契约 §2 I02/I06/I07、§7.2、§14.3）。
 *
 * 真实 Pinia session store + 真实 events.route（ANCHOR 事件入口）；网络/模块仅在
 * 权威恢复入口处 mock（restoreRuntimeState），不联网、不写真实应用数据。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../stores/session";
import { useEventStore } from "../stores/events";
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
    getRuntimeState: vi.fn(async () => ({
      instance_id: "inst-1",
      anchor: null,
    })),
  },
}));

let seq = 0;

function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: "r16e-" + type + "-" + seq, ts: "", data } as AgentEvent;
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
    position_version: 5,
    position_identity: "db-test-identity",
    ...overrides,
  };
}

function anchorEvent(payload: Record<string, unknown>): AgentEvent {
  const p = position((payload.position as Record<string, unknown> | undefined) ?? undefined);
  return ev("ANCHOR", {
    topic_id: p.topic_id,
    topic_name: p.topic_name,
    fragment_id: p.fragment_id,
    fragment_title: p.fragment_title,
    historic: p.historic,
    pending_intent_id: p.pending_intent_id,
    pending_intent_version: p.pending_intent_version,
    pending_source_fragment_id: p.pending_source_fragment_id,
    pending_source_title: p.pending_source_title,
    position: p,
    position_version: payload.position_version ?? p.position_version,
    position_identity: payload.position_identity ?? p.position_identity,
    command_id: payload.command_id ?? "request:anchor-1",
    instance_id: payload.instance_id ?? "inst-1",
    seq: payload.seq ?? null,
    origin: payload.origin ?? "manual",
    operation_id: payload.operation_id ?? null,
    approval_id: payload.approval_id ?? null,
    param_version: payload.param_version ?? null,
  });
}

function setup() {
  setActivePinia(createPinia());
  return { session: useSessionStore(), events: useEventStore() };
}

type Store = ReturnType<typeof useSessionStore>;
type EventStore = ReturnType<typeof useEventStore>;

function viaEvent(events: EventStore, payload: Record<string, unknown>) {
  const flat: Record<string, unknown> = { ...payload };
  if (payload.position) {
    Object.assign(flat, payload.position as Record<string, unknown>);
    flat.position = payload.position;
  }
  events.route(anchorEvent(flat));
}

beforeEach(() => {
  localStorage.clear();
  seq = 0;
  vi.clearAllMocks();
});

describe("R16 ①：acceptPositionState 整份接受（I06）", () => {
  it("无本地事实 → baseline 采用；ANCHOR 事件走同一入口", () => {
    const { session, events } = setup();
    viaEvent(events, { position: position({ position_version: 5 }) });
    expect(session._positionFacts?.positionVersion).toBe(5);
    expect(session._positionFacts?.positionIdentity).toBe("db-test-identity");
    expect(session.currentTopicId).toBe("t1");
    expect(session._positionStateConflict).toBeNull();
  });

  it("更高 position_version 整份采用（位置 + pending 同源，不拼接）", () => {
    const { session } = setup();
    const base = session.acceptPositionState(position(), "snapshot");
    expect(base.accepted).toBe(true);
    expect(base.adopted).toBe(true);

    const newer = position({
      topic_id: "t2",
      fragment_id: "f2",
      historic: true,
      pending_intent_id: "intent-9",
      pending_intent_version: 2,
      pending_source_fragment_id: "f-src",
      nav_version: 4,
      position_version: 6,
    });
    const result = session.acceptPositionState(newer, "command");
    expect(result.accepted).toBe(true);
    expect(result.reason).toBe("new_position_state");
    expect(session._positionFacts?.topicId).toBe("t2");
    expect(session._positionFacts?.fragmentId).toBe("f2");
    expect(session._positionFacts?.pendingIntentId).toBe("intent-9");
    expect(session.pendingContinuation?.intentId).toBe("intent-9");
    expect(session.currentTopicId).toBe("t2");
    expect(session.anchorFragmentId).toBe("f2");
  });

  it("较旧版本整份拒绝：不写 pending、不迁移草稿、不触发恢复", async () => {
    const { session } = setup();
    session.acceptPositionState(
      position({ position_version: 7, pending_intent_id: null }),
      "snapshot",
    );
    session.draft = "等待期间的草稿";
    const stale = session.acceptPositionState(
      position({
        topic_id: "t-old",
        fragment_id: "f-old",
        historic: true,
        pending_intent_id: "intent-old",
        position_version: 3,
      }),
      "anchor",
    );
    expect(stale.accepted).toBe(false);
    expect(stale.reason).toBe("stale_position_state");
    expect(stale.adopted).toBe(false);
    expect(session._positionFacts?.topicId).toBe("t1");
    expect(session._positionFacts?.fragmentId).toBeNull();
    expect(session.pendingContinuation).toBeNull();
    expect(session.draft).toBe("等待期间的草稿");
    await Promise.resolve();
    expect(restoreSpy).not.toHaveBeenCalled();
  });

  it("同版本相同内容幂等、零变化；排除派生展示字段", () => {
    const { session } = setup();
    session.acceptPositionState(position(), "snapshot");
    const beforeTopic = session.currentTopicId;
    const same = session.acceptPositionState(
      position({ topic_name: "改名后的展示标题", fragment_title: "派生标题" }),
      "anchor",
    );
    expect(same.accepted).toBe(true);
    expect(same.idempotent).toBe(true);
    expect(same.adopted).toBe(false);
    expect(session.currentTopicId).toBe(beforeTopic);
    expect(session._positionStateConflict).toBeNull();
    expect(restoreSpy).not.toHaveBeenCalled();
  });

  it("同版本不同内容：记录可观察冲突、不猜测不部分合并、触发一次权威恢复", async () => {
    const { session } = setup();
    session.acceptPositionState(
      position({
        topic_id: "t1",
        pending_intent_id: "intent-a",
        pending_intent_version: 1,
        pending_source_fragment_id: "f-a",
      }),
      "snapshot",
    );
    const conflict = session.acceptPositionState(
      position({
        topic_id: "t2",
        pending_intent_id: "intent-b",
        pending_intent_version: 1,
        pending_source_fragment_id: "f-b",
      }),
      "anchor",
    );
    expect(conflict.accepted).toBe(false);
    expect(conflict.conflicted).toBe(true);
    expect(conflict.reason).toBe("same_version_conflict");
    expect(session._positionStateConflict).not.toBeNull();
    expect(session._positionFacts?.topicId).toBe("t1");
    expect(session._positionFacts?.pendingIntentId).toBe("intent-a");
    expect(session.currentTopicId).toBe("t1");
    expect(session.pendingContinuation?.intentId).toBe("intent-a");
    await Promise.resolve();
    expect(restoreSpy).toHaveBeenCalledTimes(1);
  });
});

describe("R16 ②：position_identity 与旧实例事件（I07）", () => {
  it("权威快照换身份 → 重建基线整份采用", () => {
    const { session } = setup();
    session.acceptPositionState(position(), "snapshot");
    const rebuilt = session.acceptPositionState(
      position({
        topic_id: "t-new-db",
        position_identity: "db-other",
        position_version: 1,
        nav_version: 1,
      }),
      "snapshot",
    );
    expect(rebuilt.accepted).toBe(true);
    expect(rebuilt.reason).toBe("new_position_identity");
    expect(session._positionFacts?.positionIdentity).toBe("db-other");
    expect(session.currentTopicId).toBe("t-new-db");
  });

  it("实时事件携带旧身份：拒绝 + 触发恢复，不让旧身份事件改状态", async () => {
    const { session } = setup();
    session.acceptPositionState(position({ position_version: 9 }), "snapshot");
    const stale = session.acceptPositionState(
      position({
        topic_id: "t-old-identity",
        position_identity: "db-old",
        position_version: 99,
      }),
      "anchor",
    );
    expect(stale.accepted).toBe(false);
    expect(stale.reason).toBe("stale_position_identity");
    expect(session._positionFacts?.topicId).toBe("t1");
    expect(session.currentTopicId).toBe("t1");
    await Promise.resolve();
    expect(restoreSpy).toHaveBeenCalledTimes(1);
  });

  it("旧实例 ANCHOR（instance_id/seq 更旧）不得仅因版本字段绕过接受规则", () => {
    const { session, events } = setup();
    viaEvent(events, {
      position: position({ topic_id: "t-new", position_version: 10 }),
      instance_id: "inst-2",
      seq: 30,
    });
    expect(session._positionFacts?.positionVersion).toBe(10);

    viaEvent(events, {
      position: position({
        topic_id: "t-stale-instance",
        position_version: 8,
        position_identity: "db-test-identity",
      }),
      instance_id: "inst-1",
      seq: 5,
    });
    expect(session._positionFacts?.positionVersion).toBe(10);
    expect(session.currentTopicId).toBe("t-new");
  });
});

describe("R16 ③：所有导航响应走同一入口（I02）", () => {
  it("命令响应里的 position 与事件/快照使用同一接受规则（旧响应被拒）", () => {
    const { session } = setup();
    session.acceptPositionState(position({ position_version: 6 }), "snapshot");
    const fromStaleCommand = session.acceptPositionState(
      position({ topic_id: "t-old", position_version: 4 }),
      "command",
    );
    expect(fromStaleCommand.accepted).toBe(false);
    expect(session._positionFacts?.positionVersion).toBe(6);
    const fromNewCommand = session.acceptPositionState(
      position({ topic_id: "t-command", position_version: 7 }),
      "command",
    );
    expect(fromNewCommand.accepted).toBe(true);
    expect(session.currentTopicId).toBe("t-command");
  });
});
