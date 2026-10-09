/**
 * 契约 6 状态组反例：契约 5 的两条时序（Agent F 只写验证）。
 *
 * 断言的是**修复后的期望**；在未修复的 session.ts 基线上应当红（反例证据），
 * Lead 集成后在集成 worktree 重跑同一规格做前后对照。
 *
 * 反例 A：TURN_START → TURN_END(completed) 先到，HTTP 受理回执后到。
 *   修复后期望：回执是「受理」不是「复活」——不得把 turnRunning 拉回 true，
 *   不得把 turnPhase 从 idle 重置回 waiting/generating。
 *
 * 反例 B：TURN_START（事件）已到 → send 请求超时失败。
 *   修复后期望：该轮已真实开始——不得撤回乐观消息、不得误置
 *   turnRunning=false、activeTurnId 必须保留。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../stores/session";
import { useEventStore } from "../stores/events";

const h = vi.hoisted(() => {
  let gate: {
    promise: Promise<unknown>;
    resolve: (v: unknown) => void;
    reject: (e: unknown) => void;
  } | null = null;

  function makeGate() {
    let resolve!: (v: unknown) => void;
    let reject!: (e: unknown) => void;
    const promise = new Promise<unknown>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    gate = { promise, resolve, reject };
    return gate;
  }

  return { get gate() { return gate; }, makeGate };
});

vi.mock("../services/api", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  const sendTurn = vi.fn(() => {
    const g = h.makeGate();
    return g.promise;
  });
  // 兜底：集成后的 session.ts 可能调用「按 client_request_id 查证」等新增 api 成员。
  // 这里给出形状无害的默认应答（可被单测覆写），避免因 mock 缺导出而崩溃。
  const fallback = new Proxy({ sendTurn } as Record<string, unknown>, {
    get(target, prop, recv) {
      if (prop in target) return Reflect.get(target, prop, recv);
      return vi.fn(async () => ({ ok: true }));
    },
  });
  return {
    ...actual,
    api: fallback,
  };
});

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { session: useSessionStore(), events: useEventStore() };
}

function route(events: ReturnType<typeof useEventStore>, type: string, data: Record<string, unknown>) {
  events.route({ type, id: `evt_${type}_${String(data.turn_id ?? "")}`, ts: "", data });
}

describe("契约 5 反例 A：TURN_START→TURN_END 先到，受理回执后到", () => {
  it("晚到的受理回执不得复活已结束的轮（turnRunning/turnPhase 不被重置）", async () => {
    const { session, events } = setup();
    const sending = session.send("第一条消息");
    expect(sending).toBeInstanceOf(Promise);

    // SSE 先到：这一轮已经真实开始并结束。
    route(events, "TURN_START", { turn_id: "turn_x", revision: 1 });
    expect(session.activeTurnId).toBe("turn_x");
    route(events, "TURN_END", { turn_id: "turn_x", status: "completed" });
    expect(session.turnRunning).toBe(false);
    expect(session.turnPhase).toBe("idle");

    // HTTP 受理回执这才慢吞吞地回来。
    h.gate?.resolve({
      ok: true,
      accepted: true,
      turn_id: "turn_x",
      status: "completed",
      topic_id: null,
    });
    await sending;
    await flushPromises();

    expect(session.turnRunning).toBe(false);
    expect(session.turnPhase).toBe("idle");
    expect(session.messages.some((m) => m.content === "第一条消息")).toBe(true);
  });
});

describe("契约 5 反例 B：TURN_START 已到，send 请求超时失败", () => {
  it("不撤回消息、不误置 turnRunning=false、activeTurnId 保留", async () => {
    const { session, events } = setup();
    const sending = session.send("第二条消息");
    while (!h.gate) await flushPromises();

    // 该轮已真实开始（事件是唯一权威）。
    route(events, "TURN_START", { turn_id: "turn_b", revision: 2 });
    expect(session.activeTurnId).toBe("turn_b");
    expect(session.turnRunning).toBe(true);

    // HTTP 请求超时失败。
    h.gate.reject(new Error("请求超时（mock: network lost）"));
    await sending;
    await flushPromises();

    expect(session.messages.some((m) => m.content === "第二条消息")).toBe(true);
    expect(session.turnRunning).toBe(true);
    expect(session.activeTurnId).toBe("turn_b");
  });
});