/**
 * 后端连接解析：地址 + 会话令牌。
 *
 * 不再硬编码 `127.0.0.1:8734`：
 * 1. 桌面壳（Tauri）里后端端口是随机挑的，令牌由壳生成并只在本进程存活，
 *    前端通过 `qio_backend_info` 命令拿到 {port, token}；
 * 2. 开发模式（浏览器 + Vite dev server）读构建期注入的 VITE_* 变量；
 * 3. 什么都没配置时回落到默认端口且不带令牌 —— 只能在
 *    `QIO_DEV_INSECURE=1` 的开发后端上工作。
 *
 * §5 的两条硬规矩（这两件事以前都是错的）：
 * - **有壳就必须问壳**：壳还没发布连接信息时返回可重试的「尚未就绪」，绝不把
 *   `DEFAULT_BASE` 当成功结果 —— 否则壳恢复之后界面也永远对着一个不存在的端口；
 * - **缓存只存有效连接信息，并且有代次**：`resetBackend()`（后端重启 / 认证失效）
 *   之后，代次变化之前发起的旧解析不得再变成当前连接。
 */

export interface BackendInfo {
  base: string;
  token: string;
  /** 来源，仅用于排查（不泄露令牌） */
  source: "tauri" | "env" | "default";
}

/** 有壳但连接信息还读不到：可重试，不是「成功但用了默认地址」。 */
export class BackendNotReadyError extends Error {
  constructor(detail: string) {
    super(`后端连接信息尚未就绪（${detail}）。稍后重试即可。`);
    this.name = "BackendNotReadyError";
  }
}

/** 这次解析发起之后连接被 reset 过：结果已过期，调用方要重新解析。 */
export class BackendConnectionChangedError extends Error {
  constructor() {
    super("后端连接信息已经更新：这次解析的结果已经过期，请重新解析。");
    this.name = "BackendConnectionChangedError";
  }
}

const DEFAULT_BASE = "http://127.0.0.1:8734";

/**
 * 原生读取的上界。
 *
 * 启动握手有总截止时间（`services/boot.ts`），但那只有在**单次解析也有上界**时才
 * 真的有效 —— 壳的命令挂住时不能把整个等待预算耗光。
 */
export const NATIVE_READ_TIMEOUT_MS = 2000;

function tauriInternals(): Record<string, unknown> | undefined {
  return (globalThis as unknown as { __TAURI_INTERNALS__?: Record<string, unknown> })
    .__TAURI_INTERNALS__;
}

function envToken(): string {
  try {
    return String(import.meta.env?.VITE_QIO_SESSION_TOKEN ?? "");
  } catch {
    return "";
  }
}

function envBase(): string {
  try {
    return String(import.meta.env?.VITE_QIO_BACKEND_URL ?? "");
  } catch {
    return "";
  }
}

async function withTimeout<T>(work: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | null = null;
  try {
    return await Promise.race([
      work,
      new Promise<never>((_, reject) => {
        timer = setTimeout(
          () => reject(new BackendNotReadyError("桌面壳没有在预期时间内应答")),
          ms,
        );
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function resolveOnce(): Promise<BackendInfo> {
  if (tauriInternals()) {
    // 有桥 = 桌面外壳：只认壳发布的真实连接信息。
    let info: { port?: number; token?: string } | null = null;
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      info = (await withTimeout(
        Promise.resolve(invoke("qio_backend_info")),
        NATIVE_READ_TIMEOUT_MS,
      )) as { port?: number; token?: string } | null;
    } catch (err) {
      if (err instanceof BackendNotReadyError) throw err;
      throw new BackendNotReadyError((err as Error)?.message || "桌面壳读取失败");
    }
    if (info && info.port) {
      return { base: `http://127.0.0.1:${info.port}`, token: info.token ?? "", source: "tauri" };
    }
    throw new BackendNotReadyError("桌面壳还没有发布端口");
  }
  // 纯浏览器开发模式：没有壳，env / 默认地址就是既有行为，保持不变。
  const token = envToken();
  const base = envBase();
  if (base) return { base, token, source: "env" };
  return { base: DEFAULT_BASE, token, source: "default" };
}

let cached: Promise<BackendInfo> | null = null;
/** 连接代次：`resetBackend()` 之后，之前发起的旧解析不得再写回缓存。 */
let generation = 0;

export function resolveBackend(): Promise<BackendInfo> {
  if (cached) return cached;
  const startedAt = generation;
  const pending: Promise<BackendInfo> = resolveOnce().then(
    (info) => {
      if (startedAt !== generation) throw new BackendConnectionChangedError();
      return info;
    },
    (err) => {
      // 失败不缓存（壳稍后才就绪是常态，下次调用要能重试）。
      // 只清自己那一份：reset 之后新装的缓存不能被旧的失败顺手擦掉。
      if (cached === pending) cached = null;
      throw err;
    },
  );
  cached = pending;
  return pending;
}

/** 连接信息变化（后端重启 / 认证失效）时重置缓存并作废在飞的旧解析。 */
export function resetBackend(): void {
  generation += 1;
  cached = null;
}

/** 只在真的有令牌时才带认证头，不伪造。 */
export function authHeaders(token: string): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}
