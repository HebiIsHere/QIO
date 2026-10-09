/**
 * 契约 5（Agent E）共用测试工具：真实 store + 假 fetch 闸门。
 *
 * 这里**不** mock services/api 模块本身：走真实的 request 路径（超时、重试、
 * 错误分类都算在测试范围内），只把 fetch 换成测试控制的假闸门。
 * 查询响应的顺序完全由测试控制，不修改生产代码迁就 mock。
 */
import { createPinia, setActivePinia } from "pinia";
import { vi } from "vitest";
import { useEventStore } from "../stores/events";
import { useSessionStore } from "../stores/session";

export interface RecordedCall {
  url: string;
  method: string;
  body: Record<string, unknown> | string | null;
  init: RequestInit;
}

export function okResponse(payload: unknown): unknown {
  return {
    ok: true,
    status: 200,
    headers: { get: () => "application/json" },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
}

export function statusResponse(status: number, payload: unknown): unknown {
  return {
    ok: false,
    status,
    headers: { get: () => "application/json" },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
}

/** 挂起不返回；controller.abort() 之后才失败（驱动真实的超时路径）。 */
export function hang(signal?: AbortSignal | null): Promise<never> {
  return new Promise((_resolve, reject) => {
    if (!signal) return;
    if (signal.aborted) {
      reject(new Error("aborted"));
      return;
    }
    signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
  });
}

/** 测试给的响应器：返回 undefined = 交给默认的「空成功」兜底。 */
export type FetchResponder = (url: string, init: RequestInit) => unknown;

export interface FakeFetchGate {
  calls: RecordedCall[];
  respond: (responder: FetchResponder | null) => void;
  callsMatching: (method: string, suffix: string) => RecordedCall[];
}

export function installFakeFetch(): FakeFetchGate {
  const calls: RecordedCall[] = [];
  let responder: FetchResponder | null = null;
  const fetchMock = ((url: unknown, init: RequestInit = {}) => {
    const u = String(url);
    let body: RecordedCall["body"] = null;
    if (typeof init.body === "string") {
      try {
        body = JSON.parse(init.body) as Record<string, unknown>;
      } catch {
        body = init.body;
      }
    }
    calls.push({ url: u, method: (init.method ?? "GET").toUpperCase(), body, init });
    // requestOnce 不往 fetch init 里写 method（GET 缺省）：给响应器一份归一化的 init，
    // 让测试按「真实的 HTTP 方法」写分支，而不是迁就实现细节
    const normalized = { ...init, method: (init.method ?? "GET").toUpperCase() };
    const out = responder ? responder(u, normalized as RequestInit) : undefined;
    if (out !== undefined) return Promise.resolve(out);
    // 没有明确响应的调用（例如 TURN_END 之后自动刷新开发任务列表）给一个空成功
    return Promise.resolve(okResponse({}));
  }) as unknown as typeof fetch;
  (globalThis as unknown as { fetch: unknown }).fetch = fetchMock;
  return {
    calls,
    respond: (r) => {
      responder = r;
    },
    callsMatching: (method, suffix) =>
      calls.filter((c) => c.method === method && c.url.endsWith(suffix)),
  };
}

/** 真实 store（session + events），配合假 fetch 闸门使用。 */
export function setupSession(): {
  session: ReturnType<typeof useSessionStore>;
  events: ReturnType<typeof useEventStore>;
} {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { session: useSessionStore(), events: useEventStore() };
}

/** 假定时器下把微任务链冲干净（apiTimeouts.verify.test 同款手法）。 */
export async function flushAsyncTimers(ms = 0): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms);
  await vi.advanceTimersByTimeAsync(0);
}

/** 真实定时器下把微任务链冲干净（等 resolveBackend → fetch 被真正调用）。 */
export function flushMicrotasks(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}
