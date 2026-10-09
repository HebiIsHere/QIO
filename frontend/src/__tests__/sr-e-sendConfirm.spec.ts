/**
 * 契约 5（Agent E）· 发送回执丢失后的「确认」生命周期。
 *
 * 反例 B（修复前为红）：TURN_START 已到 → send 请求超时失败时，旧实现会
 * 撤掉乐观消息、把 turnRunning 误置 false —— 实际正在执行的轮次被界面
 * 说成「没有发出去」。
 * 修复后：超时 / 网络失败进入「正在确认」，不撤消息、不动运行态、不自动重发；
 * 稍后用同一 client_request_id 查证 GET /api/turns/by-request/{id}。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { API_TIMEOUT_MS } from "../services/api";
import {
  flushAsyncTimers,
  hang,
  installFakeFetch,
  okResponse,
  setupSession,
  statusResponse,
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

describe("契约 5 · 反例 B：TURN_START 已到，发送请求超时", () => {
  it("不撤消息、turnRunning 不被误置 false、activeTurnId 保留、进入「正在确认」", async () => {
    beforeEachImpl();
    const { session, events } = setupSession();
    vi.useFakeTimers();
    gate.respond((url, init) => {
      // POST 挂着 → 真实超时路径（20s 写超时后 abort）
      if (init.method === "POST" && url.endsWith("/api/turns")) return hang(init.signal);
      // 本用例只验证「进入正在确认」：查证也挂住，避免自动收尾干扰断言
      if (init.method === "GET" && url.includes("/api/turns/by-request/")) return hang(init.signal);
      return undefined;
    });

    const pending = session.send("消息B");
    await flushAsyncTimers(0);

    // TURN_START 先到：这一轮真实地开始了
    events.route({ type: "TURN_START", id: "s1", ts: "", data: { turn_id: "turn_x", revision: 1 } });
    expect(session.activeTurnId).toBe("turn_x");
    expect(session.turnRunning).toBe(true);

    // 请求超时（写超时预算之后 abort）
    await flushAsyncTimers(API_TIMEOUT_MS.write + 1000);
    expect(await pending).toBe(false);

    // ← 反例 B 的关键断言
    expect(session.messages.some((m) => m.content === "消息B")).toBe(true); // 不撤消息
    expect(session.turnRunning).toBe(true); // 运行态不回退
    expect(session.activeTurnId).toBe("turn_x"); // activeTurnId 保留
    expect(session.sendRejectedSeq).toBe(0); // 不是「已拒绝」
    const attempt = session.sendAttempts[0];
    expect(attempt.state).toBe("needs-confirm");
    expect(session.sendConfirm?.messageId).toBe(attempt.messageId);
    expect(session.sendConfirm?.notice).toContain("正在确认");
    // 没有自动建第二条任务：POST 只发过一次
    expect(gate.callsMatching("POST", "/api/turns").length).toBe(1);
  });
});

describe("契约 5 · 查证（GET /api/turns/by-request/{id}）", () => {
  /** 反例 B 的公共前缀：POST 超时 + TURN_START 已到，返回进入「正在确认」的 store */
  async function timedOutSend() {
    const { session, events } = setupSession();
    vi.useFakeTimers();
    // 查证响应由测试控制：先挂住，等测试明确放行再返回
    let releaseLookup!: (payload: unknown) => void;
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) return hang(init.signal);
      if (init.method === "GET" && url.includes("/api/turns/by-request/")) {
        return new Promise((resolve) => {
          releaseLookup = (payload: unknown) => resolve(okResponse(payload));
        });
      }
      return undefined;
    });
    const pending = session.send("消息B");
    await flushAsyncTimers(0);
    events.route({ type: "TURN_START", id: "s1", ts: "", data: { turn_id: "turn_x", revision: 1 } });
    await flushAsyncTimers(API_TIMEOUT_MS.write + 1000);
    await pending;
    return { session, releaseLookup };
  }

  it("查证命中：采纳 turn 状态，不再重复执行；已受理则不提供「放弃」", async () => {
    beforeEachImpl();
    const { session, releaseLookup } = await timedOutSend();
    const attempt = session.sendAttempts[0];
    expect(attempt.state).toBe("needs-confirm");

    // 延迟数百毫秒后的自动查证：命中（响应此刻才放行）
    releaseLookup({ turn_id: "turn_x", status: "running" });
    await flushAsyncTimers(700);

    expect(attempt.state).toBe("accepted");
    expect(attempt.turnId).toBe("turn_x");
    const msg = session.messages.find((m) => m.content === "消息B");
    expect(msg?.turnId).toBe("turn_x");
    expect(session.sendConfirm).toBeNull();
    expect(session.turnRunning).toBe(true);
    expect(session.activeTurnId).toBe("turn_x");
    // 已受理：不提供「放弃」（放弃被拒绝）
    expect(session.abandonSendAttempt(attempt.messageId)).toBe(false);
    expect(session.messages.some((m) => m.content === "消息B")).toBe(true);
  });

  it("查证 404：呈现「发送未确认」（不等于「未发送」），提供重试 / 放弃", async () => {
    beforeEachImpl();
    const { session } = setupSession();
    vi.useFakeTimers();
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) {
        return Promise.reject(new TypeError("Failed to fetch"));
      }
      if (init.method === "GET" && url.includes("/api/turns/by-request/")) {
        return statusResponse(404, { ok: false, unknown: true });
      }
      return undefined;
    });
    expect(await session.send("没送达的消息")).toBe(false);
    // 不撤消息
    expect(session.messages.some((m) => m.content === "没送达的消息")).toBe(true);

    await flushAsyncTimers(700);

    const attempt = session.sendAttempts[0];
    expect(attempt.state).toBe("needs-confirm");
    expect(session.sendConfirm?.notice).toBe(
      "发送未确认：后端没有该请求记录（可能未送达）",
    );
    expect(session.sendConfirm?.unknown).toBe(true);
    // 后端无记录、也没有 TURN_START 到过 → 乐观的「运行中」收敛，但消息保留
    expect(session.turnRunning).toBe(false);
    expect(session.turnPhase).toBe("idle");
    expect(session.messages.some((m) => m.content === "没送达的消息")).toBe(true);

    // 放弃：撤回消息 + 恢复草稿
    expect(session.abandonSendAttempt(attempt.messageId)).toBe(true);
    expect(session.messages.some((m) => m.content === "没送达的消息")).toBe(false);
    expect(session.draft).toBe("没送达的消息");
    expect(attempt.state).toBe("confirmed-rejected");
  });

  it("查证失败（网络）：保持「正在确认」，可以再查一次", async () => {
    beforeEachImpl();
    const { session } = setupSession();
    vi.useFakeTimers();
    let lookupOk = false;
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) {
        return Promise.reject(new TypeError("Failed to fetch"));
      }
      if (init.method === "GET" && url.includes("/api/turns/by-request/")) {
        return lookupOk
          ? okResponse({ turn_id: "turn_x", status: "accepted" })
          : Promise.reject(new TypeError("Failed to fetch"));
      }
      return undefined;
    });
    expect(await session.send("再查一次的消息")).toBe(false);

    await flushAsyncTimers(700);

    const attempt = session.sendAttempts[0];
    expect(attempt.state).toBe("needs-confirm");
    expect(session.sendConfirm?.notice).toContain("正在确认");
    expect(session.sendConfirm?.notice).toContain("可以再查一次");
    expect(session.messages.some((m) => m.content === "再查一次的消息")).toBe(true);

    // 再查一次：这次命中
    lookupOk = true;
    await session.recheckSendAttempt(attempt.messageId);
    expect(attempt.state).toBe("accepted");
    expect(attempt.turnId).toBe("turn_x");
    expect(session.sendConfirm).toBeNull();
  });
});

