/**
 * 话题申请的等待状态、发送限制与草稿跟随（契约 §2.4 / §1.7 / §1.8）。
 *
 * 钉住的行为（不测实现细节）：
 * 1. 等待状态：本对话有 pending 话题申请时 topicApprovalPending 为真，决定后自然消失；
 * 2. 发送限制只针对本对话：别的轮次的申请不影响本对话的发送；
 * 3. 草稿跟随：确认切换/新建成功后，等待期间写在输入框里的草稿仍留输入框、
 *    归属确认后的话题（之后主动发送），**不自动发送**；
 * 4. 拒绝/取消/失败：草稿留在原话题，不丢、不提前改归属；之后的普通导航不搬草稿；
 * 5. 目标话题已有草稿必须保留恢复能力（登记为被让位草稿），不得静默覆盖；
 * 6. 重复事件幂等：重复 APPROVAL_REQUIRED / APPROVAL_RESULT / ANCHOR 不重复搬草稿。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import MessageStream from "../../components/MessageStream.vue";
import TurnProcess from "../../components/TurnProcess.vue";
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
  },
}));

let seq = 0;
function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: `e-${type}-${seq}`, ts: "", data } as AgentEvent;
}

function chatKey(topicId: string): string {
  return draftStorageKey("chat", topicId);
}
function storedText(topicId: string): string | null {
  return readDraft(chatKey(topicId))?.text ?? null;
}

function setup() {
  setActivePinia(createPinia());
  const session = useSessionStore();
  const events = useEventStore();
  return { session, events };
}

/** 真实场景：当前话题 t1、turn_1 正在跑（话题申请只在跑动的一轮里产生）。 */
function startTurn(
  session: ReturnType<typeof useSessionStore>,
  events: ReturnType<typeof useEventStore>,
) {
  session.setAnchor("t1", null, "当前话题", null, false);
  events.route(ev("TURN_START", { turn_id: "turn_1" }));
}

interface RequiredOptions {
  approvalId?: string;
  kind?: "topic_switch" | "topic_create";
  turnId?: string;
  targetTopicId?: string | null;
  newName?: string;
}

/** APPROVAL_REQUIRED（后端已在申请前拦截工具调用，这里模拟事件到达）。 */
function topicApprovalRequired(
  events: ReturnType<typeof useEventStore>,
  opts: RequiredOptions = {},
): string {
  const approvalId = opts.approvalId ?? "ap-topic-1";
  const kind = opts.kind ?? "topic_switch";
  const turnId = opts.turnId ?? "turn_1";
  const payload: Record<string, unknown> = {
    action: kind === "topic_create" ? "create" : "switch",
    turn_id: turnId,
    from_topic_id: "t1",
    from_topic_name: "当前话题",
    reason: "这段内容更像目标话题",
    source: "model",
    // F12：草稿跟随的身份绑定（稳定 operation + 参数版本 + 起点）
    operation_id: "op-1",
    param_version: 1,
    base_nav_version: 1,
  };
  if (kind === "topic_create") {
    payload.new_name = opts.newName ?? "新话题";
  } else {
    payload.target_topic_id = opts.targetTopicId ?? "t2";
    payload.target_topic_name = "目标话题";
  }
  events.route(
    ev("APPROVAL_REQUIRED", {
      approval: {
        approval_id: approvalId,
        kind,
        payload,
        turn_id: turnId,
      },
      phase: "waiting",
    }),
  );
  return approvalId;
}

/** 决定后的结果事件（legacy /respond 路径不带 kind / payload，正好验证登记识别）。 */
function approvalResult(
  events: ReturnType<typeof useEventStore>,
  approvalId: string,
  phase: string,
) {
  events.route(
    ev("APPROVAL_RESULT", {
      approval_id: approvalId,
      phase,
      decision: phase === "rejected" ? "rejected" : "approved",
      turn_id: "turn_1",
    }),
  );
}

/**
 * 权威成功结果（F12）：话题工具真实执行成功后的 ANCHOR。
 *
 * 草稿跟随只消费这种带 operation 身份的事件；普通手动导航（origin=manual /
 * operation_id=null）永远不迁移草稿。
 */
function anchor(
  events: ReturnType<typeof useEventStore>,
  topicId: string,
  name = "目标话题",
) {
  events.route(
    ev("ANCHOR", {
      topic_id: topicId,
      topic_name: name,
      fragment_id: null,
      fragment_title: null,
      historic: false,
      origin: "topic_operation",
      operation_id: "op-1",
      param_version: 1,
      turn_id: "turn_1",
      nav_version: 7,
      seq: 5,
    }),
  );
}

