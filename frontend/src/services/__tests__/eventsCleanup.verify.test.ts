/**
 * 独立验证（验证方维护，`*.verify.test.ts`）：**事件流断开后的重连与清理**
 * （契约 WS1 §5 末尾：所有分支与断开时都要清理，不许留后台重连）。
 *
 * 契约要求：
 *   * 断开后要能重连（票据是一次性的，必须自己重连）；
 *   * `close()` 之后**不许**再重连、不许留挂着的事件源；
 *   * 还在飞的票据请求要被**立刻取消**（不能留在后台）。
 *
 * 用替身 EventSource + 替身 fetch，全部在假定时器下推进（不真的等重连退避）。
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { connectEvents, EVENTS_TICKET_TIMEOUT_MS } from "../../services/events";
import { resetBackend } from "../../services/backend";

/**
 * 票据请求只在**有令牌**时才发（`const ticket = token ? await fetchEventsTicket(...) : ""`），
 * 所以要装一个「壳已就绪」的 IPC 替身，让 token 路径真的被走到 —— 否则
 * 「close() 取消票据请求」这条用例会因为「根本没发票据请求」而空过。
 */
const ipcMock = vi.fn(async (cmd: string) =>
  cmd === "qio_backend_info" ? { port: 8841, token: "tk_test" } : null,
);

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  closed = false;
  onopen: (() => void) | null = null;
  onerror: ((err?: unknown) => void) | null = null;
  private listeners = new Map<string, ((raw: unknown) => void)[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, handler: (raw: unknown) => void) {
    const list = this.listeners.get(type) ?? [];
    list.push(handler);
    this.listeners.set(type, list);
  }
  close() {
    this.closed = true;
  }
  /** 模拟「连接断了」 */
  fail() {
    this.onerror?.(new Error("stream error"));
  }
}

interface FetchCall {
  url: string;
  init: { signal?: AbortSignal };
}

let fetchCalls: FetchCall[] = [];
/** true = 票据请求挂着不返回（用来验证 close() 能取消它） */
let hangTicket = false;

beforeEach(() => {
  FakeEventSource.instances = [];
  fetchCalls = [];
  hangTicket = false;
  ipcMock.mockClear();
  (globalThis as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {
    invoke: ipcMock,
  };
  resetBackend(); // 每个用例从「没有缓存」开始，保证真的去问壳拿令牌
  (globalThis as unknown as { EventSource: unknown }).EventSource = FakeEventSource;
  (globalThis as unknown as { fetch: unknown }).fetch = (url: unknown, init: FetchCall["init"] = {}) => {
    fetchCalls.push({ url: String(url), init });
    if (hangTicket) {
      return new Promise((_resolve, reject) => {
        const signal = init.signal;
        if (!signal) return;
        if (signal.aborted) {
          reject(new Error("aborted"));
          return;
        }
        signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
      });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => ({ ticket: "tk_test" }),
    });
  };
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  delete (globalThis as unknown as { EventSource?: unknown }).EventSource;
  delete (globalThis as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
});

/**
 * 在装假定时器**之前**抓一份真实 setTimeout：`vi.useFakeTimers()` 会把全局那份换掉，
 * 而 settle() 恰恰需要真实事件循环（见下）。
 */
const realSetTimeout = setTimeout;

/**
 * 等 `connectEvents()` 内部那串 await 真的推进到位（问壳拿令牌 → 请求票据 → 建 EventSource）。
 *
 * 为什么不能只推假定时器：`resolveBackend()` 里有 `await import("@tauri-apps/api/core")`，
 * 动态 import 是**真实 I/O**，必须让真实事件循环转一圈才能完成；而
 * `for (…) await vi.advanceTimersByTimeAsync(1)` 这种紧凑循环只在微任务里打转、从不给 I/O
 * 让出机会 —— 实测这么推 30ms 依然一个连接都建不出来（CI 上第 1 个用例就是这么红的，
 * 与 vitest 版本、操作系统都无关，本机 Windows 一样复现）。
 *
 * 判据用**可观察的副作用**（建出了事件源，或至少把票据请求发出去了），不用「推几毫秒」这种猜法；
 * 假时间每次只推进 1ms、总共至多 50ms，远低于重连退避（≥1000ms），不会误触发重连。
 */
async function settle() {
  for (let i = 0; i < 50; i += 1) {
    if (FakeEventSource.instances.length > 0 || fetchCalls.length > 0) return;
    await new Promise((resolve) => realSetTimeout(resolve, 0));
    await vi.advanceTimersByTimeAsync(1);
  }
  throw new Error(
    "connectEvents 没有推进：既没建出事件源，也没发出票据请求" +
      `（invoke 调用 ${ipcMock.mock.calls.length} 次）`,
  );
}

describe("WS1 §5：事件流断开与清理", () => {
  it("断开时会重连（机制本身有效，避免后面的「不重连」断言变成空过）", async () => {
    const handle = connectEvents(() => {});
    // try/finally：断言提前中断时也必须收掉连接 —— 否则这条没跑完的连接会漏进下一个用例，
    // 表现成「下一个用例数到 2 个事件源」。那是测试互相污染，不是产品行为。
    try {
      await settle();
      expect(FakeEventSource.instances.length).toBe(1);

      FakeEventSource.instances[0].fail(); // 断开
      await vi.advanceTimersByTimeAsync(2000); // 第一次退避 ~1s + 抖动

      expect(FakeEventSource.instances.length).toBe(2); // 真的重连了
    } finally {
      handle.close();
    }
  });

  it("close() 之后断开：**不再**重连，也不留挂着的事件源", async () => {
    const handle = connectEvents(() => {});
    try {
      await settle();
      const first = FakeEventSource.instances[0];

      handle.close();
      expect(first.closed).toBe(true); // 事件源被关掉

      first.fail(); // 关闭之后再断开
      await vi.advanceTimersByTimeAsync(60000); // 推进足够久

      expect(FakeEventSource.instances.length).toBe(1); // 没有新的连接
    } finally {
      handle.close(); // 幂等；断言提前中断时兜底
    }
  });

  it("close() 会立刻取消还在飞的票据请求（不留后台请求）", async () => {
    hangTicket = true;
    const handle = connectEvents(() => {});
    try {
      await settle();

      expect(fetchCalls.length).toBe(1); // 有令牌 → 真的发了票据请求（不是空过）
      expect(fetchCalls[0].url).toContain("/api/events/ticket");
      const signal = fetchCalls[0].init.signal;
      expect(signal).not.toBeUndefined();
      expect(signal?.aborted).toBe(false); // 还在飞

      handle.close();

      expect(signal?.aborted).toBe(true); // 关闭时被取消
      await vi.advanceTimersByTimeAsync(EVENTS_TICKET_TIMEOUT_MS + 1000);
      expect(FakeEventSource.instances.length).toBe(0); // 关闭后不会再建连接
    } finally {
      handle.close(); // 幂等
    }
  });
});
