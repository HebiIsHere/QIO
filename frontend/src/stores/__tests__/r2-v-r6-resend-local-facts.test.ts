/**
 * V 组独立验证：R6 前端联动（成功重发后本机留痕不得再暴露 resend）。
 *
 * 只改后端是修不干净的：过程区渲染读的是本机留痕 qio.turnFacts。
 * 断言：成功才收口（留痕 + 未完成入口），失败保留可重试；刷新以后端事实为准。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({ resendInterruptedTurn: vi.fn() }));

vi.mock("../../services/api", () => {
  class ApiError extends Error {}
  const base: Record<string, unknown> = {
    ApiError,
    resendInterruptedTurn: mocks.resendInterruptedTurn,
  };
  return {
    api: new Proxy(base, {
      get(target, prop: string) {
        if (prop in target) return target[prop];
        return vi.fn(async () => ({}));
      },
    }),
  };
});

import { useSessionStore, type TurnFacts } from "../session";

const TURN = "turn_v_r6_fe";
const KEY = "qio.turnFacts";

function facts(overrides: Partial<TurnFacts> = {}): TurnFacts {
  return {
    turnId: TURN,
    status: "interrupted",
    durationMs: null,
    queueMs: null,
    startedAt: null,
    endedAt: null,
    reason: "这条消息执行到一半，进程退出后没有完成",
    reasonCode: "interrupted",
    stoppedBy: null,
    actions: ["resend"],
    errorText: null,
    ...overrides,
  };
}

function interruptedRow() {
  return {
    turn_id: TURN,
    message: "用户的原话",
    topic_id: "topic_1",
    status: "interrupted",
    reason: "running_at_restart",
    reason_text: "这条消息执行到一半，进程退出后没有完成",
    created_at: "2026-10-10T00:00:00+00:00",
  };
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  mocks.resendInterruptedTurn.mockReset();
});

describe("R6 前端：成功重发后收口本机留痕", () => {
  it("成功：turnFacts 不再有 resend、interruptedTurns 清掉、持久化同步", async () => {
    const store = useSessionStore();
    store.turnFacts = { [TURN]: facts() };
    store.interruptedTurns = [interruptedRow()] as unknown as typeof store.interruptedTurns;
    localStorage.setItem(KEY, JSON.stringify({ [TURN]: facts() }));

    mocks.resendInterruptedTurn.mockResolvedValue({ ok: true });
    const ok = await store.resendTurn(TURN);

    expect(ok).toBe(true);
    expect(store.turnFacts[TURN]?.actions, "本机留痕不得再暴露 resend").toEqual([]);
    expect(store.interruptedTurns.map((item) => item.turn_id)).not.toContain(TURN);
    const cached = JSON.parse(localStorage.getItem(KEY) ?? "{}") as Record<
      string,
      { actions?: string[] }
    >;
    expect(cached[TURN]?.actions ?? [], "持久化留痕也不得再暴露 resend").not.toContain("resend");
  });

  it("失败（409 / 网络）：保留留痕与入口，可重试", async () => {
    const store = useSessionStore();
    store.turnFacts = { [TURN]: facts() };
    store.interruptedTurns = [interruptedRow()] as unknown as typeof store.interruptedTurns;

    mocks.resendInterruptedTurn.mockRejectedValue(new Error("这一条已经被处理过了"));
    const ok = await store.resendTurn(TURN);

    expect(ok).toBe(false);
    expect(store.turnFacts[TURN]?.actions, "失败不得动留痕").toEqual(["resend"]);
    expect(store.interruptedTurns.map((item) => item.turn_id)).toContain(TURN);
    expect(store.turnActionFeedback[TURN] ?? "", "必须如实说明失败并可重试").toContain("可以重试");
  });

  it("刷新恢复（读缓存）时也归一：cancelled + resend 变成 retry，不是死按钮", () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({
        [TURN]: facts({ status: "cancelled", reasonCode: "user_stopped", actions: ["resend"] }),
      }),
    );
    const store = useSessionStore();
    expect(store.turnFacts[TURN]?.actions).toEqual(["retry"]);
  });
});