/** 等待期间写草稿：输入框里确实有字，并已落盘（与真实防抖保存结果一致）。 */
function writeWaitingDraft(session: ReturnType<typeof useSessionStore>, text: string) {
  session.draft = text;
  session.flushDraft();
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(api.sendTurn).mockClear();
});

describe("等待状态：只读派生自统一审批队列", () => {
  it("本对话有 pending 话题申请时等待状态为真；决定后自然消失", () => {
    const { session, events } = setup();
    startTurn(session, events);
    expect(session.topicApprovalPending).toBe(false);

    const id = topicApprovalRequired(events, {});
    expect(session.topicApprovalPending).toBe(true);
    expect(session.topicApprovalKey).toBeTruthy();

    approvalResult(events, id, "rejected");
    expect(session.topicApprovalPending).toBe(false);
    expect(session.topicApprovalKey).toBeNull();
  });

  it("发送限制只针对本对话：别的轮次的申请不影响本对话的等待状态", () => {
    const { session, events } = setup();
    startTurn(session, events);
    topicApprovalRequired(events, { turnId: "turn_other", approvalId: "ap-other" });

    expect(session.topicApprovalPending).toBe(false);

    // 切到真正等待的那一轮：等待状态随之成立（只读队列，不做另一套状态机）
    events.route(ev("TURN_START", { turn_id: "turn_other" }));
    expect(session.activeTurnId).toBe("turn_other");
    expect(session.topicApprovalPending).toBe(true);
  });
});

describe("等待文案：过程区与全局状态都说「正在等待你选择话题」", () => {
  it("过程区状态行：等待选择话题时显示该文案，不再显示运行中/生成中", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const events = useEventStore();
    startTurn(session, events);
    topicApprovalRequired(events);
    session.turnPhase = "generating";
    session.activity = "generating";

    const w = mount(TurnProcess, {
      props: {
        turnId: "turn_1",
        items: [],
        stages: [],
        facts: null,
        running: true,
        queued: false,
      },
      global: { plugins: [pinia] },
    });
    await nextTick();

    const status = w.find('[data-test="turn-process-status"]').text();
    expect(status).toContain("正在等待你选择话题");
    expect(status).not.toContain("运行中");
    w.unmount();
  });

  it("没有过程区时全局状态条说同一句话，不显示成「正在生成」", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const events = useEventStore();
    startTurn(session, events);
    topicApprovalRequired(events);
    session.turnPhase = "generating";
    session.activity = "generating";

    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    await nextTick();
    await nextTick();

    const typing = w.find(".typing");
    expect(typing.exists()).toBe(true);
    expect(typing.text()).toContain("正在等待你选择话题");
    expect(typing.text()).not.toContain("正在生成");
    w.unmount();
  });
});

describe("草稿跟随：确认切换/新建成功后", () => {
  it("等待期间的草稿仍留输入框并归属确认后的话题，不自动发送", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicApprovalRequired(events, { targetTopicId: "t2" });
    writeWaitingDraft(session, "等待期间写的话");

    approvalResult(events, id, "approved");
    anchor(events, "t2");

    expect(session.currentTopicId).toBe("t2");
    expect(session.draft).toBe("等待期间写的话");
    // 草稿归属确认后的话题（旧话题的副本不留下）
    expect(storedText("t2")).toBe("等待期间写的话");
    expect(storedText("t1")).toBeNull();
    // 不自动发送：等用户主动发送，发送归属是确认后的话题
    expect(api.sendTurn).not.toHaveBeenCalled();
    expect(session.sendAttribution().topicId).toBe("t2");
  });

  it("确认新建成功后草稿跟到新话题（目标话题由 ANCHOR 给出）", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicApprovalRequired(events, { kind: "topic_create", newName: "新话题" });
    writeWaitingDraft(session, "新建后还要发的话");

    approvalResult(events, id, "approved");
    anchor(events, "t-new", "新话题");

    expect(session.currentTopicId).toBe("t-new");
    expect(session.draft).toBe("新建后还要发的话");
    expect(storedText("t-new")).toBe("新建后还要发的话");
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("目标话题已有草稿：不静默覆盖，原稿登记为可恢复的被让位草稿", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const result = writeDraft(chatKey("t2"), "目标话题原有的草稿", 1);
    expect(result.ok).toBe(true);

    const id = topicApprovalRequired(events, { targetTopicId: "t2" });
    writeWaitingDraft(session, "等待期间写的话");
    approvalResult(events, id, "approved");
    anchor(events, "t2");

    // 输入框显示的是等待期间的新稿（不拼接、不覆盖成旧稿也不被旧稿顶掉）
    expect(session.draft).toBe("等待期间写的话");
    const displaced = session.displacedDraftsForTopic("t2");
    expect(displaced.some((record) => record.text === "目标话题原有的草稿")).toBe(true);
    expect(displaced.every((record) => record.text !== "等待期间写的话")).toBe(true);

    // 目标原有草稿仍然可恢复：清空输入框后能把它取回
    session.draft = "";
    const restored = session.restoreDisplacedDraft(displaced[0]?.id);
    expect(restored.ok).toBe(true);
    expect(session.draft).toBe("目标话题原有的草稿");
  });
});

