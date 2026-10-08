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

/**
 * 服务端说「现在确实可用」的附件操作（契约 §1.4，**只有这三种取值**）。
 *
 * * retry    —— 有真实原路径：重新读它做副本（同一个附件行）；
 * * relocate —— 重新指定位置（引用型附件的唯一依据）；
 * * reupload —— 用户重新给一次文件（浏览器字节上传没有原路径，QIO 无法自行找回）。
 */
export type AttachmentAction = "retry" | "relocate" | "reupload";

export const ATTACHMENT_ACTIONS: readonly AttachmentAction[] = ["retry", "relocate", "reupload"];

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
  /** 可选事实（后端 payload 有就带上）：打开历史附件、恢复待发列表都要用 */
  storedPath?: string | null;
  sourcePath?: string | null;
  topicId?: string | null;
  turnId?: string | null;
  retryable?: boolean;
  /**
   * 服务端给的可用操作（§1.4）：**空数组 = 现在没有任何可用动作**（不显示按钮）；
   * **缺字段 = 老后端**，界面按既有状态逻辑兜底（见 attachmentActions）。
   */
  actions?: AttachmentAction[];
  /** QIO 能不能自己从已知位置把内容找回来（浏览器字节上传永远 false） */
  recoverableFromSource?: boolean;
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
  const ref: AttachmentRef = {
    id: String(payload.id ?? ""),
    name: String(payload.name ?? "未命名文件"),
    sizeBytes: Number(payload.size_bytes ?? 0),
    kind: payload.kind === "reference" ? "reference" : "copy",
    display: payload.kind === "reference" ? REFERENCE_LABEL : COPY_LABEL,
    state,
    error: normalized,
  };
  // 可选事实：payload 没给就不写进对象（冻结的 7 字段形状对老调用方逐字不变）
  if (typeof payload.stored_path === "string") ref.storedPath = payload.stored_path;
  if (typeof payload.source_path === "string") ref.sourcePath = payload.source_path;
  if (typeof payload.topic_id === "string") ref.topicId = payload.topic_id;
  if (typeof payload.turn_id === "string") ref.turnId = payload.turn_id;
  if (typeof payload.retryable === "boolean") ref.retryable = payload.retryable;
  // 可用操作：**列表（含空列表）就是权威**；只有「字段不是数组」才算老后端（不写这个键）
  if (Array.isArray(payload.actions)) ref.actions = normalizeActions(payload.actions);
  if (typeof payload.recoverable_from_source === "boolean") {
    ref.recoverableFromSource = payload.recoverable_from_source;
  }
  return ref;
}

/** 只接受冻结的三种取值：未知/重复/非字符串一律丢弃（宁可少一个按钮，也不给走不通的动作）。 */
function normalizeActions(raw: unknown[]): AttachmentAction[] {
  const out: AttachmentAction[] = [];
  for (const item of raw) {
    if (typeof item !== "string") continue;
    if (!(ATTACHMENT_ACTIONS as readonly string[]).includes(item)) continue;
    const action = item as AttachmentAction;
    if (!out.includes(action)) out.push(action);
  }
  return out;
}

/**
 * 界面该显示哪些动作：服务端给了 actions 就**完全按它**（空数组就是没有按钮）；
 * 只有缺字段（老后端 / 老历史快照）才退回既有状态逻辑。
 */
export function attachmentActions(ref: AttachmentRef): AttachmentAction[] {
  if (ref.actions) return ref.actions;
  if (ref.state === "ready" || ref.state === "prepared") return [];
  // 老后端没有 actions：保留既有行为（可重试；失败/变化/丢失可重新定位）
  return ["retry", "relocate"];
}

/**
 * 浏览器文件选择器（真实用户选择，不碰 fakepath）。
 * 取消时 resolve(null)：调用方据此**不假装成功**。
 */
