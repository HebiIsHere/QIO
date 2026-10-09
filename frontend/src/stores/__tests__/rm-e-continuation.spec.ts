/**
 * E 组 · M01 受控验收（store 层）：接续选择的原子取消与「已提交消息的起点身份」。
 *
 * 验收逐条：
 * 1. 历史 A → 明确进入 B 最新位置 → 下一条消息属于 B（不再被 A 的登记带偏）；
 * 2. 只浏览 / 只查看历史 → 不取消用户已经做出的选择；
 * 3. 排队后改选 → 已提交（排队中）消息的起点身份不被追溯改向；
 * 4. 受理结果与提交时的起点不一致 → 如实提示，绝不静默改向；
 * 5. 取消失败 → 本地状态不变（宁可不动，也不把消息送到错误的起点）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { useEventStore } from "../events";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    cancelContinuation: vi.fn(async () => ({ ok: true, cancelled: true })),
    // 刻意不提供 getSessionContext：`setAnchorAndSync` 里的历史刷新会安静失败，
    // 这样断言的是**锚点本身**（与真实后端返回权威 topic 的效果一致）。
    sendTurn: vi.fn(),
    getRuntimeState: vi.fn(async () => ({
      instance_id: "inst",
      revision: 1,
      turn_queue: { instance_id: "inst", revision: 1, running: null, queued: [], cancelled: [] },
      approvals: [],
      tasks: [],
      tools: [],
    })),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
    listDevAuthorizations: vi.fn(async () => ({ authorizations: [] })),
  },
}));

const cancelContinuation = vi.mocked(api.cancelContinuation);
const sendTurn = vi.mocked(api.sendTurn);

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { session: useSessionStore(), events: useEventStore() };
}

const HISTORY_A = { intentId: "intent_A", sourceTitle: "历史片段 A" };

beforeEach(() => {
  vi.clearAllMocks();
  cancelContinuation.mockResolvedValue({ ok: true, cancelled: true } as never);
  sendTurn.mockResolvedValue({
    ok: true,
    accepted: true,
    turn_id: "turn_queued",
    status: "accepted",
    topic_id: "B",
  } as never);
});

describe("历史 A → 明确进入 B 最新位置", () => {
  it("先原子取消旧的接续选择，再改起点；发送属于 B", async () => {
    const { session } = setup();
    session.setAnchor("A", "fA", "话题 A", { id: "fA", title: "历史片段 A" }, true);
    session.setPendingContinuation(HISTORY_A);
    expect(session.pendingContinuation).not.toBeNull();

    // 用户点「进入 B（最新位置）」：明确改变起点 → 先取消旧登记
    const cancelled = await session.cancelPendingContinuation();
    expect(cancelled).toBe(true);
    expect(cancelContinuation).toHaveBeenCalledTimes(1);
    expect(session.pendingContinuation).toBeNull(); // 界面提示与后台状态同步

    // 后端权威结果（与 ANCHOR 事件同形）：B 的最新位置
    await session.setAnchorAndSync("B", null, "话题 B", undefined, false);
    expect(session.anchorHistoric).toBe(false);
    expect(session.anchorFragmentId).toBeNull();

    await session.send("这条属于 B");
    // 提交时已经没有任何接续选择 → 后端只会按 topic_id=B 处理
    expect(sendTurn).toHaveBeenCalledWith("这条属于 B", "B");
  });

  it("取消失败：本地选择保留、起点不动（不把消息送到错误的起点）", async () => {
    const { session } = setup();
    session.setAnchor("A", "fA", "话题 A", { id: "fA", title: "历史片段 A" }, true);
    session.setPendingContinuation(HISTORY_A);
    cancelContinuation.mockRejectedValueOnce(new Error("POST /api/anchor/continue/cancel -> 500"));

    const cancelled = await session.cancelPendingContinuation();

    expect(cancelled).toBe(false);
    expect(session.pendingContinuation).toEqual(HISTORY_A); // 没被清掉
    expect(session.lastError).toContain("没能取消");
    expect(session.currentTopicId).toBe("A");
  });

  it("没有登记时取消是 no-op：不产生任何请求", async () => {
    const { session } = setup();
    expect(await session.cancelPendingContinuation()).toBe(true);
    expect(cancelContinuation).not.toHaveBeenCalled();
  });
});

describe("只浏览不取消选择", () => {
  it("收到「已登记接续」的 ANCHOR（只是查看历史）不会触发取消", () => {
    const { session, events } = setup();
    events.route({
      type: "ANCHOR",
      id: "a1",
      ts: "",
      data: {
        topic_id: "A",
        fragment_id: "fA",
        topic_name: "话题 A",
        fragment_title: "历史片段 A",
        historic: true,
        pending_intent_id: "intent_A",
        pending_source_title: "历史片段 A",
      },
    });

    expect(session.pendingContinuation).toEqual({ intentId: "intent_A", sourceTitle: "历史片段 A" });
    expect(cancelContinuation).not.toHaveBeenCalled();
  });

  it("选中另一段历史（替换语义）：不调取消接口，由后端一次登记替换旧意图", () => {
    const { session } = setup();
    session.setPendingContinuation(HISTORY_A);
    // 只是登记新选择（浏览/改选）——真正的替换发生在后端 register_intent 里
    session.setPendingContinuation({ intentId: "intent_B", sourceTitle: "历史片段 B" });
    expect(session.pendingContinuation?.intentId).toBe("intent_B");
    expect(cancelContinuation).not.toHaveBeenCalled();
  });
});

describe("已提交（排队中）消息不被后续导航追溯改向", () => {
  it("排队后改选到别的话题：那条消息的起点身份与提交时完全一致", async () => {
    const { session } = setup();
    session.setAnchor("B", "fB", "话题 B", { id: "fB", title: "片段 B" }, false);
    session.turnRunning = true; // 已有任务在跑 → 这条会排队
    sendTurn.mockResolvedValueOnce({
      ok: true,
      accepted: true,
      turn_id: "turn_1",
      status: "queued",
      topic_id: "B",
    } as never);

    await session.send("排队的第一条");
    const queuedMessage = session.messages[session.messages.length - 1];
    const captured = queuedMessage.startIdentity;
    expect(captured).toMatchObject({ topicId: "B", fragmentId: "fB", intentId: null });
    expect(queuedMessage.queued).toBe(true);
    expect(sendTurn).toHaveBeenLastCalledWith("排队的第一条", "B");

    // 之后用户去星球改选到话题 C（明确改变起点）
    session.setAnchor("C", null, "话题 C", null, false);

    // 已受理的那条消息不被追溯改向：身份与消息本体都没变
    expect(queuedMessage.startIdentity).toEqual(captured);
    expect(queuedMessage.topicName).toBe("话题 B"); // 产生时的归属快照
    expect(session.currentTopicId).toBe("C");
  });

  it("提交时带上待落实的接续选择身份（intentId 被捕获，不随后续变化）", async () => {
    const { session } = setup();
    session.setAnchor("A", "fA", "话题 A", { id: "fA", title: "历史片段 A" }, true);
    session.setPendingContinuation(HISTORY_A);

    await session.send("从 A 继续");
    const message = session.messages[session.messages.length - 1];
    expect(message.startIdentity).toMatchObject({
      topicId: "A",
      fragmentId: "fA",
      intentId: "intent_A",
    });

    // 之后选择被后台消费/取消，都不会改这条已提交消息的身份
    session.setPendingContinuation(null);
    expect(message.startIdentity?.intentId).toBe("intent_A");
  });

  it("受理结果与提交时的起点不一致：如实提示，不改已捕获的身份", async () => {
    const { session } = setup();
    session.setAnchor("A", null, "话题 A", null, false);
    sendTurn.mockResolvedValueOnce({
      ok: true,
      accepted: true,
      turn_id: "turn_1",
      status: "accepted",
      topic_id: "Z",
    } as never);

    await session.send("本该属于 A");

    const message = session.messages[session.messages.length - 1];
    expect(message.startIdentity?.topicId).toBe("A");
    expect(session.warning).toContain("不一致");
    expect(session.warning).toContain("话题 A");
  });

  it("后端没给 topic_id 时不做无依据的判定（不误报不一致）", async () => {
    const { session } = setup();
    session.setAnchor("A", null, "话题 A", null, false);
    sendTurn.mockResolvedValueOnce({
      ok: true,
      accepted: true,
      turn_id: "turn_1",
      status: "accepted",
      topic_id: null,
    } as never);

    await session.send("x");
    expect(session.warning).toBeNull();
  });
});
