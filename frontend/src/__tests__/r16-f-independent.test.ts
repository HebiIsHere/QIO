/**
 * R16 F 独立验收（前端）：完整位置状态整份接受、两类身份/版本、草稿跟随与旁路。
 *
 * 预期只从冻结契约（docs/topic-unify-20261111-contract.md §2 I01—I14、§7、§14.3）
 * 推导，不参考 C 的测试；基线红在 r15-baseline 前端源码/依赖上取得，集成绿在 qio-r14。
 *
 * 覆盖：
 * 1. §7.1 acceptPositionState 唯一入口（事件/快照/命令响应都走它）；
 * 2. §7.2 I06 整份接受：无旧状态采用、旧版本整份拒绝、同版本同内容幂等、
 *    同版本冲突不合并且记录可观察事实并触发一次权威恢复、身份变化规则；
 * 3. §3.1 I02 同源：一次可见的位置 + pending 只能来自同一份完整状态；
 * 4. §7.3/I13 草稿跟随：快照采用不覆盖等待草稿、操作结果按身份迁移一次；
 * 5. §7.1 旁路：业务代码不得直接 setAnchor/setAnchorAndSync；
 * 6. 既有语义不倒退：不自动发送、目标草稿保护。
 *
 * 诚实边界：本文件用真实 Pinia store + 事件路由，但 API/恢复入口以 mock 隔离；
 * 不是浏览器端到端，也不是真实后端联调（E 覆盖真实接线）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../stores/session";
import { useEventStore } from "../stores/events";
import { restoreRuntimeState } from "../stores/restore";
import { draftStorageKey, readDraft, writeDraft } from "../interactive/drafts";
import { api } from "../services/api";
import type { AgentEvent } from "../services/events";

vi.mock("../stores/restore", () => ({
  restoreRuntimeState: vi.fn(async (reason: string) => ({
    ok: true,
    reason,
    generation: 1,
    rounds: 1,
  })),
  invalidateRestore: vi.fn(() => 1),
  isRestoring: vi.fn(() => false),
  restoreGeneration: vi.fn(() => 1),
  restoreRoundCount: vi.fn(() => 1),
}));

vi.mock("../services/api", () => ({
  api: {
    getRuntimeState: vi.fn(async () => ({
      approvals: [],
      tasks: [],
      instance_id: "inst-A",
      anchor: null,
    })),
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

type Store = ReturnType<typeof useSessionStore>;
type EventStore = ReturnType<typeof useEventStore>;

let eventSeq = 0;

function ev(type: string, data: Record<string, unknown>): AgentEvent {
  eventSeq += 1;
  return { type, id: "r16f-" + type + "-" + eventSeq, ts: "", data } as AgentEvent;
}

function setup(): { session: Store; events: EventStore } {
  setActivePinia(createPinia());
  return { session: useSessionStore(), events: useEventStore() };
}

/** §3.1 冻结 wire 形状（snake_case 全套）。 */
function positionWire(overrides: Record<string, unknown> = {}): Record<string, unknown> {
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

type AcceptResult = {
  accepted: boolean;
  reason: string;
  idempotent: boolean;
  conflicted: boolean;
  adopted: boolean;
};

function accept(
  session: Store,
  input: Record<string, unknown> | null,
  source: "anchor" | "snapshot" | "command" = "anchor",
): AcceptResult {
  const method = (session as unknown as {
    acceptPositionState?: (
      input: Record<string, unknown> | null,
      source: "anchor" | "snapshot" | "command",
    ) => AcceptResult;
  }).acceptPositionState;
  expect(typeof method, "§7.1：session 必须提供唯一接受入口 acceptPositionState").toBe(
    "function",
  );
  return method!.call(session, input, source);
}

function factsOf(session: Store): Record<string, unknown> | null {
  return (session as unknown as { _positionFacts: Record<string, unknown> | null })
    ._positionFacts;
}

function conflictOf(session: Store): Record<string, unknown> | null {
  return (session as unknown as { _positionStateConflict: Record<string, unknown> | null })
    ._positionStateConflict;
}

function visible(session: Store): Record<string, unknown> {
  return {
    topic: session.currentTopicId,
    fragment: session.anchorFragmentId,
    historic: session.anchorHistoric,
    pending: session.pendingContinuation,
  };
}

function chatKey(topicId: string): string {
  return draftStorageKey("chat", topicId);
}

function storedText(topicId: string): string | null {
  return readDraft(chatKey(topicId))?.text ?? null;
}

function waitingDraft(session: Store, text: string): void {
  session.draft = text;
  session.flushDraft();
}

function anchorEventData(
  overrides: Record<string, unknown> = {},
  positionOverrides: Record<string, unknown> = {},
): Record<string, unknown> {
  const wire = positionWire(positionOverrides);
  return {
    ...wire, // ANCHOR 保留既有扁平字段
    origin: "manual",
    operation_id: null,
    approval_id: null,
    param_version: null,
    turn_id: null,
    command_id: null,
    seq: 1,
    instance_id: "inst-A",
    position: wire,
    ...overrides,
  };
}

beforeEach(() => {
  localStorage.clear();
  eventSeq = 0;
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// 1. §7.1 唯一入口 / I02 同源
// ---------------------------------------------------------------------------

describe("R16 · 完整位置状态整份接受（I02/I06）", () => {
  it("无本地状态时整份采用：位置、historic、pending、两类版本与 identity 同时落地", () => {
    const { session } = setup();
    const wire = positionWire({
      position_version: 3,
      nav_version: 5,
      pending_intent_id: "intent-1",
      pending_intent_version: 2,
      pending_source_fragment_id: "f-old",
      pending_source_title: "旧来源",
    });
    const result = accept(session, wire, "anchor");
    expect(result.accepted).toBe(true);
    expect(result.reason).toBe("baseline");
    expect(result.adopted).toBe(true);
    expect(session.currentTopicId).toBe("t1");
    expect(session.anchorFragmentId).toBeNull();
    expect(session.anchorHistoric).toBe(false);
    expect(session.pendingContinuation?.intentId).toBe("intent-1");
    const facts = factsOf(session);
    expect(facts?.positionVersion).toBe(3);
    expect(facts?.navVersion).toBe(5);
    expect(facts?.positionIdentity).toBe("db-1");
    expect(facts?.pendingIntentId).toBe("intent-1");
    expect(facts?.source).toBe("anchor");
  });

  it("更高版本整份采用：不得出现「新位置 + 旧 pending」的拼接（I02）", () => {
    const { session } = setup();
    accept(
      session,
      positionWire({
        topic_id: "t1",
        position_version: 4,
        nav_version: 4,
        pending_intent_id: "intent-old",
        pending_source_fragment_id: "f-old",
      }),
    );
    accept(
      session,
      positionWire({
        topic_id: "t2",
        topic_name: "话题二",
        fragment_id: "f2",
        fragment_title: "片段二",
        historic: true,
        position_version: 5,
        nav_version: 6,
        pending_intent_id: "intent-new",
        pending_intent_version: 3,
        pending_source_fragment_id: "f-new",
        pending_source_title: "新来源",
      }),
    );
    expect(session.currentTopicId).toBe("t2");
    expect(session.anchorFragmentId).toBe("f2");
    expect(session.anchorHistoric).toBe(true);
    expect(session.pendingContinuation?.intentId, "pending 必须来自同一份新状态").toBe(
      "intent-new",
    );
    expect(session.pendingContinuation?.sourceTitle).toBe("新来源");
    const facts = factsOf(session);
    expect(facts?.positionVersion).toBe(5);
    expect(facts?.navVersion).toBe(6);
    expect(facts?.topicId).toBe("t2");
    expect(facts?.fragmentId).toBe("f2");
  });

  it("两类版本分开：position_version 更新时即使 nav_version 不变也必须接受（自动分段/内部细化）", () => {
    const { session } = setup();
    accept(
      session,
      positionWire({
        position_version: 3,
        nav_version: 7,
        topic_id: "t1",
        fragment_id: "f1",
        historic: false,
      }),
    );
    // 后端 auto_segment / internal_refine：nav_version 0 推进，position_version +1。
    const result = accept(
      session,
      positionWire({
        position_version: 4,
        nav_version: 7,
        topic_id: "t1",
        fragment_id: "f1",
        historic: true,
        fragment_title: "刚封存的片段",
      }),
    );
    expect(result.accepted).toBe(true);
    expect(result.reason).toBe("new_position_state");
    expect(session.anchorHistoric, "封存后前端必须看到历史位置").toBe(true);
    const facts = factsOf(session);
    expect(facts?.positionVersion).toBe(4);
    expect(facts?.navVersion, "nav_version 不因内部推进而变").toBe(7);
  });

  it("自动分段事件的完整位置状态可被前端接受并推进片段展示（§10.2 A 前端半段）", () => {
    const { session, events } = setup();
    session.adoptInstance("inst-A");
    accept(
      session,
      positionWire({ position_version: 5, nav_version: 5, topic_id: "tA", fragment_id: "f1" }),
    );
    events.route(
      ev(
        "ANCHOR",
        anchorEventData(
          { origin: "manual", instance_id: "inst-A", seq: 2 },
          {
            topic_id: "tA",
            fragment_id: "f1",
            historic: true, // 容量分段把开放片段封存
            position_version: 6,
            nav_version: 5,
            command_id: "internal:auto_segment:turn-1:f1",
          },
        ),
      ),
    );
    expect(session.currentTopicId).toBe("tA");
    expect(session.anchorHistoric).toBe(true);
    expect(factsOf(session)?.positionVersion).toBe(6);
  });

  it("命令响应的 position 与事件的 position 走同一入口（source=command 也整份接受）", () => {
    const { session } = setup();
    accept(session, positionWire({ position_version: 1, topic_id: "t1" }), "anchor");
    const result = accept(
      session,
      positionWire({ topic_id: "t3", position_version: 9, nav_version: 9 }),
      "command",
    );
    expect(result.accepted).toBe(true);
    expect(result.adopted).toBe(true);
    expect(session.currentTopicId).toBe("t3");
    expect(factsOf(session)?.source).toBe("command");
  });
});

// ---------------------------------------------------------------------------
// 2. I06 旧状态不能部分覆盖
// ---------------------------------------------------------------------------

describe("R16 · I06 旧状态/同版本冲突整份处理", () => {
  it("较旧 position_version 整份拒绝：位置、historic、pending 三者都不动", () => {
    const { session } = setup();
    accept(
      session,
      positionWire({
        position_version: 6,
        nav_version: 6,
        topic_id: "t-new",
        fragment_id: "f-new",
        historic: false,
        pending_intent_id: "p-new",
        pending_source_fragment_id: "fp-new",
      }),
    );
    const before = JSON.stringify(visible(session));
    const result = accept(
      session,
      positionWire({
        position_version: 5, // 更旧
        nav_version: 7, // 即使 nav 版本更大也不能绕过 position_version 排序
        topic_id: "t-old",
        fragment_id: "f-old",
        historic: true,
        pending_intent_id: "p-old",
        pending_source_fragment_id: "fp-old",
      }),
    );
    expect(result.accepted).toBe(false);
    expect(result.reason).toBe("stale_position_state");
    expect(JSON.stringify(visible(session))).toBe(before);
    expect(factsOf(session)?.positionVersion).toBe(6);
  });

  it("同版本同内容：幂等接受，零副作用（不重载历史、不迁移草稿）", () => {
    const { session } = setup();
    accept(
      session,
      positionWire({
        position_version: 8,
        nav_version: 8,
        topic_id: "t1",
        fragment_id: "f1",
        historic: true,
        pending_intent_id: "p1",
        pending_source_fragment_id: "fp1",
      }),
    );
    const loadHistory = vi.spyOn(session, "loadHistory");
    waitingDraft(session, "还在写");
    const result = accept(
      session,
      positionWire({
        position_version: 8,
        nav_version: 8,
        topic_id: "t1",
        fragment_id: "f1",
        historic: true,
        pending_intent_id: "p1",
        pending_source_fragment_id: "fp1",
        // 派生展示字段不同也不构成冲突（§14.3 精确比较字段）
        topic_name: "改名后的展示标题",
        fragment_title: "另一个展示标题",
        pending_source_title: "另一个来源标题",
      }),
    );
    expect(result.accepted).toBe(true);
    expect(result.reason).toBe("idempotent");
    expect(result.idempotent).toBe(true);
    expect(loadHistory).not.toHaveBeenCalled();
    expect(session.draft).toBe("还在写");
    expect(storedText("t1")).toBe("还在写");
    expect(factsOf(session)?.positionVersion).toBe(8);
  });

  it("同版本不同内容：不猜测、不部分合并，记录冲突并触发权威恢复", async () => {
    const { session } = setup();
    accept(
      session,
      positionWire({
        position_version: 9,
        nav_version: 9,
        topic_id: "t1",
        fragment_id: "f1",
        historic: true,
        pending_intent_id: "p-local",
        pending_source_fragment_id: "fp-local",
      }),
    );
    const localBefore = JSON.stringify(visible(session));
    const result = accept(
      session,
      positionWire({
        position_version: 9,
        nav_version: 9,
        topic_id: "t1",
        fragment_id: "f1",
        historic: true,
        pending_intent_id: "p-other",
        pending_source_fragment_id: "fp-other",
      }),
    );
    expect(result.accepted).toBe(false);
    expect(result.conflicted).toBe(true);
    expect(result.reason).toBe("same_version_conflict");
    expect(conflictOf(session), "冲突必须成为可观察事实").toBeTruthy();
    expect(JSON.stringify(visible(session)), "不得部分合并").toBe(localBefore);
    expect(session.pendingContinuation?.intentId).toBe("p-local");
    await vi.waitFor(() => {
      expect(vi.mocked(restoreRuntimeState)).toHaveBeenCalledWith("position-state-conflict");
    });
  });

  it("旧实例（instance_id 不同）不得绕过接受检查；同实例 seq 只在缺版本时兜底", () => {
    const { session, events } = setup();
    session.adoptInstance("inst-B");
    accept(
      session,
      positionWire({ position_version: 10, nav_version: 10, topic_id: "t-current" }),
      "anchor",
    );
    // 旧实例 A 的迟到事件：版本更低 → 整条拒绝。
    events.route(
      ev(
        "ANCHOR",
        anchorEventData(
          { instance_id: "inst-A", seq: 999 },
          { topic_id: "t-old", position_version: 9, nav_version: 9 },
        ),
      ),
    );
    expect(session.currentTopicId).toBe("t-current");
  });
});

// ---------------------------------------------------------------------------
// 3. 身份规则
// ---------------------------------------------------------------------------

describe("R16 · position_identity 与权威恢复", () => {
  it("实时事件来自不同数据库身份：拒绝 stale_position_identity 且不改状态并触发恢复", async () => {
    const { session } = setup();
    accept(session, positionWire({ position_version: 4, position_identity: "db-1" }));
    const before = JSON.stringify(visible(session));
    const result = accept(
      session,
      positionWire({ position_version: 5, position_identity: "db-2", topic_id: "t-other" }),
      "anchor",
    );
    expect(result.accepted).toBe(false);
    expect(result.reason).toBe("stale_position_identity");
    expect(JSON.stringify(visible(session))).toBe(before);
    await vi.waitFor(() => {
      expect(vi.mocked(restoreRuntimeState)).toHaveBeenCalledWith("position-state-conflict");
    });
  });

  it("权威快照来自新身份：以新身份重建基线并整份采用", async () => {
    const { session } = setup();
    accept(session, positionWire({ position_version: 4, position_identity: "db-1" }));
    const result = accept(
      session,
      positionWire({
        position_version: 1, // 新库从 0/1 重新计数
        nav_version: 1,
        position_identity: "db-2",
        topic_id: "t-db2",
        fragment_id: "f-db2",
      }),
      "snapshot",
    );
    expect(result.accepted).toBe(true);
    expect(result.reason).toBe("new_position_identity");
    expect(result.adopted).toBe(true);
    expect(session.currentTopicId).toBe("t-db2");
    expect(factsOf(session)?.positionIdentity).toBe("db-2");
    expect(factsOf(session)?.positionVersion).toBe(1);
  });

  it("事件层与快照层都必须经过 acceptPositionState（§7.1 唯一入口）", () => {
    const { session, events } = setup();
    session.adoptInstance("inst-A");
    const spy = vi.spyOn(session as unknown as { acceptPositionState: () => void }, "acceptPositionState");
    events.route(ev("ANCHOR", anchorEventData({}, { position_version: 2 })));
    expect(spy).toHaveBeenCalled();
    const firstCall = spy.mock.calls[0] as unknown as [Record<string, unknown>, string];
    expect(firstCall[1]).toBe("anchor");
    expect(firstCall[0]?.topic_id).toBe("t1");

    spy.mockClear();
    events.applyRuntimeState({
      approvals: [],
      tasks: [],
      instance_id: "inst-A",
      anchor: positionWire({ position_version: 3, position_identity: "db-1" }),
    } as unknown as Parameters<EventStore["applyRuntimeState"]>[0]);
    expect(spy).toHaveBeenCalled();
    const snapshotCall = spy.mock.calls[0] as unknown as [Record<string, unknown>, string];
    expect(snapshotCall[1]).toBe("snapshot");
  });
});

// ---------------------------------------------------------------------------
// 4. I13 / §7.3 草稿
// ---------------------------------------------------------------------------

describe("R16 · 草稿与等待期编辑", () => {
  it("位置快照采用不覆盖等待期间的新编辑，也不自动发送", () => {
    const { session, events } = setup();
    session.adoptInstance("inst-A");
    accept(session, positionWire({ position_version: 1, topic_id: "t1" }));
    waitingDraft(session, "等待期间写的字");
    events.applyRuntimeState({
      approvals: [],
      tasks: [],
      instance_id: "inst-A",
      anchor: positionWire({
        position_version: 2,
        nav_version: 2,
        topic_id: "t2",
        topic_name: "目标话题",
      }),
    } as unknown as Parameters<EventStore["applyRuntimeState"]>[0]);
    expect(
      session.draft === "等待期间写的字" || storedText("t1") === "等待期间写的字",
      "等待期间的新编辑不得丢失（输入框或原话题已落盘副本至少一处保留）",
    ).toBe(true);
    expect(storedText("t1")).toBe("等待期间写的字");
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("目标话题已有草稿：快照接受不得静默覆盖（存储不丢、不删除）", () => {
    const { session, events } = setup();
    session.adoptInstance("inst-A");
    expect(writeDraft(chatKey("t2"), "目标话题原有草稿", 1).ok).toBe(true);
    accept(session, positionWire({ position_version: 1, topic_id: "t1" }));
    waitingDraft(session, "本次等待草稿");
    events.applyRuntimeState({
      approvals: [],
      tasks: [],
      instance_id: "inst-A",
      anchor: positionWire({
        position_version: 2,
        nav_version: 2,
        topic_id: "t2",
      }),
    } as unknown as Parameters<EventStore["applyRuntimeState"]>[0]);
    const displaced = session.displacedDraftsForTopic("t2");
    const preserved =
      storedText("t2") === "目标话题原有草稿" ||
      displaced.some((record) => record.text === "目标话题原有草稿");
    expect(preserved, "目标已有草稿不得被静默覆盖（让位/恢复语义不变）").toBe(true);
    expect(storedText("t1")).toBe("本次等待草稿");
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("重连快照先于操作结果到达：等待草稿不丢，权威结果到达后按操作身份迁移一次（§7.3/§14.8）", () => {
    const { session, events } = setup();
    session.adoptInstance("inst-A");
    accept(session, positionWire({ position_version: 1, nav_version: 1, topic_id: "t1" }));
    events.route(
      ev("APPROVAL_REQUIRED", {
        approval: {
          approval_id: "ap-r16f-follow",
          kind: "topic_switch",
          turn_id: "turn-f",
          payload: {
            action: "switch",
            turn_id: "turn-f",
            from_topic_id: "t1",
            from_fragment_id: null,
            target_topic_id: "t2",
            target_topic_name: "目标话题",
            operation_id: "op-r16f-follow",
            param_version: 1,
            base_nav_version: 1,
          },
        },
        phase: "waiting",
      }),
    );
    waitingDraft(session, "等待期间写的字");
    events.route(
      ev("APPROVAL_RESULT", {
        approval_id: "ap-r16f-follow",
        phase: "approved",
        decision: "approved",
        turn_id: "turn-f",
        operation_id: "op-r16f-follow",
        param_version: 1,
      }),
    );

    // 重连快照先到：采用位置时不得让等待草稿从输入框消失。
    events.applyRuntimeState({
      approvals: [],
      tasks: [],
      instance_id: "inst-A",
      anchor: positionWire({
        topic_id: "t2",
        topic_name: "目标话题",
        position_version: 2,
        nav_version: 2,
      }),
    } as unknown as Parameters<EventStore["applyRuntimeState"]>[0]);
    expect(
      session.draft === "等待期间写的字" || storedText("t1") === "等待期间写的字",
      "快照采用不得让等待草稿丢失",
    ).toBe(true);

    const executed = {
      approval_id: "ap-r16f-follow",
      phase: "executed",
      decision: "approved",
      turn_id: "turn-f",
      operation_id: "op-r16f-follow",
      param_version: 1,
      instance_id: "inst-A",
      position: positionWire({
        topic_id: "t2",
        topic_name: "目标话题",
        position_version: 2,
        nav_version: 2,
      }),
    };
    events.route(ev("APPROVAL_RESULT", executed));
    expect(storedText("t2"), "权威结果到达后草稿必须跟随真实目标一次").toBe(
      "等待期间写的字",
    );
    expect(api.sendTurn).not.toHaveBeenCalled();

    // 重复的权威结果：命中已消费身份，零副作用。
    events.route(ev("APPROVAL_RESULT", executed));
    expect(storedText("t2")).toBe("等待期间写的字");
    expect(api.sendTurn).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 5. §7.1 旁路静态检查：业务代码不得直接 setAnchor/setAnchorAndSync
// ---------------------------------------------------------------------------

function sourceFiles(root: string): string[] {
  const files: string[] = [];
  const walk = (dir: string): void => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      const stat = statSync(full);
      if (stat.isDirectory()) {
        if (name === "node_modules" || name === "__tests__" || name === "dist") continue;
        walk(full);
        continue;
      }
      if (!/\.(ts|tsx|vue|js)$/.test(name)) continue;
      if (/\.(test|spec)\.(ts|tsx|js)$/.test(name)) continue;
      files.push(full);
    }
  };
  walk(root);
  return files;
}

describe("R16 · 前端位置写入旁路（§7.1）", () => {
  it("业务代码不得直接 setAnchor/setAnchorAndSync，只能经 acceptPositionState", () => {
    const srcRoot = join(process.cwd(), "src");
    const violations: string[] = [];
    for (const file of sourceFiles(srcRoot)) {
      const relative = file.replace(/\\/g, "/").split("/src/")[1] ?? file;
      if (relative === "stores/session.ts") continue; // 底层视图赋值只允许被接受入口调用
      const text = readFileSync(file, "utf-8");
      if (/\bsetAnchor\s*\(/.test(text)) {
        violations.push(relative + " 直接调用 setAnchor");
      }
      if (/\bsetAnchorAndSync\s*\(/.test(text)) {
        violations.push(relative + " 直接调用 setAnchorAndSync");
      }
    }
    expect(violations, "业务代码绕过统一接受入口：" + violations.join("; ")).toEqual([]);
  });
});
