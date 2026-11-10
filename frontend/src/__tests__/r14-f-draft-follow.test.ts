/**
 * R14-F 独立回归（F12）：草稿跟随只消费本次话题操作的权威成功结果。
 *
 * 本文件是**独立验证者**的回归，不复用实现者测试；只通过真实 store / 事件路由
 * / 本机草稿存储观察用户可见行为：
 * - create 目标未知时不得通配「下一次任意导航」（旧实现的缺陷）；
 * - 手动导航 / operation 不匹配 / 无 operation_id 的 ANCHOR 不迁移、不提前消费；
 * - 审批确认身份（approval_id）：用户弹窗改名后 param_version 从 v1 推进到 v2，
 *   但 approval_id 不变 —— 同一次确认仍迁移一次；双方 approval_id 不同则不迁移；
 * - 旧的 / 重放的 ANCHOR（seq 更小）不覆盖用户之后写的新草稿；
 * - 拒绝 / 失效收口后，迟到的权威成功结果不得再带走草稿；
 * - 目标话题已有草稿：不静默覆盖（登记为被让位草稿），且永不自动发送。
 *
 * 对应契约：docs/topic-review-20261110-contract.md §3.6 + Lead 2026-11-10 F12 裁决。
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
  return { type, id: `r14f-${type}-${seq}`, ts: "", data } as AgentEvent;
}

type Store = ReturnType<typeof useSessionStore>;
type EventStore = ReturnType<typeof useEventStore>;

function setup() {
  setActivePinia(createPinia());
  return { session: useSessionStore(), events: useEventStore() };
}

function chatKey(topicId: string): string {
  return draftStorageKey("chat", topicId);
}

function storedText(topicId: string): string | null {
  return readDraft(chatKey(topicId))?.text ?? null;
}

function startTurn(session: Store, events: EventStore) {
  session.setAnchor("t1", null, "当前话题", null, false);
  events.route(ev("TURN_START", { turn_id: "turn_1" }));
}

interface RequiredOptions {
  approvalId?: string;
  kind?: "topic_switch" | "topic_create";
  operationId?: string;
  paramVersion?: number;
  targetTopicId?: string;
  newName?: string;
}

function topicRequired(events: EventStore, opts: RequiredOptions = {}): string {
  const approvalId = opts.approvalId ?? "ap-topic-1";
  const kind = opts.kind ?? "topic_switch";
  const payload: Record<string, unknown> = {
    action: kind === "topic_create" ? "create" : "switch",
    turn_id: "turn_1",
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
      approval: { approval_id: approvalId, kind, payload, turn_id: "turn_1" },
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
      decision: phase === "approved" ? "approved" : "rejected",
      turn_id: "turn_1",
      ...extra,
    }),
  );
}

interface AnchorOptions {
  origin: "topic_operation" | "manual";
  topicId: string;
  name?: string;
  operationId?: string | null;
  approvalId?: string | null;
  paramVersion?: number | null;
  seq?: number;
  /** R2：真实导航会推进 nav_version；同版本换话题按契约拒绝，测试需要时显式给更高版本。 */
  navVersion?: number;
}

function anchor(events: EventStore, opts: AnchorOptions) {
  events.route(
    ev("ANCHOR", {
      topic_id: opts.topicId,
      topic_name: opts.name ?? "话题",
      fragment_id: null,
      fragment_title: null,
      historic: false,
      origin: opts.origin,
      operation_id: opts.operationId ?? null,
      approval_id: opts.approvalId ?? null,
      param_version: opts.paramVersion ?? null,
      turn_id: opts.origin === "topic_operation" ? "turn_1" : null,
      nav_version: opts.navVersion ?? 7,
      seq: opts.seq ?? 5,
    }),
  );
}

/** 权威成功结果：工具真实执行成功后的 ANCHOR（Lead 冻结事件契约）。 */
function authoritative(
  events: EventStore,
  opts: Omit<AnchorOptions, "origin">,
) {
  anchor(events, {
    ...opts,
    origin: "topic_operation",
    operationId: opts.operationId === undefined ? "op-1" : opts.operationId,
    paramVersion: opts.paramVersion === undefined ? 1 : opts.paramVersion,
  });
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
});

