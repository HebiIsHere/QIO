/**
 * 附件客户端：路径选择（原生）→ 登记 / 上传 → 状态跟进 → 移除 / 重定位。
 *
 * 归属（Lead 裁决）：附件相关的 HTTP 调用全部写在这里，不改 api.ts（api.ts 归 B，
 * 只加 sendTurn 的可选 attachment_ids）。这里复用既有请求辅助（backend.ts 的
 * resolveBackend/authHeaders）与 api.ts 的错误类型，写操作**不自动重试**的语义一致。
 *
 * 三条诚实规则：
 * 1. 只接受**真实路径**：Tauri 侧走原生选择器 / 拖放事件；浏览器回退走字节上传，
 *    绝不把 input[type=file] 的 fakepath 当路径。
 * 2. 「准备中」不是「已就绪」：prepared 必须继续跟进，发送前必须是 ready。
 * 3. 失败/变化/丢失都如实显示并保留重试，不假装成功。
 */

import { authHeaders, resetBackend, resolveBackend } from "./backend";

/**
 * 超时口径与 api.ts 保持一致（这里不 import api.ts：既有组件测试会把
 * services/api 整个 mock 掉，附件模块不该因为那个 mock 而拿不到常量）。
 */
const TIMEOUT_MS = { read: 15_000, write: 20_000, long: 90_000 } as const;

/** 附件请求错误：带上状态码，调用方才能区分「没权限」与「真的坏了」。 */
export class AttachmentRequestError extends Error {
  constructor(
    readonly status: number,
    readonly path: string,
    detail: string,
  ) {
    super(status === 401 || status === 403 ? path + " -> " + status + "：本机 API 拒绝了这次请求" : path + " -> " + status + "：" + detail);
    this.name = "AttachmentRequestError";
  }
}

/** 请求超时（已取消）：如实说「没有响应」，由调用方决定是否重试。 */
export class AttachmentTimeoutError extends Error {
  constructor(readonly path: string, readonly timeoutMs: number) {
    super(path + " 在 " + Math.round(timeoutMs / 1000) + " 秒内没有响应（这次请求已取消，可以重试）");
    this.name = "AttachmentTimeoutError";
  }
}

/** 十进制阈值：≤ 存副本 / > 记引用（与后端一致）。 */
export const COPY_MAX_BYTES = 100_000_000;

export type AttachmentKind = "copy" | "reference";
export type AttachmentState = "prepared" | "ready" | "failed" | "missing" | "changed";

/**
 * 附件引用（Lead 冻结形状，逐字一致；B 也按这个形状消费）。
 * display 只有两种取值：「已保存副本」/「引用本地文件」。
 */
export interface AttachmentRef {
  id: string;
  name: string;
  sizeBytes: number;
  kind: AttachmentKind;
  display: string;
  state: AttachmentState;
  error?: string | null;
}

export const COPY_LABEL = "已保存副本";
export const REFERENCE_LABEL = "引用本地文件";
export const REFERENCE_CAVEAT = "历史保留的是位置，不保证内容仍然存在";

/** 后端可能返回的原始状态（含冻结类型之外的内部值）：一律归到冻结的五个状态里。 */
function normalizeState(raw: unknown): AttachmentState {
  switch (raw) {
    case "prepared":
    case "ready":
    case "failed":
    case "missing":
    case "changed":
      return raw;
    // cancelled 是内部状态（行通常已被删除）：界面上归为「失败 + 原因」，
    // 而不是发明一个新状态去污染冻结类型。
    case "cancelled":
      return "failed";
    default:
      return "failed";
  }
}

/** 把后端 payload 收敛成 AttachmentRef（未知状态不掩盖：error 里带着原始值）。 */
export function toAttachmentRef(payload: Record<string, unknown>): AttachmentRef {
  const state = normalizeState(payload.state);
  const rawError = typeof payload.error === "string" && payload.error ? payload.error : null;
  const normalized = state === "failed" && payload.state === "cancelled" ? (rawError ?? "已取消（可以重试）") : rawError;
  return {
    id: String(payload.id ?? ""),
    name: String(payload.name ?? "未命名文件"),
    sizeBytes: Number(payload.size_bytes ?? 0),
    kind: payload.kind === "reference" ? "reference" : "copy",
    display: payload.kind === "reference" ? REFERENCE_LABEL : COPY_LABEL,
    state,
    error: normalized,
  };
}

