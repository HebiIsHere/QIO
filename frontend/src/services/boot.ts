/**
 * 启动握手：先拿后端地址与令牌，再确认后端**真的在应答**。
 *
 * 为什么要这一步（2026-09-24 实测）：外壳会先把窗口显示出来，后端要几秒才起来；
 * 这段时间里界面若直接调接口，只会拿到"连接失败"，看起来就是一片空白（用户只能
 * 判断"它没启动"）。把等待做成一个明确的状态：界面说清在等什么，等不到给原因与重试。
 *
 * 判定标准用 `/api/health`：它不需要会话令牌，是"后端真的在服务"的最短握手。
 * 浏览器开发模式下同样适用 —— 没有后端在跑时如实报错，而不是渲染一个坏掉的界面。
 */

import { resolveBackend } from "./backend";

export interface BackendReady {
  base: string;
  token: string;
}

export interface BootOptions {
  /** 等待后端应答的总时长（毫秒） */
  timeoutMs?: number;
  /** 两次探测之间的间隔（毫秒） */
  intervalMs?: number;
  /** 单次探测的超时（毫秒）：卡住的请求不能拖住整个等待 */
  probeTimeoutMs?: number;
  /** 每次失败后回报已等待时长（界面用它显示"已等 N 秒"） */
  onTick?: (elapsedMs: number) => void;
}

export const BOOT_DEFAULT_TIMEOUT_MS = 60_000;
const DEFAULT_INTERVAL_MS = 400;
const DEFAULT_PROBE_TIMEOUT_MS = 1500;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** 单次健康探测：带超时，避免请求挂住把整个启动卡死。 */
async function probeHealth(base: string, probeTimeoutMs: number): Promise<void> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), probeTimeoutMs);
  try {
    const resp = await fetch(`${base}/api/health`, { signal: controller.signal });
    if (!resp.ok) throw new Error(`/api/health -> ${resp.status}`);
  } finally {
    clearTimeout(timer);
  }
}

/**
 * 等到后端能应答为止；`timeoutMs` 内没等到就抛出人话原因（含最后一次错误）。
 *
 * 每次循环都重新解析一次连接信息：外壳发布令牌是异步的，第一次可能还没就绪；
 * `resolveBackend()` 本身不会把失败缓存下来（见 services/backend.ts）。
 */
export async function waitForBackend(opts: BootOptions = {}): Promise<BackendReady> {
  const timeoutMs = opts.timeoutMs ?? BOOT_DEFAULT_TIMEOUT_MS;
  const intervalMs = opts.intervalMs ?? DEFAULT_INTERVAL_MS;
  const probeTimeoutMs = opts.probeTimeoutMs ?? DEFAULT_PROBE_TIMEOUT_MS;
  const started = Date.now();
  let lastError = "";

  while (Date.now() - started < timeoutMs) {
    try {
      const info = await resolveBackend();
      await probeHealth(info.base, probeTimeoutMs);
      return { base: info.base, token: info.token };
    } catch (err) {
      lastError = (err as Error)?.message || String(err);
    }
    opts.onTick?.(Date.now() - started);
    await sleep(intervalMs);
  }

  throw new Error(
    lastError ? `后端没有应答（最后一条错误：${lastError}）` : "后端没有应答",
  );
}