describe("F12 · 无关手动导航不得参与草稿跟随（旧实现 targetTopicId=null 通配）", () => {
  it("create 目标未知时，手动 ANCHOR 不搬等待草稿、不消费跟随", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, {
      kind: "topic_create",
      operationId: "op-create-1",
      newName: "新话题",
    });
    waitingDraft(session, "新建之后还要发的话");
    approvalResult(events, id, "approved");
    expect(followOf(session)).not.toBeNull();

    // 用户自己点了另一个真实话题（与本次操作无关）：绝对不是本次操作的成功结果。
    anchor(events, { origin: "manual", topicId: "t-unrelated", name: "无关话题" });

    expect(session.currentTopicId).toBe("t-unrelated");
    expect(session.draft, "无关手动导航不得把等待草稿带过去").toBe("");
    expect(storedText("t1")).toBe("新建之后还要发的话");
    expect(storedText("t-unrelated")).toBeNull();
    expect(followOf(session)).not.toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("operation 不匹配 / 缺失 operation_id 的 ANCHOR 不迁移、不提前消费", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, {
      kind: "topic_switch",
      operationId: "op-match",
      targetTopicId: "t2",
    });
    waitingDraft(session, "只跟 op-match");
    approvalResult(events, id, "approved");

    // 别的操作的迟到结果（锚点到同一话题，界面位置不变）：没有本次操作的权威身份，
    // 不得迁移草稿，也不得提前消费跟随。
    anchor(events, {
      origin: "topic_operation",
      topicId: "t1",
      operationId: "op-other",
      approvalId: id,
      paramVersion: 1,
      seq: 3,
    });
    expect(session.draft).toBe("只跟 op-match");
    expect(storedText("t1")).toBe("只跟 op-match");
    expect(storedText("t2")).toBeNull();
    expect(followOf(session)).not.toBeNull();

    // 本次操作的权威成功结果到达：迁移一次（真实导航推进 nav_version → v8）。
    authoritative(events, {
      topicId: "t2",
      operationId: "op-match",
      approvalId: id,
      seq: 5,
      navVersion: 8,
    });
    expect(session.currentTopicId).toBe("t2");
    expect(session.draft).toBe("只跟 op-match");
    expect(storedText("t2")).toBe("只跟 op-match");
    expect(storedText("t1")).toBeNull();
    expect(followOf(session)).toBeNull();
  });
});

describe("F12 · 审批确认身份：改名后版本推进仍是同一次确认", () => {
  it("用户弹窗改名后批准（approval_id 不变、param_version v1→v2）：草稿仍跟随真实目标", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, {
      kind: "topic_switch",
      operationId: "op-edited",
      paramVersion: 1,
      targetTopicId: "t2",
    });
    waitingDraft(session, "确认之后要发的话");
    // 用户弹窗编辑并确认：执行后的参数版本推进到 v2，approval_id 不变。
    approvalResult(events, id, "approved", { param_version: 2 });

    authoritative(events, {
      topicId: "t2",
      operationId: "op-edited",
      approvalId: id,
      paramVersion: 2,
      seq: 5,
    });

    expect(session.currentTopicId).toBe("t2");
    expect(session.draft).toBe("确认之后要发的话");
    expect(storedText("t2")).toBe("确认之后要发的话");
    expect(storedText("t1")).toBeNull();
    expect(followOf(session)).toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("双方 approval_id 都有但不同：不是这次确认 → 不迁移、不消费；随后正确确认仍迁移", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, {
      kind: "topic_switch",
      operationId: "op-approval",
      targetTopicId: "t2",
    });
    waitingDraft(session, "只跟这次确认");
    approvalResult(events, id, "approved");

    // 同一 operation 的另一次确认（approval_id 不同）：锚点同话题 → 不改位置、不搬草稿。
    anchor(events, {
      origin: "topic_operation",
      topicId: "t1",
      operationId: "op-approval",
      approvalId: "ap-other",
      paramVersion: 1,
      seq: 4,
    });
    expect(session.draft).toBe("只跟这次确认");
    expect(storedText("t1")).toBe("只跟这次确认");
    expect(followOf(session)).not.toBeNull();

    // 本次确认的权威成功结果：迁移一次（真实导航推进 nav_version → v8）。
    authoritative(events, {
      topicId: "t2",
      operationId: "op-approval",
      approvalId: id,
      seq: 5,
      navVersion: 8,
    });
    expect(session.draft).toBe("只跟这次确认");
    expect(storedText("t2")).toBe("只跟这次确认");
    expect(storedText("t1")).toBeNull();
  });

  it("旧后端无 approval_id 的权威结果：退回 operation_id + param_version 匹配", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, {
      kind: "topic_switch",
      operationId: "op-legacy",
      paramVersion: 1,
      targetTopicId: "t2",
    });
    waitingDraft(session, "旧后端也要跟随");
    approvalResult(events, id, "approved");

    // 事件没有 approval_id（旧后端）：operation 相同且参数版本相同 → 仍迁移一次。
    authoritative(events, {
      topicId: "t2",
      operationId: "op-legacy",
      approvalId: null,
      paramVersion: 1,
      seq: 5,
    });
    expect(session.currentTopicId).toBe("t2");
    expect(session.draft).toBe("旧后端也要跟随");
    expect(storedText("t2")).toBe("旧后端也要跟随");
    expect(storedText("t1")).toBeNull();
  });
});