export function pickBrowserFile(): Promise<File | null> {
  return new Promise((resolve) => {
    if (typeof document === "undefined") {
      resolve(null);
      return;
    }
    const input = document.createElement("input");
    input.type = "file";
    input.style.position = "fixed";
    input.style.left = "-10000px";
    let settled = false;
    const finish = (file: File | null) => {
      if (settled) return;
      settled = true;
      input.remove();
      resolve(file);
    };
    input.addEventListener("change", () => finish(input.files?.[0] ?? null), { once: true });
    // 取消不会触发 change：窗口重新获得焦点后再等一拍，仍没有选择就当作取消
    window.addEventListener(
      "focus",
      () => {
        setTimeout(() => finish(input.files?.[0] ?? null), 300);
      },
      { once: true },
    );
    document.body.appendChild(input);
    input.click();
  });
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
// ---------------------------------------------------------------------------
// 打开 / 下载 / 在文件夹中显示（问题 5）
// ---------------------------------------------------------------------------

/**
 * 可执行 / 脚本类扩展名：**绝不自动执行**。
 *
 * 桌面壳里这类文件不交给系统默认程序，改为「在文件夹中显示」并说明原因 ——
 * 附件是数据，不该因为一个点击就变成进程。Rust 侧还有一道同样的拒绝（fail-closed）。
 */
export const EXECUTABLE_EXTS = new Set([
  ".exe", ".com", ".scr", ".msi", ".msix", ".appx", ".bat", ".cmd", ".ps1", ".psm1",
  ".vbs", ".vbe", ".js", ".jse", ".ws", ".wsf", ".wsh", ".jar", ".lnk", ".reg",
  ".sh", ".bash", ".zsh", ".py", ".pyw", ".pl", ".rb", ".dll", ".so", ".dylib",
  ".apk", ".deb", ".rpm", ".run", ".cpl", ".hta", ".inf",
]);

/** 浏览器里可以安全内联查看的类型（html / svg / xml 会执行脚本或带外链，绝不内联）。 */
const VIEWABLE_EXTS = new Set([
  ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".txt", ".md", ".log", ".csv",
  ".json", ".pdf", ".mp3", ".wav", ".mp4", ".webm", ".ogg",
]);

function extensionOf(name: string): string {
  const dot = String(name || "").lastIndexOf(".");
  return dot < 0 ? "" : String(name).slice(dot).toLowerCase();
}

export function isExecutableName(name: string): boolean {
  return EXECUTABLE_EXTS.has(extensionOf(name));
}

export function isViewableName(name: string): boolean {
  return VIEWABLE_EXTS.has(extensionOf(name));
}

export type AttachmentOpenAction = "open" | "reveal" | "view" | "download" | "blocked";

/** 打开方式 = 计划；reason 是给用户看的原因（绝不假装「已打开」）。 */
export interface AttachmentOpenPlan {
  action: AttachmentOpenAction;
  reason: string;
  path?: string;
}

export interface AttachmentOpenResult {
  action: Exclude<AttachmentOpenAction, "blocked">;
  note: string;
}

/**
 * 这个附件点「打开」会发生什么（纯函数，可测）：
 *
 * * 桌面 + 有真实路径 + 普通文件 → open（系统默认程序）；
 * * 桌面 + 可执行/脚本类 → reveal（在文件夹中显示，绝不自动执行）；
 * * 浏览器 + QIO 副本 → view（安全类型：认证 fetch 出的 Blob 新标签页）或 download；
 * * 浏览器 + 引用型（没有副本）→ blocked，如实说明拿不到本地路径。
 */
export function openPlanFor(ref: AttachmentRef, options: { desktop?: boolean } = {}): AttachmentOpenPlan {
  const desktop = options.desktop ?? isDesktopShell();
  if (ref.state !== "ready" && ref.state !== "changed") {
    return {
      action: "blocked",
      reason: "这个附件现在不能打开（" + stateText(ref) + "）：先重试或重新指定位置",
    };
  }
  const path = ref.storedPath || ref.sourcePath || "";
  if (desktop && path) {
    if (isExecutableName(ref.name)) {
      return {
        action: "reveal",
        path,
        reason: "可执行 / 脚本类文件不自动运行：已在文件夹中显示，确认来源后再自行打开",
      };
    }
    return { action: "open", path, reason: "" };
  }
  if (ref.kind === "copy") {
    return isViewableName(ref.name)
      ? { action: "view", reason: "" }
      : { action: "download", reason: "这种类型不内联查看（可能带脚本）：改为下载副本" };
  }
  return {
    action: "blocked",
    reason: "引用型附件没有 QIO 副本，浏览器里打不开本地路径；请在桌面端打开，或用「重新定位」重新指定位置",
  };
}

/** 带认证 fetch 副本字节（不新增无认证的裸链接）。 */
export async function fetchAttachmentContent(
  id: string,
  timeoutMs: number = TIMEOUT_MS.long,
): Promise<Blob> {
  const { base, token } = await resolveBackend();
  const path = "/api/attachments/" + encodeURIComponent(id) + "/content";
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(base + path, { signal: controller.signal, headers: authHeaders(token) });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      if (resp.status === 401 || resp.status === 403) resetBackend();
      throw new AttachmentRequestError(resp.status, path, detailOf(text, resp.statusText));
    }
    return await resp.blob();
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new AttachmentTimeoutError(path, timeoutMs);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

function blobUrlOf(blob: Blob): string | null {
  const create = (URL as unknown as { createObjectURL?: (value: Blob) => string }).createObjectURL;
  return typeof create === "function" ? create(blob) : null;
}

/** 把 Blob 存成文件（下载）。环境不支持 Blob 下载时如实报错，不假装成功。 */
export function downloadBlob(blob: Blob, name: string): void {
  if (typeof document === "undefined") throw new Error("这个环境不支持下载；请在桌面端使用「打开」");
  const url = blobUrlOf(blob);
  if (!url) throw new Error("这个环境不支持 Blob 下载；请在桌面端使用「打开」");
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = name || "attachment";
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    setTimeout(() => {
      try {
        URL.revokeObjectURL(url);
      } catch {
        /* 已经释放过就算了 */
      }
    }, 1000);
  }
}