export function humanSize(bytes: number): string {
  const value = Number(bytes) || 0;
  if (value < 1000) return value + " B";
  const units = ["KB", "MB", "GB", "TB"];
  let current = value / 1000;
  let index = 0;
  while (current >= 1000 && index < units.length - 1) {
    current /= 1000;
    index += 1;
  }
  return current.toFixed(1) + " " + units[index];
}

/** 附件能不能随消息发送：只有 ready 才算准备好。 */
export function isSendable(ref: AttachmentRef): boolean {
  return ref.state === "ready";
}

/** 一句话状态（界面与错误提示共用，避免各处自己编词）。 */
export function stateText(ref: AttachmentRef): string {
  switch (ref.state) {
    case "prepared":
      return "准备中…";
    case "ready":
      return ref.display;
    case "failed":
      return "准备失败";
    case "missing":
      return "文件不在原位";
    case "changed":
      return "内容有变化";
    default:
      return "状态未知";
  }
}

// ---------------------------------------------------------------------------
// HTTP（与 api.ts 同一套语义：写不重试、超时给长操作、401/403 重置连接）
// ---------------------------------------------------------------------------

function detailOf(text: string, fallback: string): string {
  try {
    const parsed = JSON.parse(text) as { detail?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail) return parsed.detail;
  } catch {
    /* 不是 JSON：用原文 */
  }
  return text.slice(0, 200) || fallback;
}

async function request<T>(path: string, init: RequestInit = {}, timeoutMs: number = TIMEOUT_MS.write): Promise<T> {
  const { base, token } = await resolveBackend();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(base + path, {
      ...init,
      signal: controller.signal,
      headers: {
        ...authHeaders(token),
        ...((init.headers as Record<string, string> | undefined) ?? {}),
      },
    });
    if (!resp.ok) {
      const text = await resp.text();
      if (resp.status === 401 || resp.status === 403) resetBackend();
      throw new AttachmentRequestError(resp.status, path, detailOf(text, resp.statusText));
    }
    return (await resp.json()) as T;
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new AttachmentTimeoutError(path, timeoutMs);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

interface AttachmentPayload {
  attachment: Record<string, unknown>;
}

/** 登记一个真实存在的本地路径（≤100MB 后台复制副本；>100MB 只记引用）。 */
export async function prepareAttachment(
  sourcePath: string,
  options: { topicId?: string | null; name?: string | null; size?: number | null } = {},
): Promise<AttachmentRef> {
  const body: Record<string, unknown> = { source_path: sourcePath };
  if (options.topicId) body.topic_id = options.topicId;
  if (options.name) body.name = options.name;
  if (options.size != null) body.size = options.size;
  const res = await request<AttachmentPayload>(
    "/api/attachments",
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
    TIMEOUT_MS.write,
  );
  return toAttachmentRef(res.attachment);
}

/** 浏览器回退：把文件字节原样传上去（不用 multipart，也不碰 fakepath）。 */
export async function uploadAttachment(
  file: File,
  options: { topicId?: string | null } = {},
): Promise<AttachmentRef> {
  if (file.size > COPY_MAX_BYTES) {
    throw new Error(
      "浏览器上传只用于 ≤ " +
        humanSize(COPY_MAX_BYTES) +
        " 的文件；这个文件 " +
        humanSize(file.size) +
        "，请用「选择本地文件」（桌面端）或把路径粘进来（大于 " +
        humanSize(COPY_MAX_BYTES) +
        " 的文件只记位置，不复制内容）",
    );
  }
  const headers: Record<string, string> = {
    "Content-Type": "application/octet-stream",
    // 头部不能放非 ASCII：URL 编码后由后端解码
    "X-QIO-Name": encodeURIComponent(file.name || "attachment"),
  };
  if (options.topicId) headers["X-QIO-Topic-Id"] = options.topicId;
  const res = await request<AttachmentPayload>(
    "/api/attachments/upload",
    { method: "POST", headers, body: file },
    TIMEOUT_MS.long,
  );
  return toAttachmentRef(res.attachment);
}

export async function getAttachment(id: string): Promise<AttachmentRef> {
  const res = await request<AttachmentPayload>("/api/attachments/" + encodeURIComponent(id));
  return toAttachmentRef(res.attachment);
}

export async function listAttachments(
  params: { topicId?: string | null; turnId?: string | null; unbound?: boolean } = {},
): Promise<AttachmentRef[]> {
  const query = new URLSearchParams();
  if (params.topicId) query.set("topic_id", params.topicId);
  if (params.turnId) query.set("turn_id", params.turnId);
  if (params.unbound) query.set("unbound", "true");
  const suffix = query.toString() ? "?" + query.toString() : "";
  const res = await request<{ attachments: Record<string, unknown>[] }>("/api/attachments" + suffix);
  return (res.attachments ?? []).map(toAttachmentRef);
}

/** 移除附件（只删 QIO 自己保存的副本，用户原文件不动）；复制中调用即取消。 */
export async function removeAttachment(id: string): Promise<void> {
  await request<Record<string, unknown>>("/api/attachments/" + encodeURIComponent(id), {
    method: "DELETE",
  });
}

/** 文件被移动/改名之后重新指定位置。 */
export async function relocateAttachment(id: string, sourcePath: string): Promise<AttachmentRef> {
  const res = await request<AttachmentPayload>(
    "/api/attachments/" + encodeURIComponent(id) + "/relocate",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_path: sourcePath }),
    },
    TIMEOUT_MS.long,
  );
  return toAttachmentRef(res.attachment);
}

