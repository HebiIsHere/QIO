/**
 * R15-C · R2 验收：旧位置事件必须整条拒绝，位置事实与草稿跟随分离（契约 §4.4）。
 *
 * 已复现问题（baseline 44d92fb3）：events.ts 的 ANCHOR 分支先调用
 * applyAuthoritativeTopicAnchor（只决定草稿是否迁移），随后**无条件** setAnchor /
 * setPendingContinuation。位置顺序保护依附一次性草稿跟随对象：跟随不存在 / 已消费 /
 * 草稿为空时，旧事件仍然能改当前位置（手感：手动导航到 manual-new 后，旧操作成功
 * 事件把界面跳回旧目标）。
 *
 * 本文件只通过真实 Pinia store + 事件路由 + 本机草稿存储观察行为，覆盖契约 §4.4：
 * 1. 新手动导航后旧操作成功事件整条拒绝；
 * 2. 跟随已消费后重放旧事件；
 * 3. 没有草稿时重放旧事件（不只断言草稿）；
 * 4. 同话题不同片段的乱序事件（旧 v < 新 v）；
 * 5. 失效/拒绝操作的迟到事件（invalid 集合）；
 * 6. 重连 / 权威快照 / 服务重启（instance 变化、seq 归零）；
 * 7. 同版本合法细化 / 同版本冲突；
 * 8. 目标已有草稿保护 + 确认后跟随真实目标 + 不自动发送（既有语义不倒退）。
 * 另外覆盖权威快照接受（applyAuthoritativePositionSnapshot）与 /api/runtime/state
 * 应用路径（旧后端没有 anchor 字段时不伪造）。
 *
 * 每个场景同时断言：当前位置（currentTopicId / anchorFragmentId / anchorHistoric）、
 * 待接续提示（pendingContinuation）、草稿跟随/草稿正文（输入框 + 本机存储）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";
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

type Session = ReturnType<typeof useSessionStore>;
type EventStore = ReturnType<typeof useEventStore>;

let seq = 0;

function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: `r15c-${type}-${seq}`, ts: "", data } as AgentEvent;
}

function setup(): { session: Session; events: EventStore } {
  setActivePinia(createPinia());
  const session = useSessionStore();
  const events = useEventStore();
  session.adoptInstance("inst-1");
  session.setAnchor("t1", null, "当前话题", null, false);
  return { session, events };
}

function chatKey(topicId: string): string {
  return draftStorageKey("chat", topicId);
}

function storedText(topicId: string): string | null {
  return readDraft(chatKey(topicId))?.text ?? null;
}

/** 当前位置（用户可见的三元组）。 */
function positionOf(session: Session) {
  return {
    topicId: session.currentTopicId,
    fragmentId: session.anchorFragmentId,
    historic: session.anchorHistoric,
  };
}

function pendingOf(session: Session) {
  // R17-C（Lead 授权的最小冻结接口适配）：本地载荷新增 intentVersion（取消命令的
  // 目标身份）。旧用例按原来的「提示文案可见事实」比较；新字段由
  // r17-c-cancel-position.test.ts 专项断言。
  const pending = session.pendingContinuation;
  return pending ? { intentId: pending.intentId, sourceTitle: pending.sourceTitle } : null;
}

function factsOf(session: Session) {
  return session._positionFacts;
}

function followOf(session: Session) {
  return session._topicDraftFollow;
}

interface AnchorOptions {
  topicId: string;
  name?: string | null;
  fragmentId?: string | null;
  fragmentTitle?: string | null;
  historic?: boolean;
  navVersion?: number | null;
  seq?: number | null;
  /** 默认 inst-1；显式 null = 旧后端事件没有实例字段。 */
  instanceId?: string | null;
  origin?: "manual" | "topic_operation";
  operationId?: string | null;
  approvalId?: string | null;
  paramVersion?: number | null;
  turnId?: string | null;
  pendingIntentId?: string | null;
  pendingSourceTitle?: string | null;
}