describe("契约 5 · 幂等重试与串联恢复", () => {
  it("待确认下重试：复用同一个 client_request_id 重新 POST（幂等），命中即采纳", async () => {
    beforeEachImpl();
    const { session } = setupSession();
    vi.useFakeTimers();
    let postCount = 0;
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) {
        postCount += 1;
        if (postCount === 1) return Promise.reject(new TypeError("Failed to fetch"));
        return okResponse({ ok: true, turn_id: "turn_x", status: "accepted", deduplicated: true });
      }
      if (init.method === "GET" && url.includes("/api/turns/by-request/")) {
        return statusResponse(404, { ok: false, unknown: true });
      }
      return undefined;
    });
    expect(await session.send("幂等的消息")).toBe(false);
    await flushAsyncTimers(700);
    const attempt = session.sendAttempts[0];
    expect(attempt.state).toBe("needs-confirm");

    expect(await session.retrySendAttempt(attempt.messageId)).toBe(true);

    const posts = gate.callsMatching("POST", "/api/turns");
    expect(posts.length).toBe(2);
    const id1 = (posts[0].body as Record<string, unknown>)?.client_request_id;
    const id2 = (posts[1].body as Record<string, unknown>)?.client_request_id;
    expect(typeof id1).toBe("string");
    // ← 关键：重试不换 id（幂等）
    expect(id2).toBe(id1);
    expect(attempt.state).toBe("accepted");
    expect(attempt.turnId).toBe("turn_x");
    // 不重复执行、不重复展示
    expect(session.messages.filter((m) => m.content === "幂等的消息").length).toBe(1);
  });

  it("串联：已受理但 HTTP 回执与 SSE 都暂时丢失 → 稍后查证恢复真实状态", async () => {
    beforeEachImpl();
    const { session, events } = setupSession();
    vi.useFakeTimers();
    // 查证响应由测试控制：稍后才放行（命中已受理）
    let releaseLookup!: (payload: unknown) => void;
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) return hang(init.signal);
      if (init.method === "GET" && url.includes("/api/turns/by-request/")) {
        return new Promise((resolve) => {
          releaseLookup = (payload: unknown) => resolve(okResponse(payload));
        });
      }
      return undefined;
    });

    const pending = session.send("丢失回执的消息");
    await flushAsyncTimers(0);
    // SSE 也没到：此刻没有任何 TURN_START
    expect(session.activeTurnId).toBeNull();

    await flushAsyncTimers(API_TIMEOUT_MS.write + 1000);
    expect(await pending).toBe(false);
    expect(session.sendAttempts[0].state).toBe("needs-confirm");
    expect(session.messages.some((m) => m.content === "丢失回执的消息")).toBe(true);

    // 稍后查证：命中已受理（响应此刻才放行）
    releaseLookup({ turn_id: "turn_x", status: "accepted" });
    await flushAsyncTimers(700);
    const attempt = session.sendAttempts[0];
    expect(attempt.state).toBe("accepted");
    expect(attempt.turnId).toBe("turn_x");

    // SSE 恢复：TURN_START 这才到达 → 状态照常推进，与已采纳的事实一致
    events.route({ type: "TURN_START", id: "s2", ts: "", data: { turn_id: "turn_x", revision: 2 } });
    expect(session.activeTurnId).toBe("turn_x");
    expect(session.turnRunning).toBe(true);
    events.route({ type: "TURN_END", id: "e2", ts: "", data: { turn_id: "turn_x", status: "completed" } });
    expect(session.turnRunning).toBe(false);
    expect(session.turnPhase).toBe("idle");
    expect(
      session.messages.some((m) => m.content === "丢失回执的消息" && m.turnId === "turn_x"),
    ).toBe(true);
  });

  it("查证命中后提供的是「取消」：cancelConfirmedSend 走取消通道", async () => {
    beforeEachImpl();
    const { session } = setupSession();
    vi.useFakeTimers();
    let cancelCalled = "";
    let releaseLookup!: (payload: unknown) => void;
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) {
        return Promise.reject(new TypeError("Failed to fetch"));
      }
      if (init.method === "GET" && url.includes("/api/turns/by-request/")) {
        return new Promise((resolve) => {
          releaseLookup = (payload: unknown) => resolve(okResponse(payload));
        });
      }
      if (init.method === "POST" && url.includes("/api/turns/turn_x/cancel")) {
        cancelCalled = url;
        return okResponse({ ok: true, cancelled: true, turn_id: "turn_x" });
      }
      return undefined;
    });
    expect(await session.send("要取消的消息")).toBe(false);
    await flushAsyncTimers(700); // 查证发出并挂住（测试控制响应时序）
    releaseLookup({ turn_id: "turn_x", status: "running" }); // 放行：命中
    await flushAsyncTimers(0);
    expect(session.sendAttempts[0].state).toBe("accepted");

    expect(await session.cancelConfirmedSend(session.sendAttempts[0].messageId)).toBe(true);
    expect(cancelCalled).toContain("/api/turns/turn_x/cancel");
  });
});
