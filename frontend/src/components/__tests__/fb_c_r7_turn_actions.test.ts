/**
 * R7 反例（fb-c）：终态动作的渲染与接线 —— retry 创建新 turn、resend 只对 interrupted、
 * cancelled + resend 防御性归一、绝不自动启动。
 *
 * 契约 K3（docs/plans/2026-10-10-final-boundaries-r1-r7.md）：
 * * retry：既有发送接口创建新 turn（同话题、retry_of_turn_id = 原轮），只有用户点击才执行；
 * * 重复点击只产生一个新 turn；
 * * resend 仅 interrupted（一键一次性 claim）；
 * * 历史 status === cancelled 且 actions 含 resend 的旧记录，实时与读路径都归一到 retry；
 *   interrupted（reason_code = "interrupted"）保留 resend。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/fb_c_r7_turn_actions.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

import TurnProcess from "../TurnProcess.vue";
import { resetProcessState } from "../../stores/turnProcess";
import { useEventStore } from "../../stores/events";
import { useSessionStore, type TurnFacts } from "../../stores/session";

const { getSessionContext, sendTurn, resendInterruptedTurn } = vi.hoisted(() => ({
  getSessionContext: vi.fn(),
  sendTurn: vi.fn(async () => ({
    ok: true,
    accepted: true,
    turn_id: "turn_new",
    status: "accepted",
    topic_id: "topic_1",
  })),
  resendInterruptedTurn: vi.fn(async () => ({ ok: true })),
}));

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext,
    sendTurn,
    resendInterruptedTurn,
    getRuntimeState: vi.fn(async () => ({
      instance_id: "inst_r7",
      revision: 1,
      turn_queue: { instance_id: "inst_r7", revision: 1, running: null, queued: [], cancelled: [] },
      approvals: [],
      tasks: [],
      tools: [],
      narratives: [],
    })),
    getTrace: vi.fn(async () => ({ turn_id: "turn_1", duration_ms: 0, phases: {} })),
    getUISettings: vi.fn(async () => ({ typewriter_cps: 50 })),
  },
  ApiError: class ApiError extends Error {},
}));

function facts(partial: Partial<TurnFacts> = {}): TurnFacts {
  const base: TurnFacts = {
    turnId: "turn_1",
    status: "cancelled",
    durationMs: 1200,
    queueMs: null,
    startedAt: null,
    endedAt: null,
    reason: "你停止了这一轮",
    reasonCode: "user_stopped",
    stoppedBy: "user",
    actions: ["retry"],
    errorText: null,
  };
  return { ...base, ...partial };
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore(), pinia };
}

function withUserMessage(session: ReturnType<typeof useSessionStore>, turnId: string, text: string) {
  session.pushUser(text);
  const m = session.messages[session.messages.length - 1];
  if (m) m.turnId = turnId;
  return m;
}

function mountProcess(
  session: ReturnType<typeof useSessionStore>,
  pinia: ReturnType<typeof createPinia>,
  props: Record<string, unknown> = {},
) {
  return mount(TurnProcess, {
    props: { turnId: "turn_1", items: [], stages: [], facts: null, running: false, ...props },
    global: { plugins: [pinia], stubs: { MarkdownContent: true } },
  });
}

beforeEach(() => {
  resetProcessState();
  localStorage.clear();
  getSessionContext.mockReset();
  sendTurn.mockClear();
  resendInterruptedTurn.mockClear();
  getSessionContext.mockResolvedValue({
    topic_id: "topic_1",
    topic_name: "默认话题",
    anchor_fragment: null,
    messages: [],
    tool_records: [],
    turn_facts: [],
    has_more: false,
    next_before: null,
  });
});

describe("R7：retry 接线（创建新 turn，绝不自动启动）", () => {
  it("渲染本身不启动任何请求；点击 retry 才用既有发送接口新建一轮", async () => {
    const { session, pinia } = setup();
    session.currentTopicId = "topic_1";
    withUserMessage(session, "turn_1", "帮我核对实现");
    const w = mountProcess(session, pinia, { turnId: "turn_1", facts: facts() });
    await nextTick();

    expect(sendTurn, "只是渲染，绝不自启动").not.toHaveBeenCalled();
    expect(resendInterruptedTurn).not.toHaveBeenCalled();

    const retry = w.find("[data-test='turn-process-action-retry']");
    expect(retry.exists()).toBe(true);
    await retry.trigger("click");
    await flushPromises();

    expect(sendTurn).toHaveBeenCalledTimes(1);
    expect(sendTurn).toHaveBeenCalledWith("帮我核对实现", "topic_1", [], "turn_1");
    expect(resendInterruptedTurn, "cancelled 不再走 resend（那条路必然 409）").not.toHaveBeenCalled();
    w.unmount();
  });

  it("重复点击只产生一个新 turn", async () => {
    const { session, pinia } = setup();
    session.currentTopicId = "topic_1";
    withUserMessage(session, "turn_1", "再试一次");
    let release: (value: unknown) => void = () => undefined;
    sendTurn.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }) as never,
    );
    const w = mountProcess(session, pinia, { turnId: "turn_1", facts: facts() });
    await nextTick();

    const retry = w.find("[data-test='turn-process-action-retry']");
    await retry.trigger("click");
    await retry.trigger("click");
    await retry.trigger("click");
    expect(sendTurn).toHaveBeenCalledTimes(1);
    release({ ok: true, accepted: true, turn_id: "turn_new", status: "accepted" });
    await flushPromises();
    expect(sendTurn).toHaveBeenCalledTimes(1);
    w.unmount();
  });

  it("排队取消（status=cancelled + retry）：按钮可用，且不影响正在跑的 active", async () => {
    const { session, pinia } = setup();
    session.currentTopicId = "topic_1";
    withUserMessage(session, "turn_1", "排队后被取消的那条");
    session.activeTurnId = "turn_other";
    session.turnRunning = true;
    const w = mountProcess(session, pinia, { turnId: "turn_1", facts: facts({ queueMs: 321 }) });
    await nextTick();

    await w.find("[data-test='turn-process-action-retry']").trigger("click");
    await flushPromises();
    expect(sendTurn).toHaveBeenCalledTimes(1);
    expect(session.activeTurnId, "点重试不改变当前 active 归属").toBe("turn_other");
    w.unmount();
  });
});

describe("R7：resend 只对 interrupted；cancelled + resend 防御性归一", () => {
  it("interrupted 保留 resend，点击走一次性 claim 接口", async () => {
    const { session, pinia } = setup();
    withUserMessage(session, "turn_1", "进程中断的那条");
    const w = mountProcess(session, pinia, {
      turnId: "turn_1",
      facts: facts({
        status: "cancelled",
        reason: "程序在这次回答结束前中断了。",
        reasonCode: "interrupted",
        stoppedBy: "system",
        actions: ["resend"],
      }),
    });
    await nextTick();

    const resend = w.find("[data-test='turn-process-action-resend']");
    expect(resend.exists()).toBe(true);
    expect(w.find("[data-test='turn-process-action-retry']").exists()).toBe(false);
    await resend.trigger("click");
    await flushPromises();
    expect(resendInterruptedTurn).toHaveBeenCalledWith("turn_1");
    expect(sendTurn, "resend 不走发送接口").not.toHaveBeenCalled();
    w.unmount();
  });

  it("cancelled + user_stopped + 旧 actions=[resend]：渲染时归一到 retry，不出死按钮", async () => {
    const { session, pinia } = setup();
    session.currentTopicId = "topic_1";
    withUserMessage(session, "turn_1", "旧历史里的取消轮");
    const w = mountProcess(session, pinia, {
      turnId: "turn_1",
      facts: facts({ actions: ["resend"] }),
    });
    await nextTick();

    expect(w.find("[data-test='turn-process-action-resend']").exists()).toBe(false);
    const retry = w.find("[data-test='turn-process-action-retry']");
    expect(retry.exists()).toBe(true);
    await retry.trigger("click");
    await flushPromises();
    expect(sendTurn).toHaveBeenCalledTimes(1);
    expect(resendInterruptedTurn).not.toHaveBeenCalled();
    w.unmount();
  });
});

describe("R7：结束事实的动作在实时与读路径都归一", () => {
  function routeCancel(events: ReturnType<typeof useEventStore>, actions: unknown) {
    events.route({ type: "TURN_START", id: "s1", ts: "", data: { turn_id: "turn_c", revision: 1 } });
    events.route({
      type: "TURN_END",
      id: "s2",
      ts: "",
      data: {
        turn_id: "turn_c",
        status: "cancelled",
        reason_code: "user_stopped",
        reason: "你按下了停止。",
        stopped_by: "user",
        actions,
        revision: 2,
      },
    });
  }

  it("实时 cancelled + user_stopped + resend -> retry", () => {
    const { events, session } = setup();
    routeCancel(events, ["resend"]);
    const factsOfTurn = session.factsFor("turn_c");
    expect(factsOfTurn?.actions).toEqual(["retry"]);
    expect(factsOfTurn?.actions).not.toContain("resend");
  });

  it("实时 interrupted 保留 resend", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "s1", ts: "", data: { turn_id: "turn_i", revision: 1 } });
    events.route({
      type: "TURN_END",
      id: "s2",
      ts: "",
      data: {
        turn_id: "turn_i",
        status: "cancelled",
        reason_code: "interrupted",
        reason: "程序在这次回答结束前中断了。",
        stopped_by: "system",
        actions: ["resend"],
        revision: 2,
      },
    });
    expect(session.factsFor("turn_i")?.actions).toEqual(["resend"]);
  });

  it("历史旧记录（turn_facts）cancelled + resend -> retry；interrupted 保留 resend", async () => {
    const { session } = setup();
    getSessionContext.mockResolvedValueOnce({
      topic_id: "topic_1",
      topic_name: "默认话题",
      anchor_fragment: null,
      messages: [
        {
          id: "m_u1",
          role: "user",
          content: "旧取消轮",
          content_type: "text",
          created_at: "2026-10-09T05:00:00+00:00",
          turn_id: "turn_old",
        },
        {
          id: "m_u2",
          role: "user",
          content: "旧中断轮",
          content_type: "text",
          created_at: "2026-10-09T06:00:00+00:00",
          turn_id: "turn_int",
        },
      ],
      tool_records: [],
      turn_facts: [
        {
          turn_id: "turn_old",
          status: "cancelled",
          reason_code: "user_stopped",
          reason: "你停止了这一轮",
          stopped_by: "user",
          actions: ["resend"],
        },
        {
          turn_id: "turn_int",
          status: "cancelled",
          reason_code: "interrupted",
          reason: "程序中断",
          stopped_by: "system",
          actions: ["resend"],
        },
      ],
      has_more: false,
      next_before: null,
    });

    await session.loadHistory();

    expect(session.factsFor("turn_old")?.actions, "旧 cancelled 归一成 retry").toEqual(["retry"]);
    expect(session.factsFor("turn_int")?.actions, "interrupted 保留 resend").toEqual(["resend"]);
  });

  it("localStorage 留痕里的旧 cancelled+resend 也要归一（刷新兜底路径）", () => {
    localStorage.setItem(
      "qio.turnFacts",
      JSON.stringify({
        turn_cached: {
          turnId: "turn_cached",
          status: "cancelled",
          durationMs: null,
          queueMs: null,
          startedAt: null,
          endedAt: null,
          reason: "你停止了这一轮",
          reasonCode: "user_stopped",
          stoppedBy: "user",
          actions: ["resend"],
          errorText: null,
        },
      }),
    );
    const { session } = setup();
    expect(session.factsFor("turn_cached")?.actions).toEqual(["retry"]);
  });
});
