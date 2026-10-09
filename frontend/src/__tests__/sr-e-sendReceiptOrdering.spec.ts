/**
 * 契约 5（Agent E）· 发送回执的单向生命周期。
 *
 * 反例 A（修复前为红）：TURN_START → TURN_END 先于 HTTP 受理回执到达时，
 * 旧实现会在回执处把 turnRunning 拉回 true、turnPhase 拉回 waiting ——
 * 用户刚看着回答结束，界面却重新变回「运行中」。
 * 修复后：受理回执只能 pending → accepted 单向落定，绝不写运行态。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  flushMicrotasks,
  installFakeFetch,
  okResponse,
  setupSession,
  type FakeFetchGate,
} from "./sr-e-helpers";

let gate: FakeFetchGate;

/** 每个用例开头调用：装上本用例自己的假 fetch 闸门 */
const beforeEachImpl = () => {
  gate = installFakeFetch();
};

afterEach(() => {
  vi.useRealTimers();
});

describe("契约 5 · 反例 A：受理回执晚于 TURN_START→TURN_END 到达", () => {
  it("回执不得把 turnRunning 拉回 true，也不得把 turnPhase 拉回 waiting", async () => {
    beforeEachImpl();
    const { session, events } = setupSession();
    let release!: (payload: unknown) => void;
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) {
        return new Promise((resolve) => {
          release = (payload: unknown) => resolve(okResponse(payload));
        });
      }
      return undefined;
    });

    const pending = session.send("回执迟到的一条消息");
    await flushMicrotasks(); // 等 resolveBackend 的微任务链跑完，POST 已在飞

    // 事件先到：这一轮开始并正常结束
    events.route({ type: "TURN_START", id: "s1", ts: "", data: { turn_id: "turn_x", revision: 1 } });
    events.route({ type: "TURN_END", id: "e1", ts: "", data: { turn_id: "turn_x", status: "completed" } });
    expect(session.turnRunning).toBe(false);
    expect(session.turnPhase).toBe("idle");

    // HTTP 受理回执现在才回来
    release({ ok: true, turn_id: "turn_x", status: "accepted" });
    expect(await pending).toBe(true);

    // ← 反例 A 的关键断言：受理回执不得覆盖已经结束的运行态
    expect(session.turnRunning).toBe(false);
    expect(session.turnPhase).toBe("idle");
    expect(session.activeTurnId).toBeNull();

    // 但受理事实本身不丢：attempt 落定为 accepted，turn_id 关联到乐观消息
    const attempt = session.sendAttempts[0];
    expect(attempt.state).toBe("accepted");
    expect(attempt.turnId).toBe("turn_x");
    const msg = session.messages.find((m) => m.content === "回执迟到的一条消息");
    expect(msg?.turnId).toBe("turn_x");
    expect(session.sendRejectedSeq).toBe(0);
    expect(session.sendConfirm).toBeNull();
  });

  it("幂等命中（deduplicated）的回执同样只落定一次，不重复执行、不重复展示", async () => {
    beforeEachImpl();
    const { session } = setupSession();
    let release!: (payload: unknown) => void;
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) {
        return new Promise((resolve) => {
          release = (payload: unknown) => resolve(okResponse(payload));
        });
      }
      return undefined;
    });

    const pending = session.send("幂等命中的消息");
    await flushMicrotasks();
    release({ ok: true, turn_id: "turn_x", status: "accepted", deduplicated: true });
    expect(await pending).toBe(true);

    const attempt = session.sendAttempts[0];
    expect(attempt.state).toBe("accepted");
    expect(attempt.turnId).toBe("turn_x");
    expect(session.messages.filter((m) => m.content === "幂等命中的消息").length).toBe(1);
    expect(session.sendConfirm).toBeNull();
  });
});

describe("契约 5 · 既有发送体验不回归", () => {
  it("正常受理：仍是「等待执行」状态；POST body 携带 client_request_id", async () => {
    beforeEachImpl();
    const { session } = setupSession();
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) {
        return okResponse({ ok: true, turn_id: "turn_n", status: "accepted" });
      }
      return undefined;
    });

    const ok = await session.send("正常发送");

    expect(ok).toBe(true);
    expect(session.turnRunning).toBe(true);
    expect(session.turnPhase).toBe("waiting");
    expect(session.activeTurnId).toBeNull(); // 受理 ≠ 开始执行（既有约束）
    expect(session.sendAttempts[0].state).toBe("accepted");
    expect(session.messages[session.messages.length - 1]?.turnId).toBe("turn_n");

    const posts = gate.callsMatching("POST", "/api/turns");
    expect(posts.length).toBe(1);
    const requestId = (posts[0].body as Record<string, unknown>)?.client_request_id;
    expect(typeof requestId).toBe("string");
    expect(String(requestId).length).toBeGreaterThan(0);
    expect(session.sendRejectedSeq).toBe(0);
  });

  it("运行中再发送（排队）：回执只登记排队，不动 active（既有语义不回归）", async () => {
    beforeEachImpl();
    const { session, events } = setupSession();
    events.route({ type: "TURN_START", id: "s1", ts: "", data: { turn_id: "turn_a", revision: 1 } });
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) {
        return okResponse({ ok: true, turn_id: "turn_b", status: "queued" });
      }
      return undefined;
    });

    await session.send("第二条");

    expect(session.activeTurnId).toBe("turn_a");
    expect(session.queuedTurnIds).toContain("turn_b");
    expect(session.turnRunning).toBe(true);
    const attempt = session.sendAttempts[0];
    expect(attempt.state).toBe("accepted");
    expect(attempt.queued).toBe(true);
  });

  it("明确 4xx：撤回乐观消息 + 明确失败文案（与「正在确认」区分开）", async () => {
    beforeEachImpl();
    const { session } = setupSession();
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) {
        return {
          ok: false,
          status: 400,
          headers: { get: () => "application/json" },
          json: async () => ({ detail: "bad request" }),
          text: async () => JSON.stringify({ detail: "bad request" }),
        };
      }
      return undefined;
    });

    const ok = await session.send("会被拒绝的消息");

    expect(ok).toBe(false);
    // 撤回乐观消息（现行为）
    expect(session.messages.some((m) => m.content === "会被拒绝的消息")).toBe(false);
    expect(session.sendRejectedSeq).toBe(1);
    // 文案是「明确失败：被拒绝」，不是「正在确认」
    expect(session.lastError).toContain("发送失败：被拒绝");
    expect(session.sendConfirm).toBeNull();
    const attempt = session.sendAttempts[0];
    expect(attempt.state).toBe("confirmed-rejected");
  });
});
