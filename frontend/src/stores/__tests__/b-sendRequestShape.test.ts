/**
 * 子 agent B 定向验收（F02 补充）：**真实请求体**里的发送快照语义。
 *
 * 这里**不 mock services/api**：走真实的 request 路径，用假 fetch 闸门看真正发出去的
 * JSON body。要证的是两件只有在这一层才看得见的事：
 * 1. `attachment_ids: []` 必须是**存在的空数组**（字段缺失 = 旧客户端 = 后端遗留兜底绑附件）；
 * 2. 同一次发送的所有重试复用**同一个** client_request_id，且原文 / 话题 / 附件列表
 *    与第一次完全一致（不读当前输入框、当前话题或后来加入的附件）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  flushAsyncTimers,
  installFakeFetch,
  statusResponse,
  setupSession,
  type FakeFetchGate,
} from "../../__tests__/sr-e-helpers";

let gate: FakeFetchGate;

function bodyOf(index: number): Record<string, unknown> {
  const posts = gate.callsMatching("POST", "/api/turns");
  return posts[index].body as Record<string, unknown>;
}

beforeEach(() => {
  localStorage.clear();
  gate = installFakeFetch();
  // 网络层失败：请求根本到不了后端（受理情况未知）
  gate.respond((url, init) => {
    if (init.method === "POST" && url.endsWith("/api/turns")) {
      return Promise.reject(new TypeError("Failed to fetch"));
    }
    if (init.method === "GET" && url.includes("/api/turns/by-request/")) {
      return statusResponse(404, { ok: false, unknown: true });
    }
    return undefined;
  });
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe("F02：提交快照在真实请求体上的语义", () => {
  it("显式无附件：第一次与同身份重试都发出存在的空数组", async () => {
    vi.useFakeTimers();
    const { session } = setupSession();
    session.currentTopicId = "A";
    expect(await session.send("这条消息没有附件", [])).toBe(false);

    const first = bodyOf(0);
    expect(
      Object.prototype.hasOwnProperty.call(first, "attachment_ids"),
      "attachment_ids 字段缺失：后端会走遗留兜底，把话题下遗留的未绑定附件绑上来",
    ).toBe(true);
    expect(first.attachment_ids).toEqual([]);
    expect(first.topic_id).toBe("A");

    await session.retrySendAttempt(session.pendingSends[0].id);
    const second = bodyOf(1);
    expect(second.client_request_id, "重试换了新的请求身份").toBe(first.client_request_id);
    expect(second.attachment_ids, "重试把显式空列表变没了").toEqual([]);
    expect(second.topic_id).toBe(first.topic_id);
    expect(second.message).toBe(first.message);
  });

  it("有附件 + 发送期间切话题：重试原样复用提交那一刻的三元组", async () => {
    vi.useFakeTimers();
    const { session } = setupSession();
    session.currentTopicId = "A";
    expect(await session.send("带两个附件的原文", ["att_1", "att_2"])).toBe(false);

    const first = bodyOf(0);
    expect(first.attachment_ids).toEqual(["att_1", "att_2"]);

    // 发送期间用户切到 B（真实请求语义一个字都不许变）
    session.currentTopicId = "B";
    await session.retrySendAttempt(session.pendingSends[0].id);
    const second = bodyOf(1);
    expect(second.message).toBe("带两个附件的原文");
    expect(second.topic_id, "重试跟着当前话题走了").toBe("A");
    expect(second.attachment_ids).toEqual(["att_1", "att_2"]);
    expect(second.client_request_id).toBe(first.client_request_id);
  });

  it("查证用的就是提交那一刻的请求身份（GET /api/turns/by-request/{id}）", async () => {
    vi.useFakeTimers();
    const { session } = setupSession();
    session.currentTopicId = "A";
    expect(await session.send("查证要看同一条请求", [])).toBe(false);
    const requestId = bodyOf(0).client_request_id as string;

    await session.recheckPendingSend();
    const lookups = gate.calls.filter(
      (call) => call.method === "GET" && call.url.includes("/api/turns/by-request/"),
    );
    expect(lookups.length).toBeGreaterThanOrEqual(1);
    expect(decodeURIComponent(lookups[lookups.length - 1].url.split("/").pop() as string)).toBe(requestId);
    expect(session.pendingSends.length, "404 之后未决记录必须保留").toBe(1);
  });
});