describe("F12 · 权威成功结果只消费一次", () => {
  it("旧/重放 ANCHOR（seq 更小）不覆盖迁移后用户写的新草稿", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, {
      kind: "topic_switch",
      operationId: "op-seq",
      targetTopicId: "t2",
    });
    waitingDraft(session, "搬一次就好");
    approvalResult(events, id, "approved");
    authoritative(events, {
      topicId: "t2",
      operationId: "op-seq",
      approvalId: id,
      seq: 9,
    });
    expect(session.draft).toBe("搬一次就好");
    expect(storedText("t2")).toBe("搬一次就好");

    // 用户在新话题重新写稿
    session.draft = "切换之后新写的字";
    session.flushDraft();
    // 重连/乱序回放更旧的权威结果：不得覆盖用户现在的草稿
    authoritative(events, {
      topicId: "t2",
      operationId: "op-seq",
      approvalId: id,
      seq: 3,
    });

    expect(session.draft).toBe("切换之后新写的字");
    expect(storedText("t2")).toBe("切换之后新写的字");
    expect(storedText("t1")).toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("拒绝收口后，迟到的权威成功结果不得再带走草稿", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicRequired(events, {
      kind: "topic_switch",
      operationId: "op-rejected",
      targetTopicId: "t2",
    });
    waitingDraft(session, "拒绝就留在这里");
    approvalResult(events, id, "approved");
    approvalResult(events, id, "rejected");
    expect(followOf(session)).toBeNull();

    authoritative(events, {
      topicId: "t2",
      operationId: "op-rejected",
      approvalId: id,
      seq: 5,
    });
    expect(storedText("t1")).toBe("拒绝就留在这里");
    expect(storedText("t2")).toBeNull();
    expect(api.sendTurn).not.toHaveBeenCalled();
  });
});

describe("F12 · 目标已有草稿保护", () => {
  it("目标话题本来就有草稿：不静默覆盖、登记为被让位草稿、不自动发送", () => {
    const { session, events } = setup();
    startTurn(session, events);
    expect(writeDraft(chatKey("t2"), "目标话题原有的草稿", 1).ok).toBe(true);
    const id = topicRequired(events, {
      kind: "topic_switch",
      operationId: "op-displace",
      targetTopicId: "t2",
    });
    waitingDraft(session, "等待期间写的话");
    approvalResult(events, id, "approved");

    authoritative(events, {
      topicId: "t2",
      operationId: "op-displace",
      approvalId: id,
      seq: 5,
    });

    expect(session.draft, "当前编辑的仍是等待期间那份").toBe("等待期间写的话");
    const displaced = session.displacedDraftsForTopic("t2");
    expect(displaced.some((record) => record.text === "目标话题原有的草稿")).toBe(true);
    expect(displaced.every((record) => record.text !== "等待期间写的话")).toBe(true);
    expect(api.sendTurn).not.toHaveBeenCalled();
  });
});