/** 真实事件形状：ANCHOR 事件的 snake_case 原文。 */
function anchorData(opts: AnchorOptions): Record<string, unknown> {
  return {
    topic_id: opts.topicId,
    topic_name: opts.name ?? null,
    fragment_id: opts.fragmentId ?? null,
    fragment_title: opts.fragmentTitle ?? null,
    historic: opts.historic ?? false,
    origin: opts.origin ?? "manual",
    operation_id: opts.operationId ?? null,
    approval_id: opts.approvalId ?? null,
    param_version: opts.paramVersion ?? null,
    turn_id: opts.turnId ?? null,
    nav_version: opts.navVersion === undefined ? null : opts.navVersion,
    seq: opts.seq === undefined ? null : opts.seq,
    instance_id: opts.instanceId === undefined ? "inst-1" : opts.instanceId,
    pending_intent_id: opts.pendingIntentId ?? null,
    pending_source_title: opts.pendingSourceTitle ?? null,
  };
}

function routeAnchor(events: EventStore, opts: AnchorOptions) {
  events.route(ev("ANCHOR", anchorData(opts)));
}

function snapshot(
  session: Session,
  anchor: Record<string, unknown>,
  instanceId: string | null = "inst-1",
) {
  return session.applyAuthoritativePositionSnapshot(anchor, instanceId);
}

function startTurn(events: EventStore, turnId = "turn_1") {
  events.route(ev("TURN_START", { turn_id: turnId }));
}

