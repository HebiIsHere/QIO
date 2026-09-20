/**
 * 后端连接解析：地址 + 会话令牌。
 *
 * 不再硬编码 `127.0.0.1:8734`：
 * 1. 桌面壳（Tauri）里后端端口是随机挑的，令牌由壳生成并只在本进程存活，
 *    前端通过 `qio_backend_info` 命令拿到 {port, token}；
 * 2. 开发模式（浏览器 + Vite dev server）读构建期注入的 VITE_* 变量；
 * 3. 什么都没配置时回落到默认端口且不带令牌 —— 只能在
 *    `QIO_DEV_INSECURE=1` 的开发后端上工作。
 */

export interface BackendInfo {
  base: string;
  token: string;
  /** 来源，仅用于排查（不泄露令牌） */
  source: "tauri" | "env" | "default";
}

const DEFAULT_BASE = "http://127.0.0.1:8734";

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

let cached: Promise<BackendInfo> | null = null;

async function resolveOnce(): Promise<BackendInfo> {
  if (tauriInternals()) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const info = (await invoke("qio_backend_info")) as { port: number; token: string };
      if (info && info.port) {
        return { base: `http://127.0.0.1:${info.port}`, token: info.token ?? "", source: "tauri" };
      }
    } catch {
      // 壳还没就绪：落到下面的兜底路径，由调用方重试
    }
  }
  const token = envToken();
  const base = envBase();
  if (base) return { base, token, source: "env" };
  return { base: DEFAULT_BASE, token, source: "default" };
}

export function resolveBackend(): Promise<BackendInfo> {
  if (!cached) {
    cached = resolveOnce().catch((err) => {
      cached = null; // 下次调用重试，而不是把失败永久缓存
      throw err;
    });
  }
  return cached;
}

/** 连接信息变化（例如后端重启换了端口）时重置缓存。 */
export function resetBackend(): void {
  cached = null;
}

/** 只在真的有令牌时才带认证头，不伪造。 */
export function authHeaders(token: string): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}