export async function downloadAttachment(ref: AttachmentRef): Promise<AttachmentOpenResult> {
  const blob = await fetchAttachmentContent(ref.id);
  downloadBlob(blob, ref.name);
  return { action: "download", note: "已开始下载 QIO 保存的副本：" + ref.name };
}

function openBlobInNewTab(blob: Blob): boolean {
  if (typeof window === "undefined") return false;
  const url = blobUrlOf(blob);
  if (!url) return false;
  const opened = window.open(url, "_blank", "noopener,noreferrer");
  if (!opened) {
    URL.revokeObjectURL(url);
    return false;
  }
  setTimeout(() => {
    try {
      URL.revokeObjectURL(url);
    } catch {
      /* 已经释放过就算了 */
    }
  }, 60_000);
  return true;
}

async function invokeNativePath(command: string, path: string): Promise<void> {
  if (!path) throw new Error("这个附件没有可打开的本地路径");
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke(command, { path });
}

/**
 * 打开一个附件：桌面走原生（可执行类降级为「在文件夹中显示」），浏览器走认证 fetch + Blob。
 *
 * 失败一律抛错（调用方如实显示），绝不返回「已打开」的假结果。
 */
export async function openAttachment(
  ref: AttachmentRef,
  options: { desktop?: boolean } = {},
): Promise<AttachmentOpenResult> {
  const plan = openPlanFor(ref, options);
  if (plan.action === "blocked") throw new Error(plan.reason);
  if (plan.action === "reveal") {
    await invokeNativePath("reveal_attachment_path", plan.path ?? "");
    return { action: "reveal", note: plan.reason };
  }
  if (plan.action === "open") {
    await invokeNativePath("open_attachment_path", plan.path ?? "");
    return { action: "open", note: "已交给系统默认程序打开：" + ref.name };
  }
  const blob = await fetchAttachmentContent(ref.id);
  if (plan.action === "view" && openBlobInNewTab(blob)) {
    return { action: "view", note: "" };
  }
  downloadBlob(blob, ref.name);
  return { action: "download", note: plan.reason || "已开始下载 QIO 保存的副本：" + ref.name };
}

// ---------------------------------------------------------------------------
// 待发附件：与「话题 + 草稿」绑定并在组件重建 / 刷新后可见恢复（问题 3）
// ---------------------------------------------------------------------------

const PENDING_KEY = "qio.pending-attachments.v1";

function pendingKey(topicId: string | null | undefined): string {
  return String(topicId ?? "");
}

function readPendingBox(): Record<string, AttachmentRef[]> {
  try {
    const raw = globalThis.localStorage?.getItem(PENDING_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const box: Record<string, AttachmentRef[]> = {};
    for (const [key, value] of Object.entries(parsed)) {
      if (Array.isArray(value)) box[key] = value.map((item) => toAttachmentRef(item as Record<string, unknown>));
    }
    return box;
  } catch {
    return {};
  }
}

/** 持久化待发列表（按话题分键：切话题不串）。存不了不打断使用，界面仍如实显示当前列表。 */
export function savePendingAttachments(topicId: string | null | undefined, items: AttachmentRef[]): void {
  try {
    const box = readPendingBox();
    const key = pendingKey(topicId);
    if (items.length) box[key] = items;
    else delete box[key];
    globalThis.localStorage?.setItem(PENDING_KEY, JSON.stringify(box));
  } catch {
    /* 存储不可用时静默降级：列表仍在内存里，发送路径不受影响 */
  }
}

export function loadPendingAttachments(topicId: string | null | undefined): AttachmentRef[] {
  return readPendingBox()[pendingKey(topicId)] ?? [];
}

/** 能从待发列表发送的状态（与后端绑定校验一致：prepared / ready / changed）。 */
export function isBindable(ref: AttachmentRef): boolean {
  return ref.state === "prepared" || ref.state === "ready" || ref.state === "changed";
}

/**
 * 恢复待发附件：**用户看到的必须等于将发送的**，所以逐条向后端核对现在的事实：
 * 还在不在、属不属于本话题、有没有被别的轮次绑走（已被绑走的不能再发）。
 * 对不上的丢掉并报出名字（调用方显示原因），绝不「显示着但其实发不出去」。
 */
export async function restorePendingAttachments(
  topicId: string | null | undefined,
): Promise<{ items: AttachmentRef[]; dropped: string[] }> {
  const stored = loadPendingAttachments(topicId);
  const items: AttachmentRef[] = [];
  const dropped: string[] = [];
  for (const item of stored) {
    try {
      const fresh = await getAttachment(item.id);
      if (fresh.turnId) {
        dropped.push(item.name);
        continue;
      }
      if (topicId && fresh.topicId && fresh.topicId !== String(topicId)) {
        dropped.push(item.name);
        continue;
      }
      items.push({ ...fresh, name: fresh.name || item.name, error: fresh.error ?? item.error ?? null });
    } catch {
      dropped.push(item.name);
    }
  }
  savePendingAttachments(topicId, items);
  return { items, dropped };
}

