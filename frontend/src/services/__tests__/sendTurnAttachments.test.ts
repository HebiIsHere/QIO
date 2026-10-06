/**
 * sendTurn 的附件字段（问题 3）：attachment_ids 的**存在性**即语义。
 *
 * 契约：只要调用方给了附件列表（**包括空数组**），请求体里就必须带 attachment_ids；
 * 只有完全不传第三个参数（旧客户端）才省略字段，让后端走兜底。
 * 修复前 api.ts 写的是 attachmentIds?.length ? {...} : {} —— 空数组被省略，
 * 后端于是把话题下的遗留附件绑到这条纯文字消息上。
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const backendMock = vi.hoisted(() => ({
  resolveBackend: vi.fn(async () => ({ base: "http://127.0.0.1:1", token: "t0ken" })),
  authHeaders: vi.fn((token: string) => ({ Authorization: "Bearer " + token })),
  resetBackend: vi.fn(),
}));
vi.mock("../backend", () => backendMock);

import { api } from "../api";

const fetchMock = vi.fn();

function okResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    text: async () => JSON.stringify(body),
    json: async () => body,
  } as unknown as Response;
}

function lastBody(): Record<string, unknown> {
  const init = fetchMock.mock.calls[0][1] as RequestInit;
  return JSON.parse(String(init.body)) as Record<string, unknown>;
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockResolvedValue(okResponse({ ok: true, accepted: true, turn_id: "turn_1", status: "running" }));
});

describe("sendTurn 的 attachment_ids（存在性即语义）", () => {
  it("空数组也要带字段：这一轮没有附件，不是「没给这个字段」", async () => {
    await api.sendTurn("纯文字", "t1", []);

    const body = lastBody();
    expect(body.message).toBe("纯文字");
    expect(body.topic_id).toBe("t1");
    expect(Object.prototype.hasOwnProperty.call(body, "attachment_ids")).toBe(true);
    expect(body.attachment_ids).toEqual([]);
  });

  it("有附件时按原顺序原样带上", async () => {
    await api.sendTurn("带附件", "t1", ["att_1", "att_2"]);
    expect(lastBody().attachment_ids).toEqual(["att_1", "att_2"]);
  });

  it("完全不传第三参（旧客户端路径）才省略字段，交给后端兜底", async () => {
    await api.sendTurn("旧客户端");
    expect(Object.prototype.hasOwnProperty.call(lastBody(), "attachment_ids")).toBe(false);
  });
});