function waitTopicApproval(
  events: EventStore,
  opts: { approvalId?: string; operationId?: string; turnId?: string; targetTopicId?: string } = {},
): string {
  const approvalId = opts.approvalId ?? "ap-1";
  const operationId = opts.operationId ?? "op-old";
  const turnId = opts.turnId ?? "turn_1";
  events.route(
    ev("APPROVAL_REQUIRED", {
      approval: {
        approval_id: approvalId,
        kind: "topic_switch",
        payload: {
          action: "switch",
          turn_id: turnId,
          from_topic_id: "t1",
          from_topic_name: "当前话题",
          from_fragment_id: null,
          reason: "目标话题更合适",
          operation_id: operationId,
          param_version: 1,
          base_nav_version: 1,
          target_topic_id: opts.targetTopicId ?? "old-target",
          target_topic_name: "旧目标",
        },
        turn_id: turnId,
      },
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

function waitingDraft(session: Session, text: string) {
  session.draft = text;
  session.flushDraft();
}

/** 武装一次性草稿跟随并在原话题写下等待期间的草稿（真实事件顺序）。 */
function armFollowWithWaitingDraft(
  session: Session,
  events: EventStore,
  opts: {
    approvalId?: string;
    operationId?: string;
    targetTopicId?: string;
    draftText?: string;
  } = {},
) {
  startTurn(events);
  const approvalId = waitTopicApproval(events, opts);
  waitingDraft(session, opts.draftText ?? "等待期间写的话");
  approvalResult(events, approvalId, "approved");
  expect(followOf(session), "确认后应武装一次性草稿跟随").not.toBeNull();
  return approvalId;
}

beforeEach(() => {
  localStorage.clear();
  seq = 0;
  vi.mocked(api.sendTurn).mockClear();
});

describe("R2 · 旧位置事件整条拒绝（位置事实与草稿跟随分离）", () => {
  it("1) 新手动导航（v9/seq20）后旧操作成功事件（v8/seq19）：位置/待接续/草稿都不动", () => {
    const { session, events } = setup();
    armFollowWithWaitingDraft(session, events);

    // 用户手动导航到 manual-new（更高 nav_version）；后端同时登记了一个待接续选择。
    routeAnchor(events, {
      topicId: "manual-new",
      name: "新话题",
      navVersion: 9,
      seq: 20,
      pendingIntentId: "intent-manual",
      pendingSourceTitle: "手动选的历史",
    });
    expect(positionOf(session)).toEqual({
      topicId: "manual-new",
      fragmentId: null,
      historic: false,
    });
    expect(pendingOf(session)).toEqual({ intentId: "intent-manual", sourceTitle: "手动选的历史" });
    expect(session.draft).toBe("");

    // 旧操作的成功结果迟到（v8/seq19，带旧 pending）：整条拒绝。
    routeAnchor(events, {
      topicId: "old-target",
      name: "旧目标",
      navVersion: 8,
      seq: 19,
      origin: "topic_operation",
      operationId: "op-old",
      approvalId: "ap-1",
      paramVersion: 1,
      turnId: "turn_1",
      pendingIntentId: "intent-old",
      pendingSourceTitle: "旧选择",
    });

    // 当前位置仍是 manual-new，旧事件没有把它跳回旧目标。
    expect(positionOf(session)).toEqual({
      topicId: "manual-new",
      fragmentId: null,
      historic: false,
    });
    // 待接续提示没有被旧值覆盖。
    expect(pendingOf(session)).toEqual({ intentId: "intent-manual", sourceTitle: "手动选的历史" });
    // 草稿没有迁移到旧目标（仍在原话题的本机草稿里）。
    expect(session.draft).toBe("");
    expect(storedText("t1")).toBe("等待期间写的话");
    expect(storedText("old-target")).toBeNull();
    // 旧事件也没有提前消费跟随。
    expect(followOf(session)).not.toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("2) 跟随标记已消费后重放旧事件：位置/待接续/草稿都不变", () => {
    const { session, events } = setup();
    const approvalId = armFollowWithWaitingDraft(session, events);

    // 本次操作的权威成功结果（v7/seq5）：位置到 old-target，草稿跟随一次。
    routeAnchor(events, {
      topicId: "old-target",
      name: "旧目标",
      navVersion: 7,
      seq: 5,
      origin: "topic_operation",
      operationId: "op-old",
      approvalId,
      paramVersion: 1,
      turnId: "turn_1",
    });
    expect(positionOf(session)).toEqual({
      topicId: "old-target",
      fragmentId: null,
      historic: false,
    });
    expect(session.draft).toBe("等待期间写的话");
    expect(storedText("old-target")).toBe("等待期间写的话");
    expect(followOf(session), "权威结果只处理一次，跟随已消费").toBeNull();

    // 用户在新话题继续写；随后又手动导航到 manual-new（v9）。
    session.draft = "切换后新写的字";
    session.flushDraft();
    routeAnchor(events, { topicId: "manual-new", name: "新话题", navVersion: 9, seq: 20 });
    expect(positionOf(session)).toEqual({
      topicId: "manual-new",
      fragmentId: null,
      historic: false,
    });
    expect(session.draft).toBe("");
    expect(storedText("old-target")).toBe("切换后新写的字");

    // 重连后重放旧事件（同 v7/seq5）：跟随已不存在，但位置同样不能被改回。
    routeAnchor(events, {
      topicId: "old-target",
      name: "旧目标",
      navVersion: 7,
      seq: 5,
      origin: "topic_operation",
      operationId: "op-old",
      approvalId,
      paramVersion: 1,
      turnId: "turn_1",
    });
    expect(positionOf(session)).toEqual({
      topicId: "manual-new",
      fragmentId: null,
      historic: false,
    });
    expect(pendingOf(session)).toBeNull();
    expect(session.draft).toBe("");
    expect(storedText("old-target")).toBe("切换后新写的字");
    expect(storedText("manual-new")).toBeNull();
  });

  it("3) 没有草稿时重放旧事件：位置同样不变（不只断言草稿）", () => {
    const { session, events } = setup();
    // 权威位置先到 v9/seq20（手动导航），并带一个待接续提示。
    routeAnchor(events, {
      topicId: "manual-new",
      name: "新话题",
      navVersion: 9,
      seq: 20,
      pendingIntentId: "intent-manual",
      pendingSourceTitle: "手动选的历史",
    });
    expect(positionOf(session)).toEqual({
      topicId: "manual-new",
      fragmentId: null,
      historic: false,
    });
    expect(factsOf(session)).toMatchObject({ topicId: "manual-new", navVersion: 9, seq: 20 });

    // 没有草稿、没有跟随、没有活动轮：旧操作成功事件（v8/seq19）仍然必须被拒绝。
    routeAnchor(events, {
      topicId: "old-target",
      name: "旧目标",
      navVersion: 8,
      seq: 19,
      origin: "topic_operation",
      operationId: "op-old",
      approvalId: "ap-1",
      paramVersion: 1,
      turnId: "turn_1",
      pendingIntentId: "intent-old",
    });

    expect(positionOf(session)).toEqual({
      topicId: "manual-new",
      fragmentId: null,
      historic: false,
    });
    expect(pendingOf(session)).toEqual({ intentId: "intent-manual", sourceTitle: "手动选的历史" });
    // 位置事实本身也没有倒退（不是只把 UI 藏起来）。
    expect(factsOf(session)).toMatchObject({
      topicId: "manual-new",
      navVersion: 9,
      seq: 20,
      source: "anchor",
    });
    expect(session.draft).toBe("");
    expect(storedText("t1")).toBeNull();
    expect(storedText("manual-new")).toBeNull();
    expect(storedText("old-target")).toBeNull();
    // 跟随从未武装：位置拒绝同样不依赖它。
    expect(followOf(session)).toBeNull();
  });

  it("4) 同话题不同片段的乱序事件：旧 v 拒绝、新 v 接受、同版本换片段拒绝", () => {
    const { session, events } = setup();
    snapshot(session, {
      topic_id: "tA",
      topic_name: "话题A",
      fragment_id: null,
      historic: false,
      nav_version: 5,
    });
    session.draft = "话题A里写的草稿";
    session.flushDraft();
    expect(positionOf(session)).toEqual({ topicId: "tA", fragmentId: null, historic: false });

    // 更新的 v6：同话题补充片段信息（历史位置）。
    routeAnchor(events, {
      topicId: "tA",
      name: "话题A",
      fragmentId: "f2",
      fragmentTitle: "片段二",
      historic: true,
      navVersion: 6,
      seq: 6,
    });
    expect(positionOf(session)).toEqual({ topicId: "tA", fragmentId: "f2", historic: true });
    expect(pendingOf(session)).toBeNull();
    expect(session.draft).toBe("话题A里写的草稿");

    // 乱序旧事件（v4）：拒绝，片段仍是 f2。
    routeAnchor(events, {
      topicId: "tA",
      name: "话题A",
      fragmentId: "f1",
      fragmentTitle: "片段一",
      historic: true,
      navVersion: 4,
      seq: 4,
      pendingIntentId: "intent-old",
    });
    expect(positionOf(session)).toEqual({ topicId: "tA", fragmentId: "f2", historic: true });
    expect(pendingOf(session)).toBeNull();

    // 同版本换成另一个非空片段 = 没有版本推进的位置变化：拒绝。
    routeAnchor(events, {
      topicId: "tA",
      name: "话题A",
      fragmentId: "f3",
      historic: true,
      navVersion: 6,
      seq: 7,
      pendingIntentId: "intent-next",
    });
    expect(positionOf(session)).toEqual({ topicId: "tA", fragmentId: "f2", historic: true });
    expect(pendingOf(session)).toBeNull();
    expect(session.draft).toBe("话题A里写的草稿");
    expect(storedText("tA")).toBe("话题A里写的草稿");
    expect(followOf(session)).toBeNull();
  });

  it("5) 失效操作的迟到成功事件：即使 nav_version 更高也整条拒绝", () => {
    const { session, events } = setup();
    armFollowWithWaitingDraft(session, events);
    // 操作失效（后端结构化失效，不是用户拒绝）：登记为 invalid 操作。
    approvalResult(events, "ap-1", "invalidated", {
      decision: "stale",
      code: "topic_application_stale",
      message: "这条话题申请已经失效",
    });
    expect(session.isTopicOperationInvalid("op-old")).toBe(true);
    expect(followOf(session)).toBeNull();

    // 之后的一次真实手动导航（v9）。
    routeAnchor(events, {
      topicId: "manual-new",
      name: "新话题",
      navVersion: 9,
      seq: 20,
      pendingIntentId: "intent-manual",
      pendingSourceTitle: "手动选的历史",
    });
    expect(positionOf(session)).toEqual({
      topicId: "manual-new",
      fragmentId: null,
      historic: false,
    });

    // 失效操作迟到的“成功”事件（v12 > 9）：位置/待接续/草稿都不动。
    routeAnchor(events, {
      topicId: "old-target",
      name: "旧目标",
      navVersion: 12,
      seq: 30,
      origin: "topic_operation",
      operationId: "op-old",
      approvalId: "ap-1",
      paramVersion: 1,
      turnId: "turn_1",
      pendingIntentId: "intent-old",
    });
    expect(positionOf(session)).toEqual({
      topicId: "manual-new",
      fragmentId: null,
      historic: false,
    });
    expect(pendingOf(session)).toEqual({ intentId: "intent-manual", sourceTitle: "手动选的历史" });
    expect(session.draft).toBe("");
    expect(storedText("t1")).toBe("等待期间写的话");
    expect(storedText("old-target")).toBeNull();
  });

  it("5b) recoverable_failure 不是失效/拒绝终态：不登记 invalid；迟到事件只按位置顺序拒绝", () => {
    const { session, events } = setup();
    armFollowWithWaitingDraft(session, events);
    // 阶段失败：草稿跟随按失败收口；但操作本身没有被拒绝 / 失效（恢复重试可能成功），
    // 不能把它整条拉黑，否则恢复成功后的权威 ANCHOR 会被误拒。
    events.route(
      ev("TOPIC_PHASE", {
        phase: "recoverable_failure",
        operation_id: "op-old",
        turn_id: "turn_1",
        recovery: { record_id: "rec-1", message: "上下文准备失败", retryable: true },
      }),
    );
    expect(session.isTopicOperationInvalid("op-old")).toBe(false);
    expect(followOf(session)).toBeNull();

    // 用户手动导航（v9）后，该操作迟到的“成功”事件（v8）按 nav_version 顺序拒绝；
    // 拒绝来自位置顺序，不来自 invalid 集合。
    routeAnchor(events, {
      topicId: "manual-new",
      name: "新话题",
      navVersion: 9,
      seq: 20,
      pendingIntentId: "intent-manual",
      pendingSourceTitle: "手动选的历史",
    });
    routeAnchor(events, {
      topicId: "old-target",
      name: "旧目标",
      navVersion: 8,
      seq: 19,
      origin: "topic_operation",
      operationId: "op-old",
      approvalId: "ap-1",
      paramVersion: 1,
      turnId: "turn_1",
      pendingIntentId: "intent-old",
    });

    expect(positionOf(session)).toEqual({
      topicId: "manual-new",
      fragmentId: null,
      historic: false,
    });
    expect(pendingOf(session)).toEqual({ intentId: "intent-manual", sourceTitle: "手动选的历史" });
    expect(session.draft).toBe("");
    expect(storedText("t1")).toBe("等待期间写的话");
    expect(storedText("old-target")).toBeNull();
  });

  it("5c) 旧轮（stale_turn）的话题操作事件：即使版本更新也拒绝", () => {
    const { session, events } = setup();
    startTurn(events, "turn_2");
    expect(session.activeTurnId).toBe("turn_2");

    routeAnchor(events, {
      topicId: "manual-new",
      name: "新话题",
      navVersion: 9,
      seq: 20,
      pendingIntentId: "intent-manual",
      pendingSourceTitle: "手动选的历史",
    });
    // turn_1 的迟到结果：属于另一个已知活动轮 → 拒绝。
    routeAnchor(events, {
      topicId: "old-target",
      name: "旧目标",
      navVersion: 10,
      seq: 21,
      origin: "topic_operation",
      operationId: "op-x",
      turnId: "turn_1",
      pendingIntentId: "intent-old",
    });

    expect(positionOf(session)).toEqual({
      topicId: "manual-new",
      fragmentId: null,
      historic: false,
    });
    expect(pendingOf(session)).toEqual({ intentId: "intent-manual", sourceTitle: "手动选的历史" });
    expect(session.draft).toBe("");
    expect(storedText("old-target")).toBeNull();
  });

  it("6) 重连 / 重启：旧实例事件拒绝、新实例事件接受、seq 不跨实例比较", () => {
    const { session, events } = setup();
    // 旧后端事件读不到 nav_version：以快照 + instance + seq 为序。
    snapshot(
      session,
      {
        topic_id: "t1",
        topic_name: "当前话题",
        fragment_id: null,
        historic: false,
        nav_version: null,
      },
      "inst-A",
    );
    expect(session.instanceId).toBe("inst-A");
    expect(factsOf(session)).toMatchObject({ instanceId: "inst-A", navVersion: null, seq: null });

    // 同实例：seq 5 接受（首条写出基线），seq 4 拒绝。
    routeAnchor(events, { topicId: "t1", navVersion: null, seq: 5, instanceId: "inst-A" });
    expect(factsOf(session)).toMatchObject({ instanceId: "inst-A", seq: 5 });
    routeAnchor(events, {
      topicId: "t1",
      navVersion: null,
      seq: 4,
      instanceId: "inst-A",
      pendingIntentId: "intent-old",
    });
    expect(factsOf(session)).toMatchObject({ seq: 5 });
    expect(pendingOf(session)).toBeNull();

    // 服务重启：新实例 inst-B 的权威快照（旧实例的本地 seq 不复用）。
    snapshot(
      session,
      {
        topic_id: "t2",
        topic_name: "重启后话题",
        fragment_id: null,
        historic: false,
        nav_version: null,
      },
      "inst-B",
    );
    expect(session.instanceId).toBe("inst-B");
    expect(factsOf(session)).toMatchObject({ topicId: "t2", instanceId: "inst-B", seq: null });

    // 旧实例的迟到事件（seq 99，比新实例任何序号都大）：拒绝 stale_instance。
    routeAnchor(events, {
      topicId: "t1",
      navVersion: null,
      seq: 99,
      instanceId: "inst-A",
      pendingIntentId: "intent-old",
    });
    expect(positionOf(session)).toEqual({
      topicId: "t2",
      fragmentId: null,
      historic: false,
    });
    expect(pendingOf(session)).toBeNull();
    expect(factsOf(session)).toMatchObject({ topicId: "t2", instanceId: "inst-B" });

    // 新实例的 seq 从 1 开始：不与旧实例的 seq 比较，正常接受。
    routeAnchor(events, {
      topicId: "t2",
      name: "重启后话题",
      navVersion: null,
      seq: 1,
      instanceId: "inst-B",
    });
    expect(factsOf(session)).toMatchObject({ instanceId: "inst-B", seq: 1 });
    routeAnchor(events, {
      topicId: "t2",
      name: "重启后话题",
      navVersion: null,
      seq: 3,
      instanceId: "inst-B",
    });
    expect(factsOf(session)).toMatchObject({ instanceId: "inst-B", seq: 3 });
    expect(positionOf(session)).toEqual({
      topicId: "t2",
      fragmentId: null,
      historic: false,
    });
    expect(session.draft).toBe("");
    expect(followOf(session)).toBeNull();
  });

  it("7) 同版本合法补充片段信息接受；同版本 topic 冲突 / 抹掉片段 / 换片段拒绝", () => {
    const { session, events } = setup();
    snapshot(session, {
      topic_id: "tA",
      topic_name: "话题A",
      fragment_id: null,
      historic: false,
      nav_version: 5,
    });
    session.draft = "话题A里写的草稿";
    session.flushDraft();

    // 同版本 + 同话题：fragment 从 null 补充为 fX = 合法细化。
    routeAnchor(events, {
      topicId: "tA",
      name: "话题A",
      fragmentId: "fX",
      fragmentTitle: "片段X",
      historic: true,
      navVersion: 5,
      pendingIntentId: "intent-x",
      pendingSourceTitle: "片段X",
    });
    expect(positionOf(session)).toEqual({ topicId: "tA", fragmentId: "fX", historic: true });
    expect(pendingOf(session)).toEqual({ intentId: "intent-x", sourceTitle: "片段X" });
    expect(session.draft).toBe("话题A里写的草稿");

    // 同版本 topic 冲突：拒绝（三者都不动）。
    routeAnchor(events, {
      topicId: "tB",
      name: "话题B",
      fragmentId: "fY",
      historic: false,
      navVersion: 5,
      pendingIntentId: "intent-y",
    });
    expect(positionOf(session)).toEqual({ topicId: "tA", fragmentId: "fX", historic: true });
    expect(pendingOf(session)).toEqual({ intentId: "intent-x", sourceTitle: "片段X" });
    expect(session.draft).toBe("话题A里写的草稿");

    // 同版本缺字段事件不得抹掉已知真实片段（ambiguous_same_version）。
    routeAnchor(events, {
      topicId: "tA",
      name: "话题A",
      fragmentId: null,
      historic: false,
      navVersion: 5,
      pendingIntentId: "intent-z",
    });
    expect(positionOf(session)).toEqual({ topicId: "tA", fragmentId: "fX", historic: true });
    expect(pendingOf(session)).toEqual({ intentId: "intent-x", sourceTitle: "片段X" });

    // 同版本换成另一个非空片段：拒绝（fragment_conflict_same_version）。
    routeAnchor(events, {
      topicId: "tA",
      name: "话题A",
      fragmentId: "fZ",
      historic: false,
      navVersion: 5,
    });
    expect(positionOf(session)).toEqual({ topicId: "tA", fragmentId: "fX", historic: true });
    expect(pendingOf(session)).toEqual({ intentId: "intent-x", sourceTitle: "片段X" });
    expect(session.draft).toBe("话题A里写的草稿");
    expect(storedText("tA")).toBe("话题A里写的草稿");
    expect(followOf(session)).toBeNull();
  });

  it("8) 目标已有草稿保护 + 确认后跟随真实目标 + 不自动发送（既有语义不倒退）", () => {
    const { session, events } = setup();
    expect(writeDraft(chatKey("t2"), "目标话题原有的草稿", 1).ok).toBe(true);

    const approvalId = armFollowWithWaitingDraft(session, events, { targetTopicId: "t2" });

    // 本次操作的权威成功结果：位置到真实目标 t2，草稿跟过去一次。
    routeAnchor(events, {
      topicId: "t2",
      name: "目标话题",
      navVersion: 7,
      seq: 5,
      origin: "topic_operation",
      operationId: "op-old",
      approvalId,
      paramVersion: 1,
      turnId: "turn_1",
    });

    expect(positionOf(session)).toEqual({
      topicId: "t2",
      fragmentId: null,
      historic: false,
    });
    expect(pendingOf(session)).toBeNull();
    // 不静默覆盖目标已有草稿：登记为被让位草稿，当前编辑的仍是等待期间那份。
    expect(session.draft).toBe("等待期间写的话");
    expect(storedText("t2")).toBe("等待期间写的话");
    expect(storedText("t1")).toBeNull();
    const displaced = session.displacedDraftsForTopic("t2");
    expect(displaced.some((record) => record.text === "目标话题原有的草稿")).toBe(true);
    expect(displaced.every((record) => record.text !== "等待期间写的话")).toBe(true);
    // 不自动发送：草稿只跟随，发送仍由用户主动触发。
    expect(api.sendTurn).not.toHaveBeenCalled();

    // 目标原有草稿仍可取回（两份都还在）。
    session.draft = "";
    session.flushDraft();
    const restored = session.restoreDisplacedDraft(displaced[0]?.id);
    expect(restored.ok).toBe(true);
    expect(session.draft).toBe("目标话题原有的草稿");
  });
});

describe("R2 · 权威位置快照与运行时状态应用", () => {
  it("快照不倒退：本地已有更新的实时位置时保留，pending 仍以服务器为准", () => {
    const { session, events } = setup();
    snapshot(session, {
      topic_id: "tA",
      topic_name: "话题A",
      fragment_id: null,
      historic: false,
      nav_version: 5,
    });
    routeAnchor(events, {
      topicId: "tA",
      name: "话题A",
      fragmentId: "f2",
      historic: false,
      navVersion: 6,
      seq: 6,
    });
    expect(positionOf(session)).toEqual({ topicId: "tA", fragmentId: "f2", historic: false });

    // 迟到的旧快照（v5 的旧位置）：位置不倒退，但服务器登记的 pending 采用。
    const applied = snapshot(session, {
      topic_id: "tOld",
      topic_name: "旧话题",
      fragment_id: null,
      historic: false,
      nav_version: 5,
      pending_intent_id: "intent-snap",
      pending_source_title: "服务器登记的接续",
    });
    expect(applied.applied).toBe(false);
    expect(applied.keptNewerLocal).toBe(true);
    expect(positionOf(session)).toEqual({ topicId: "tA", fragmentId: "f2", historic: false });
    expect(pendingOf(session)).toEqual({ intentId: "intent-snap", sourceTitle: "服务器登记的接续" });
    expect(factsOf(session)).toMatchObject({ topicId: "tA", navVersion: 6, source: "anchor" });
  });

  it("applyRuntimeState 应用权威 anchor 快照；旧后端没有 anchor 字段时保持现状", () => {
    const { session, events } = setup();
    // 旧后端：快照没有 anchor 字段 → 保持现状，不伪造位置。
    events.applyRuntimeState({ approvals: [], tasks: [] });
    expect(positionOf(session)).toEqual({
      topicId: "t1",
      fragmentId: null,
      historic: false,
    });
    expect(factsOf(session)).toBeNull();

    // 新后端：/api/runtime/state 带 anchor（与 ANCHOR 事件同源）。
    events.applyRuntimeState({
      instance_id: "inst-1",
      approvals: [],
      tasks: [],
      anchor: {
        topic_id: "t-restored",
        topic_name: "恢复话题",
        fragment_id: null,
        historic: false,
        nav_version: 12,
        pending_intent_id: "intent-r",
        pending_source_title: "恢复的接续",
      },
    });
    expect(positionOf(session)).toEqual({
      topicId: "t-restored",
      fragmentId: null,
      historic: false,
    });
    expect(pendingOf(session)).toEqual({ intentId: "intent-r", sourceTitle: "恢复的接续" });
    expect(factsOf(session)).toMatchObject({
      topicId: "t-restored",
      navVersion: 12,
      source: "snapshot",
    });

    // 快照之后到达的旧位置事件（v11）被拒绝：位置仍以权威快照为准。
    routeAnchor(events, {
      topicId: "t-old",
      name: "旧话题",
      navVersion: 11,
      seq: 3,
      pendingIntentId: "intent-old",
    });
    expect(positionOf(session)).toEqual({
      topicId: "t-restored",
      fragmentId: null,
      historic: false,
    });
    expect(pendingOf(session)).toEqual({ intentId: "intent-r", sourceTitle: "恢复的接续" });
  });

  it("接受入口（E/F 冻结 reason / migrated）：主序、兜底、轮次、失效操作与草稿迁移", () => {
    // nav_version 主序：baseline / stale_nav_version / new_nav_version。
    {
      const { session } = setup();
      expect(
        session.acceptTopicPositionEvent({ topicId: "tA", navVersion: 5, instanceId: "inst-1", seq: 1 }),
      ).toMatchObject({ accepted: true, reason: "baseline", migrated: false });
      expect(
        session.acceptTopicPositionEvent({ topicId: "tA", navVersion: 4, instanceId: "inst-1", seq: 2 }),
      ).toMatchObject({ accepted: false, reason: "stale_nav_version" });
      expect(
        session.acceptTopicPositionEvent({ topicId: "tA", navVersion: 6, instanceId: "inst-1", seq: 3 }),
      ).toMatchObject({ accepted: true, reason: "new_nav_version" });
    }
    // 读不到版本：实例 + seq 兜底；旧实例事件拒绝、新实例 seq 重新起算。
    {
      const { session } = setup();
      session.adoptInstance("inst-A");
      expect(
        session.acceptTopicPositionEvent({ topicId: "tA", instanceId: "inst-A", seq: 5 }),
      ).toMatchObject({ accepted: true, reason: "baseline" });
      expect(
        session.acceptTopicPositionEvent({ topicId: "tA", instanceId: "inst-A", seq: 4 }),
      ).toMatchObject({ accepted: false, reason: "stale_seq" });
      session.applyAuthoritativePositionSnapshot({ topic_id: "tB", nav_version: null }, "inst-B");
      expect(factsOf(session)).toMatchObject({ instanceId: "inst-B", seq: null });
      expect(
        session.acceptTopicPositionEvent({ topicId: "tA", instanceId: "inst-A", seq: 99 }),
      ).toMatchObject({ accepted: false, reason: "stale_instance" });
      expect(
        session.acceptTopicPositionEvent({ topicId: "tB", instanceId: "inst-B", seq: 1 }),
      ).toMatchObject({ accepted: true, reason: "no_order_facts" });
    }
    // 旧轮迟到结果：stale_turn。
    {
      const { session, events } = setup();
      startTurn(events, "turn_2");
      expect(
        session.acceptTopicPositionEvent({
          topicId: "tA",
          origin: "topic_operation",
          operationId: "op-x",
          turnId: "turn_1",
          navVersion: 9,
        }),
      ).toMatchObject({ accepted: false, reason: "stale_turn" });
    }
    // invalid 集合里的操作：invalidated_operation（reason 可断言）。
    {
      const { session } = setup();
      session.rememberInvalidTopicOperation("op-invalid");
      expect(session.isTopicOperationInvalid("op-invalid")).toBe(true);
      expect(
        session.acceptTopicPositionEvent({
          topicId: "tA",
          origin: "topic_operation",
          operationId: "op-invalid",
          navVersion: 9,
        }),
      ).toMatchObject({ accepted: false, reason: "invalidated_operation" });
    }
    // migrated：本次操作的权威成功结果接受后确实迁移一次草稿。
    {
      const { session, events } = setup();
      const approvalId = armFollowWithWaitingDraft(session, events);
      const result = session.acceptTopicPositionEvent({
        topicId: "old-target",
        origin: "topic_operation",
        operationId: "op-old",
        approvalId,
        paramVersion: 1,
        turnId: "turn_1",
        navVersion: 7,
        seq: 5,
      });
      expect(result).toMatchObject({ accepted: true, migrated: true });
      expect(session.draft).toBe("等待期间写的话");
      expect(storedText("old-target")).toBe("等待期间写的话");
      expect(followOf(session)).toBeNull();
    }
  });
});
