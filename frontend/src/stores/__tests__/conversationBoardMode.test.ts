/**
 * Stage 2：普通对话请求默认携带板面上下文（mode=board）。
 *
 * 走真实 request 路径 + 假 fetch 闸门（不 mock services/api），看真正发出去的 JSON body：
 * 1. 普通对话（Composer / session.send）默认 mode=board，且不带别的板面身份；
 * 2. 显式 interactive（互动板 ChatDock 的续作绑定）原样保留，不被默认值改坏；
 * 3. 受理情况未知后的同身份重试仍然复用快照里的 mode=board（不因重试丢板面上下文）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  flushAsyncTimers,
  installFakeFetch,
  okResponse,
  setupSession,
  type FakeFetchGate,
} from "../../__tests__/sr-e-helpers";

let gate: FakeFetchGate;

function acceptedTurn(): unknown {
  return okResponse({
    ok: true,
    accepted: true,
    cancelled: false,
    turn_id: "turn_board_1",
    status: "accepted",
    topic_id: "A",
    receipt: {
      state: "accepted",
      accepted: true,
      cancelled: false,
      pending: false,
      reason: null,
      deduped: false,
      turn_id: "turn_board_1",
      status: "accepted",
      attachment_ids: [],
    },
  });
}

function posts(): Record<string, unknown>[] {
  return gate.callsMatching("POST", "/api/turns").map((call) => call.body as Record<string, unknown>);
}

beforeEach(() => {
  localStorage.clear();
  gate = installFakeFetch();
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe("普通对话默认携带板面上下文（mode=board）", () => {
  it("普通发送：body 里有 mode=board，且没有板面 / 提交身份（后端用默认板面）", async () => {
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) return acceptedTurn();
      return undefined;
    });
    const { session } = setupSession();
    session.currentTopicId = "A";
    await session.send("你好 QIO");

    const body = posts()[0];
    expect(body.mode).toBe("board");
    expect(Object.prototype.hasOwnProperty.call(body, "boardId")).toBe(false);
    expect(Object.prototype.hasOwnProperty.call(body, "submissionId")).toBe(false);
  });

  it("显式 interactive（互动板续作绑定）原样保留：不被默认 board 覆盖", async () => {
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) return acceptedTurn();
      return undefined;
    });
    const { session } = setupSession();
    session.currentTopicId = "A";
    await session.send("继续整理", [], [], {
      interactive: {
        mode: "interactive",
        boardId: "board_1",
        submissionId: "sub_1",
      },
    });

    const body = posts()[0];
    expect(body.mode).toBe("interactive");
    expect(body.boardId).toBe("board_1");
    expect(body.submissionId).toBe("sub_1");
  });

  it("收不到受理回执后的同身份重试：仍然带 mode=board（快照语义不丢）", async () => {
    vi.useFakeTimers();
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) {
        return Promise.reject(new TypeError("Failed to fetch"));
      }
      if (init.method === "GET" && url.includes("/api/turns/by-request/")) {
        return okResponse({ ok: false, unknown: true });
      }
      return undefined;
    });
    const { session } = setupSession();
    session.currentTopicId = "A";
    expect(await session.send("会断网的消息", [])).toBe(false);
    const pending = session.pendingSends[0];
    expect(pending, "网络失败后必须留下未决记录").toBeTruthy();

    // 恢复网络后用同一个请求身份重试
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) return acceptedTurn();
      return undefined;
    });
    await session.retrySendAttempt(pending.id);
    await flushAsyncTimers();

    const bodies = posts();
    expect(bodies.length).toBeGreaterThanOrEqual(2);
    const first = bodies[0];
    const retried = bodies[bodies.length - 1];
    expect(first.mode).toBe("board");
    expect(retried.mode).toBe("board");
    // 同一次发送动作：同一个请求身份
    expect(retried.client_request_id).toBe(first.client_request_id);
  });

  it("重开应用后：本机恢复的未决发送快照仍带 mode=board（重试不丢板面上下文）", async () => {
    vi.useFakeTimers();
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) {
        return Promise.reject(new TypeError("Failed to fetch"));
      }
      if (init.method === "GET" && url.includes("/api/turns/by-request/")) {
        return okResponse({ ok: false, unknown: true });
      }
      return undefined;
    });
    const first = setupSession();
    first.session.currentTopicId = "A";
    expect(await first.session.send("断网的消息", [])).toBe(false);
    expect(first.session.pendingSends.length).toBe(1);

    // 模拟重开应用：新的 pinia = 新的 store，从本机存储读回未决发送
    const second = setupSession();
    const restored = second.session.pendingSends[0];
    expect(restored, "重开之后未决发送必须还在").toBeTruthy();
    expect(restored.snapshot.interactive).toEqual({ mode: "board" });
  });
});