/** 失败/变化之后重试同一行（不新建附件）。 */
export async function retryAttachment(id: string): Promise<AttachmentRef> {
  const res = await request<AttachmentPayload>(
    "/api/attachments/" + encodeURIComponent(id) + "/retry",
    { method: "POST" },
    TIMEOUT_MS.write,
  );
  return toAttachmentRef(res.attachment);
}

/** 等待附件离开 prepared（准备中 → ready / failed / changed / missing）。 */
export async function waitUntilSettled(
  ref: AttachmentRef,
  options: { timeoutMs?: number; intervalMs?: number; onUpdate?: (next: AttachmentRef) => void } = {},
): Promise<AttachmentRef> {
  const timeoutMs = options.timeoutMs ?? 120_000;
  const intervalMs = options.intervalMs ?? 400;
  const deadline = Date.now() + timeoutMs;
  let current = ref;
  while (current.state === "prepared" && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
    current = await getAttachment(ref.id);
    options.onUpdate?.(current);
  }
  return current;
}

// ---------------------------------------------------------------------------
// 原生选择 / 拖放（Tauri）
// ---------------------------------------------------------------------------

interface TauriGlobal {
  __TAURI_INTERNALS__?: unknown;
}

/** 是否运行在 Tauri 桌面壳里（浏览器回退与原生路径的分界线）。 */
export function isDesktopShell(): boolean {
  return typeof window !== "undefined" && Boolean((window as unknown as TauriGlobal).__TAURI_INTERNALS__);
}

/**
 * 点击「选择本地文件」：调用壳里的原生选择器（Rust 命令 pick_attachment_file），
 * 返回**真实路径**；取消返回 null。非桌面壳返回 null（由调用方降级为字节上传/粘贴路径）。
 */
export async function pickLocalPath(): Promise<string | null> {
  if (!isDesktopShell()) return null;
  const { invoke } = await import("@tauri-apps/api/core");
  const picked = await invoke<string | null>("pick_attachment_file");
  return picked && picked.trim() ? picked : null;
}

/**
 * 拖放：Tauri 核心的 onDragDropEvent 给的是**真实路径**（不需要 dialog 插件）。
 * 返回取消订阅函数；非桌面壳返回 null，由调用方使用浏览器 drag&drop 回退。
 */
export async function onPathDrop(
  handler: (paths: string[]) => void,
  onState?: (state: "over" | "leave" | "drop") => void,
): Promise<(() => void) | null> {
  if (!isDesktopShell()) return null;
  const { getCurrentWebview } = await import("@tauri-apps/api/webview");
  const unlisten = await getCurrentWebview().onDragDropEvent((event) => {
    const payload = event.payload;
    if (payload.type === "over") {
      onState?.("over");
    } else if (payload.type === "leave") {
      onState?.("leave");
    } else if (payload.type === "drop") {
      onState?.("drop");
      if (payload.paths.length) handler(payload.paths);
    }
  });
  return () => {
    void unlisten();
  };
}