describe("拒绝/取消/失败：草稿留在原话题", () => {
  it("拒绝后草稿留在原话题，不丢、不提前改归属", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicApprovalRequired(events, {});
    writeWaitingDraft(session, "留在原话题继续");

    approvalResult(events, id, "rejected");

    expect(session.currentTopicId).toBe("t1");
    expect(session.draft).toBe("留在原话题继续");
    expect(storedText("t1")).toBe("留在原话题继续");
    expect(session.displacedDraftsForTopic("t2")).toHaveLength(0);
    expect(session.topicApprovalPending).toBe(false);
  });

  it("本轮结束仍未确认（无 ANCHOR）：草稿不丢，之后普通导航也不带走", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicApprovalRequired(events, { targetTopicId: "t2" });
    writeWaitingDraft(session, "没切成也要留着");
    approvalResult(events, id, "approved");

    // 权威队列显示服务端已空闲：一次性跟随收口（TURN_END 丢帧也一样）
    events.route(
      ev("TURN_QUEUE", {
        instance_id: null,
        revision: 1,
        running: null,
        queued: [],
        cancelled: [],
      }),
    );

    // 普通导航到别的话题：草稿按话题各存各的（不跟着跑）
    session.setAnchor("t3", null, "另一个话题", null, false);
    expect(session.draft).toBe("");
    session.setAnchor("t1", null, "当前话题", null, false);
    expect(session.draft).toBe("没切成也要留着");
    expect(storedText("t1")).toBe("没切成也要留着");
    expect(api.sendTurn).not.toHaveBeenCalled();
  });

  it("执行失败（failed）不移动归属；下一次普通导航不搬草稿", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicApprovalRequired(events, { targetTopicId: "t2" });
    writeWaitingDraft(session, "失败也留在原话题");
    approvalResult(events, id, "failed");

    session.setAnchor("t3", null, "另一个话题", null, false);
    expect(session.draft).toBe("");
    expect(storedText("t1")).toBe("失败也留在原话题");
  });
});

describe("重复/乱序事件幂等", () => {
  it("重复 APPROVAL_REQUIRED / approved / ANCHOR：不重复搬草稿、不改归属", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicApprovalRequired(events, { targetTopicId: "t2" });
    // 同一申请重复到达（重连 / 轮询恢复）
    topicApprovalRequired(events, { approvalId: id, targetTopicId: "t2" });
    expect(session._topicApplications.filter((item) => item.approvalId === id)).toHaveLength(1);

    writeWaitingDraft(session, "只搬一次");
    approvalResult(events, id, "approved");
    approvalResult(events, id, "approved");
    anchor(events, "t2");
    anchor(events, "t2", "目标话题");

    expect(session.currentTopicId).toBe("t2");
    expect(session.draft).toBe("只搬一次");
    expect(storedText("t2")).toBe("只搬一次");
    expect(storedText("t1")).toBeNull();
    // 跟随已消费：再导航到别的话题不会再带走
    session.setAnchor("t3", null, "另一个话题", null, false);
    expect(session.draft).toBe("");
  });

  it("executed 结局不会在跟随已消费后重新武装（不残留跨导航标记）", () => {
    const { session, events } = setup();
    startTurn(session, events);
    const id = topicApprovalRequired(events, { targetTopicId: "t2" });
    writeWaitingDraft(session, "跟着过去");
    approvalResult(events, id, "approved");
    anchor(events, "t2");
    // 执行结果事件晚到（legacy executed 带顶层 kind）
    events.route(
      ev("APPROVAL_RESULT", {
        approval_id: id,
        phase: "executed",
        decision: "approved",
        turn_id: "turn_1",
        kind: "topic_switch",
        ok: true,
      }),
    );

    // 跟随已经消费过：executed 晚到不会重新武装，普通导航不带走新写的草稿
    expect(session._topicDraftFollow).toBeNull();
    session.draft = "切换之后新写的字";
    session.flushDraft();
    session.setAnchor("t3", null, "另一个话题", null, false);
    expect(session.draft).toBe("");
    expect(storedText("t2")).toBe("切换之后新写的字");
  });
});
