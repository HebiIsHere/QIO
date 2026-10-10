/**
 * D 独立验收：轮次结束事实（审计问题 7 的前端一半 / plan §1.2）。
 *
 * 契约来源：docs/plans/2026-10-06-audit-seven-fixes.md §0 第 7 条 + §1.2。
 * 只依据产品规则：
 *
 *   TURN_END.data 增加 reason_code / reason / stopped_by / actions；
 *   前端按 turn_id 记进 TurnFacts（历史分页与 RESYNC 快照同样带回）；
 *   旧记录没有这些字段 → **不伪造原因**，只显示原有状态词；
 *   可恢复的单次工具错误不等于整轮失败；已知耗时不受失败明细影响。
 *
 * 基线（e428bb9）现状：TurnFacts（session.ts:111-118）只有
 * status/durationMs/queueMs/startedAt/endedAt，recordTurnFacts 只取这几个字段 ——
 * 因此本文件在修复前应当是**红的**。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/TurnEndFacts.audit.verify.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "",
      topic_name: null,
      anchor_fragment: null,
      messages: [],
    })),
    sendTurn: vi.fn(async () => ({})),
  },
}));

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

/** 把一条 TURN_END 送进事件通道（前端唯一入口）。 */
function end(events: ReturnType<typeof useEventStore>, turnId: string, data: Record<string, unknown>) {
  events.route({ type: "TURN_END", id: "end_" + turnId + "_" + Math.random(), ts: "", data: { turn_id: turnId, ...data } });
}

/** 允许实现用不同字段名承载同一事实，但语义必须齐全（不能什么都没有）。 */
function factsOf(session: ReturnType<typeof useSessionStore>, turnId: string): Record<string, unknown> {
  const facts = session.factsFor(turnId) as unknown as Record<string, unknown> | null;
  expect(facts, "TURN_END 之后必须按 turn_id 记下这一轮的事实").not.toBeNull();
  return facts as Record<string, unknown>;
}

function pick(facts: Record<string, unknown>, ...names: string[]): unknown {
  for (const name of names) {
    if (facts[name] !== undefined) return facts[name];
  }
  return undefined;
}

beforeEach(() => {
  setActivePinia(createPinia());
});

