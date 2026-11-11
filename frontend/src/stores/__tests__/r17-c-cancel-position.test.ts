/**
 * R17-C（契约 §2.3 / §5 B1）· 取消接续响应的整份接受与迟到响应零副作用。
 *
 * 只通过真实 Pinia store + 可控 HTTP 响应（deferred）观察用户可见行为：
 * 1. 取消 A 在飞 → 先接受较新的 B → 旧取消响应（pos5/pending null）到达：
 *    位置 B / 接续提示 B / 草稿不变 / positionVersion 仍 6；
 *    commandSucceeded=true、stateAdopted=false、ok=false，触发一次权威恢复，零部分修改。
 * 2. 正常取消（响应 pos7/pending null 被接受）→ 提示清理、ok=true、stateAdopted=true。
 * 3. 重放/同版本幂等响应：整份接受（幂等）不推进版本，ok=true。
 * 4. 409 目标冲突：命令失败、零状态修改、提示保留、触发一次权威恢复。
 * 5. 同版本内容冲突响应：接受被拒、零部分修改、触发一次权威恢复。
 * 6. 没有待落实接续时 no-op；网络/5xx 保留同一 request 身份可重试；
 * 7. 旧后端无 position 的兼容分支（只允许旧后端）：命令成功才撤下同一目标的提示。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";
import { api } from "../../services/api";

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

type Session = ReturnType<typeof useSessionStore>;

function setup(): { session: Session } {
  setActivePinia(createPinia());
  const session = useSessionStore();
  useEventStore();
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
  return { session };
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
    nav_version: 5,
    position_version: 5,
    position_identity: "db-1",
    ...overrides,
  };
}

function accept(session: Session, input: Record<string, unknown>, source: "anchor" | "snapshot" | "command" = "anchor") {
  return session.acceptPositionState(input, source);
}

function positionOf(session: Session) {
  return {
    topicId: session.currentTopicId,
    fragmentId: session.anchorFragmentId,
    historic: session.anchorHistoric,
  };
}

function factsOf(session: Session): Record<string, unknown> | null {
  return (session as unknown as { _positionFacts: Record<string, unknown> | null })._positionFacts;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const PENDING_A = {
  pending_intent_id: "intent-A",
  pending_intent_version: 2,
  pending_source_fragment_id: "f-a",
  pending_source_title: "历史 A",
};

const PENDING_B = {
  pending_intent_id: "intent-B",
  pending_intent_version: 1,
  pending_source_fragment_id: "f-b",
  pending_source_title: "历史 B",
};

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  vi.mocked(api.cancelContinuation).mockResolvedValue({ ok: true, cancelled: true } as never);
});

describe("R17-C · 取消响应整份接受（契约 §2.3）", () => {
  it("取消 A 在飞 → 先接受 B → 旧响应到达：B 的位置/接续/草稿/版本全部保持，零部分修改", async () => {
    const { session } = setup();
    accept(session, wire(PENDING_A));
    expect(session.pendingContinuation).toMatchObject({ intentId: "intent-A", intentVersion: 2 });

    const response = deferred<Record<string, unknown>>();
    vi.mocked(api.cancelContinuation).mockReturnValueOnce(response.promise as never);
    const pending = session.cancelPendingContinuation();
    expect(api.cancelContinuation).toHaveBeenCalledTimes(1);
    const options = vi.mocked(api.cancelContinuation).mock.calls[0][0];
    // 取消命令明确携带目标身份（id 必填、版本可选）。
    expect(options.pendingIntentId).toBe("intent-A");
    expect(options.pendingIntentVersion).toBe(2);

    // 取消在飞期间：用户先接受了更新的 B（pos6 + pending B）。
    accept(
      session,
      wire({
        ...PENDING_B,
        fragment_id: "f-b",
        fragment_title: "片段 B",
        position_version: 6,
        nav_version: 6,
      }),
    );
    expect(positionOf(session)).toEqual({ topicId: "t1", fragmentId: "f-b", historic: false });
    expect(session.pendingContinuation).toMatchObject({ intentId: "intent-B", intentVersion: 1 });
    session.draft = "等待期间写的话";
    vi.mocked(api.getRuntimeState).mockClear();

    // 迟到的取消响应（pos5, pending null）：整份拒绝，绝不部分修改。
    response.resolve({
      ok: true,
      cancelled: true,
      position: wire({ position_version: 5, nav_version: 5 }),
    });
    const result = await pending;

    expect(result).toMatchObject({
      ok: false,
      commandSucceeded: true,
      stateAdopted: false,
      code: "stale_position_state",
    });
    expect(result.error, "结论不能伪造成功，要给可读原因").toBeTruthy();
    expect(positionOf(session)).toEqual({ topicId: "t1", fragmentId: "f-b", historic: false });
    expect(session.pendingContinuation).toMatchObject({ intentId: "intent-B", intentVersion: 1 });
    expect(factsOf(session)).toMatchObject({ positionVersion: 6, pendingIntentId: "intent-B" });
    expect(session.draft).toBe("等待期间写的话");
    // 拒绝路径触发一次权威恢复（single-flight）。
    await vi.waitFor(() => expect(api.getRuntimeState).toHaveBeenCalledTimes(1));
    expect(session.lastError).toBeTruthy();
  });

  it("正常取消：响应 position 被整份接受 → 提示清理、ok=true、stateAdopted=true", async () => {
    const { session } = setup();
    accept(session, wire(PENDING_A));
    vi.mocked(api.cancelContinuation).mockResolvedValueOnce({
      ok: true,
      cancelled: true,
      position: wire({ position_version: 7, nav_version: 5 }),
    } as never);

    const result = await session.cancelPendingContinuation();

    expect(result).toEqual({
      ok: true,
      commandSucceeded: true,
      stateAdopted: true,
      code: null,
      error: null,
    });
    expect(session.pendingContinuation).toBeNull();
    expect(factsOf(session)).toMatchObject({ positionVersion: 7, pendingIntentId: null });
    expect(positionOf(session)).toEqual({ topicId: "t1", fragmentId: null, historic: false });
    // 正常接受不触发权威恢复。
    expect(api.getRuntimeState).not.toHaveBeenCalled();
  });

  it("重放/同版本幂等响应：整份接受（幂等）不推进版本，ok=true", async () => {
    const { session } = setup();
    accept(session, wire(PENDING_A));
    const response = deferred<Record<string, unknown>>();
    vi.mocked(api.cancelContinuation).mockReturnValueOnce(response.promise as never);
    const pending = session.cancelPendingContinuation();

    // 取消的 ANCHOR 广播先到（pos6, pending null）；随后重放响应同版本同内容。
    accept(session, wire({ position_version: 6, nav_version: 5 }), "anchor");
    expect(session.pendingContinuation).toBeNull();
    vi.mocked(api.getRuntimeState).mockClear();

    response.resolve({
      ok: true,
      cancelled: true,
      replay: true,
      position: wire({ position_version: 6, nav_version: 5 }),
    });
    const result = await pending;

    expect(result).toEqual({
      ok: true,
      commandSucceeded: true,
      stateAdopted: true,
      code: null,
      error: null,
    });
    expect(factsOf(session)).toMatchObject({ positionVersion: 6 });
    expect(api.getRuntimeState, "幂等重放不得触发恢复").not.toHaveBeenCalled();
  });

  it("409 目标冲突：命令失败、零状态修改、提示保留、触发一次权威恢复", async () => {
    const { session } = setup();
    accept(session, wire(PENDING_A));
    vi.mocked(api.cancelContinuation).mockRejectedValueOnce({
      status: 409,
      body: { code: "continuation_cancel_target_mismatch" },
      message: "POST /api/anchor/continue/cancel -> 409",
    } as never);
    vi.mocked(api.getRuntimeState).mockClear();

    const result = await session.cancelPendingContinuation();

    expect(result).toMatchObject({
      ok: false,
      commandSucceeded: false,
      stateAdopted: false,
      code: "continuation_cancel_target_mismatch",
    });
    expect(positionOf(session)).toEqual({ topicId: "t1", fragmentId: null, historic: false });
    expect(session.pendingContinuation).toMatchObject({ intentId: "intent-A", intentVersion: 2 });
    expect(factsOf(session)).toMatchObject({ positionVersion: 5 });
    await vi.waitFor(() => expect(api.getRuntimeState).toHaveBeenCalledTimes(1));
  });

  it("同版本内容冲突响应：接受被拒、零部分修改、触发一次权威恢复", async () => {
    const { session } = setup();
    accept(session, wire(PENDING_A));
    vi.mocked(api.cancelContinuation).mockResolvedValueOnce({
      ok: true,
      cancelled: true,
      position: wire({
        position_version: 5,
        nav_version: 5,
        topic_id: "t-other",
        pending_intent_id: "intent-B",
      }),
    } as never);
    vi.mocked(api.getRuntimeState).mockClear();

    const result = await session.cancelPendingContinuation();

    expect(result).toMatchObject({
      ok: false,
      commandSucceeded: true,
      stateAdopted: false,
      code: "same_version_conflict",
    });
    expect(positionOf(session)).toEqual({ topicId: "t1", fragmentId: null, historic: false });
    expect(session.pendingContinuation).toMatchObject({ intentId: "intent-A", intentVersion: 2 });
    expect(factsOf(session)).toMatchObject({ positionVersion: 5, pendingIntentId: "intent-A" });
    await vi.waitFor(() => expect(api.getRuntimeState).toHaveBeenCalledTimes(1));
  });

  it("没有待落实接续时是 no-op：不发请求、ok=true", async () => {
    const { session } = setup();
    const result = await session.cancelPendingContinuation();
    expect(result.ok).toBe(true);
    expect(api.cancelContinuation).not.toHaveBeenCalled();
  });

  it("网络/5xx 保留同一 request 身份与目标：重试复用 request_id 与 base_nav_version", async () => {
    const { session } = setup();
    accept(session, wire(PENDING_A));
    vi.mocked(api.cancelContinuation).mockRejectedValueOnce(new Error("fetch failed"));

    const first = await session.cancelPendingContinuation();
    expect(first).toMatchObject({ ok: false, commandSucceeded: false, stateAdopted: false, code: "network" });
    expect(session.pendingContinuation).toMatchObject({ intentId: "intent-A" });
    const firstOptions = vi.mocked(api.cancelContinuation).mock.calls[0][0];

    vi.mocked(api.cancelContinuation).mockResolvedValueOnce({
      ok: true,
      cancelled: true,
      position: wire({ position_version: 6, nav_version: 5 }),
    } as never);
    const retry = await session.cancelPendingContinuation();
    expect(retry.ok).toBe(true);
    const retryOptions = vi.mocked(api.cancelContinuation).mock.calls[1][0];
    expect(retryOptions.requestId).toBe(firstOptions.requestId);
    expect(retryOptions.baseNavVersion).toBe(firstOptions.baseNavVersion);
    expect(retryOptions.pendingIntentId).toBe("intent-A");
    expect(retryOptions.pendingIntentVersion).toBe(2);
  });

  it("旧后端（响应无 position）兼容：命令成功才撤下同一目标的本地提示", async () => {
    const { session } = setup();
    accept(session, wire(PENDING_A));
    vi.mocked(api.cancelContinuation).mockResolvedValueOnce({ ok: true, cancelled: true } as never);

    const result = await session.cancelPendingContinuation();

    expect(result).toMatchObject({ ok: true, commandSucceeded: true, stateAdopted: true });
    expect(session.pendingContinuation).toBeNull();
  });

  it("旧后端兼容分支不得清掉飞行期间登记的更新目标", async () => {
    const { session } = setup();
    accept(session, wire(PENDING_A));
    const response = deferred<Record<string, unknown>>();
    vi.mocked(api.cancelContinuation).mockReturnValueOnce(response.promise as never);
    const pending = session.cancelPendingContinuation();

    accept(session, wire({ ...PENDING_B, position_version: 6, nav_version: 6 }));
    response.resolve({ ok: true, cancelled: true });
    const result = await pending;

    expect(result).toMatchObject({
      ok: false,
      commandSucceeded: true,
      stateAdopted: false,
      code: "continuation_target_superseded",
    });
    expect(session.pendingContinuation).toMatchObject({ intentId: "intent-B", intentVersion: 1 });
  });
});
