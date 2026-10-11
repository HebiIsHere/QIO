/**
 * 【独立验收 D · 第八轮】真实服务层 + 假传输（拦截 globalThis.fetch）。
 *
 * 组件测试要证明的是「界面点下去真的发出了什么请求」，所以这里不 mock 服务模块，
 * 而是拦下 fetch：组件 → store → services/interactive.ts → fetch 的整条链都走真实代码，
 * 只有传输层（真实后端）被替换。证据分层：②真实组件 DOM + ③服务层请求集合（传输为模拟）。
 */
export interface RecordedRequest {
  method: string;
  url: string;
  body: Record<string, unknown> | null;
}

export interface FakeResponse {
  status?: number;
  body?: unknown;
}

export type FakeHandler = (req: RecordedRequest) => FakeResponse | undefined;

export interface FakeBackend {
  requests: RecordedRequest[];
  /** 只看某个方法的请求 */
  of(method: string): RecordedRequest[];
  restore(): void;
}

function makeResponse(status: number, body: unknown): Response {
  const text = JSON.stringify(body ?? null);
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => JSON.parse(text),
    text: async () => text,
  } as unknown as Response;
}

export function installFakeBackend(handler: FakeHandler): FakeBackend {
  const requests: RecordedRequest[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : String((input as Request).url);
    const method = String(init?.method ?? (typeof input === "object" && input && "method" in input ? (input as Request).method : "GET")).toUpperCase();
    let body: Record<string, unknown> | null = null;
    if (typeof init?.body === "string") {
      try {
        body = JSON.parse(init.body) as Record<string, unknown>;
      } catch {
        body = null;
      }
    }
    const req: RecordedRequest = { method, url, body };
    requests.push(req);
    const result = handler(req);
    if (!result) return makeResponse(404, { detail: "测试装置没有为这个请求定义响应：" + method + " " + url });
    return makeResponse(result.status ?? 200, result.body ?? {});
  }) as typeof fetch;
  return {
    requests,
    of: (m: string) => requests.filter((r) => r.method === m.toUpperCase()),
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

/** 在任意嵌套结构里找出所有字符串（用来断言 decisionIds 是否真的被带出去）。 */
export function collectStrings(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.flatMap(collectStrings);
  if (value && typeof value === "object") {
    return Object.values(value as Record<string, unknown>).flatMap(collectStrings);
  }
  return [];
}
