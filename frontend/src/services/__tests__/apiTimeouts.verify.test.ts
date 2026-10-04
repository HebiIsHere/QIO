/**
 * 独立验证（验证方维护，`*.verify.test.ts`）：**超时语义与「写操作不得自动重复提交」**
 * （契约 WS1 §5 / 验收清单第 4 条的后半句）。
 *
 * 契约要求：
 *   * 每类请求有**自己的超时**，超时后如实报错（可重试的提示）；
 *   * 读类可以**有限重试**（默认 1 次）；
 *   * **写操作永远不自动重试** —— 超时只说明「没等到响应」，操作可能已经生效，
 *     自动重发就是重复提交。
 *
 * 断言只依据行为（fetch 被调用几次、最终抛什么），不依赖内部实现细节。
 * 超时用假定时器推进，所以不需要真的等 15/20 秒。
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { api, API_TIMEOUT_MS, ApiTimeoutError } from "../../services/api";

interface Call {
  url: string;
  init: RequestInit & { signal?: AbortSignal };
}

let calls: Call[] = [];
/** 前 N 次调用「挂着不返回」（用来触发超时），之后的调用成功返回 */
let hangFirst = 0;
let payload: unknown = { tasks: [] };

function okResponse(): unknown {
  return {
    ok: true,
    status: 200,
    headers: { get: () => "application/json" },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
}

function hang(init: { signal?: AbortSignal }): Promise<never> {
  return new Promise((_resolve, reject) => {
    const signal = init.signal;
    if (!signal) return; // 没有 signal 就永远挂着（本用例不会走到）
    if (signal.aborted) {
      reject(new Error("aborted"));
      return;
    }
    signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
  });
}

beforeEach(() => {
  calls = [];
  hangFirst = 0;
  payload = { tasks: [] };
  (globalThis as unknown as { fetch: unknown }).fetch = (url: unknown, init: Call["init"] = {}) => {
    calls.push({ url: String(url), init });
    if (hangFirst > 0) {
      hangFirst -= 1;
      return hang(init);
    }
    return Promise.resolve(okResponse());
  };
});

afterEach(() => {
  vi.useRealTimers();
});

describe("WS1 §5：超时后「读可有限重试 / 写绝不重发」", () => {
  it("写操作超时：只发一次请求，绝不自动重复提交，并抛出可重试的超时错误", async () => {
    vi.useFakeTimers();
    hangFirst = 1; // 第一次挂着 → 超时
    const pending = api.sendTurn("你好").catch((err) => err);

    // 第一次 0 推进：让 `await resolveBackend()` 的微任务链跑完，超时定时器才被排上
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(API_TIMEOUT_MS.write + 1000);
    const error = await pending;

    expect(calls.length).toBe(1); // ← 关键：没有第二次请求
    expect(error instanceof ApiTimeoutError).toBe(true);
    expect(String((error as Error).message)).toMatch(/可以重试|没有响应/);
  });

  it("读操作超时：允许重试一次，第二次成功就返回结果", async () => {
    vi.useFakeTimers();
    hangFirst = 1;
    payload = { tasks: [{ id: "t1" }] };
    const pending = api.getDevTasks();

    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(API_TIMEOUT_MS.read + 1000); // 第一次超时 → 触发重试
    const data = (await pending) as { tasks: unknown[] };

    expect(calls.length).toBe(2);
    expect(data.tasks.length).toBe(1);
  });

  it("读操作网络层失败：允许重试一次（fetch 两次）", async () => {
    let first = true;
    (globalThis as unknown as { fetch: unknown }).fetch = (url: unknown, init: Call["init"] = {}) => {
      calls.push({ url: String(url), init });
      if (first) {
        first = false;
        return Promise.reject(new TypeError("Failed to fetch"));
      }
      return Promise.resolve(okResponse());
    };

    await api.getDevTasks();
    expect(calls.length).toBe(2);
  });

  it("写操作网络层失败：**不**重试（fetch 一次），如实抛出", async () => {
    (globalThis as unknown as { fetch: unknown }).fetch = (url: unknown, init: Call["init"] = {}) => {
      calls.push({ url: String(url), init });
      return Promise.reject(new TypeError("Failed to fetch"));
    };

    const error = await api.sendTurn("你好").catch((err) => err);
    expect(calls.length).toBe(1); // ← 写操作不重发
    expect(error instanceof TypeError).toBe(true);
  });
});

describe("WS1 §5：超时预算是有区分的常量（不是「一个很大的数」）", () => {
  it("读 / 写 / 长操作的超时预算各自明确，且写 ≥ 读", () => {
    expect(API_TIMEOUT_MS.read).toBeGreaterThan(0);
    expect(API_TIMEOUT_MS.write).toBeGreaterThan(0);
    expect(API_TIMEOUT_MS.write).toBeGreaterThanOrEqual(API_TIMEOUT_MS.read);
    expect(API_TIMEOUT_MS.long).toBeGreaterThan(API_TIMEOUT_MS.write);
  });
});