describe("契约 §1.2：结束事实必须落到这一轮", () => {
  it("厂商失败：reason_code / reason / stopped_by / actions 都记进 TurnFacts", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "s1", ts: "", data: { turn_id: "turn_a" } });
    end(events, "turn_a", {
      status: "failed",
      reason_code: "provider_error",
      reason: "厂商返回 500，这一轮没有拿到回答。",
      stopped_by: "system",
      actions: ["retry"],
      duration_ms: 1200,
      queue_ms: 3,
    });

    const facts = factsOf(session, "turn_a");
    expect(String(pick(facts, "status"))).toBe("failed");
    expect(pick(facts, "reasonCode", "reason_code"), "必须记下 reason_code").toBe("provider_error");
    expect(String(pick(facts, "reason")), "必须记下一句话原因").toContain("厂商返回 500");
    expect(pick(facts, "stoppedBy", "stopped_by"), "必须记下是谁停的").toBe("system");
    const actions = pick(facts, "actions");
    expect(Array.isArray(actions) ? actions : [], "必须记下当前可用的操作").toContain("retry");
    expect(pick(facts, "durationMs", "duration_ms"), "已知耗时不受失败明细影响").toBe(1200);
  });

  it("原因属于正确的轮次：并发两轮不串", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "s1", ts: "", data: { turn_id: "turn_a" } });
    end(events, "turn_a", {
      status: "failed",
      reason_code: "provider_error",
      reason: "A 轮失败了",
      stopped_by: "system",
      actions: ["retry"],
    });
    end(events, "turn_b", {
      status: "completed",
      reason_code: "none",
      reason: "",
      stopped_by: null,
      actions: [],
    });

    const a = factsOf(session, "turn_a");
    const b = factsOf(session, "turn_b");
    expect(String(pick(a, "reason"))).toContain("A 轮失败了");
    expect(String(pick(b, "reason") ?? ""), "B 轮没有失败，不得继承 A 轮的原因").not.toContain("A 轮失败了");
    expect(pick(b, "reasonCode", "reason_code")).toBe("none");
    expect(pick(a, "reasonCode", "reason_code")).toBe("provider_error");
  });

  it("用户停止：说成用户停止，不是厂商故障；旧动作 resend 归一到 retry", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "s1", ts: "", data: { turn_id: "turn_stop" } });
    end(events, "turn_stop", {
      status: "cancelled",
      reason_code: "user_stopped",
      reason: "你按下了停止。",
      stopped_by: "user",
      actions: ["resend"],
    });

    const facts = factsOf(session, "turn_stop");
    expect(pick(facts, "reasonCode", "reason_code")).toBe("user_stopped");
    expect(pick(facts, "stoppedBy", "stopped_by")).toBe("user");
    /**
     * 契约 K3.1 / K3.4（Lead 裁定，2026-10-10）：user_stopped 的可用动作是 retry；
     * cancelled + resend 是点不通的死按钮（/api/turns/{id}/resend 只接受 interrupted 的行），
     * 实时与读路径都要归一。
     */
    const actions = (pick(facts, "actions") as string[]) ?? [];
    expect(actions).toContain("retry");
    expect(actions).not.toContain("resend");
  });

  it("真正的中断（interrupted）：保留 resend 入口，不被归一成 retry", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "s2", ts: "", data: { turn_id: "turn_int" } });
    end(events, "turn_int", {
      status: "cancelled",
      reason_code: "interrupted",
      reason: "程序在这次回答结束前中断了。",
      stopped_by: "system",
      actions: ["resend"],
    });

    const facts = factsOf(session, "turn_int");
    expect(pick(facts, "reasonCode", "reason_code")).toBe("interrupted");
    const actions = (pick(facts, "actions") as string[]) ?? [];
    expect(actions).toContain("resend");
    expect(actions, "interrupted 不走 retry 归一").not.toContain("retry");
  });

  it("可恢复的工具错误不是整轮失败：completed + reason_code=none 不得显示失败原因", () => {
    const { events, session } = setup();
    events.route({ type: "TURN_START", id: "s1", ts: "", data: { turn_id: "turn_tool" } });
    end(events, "turn_tool", {
      status: "completed",
      reason_code: "none",
      reason: "",
      stopped_by: null,
      actions: [],
      duration_ms: 800,
    });

    const facts = factsOf(session, "turn_tool");
    expect(String(pick(facts, "status"))).toBe("completed");
    expect(pick(facts, "reasonCode", "reason_code")).toBe("none");
    const reason = pick(facts, "reason");
    expect(reason === null || reason === undefined || String(reason).trim() === "", (
      "整轮没有失败就不能给一个失败原因（不得编造）"
    )).toBe(true);
    const actions = pick(facts, "actions");
    expect(Array.isArray(actions) ? actions.length : 0, "没有失败就不该有重试入口").toBe(0);
  });

  it("旧历史（没有这些字段）不得伪造原因，状态语义不变", () => {
    const { events, session } = setup();
    end(events, "turn_legacy", { status: "completed", final_content: "旧记录的回答", duration_ms: 500 });

    const facts = factsOf(session, "turn_legacy");
    expect(String(pick(facts, "status"))).toBe("completed");
    const reason = pick(facts, "reason");
    expect(reason === null || reason === undefined || String(reason).trim() === "", (
      "旧记录没有原因 → 就显示原有状态词，绝不伪造一句原因"
    )).toBe(true);
    const code = pick(facts, "reasonCode", "reason_code");
    expect(code === null || code === undefined || code === "", (
      "旧记录没有 reason_code → 不得自动填一个（例如 unknown）"
    )).toBe(true);
    const actions = pick(facts, "actions");
    expect(Array.isArray(actions) ? actions.length : 0, "旧记录没有操作 → 不得凭空给一个入口").toBe(0);
  });
});
