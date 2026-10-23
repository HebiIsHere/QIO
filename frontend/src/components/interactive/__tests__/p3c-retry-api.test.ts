/**
 * Phase 3-C：重试系列 API 兼容层（纯归一化 + 接口未上线）。
 *
 * 钉住的行为：
 * - 字段缺失 → null（不补 0 / 不编造上限）；
 * - 容忍 snake_case / camelCase 与 series / retries / items 三种数组键；
 * - 服务端 404/405/501 → available=false 的**可读原因**（不是空列表、不是失败）；
 * - 手动重试只把服务端 ok 当成功，不把「没有回执」说成成功。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  resolveBackend: vi.fn(async () => ({ base: "http://127.0.0.1:1", token: "tk", source: "default" })),
  authHeaders: vi.fn((token: string) => (token ? { Authorization: "Bearer " + token } : {})),
}));

vi.mock("../../../services/backend", () => ({
  resolveBackend: mocks.resolveBackend,
  authHeaders: mocks.authHeaders,
}));

import {
  fetchRetrySeries,
  normalizeManualRetryResult,
  normalizeRetryEnvelope,
  normalizeRetrySeries,
  requestManualRetry,
  retryAttemptsText,
  retryInFlight,
  retryStatusLabel,
} from "../v2/retryApi";

beforeEach(() => {
  mocks.resolveBackend.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("归一化：只保留服务端事实", () => {
  it("缺字段保持 null（不补 0、不编造上限）", () => {
    const series = normalizeRetrySeries({ series_id: "rs", intent_id: "i1", status: "failed" });
    expect(series).not.toBeNull();
    expect(series!.attempts).toBeNull();
    expect(series!.limit).toBeNull();
    expect(series!.lastError).toBeNull();
    expect(series!.nextAction).toBeNull();
    expect(retryAttemptsText(series!)).toContain("未记录");
    expect(retryAttemptsText(series!)).not.toContain("0 /");
  });

  it("容忍 camelCase 与 series/retries/items 三种数组键", () => {
    const camel = normalizeRetrySeries({
      seriesId: "rs2",
      intentId: "i2",
      attempts: 2,
      limit: 3,
      status: "retrying",
      lastError: "网络中断",
      nextAction: "稍后自动重试",
      updatedAt: "2026-10-24T00:00:00Z",
    });
    expect(camel).toMatchObject({ seriesId: "rs2", attempts: 2, limit: 3, lastError: "网络中断" });

    for (const key of ["series", "retries", "items"]) {
      const envelope = normalizeRetryEnvelope({ available: true, [key]: [{ series_id: "rs3", intent_id: "i3" }] });
      expect(envelope.available).toBe(true);
      expect(envelope.series).toHaveLength(1);
    }
    expect(normalizeRetryEnvelope([{ series_id: "s", intent_id: "i" }]).series).toHaveLength(1);
  });

  it("缺 series_id / intent_id 的记录被丢弃（身份不明不展示）", () => {
    expect(normalizeRetrySeries({ status: "failed" })).toBeNull();
    expect(normalizeRetryEnvelope({ available: true, series: [{ intent_id: "i" }] }).series).toHaveLength(0);
  });

  it("手动重试回执：只有 ok===true 才算成功", () => {
    expect(normalizeManualRetryResult({ ok: true, reason: "已受理" }).ok).toBe(true);
    expect(normalizeManualRetryResult({ ok: "true" }).ok).toBe(false);
    expect(normalizeManualRetryResult(null).ok).toBe(false);
  });

  it("状态文案与在飞判定：未知状态原样显示", () => {
    expect(retryStatusLabel("exhausted")).toContain("用尽");
    expect(retryStatusLabel("weird_state")).toContain("weird_state");
    expect(retryInFlight("retrying")).toBe(true);
    expect(retryInFlight("exhausted")).toBe(false);
  });
});

describe("真实调用：接口未上线要如实说明", () => {
  function respond(status: number, body: unknown): void {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: status >= 200 && status < 300,
        status,
        json: async () => body,
        text: async () => JSON.stringify(body),
      })),
    );
  }

  it("404 → available=false 且原因说清「未上线」，不是空列表", async () => {
    respond(404, { detail: "Not Found" });
    const envelope = await fetchRetrySeries({ boardId: "b1" });
    expect(envelope.available).toBe(false);
    expect(envelope.reason).toContain("未上线");
    expect(envelope.series).toEqual([]);
  });

  it("200 → 走真实端点并归一化；请求带认证头", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ available: true, series: [{ series_id: "rs", intent_id: "i1", attempts: 1, limit: 3 }] }),
      text: async () => "",
    }));
    vi.stubGlobal("fetch", fetchMock);
    const envelope = await fetchRetrySeries({ intentId: "i1" });
    expect(envelope.series[0]).toMatchObject({ seriesId: "rs", attempts: 1, limit: 3 });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/api/interactive/intents/i1/retry-series");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tk");
  });

  it("手动重试：POST 一次并原样带回执", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, reason: "已受理" }),
      text: async () => "",
    }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await requestManualRetry("i1", { boardId: "b1", seriesId: "rs", expectedAuthorizationVersion: "v2" });
    expect(result).toMatchObject({ ok: true, reason: "已受理" });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/api/interactive/intents/i1/retry");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      boardId: "b1",
      seriesId: "rs",
      expectedAuthorizationVersion: "v2",
    });
  });

  it("5xx / 网络错误 → 抛出可读错误（不当成没有记录）", async () => {
    respond(500, { reason: "内部错误" });
    await expect(fetchRetrySeries({ boardId: "b1" })).rejects.toThrow();
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("ECONNREFUSED"); }));
    await expect(fetchRetrySeries({ boardId: "b1" })).rejects.toThrow("没有连上本机后端");
  });
});
