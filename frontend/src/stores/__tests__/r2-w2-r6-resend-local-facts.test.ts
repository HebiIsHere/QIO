/**
 * R6 前端联动（W2）中断轮次成功重发后，本机留痕不得再暴露 resend（round2）。
 *
 * 根因：`session.resendTurn()` 成功后只清 turnActionBusy，不碰本机事实留痕（qio.turnFacts）；
 * 于是刚重发成功的那一轮，过程区仍然挂着一个「重新发送」按钮 —— 再点必然 409。
 *
 * 冻结规则：resendTurn 成功后，该轮的本地留痕不得再暴露 `resend`，并从 interruptedTurns 清掉；
 * 失败（409 等）不得吞掉：留痕与入口都保留，可重试。刷新后以后端事实为准（后端由 W4 修）。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/r2-w2-r6-resend-local-facts.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import type { AgentEvent } from "../../services/events";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";

const { getSessionContext, resendInterruptedTurn, sendTurn } = vi.hoisted(() => ({
  getSessionContext: vi.fn(async () => ({
    topic_id: "topic_1",
    topic_name: null,
    anchor_fragment: null,
    messages: [],
    turn_facts: [] as Record<string, unknown>[],
  })),
  resendInterruptedTurn: vi.fn(async () => ({
    ok: true,
    recovered_turn_id: "turn_1",
    turn_id: "turn_new",
    status: "queued",
  })),
  sendTurn: vi.fn(async () => ({
    ok: true,
    accepted: true,
    turn_id: "turn_new",
    status: "accepted",
    topic_id: "topic_1",
  })),
}));

vi.mock("../../services/api", () => ({
  api: { getSessionContext, resendInterruptedTurn, sendTurn },
  ApiError: class ApiError extends Error {},
}));

let seq = 0;
function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: "evt_" + seq, ts: new Date().toISOString(), data };
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

/** 造一轮「中断 + 只给了 resend」的终态事实（后端恢复入口此时的真实形状）。 */
function recordInterrupted(events: ReturnType<typeof useEventStore>, session: ReturnType<typeof useSessionStore>) {
  events.route(ev("TURN_START", { turn_id: "turn_1", revision: 1 }));
  events.route(
    ev("TURN_END", {
      turn_id: "turn_1",
      status: "interrupted",
      reason_code: "interrupted",
      reason: "这一轮在进程退出时被中断了",
      stopped_by: "system",
      actions: ["resend"],
    }),
  );
  session.interruptedTurns = [{ turn_id: "turn_1", message: "原文", status: "interrupted" }];
}

const FACTS_KEY = "qio.turnFacts";

beforeEach(() => {
  seq = 0;
  localStorage.clear();
  getSessionContext.mockClear();
  resendInterruptedTurn.mockClear();
  sendTurn.mockClear();
});

describe("R6 resendTurn 成功后的本机留痕", () => {
  it("成功后该轮不再暴露 resend，并从 interruptedTurns 清掉；刷新读同一份留痕也不暴露", async () => {
    const { events, session } = setup();
    recordInterrupted(events, session);
    expect(session.factsFor("turn_1")?.actions, "前置：中断轮本来就有 resend").toEqual(["resend"]);

    const ok = await session.resendTurn("turn_1");
    expect(ok).toBe(true);
    expect(resendInterruptedTurn).toHaveBeenCalledWith("turn_1");

    expect(session.factsFor("turn_1")?.actions, "重发成功后本地留痕仍暴露 resend").not.toContain("resend");
    expect(session.interruptedTurns.map((t) => t.turn_id), "重发成功后入口没有清掉").toEqual([]);

    // 持久化留痕同样不许带 resend
    const persisted = JSON.parse(localStorage.getItem(FACTS_KEY) ?? "{}") as Record<
      string,
      { actions?: string[] }
    >;
    expect(persisted["turn_1"]?.actions ?? [], "持久化留痕仍带 resend").not.toContain("resend");

    // 刷新（新 pinia 实例读同一份 localStorage）也不得再暴露
    setActivePinia(createPinia());
    const refreshed = useSessionStore();
    expect(refreshed.factsFor("turn_1")?.actions ?? [], "刷新后本机留痕仍带 resend").not.toContain("resend");
  });

  it("409 失败不得吞掉：留痕与入口都保留，还能重试", async () => {
    const { events, session } = setup();
    recordInterrupted(events, session);
    resendInterruptedTurn.mockRejectedValueOnce(
      Object.assign(new Error("POST /api/turns/turn_1/resend -> 409: already claimed"), { status: 409 }),
    );

    const ok = await session.resendTurn("turn_1");
    expect(ok).toBe(false);
    expect(session.factsFor("turn_1")?.actions, "失败却把 resend 抹掉了").toContain("resend");
    expect(session.interruptedTurns.map((t) => t.turn_id), "失败却把入口清掉了").toEqual(["turn_1"]);
    expect(session.turnActionFeedback["turn_1"] ?? "").toContain("没有成功");
  });
});
