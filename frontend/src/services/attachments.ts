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
  /**
   * F10：恢复时**暂时没能确认**（超时 / 5xx / 离线 / 鉴权失败）。
   * 身份与原持久化数据保留，但状态未知 —— 不能当成就绪发送；界面显示「暂时无法确认」+ 重试。
   */
  unconfirmed?: boolean;
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
    // F24：后端可能把「已受理、正在准备」表达成这些非最终态别名（E 的字段定稿前的兼容）。
    // 一律归到 prepared（准备中），让等待逻辑继续跟进，而不是误判成最终失败。
    case "preparing":
    case "processing":
    case "pending":
    case "queued":
    case "running":
    case "accepted":
    case "copying":
    case "uploading":
      return "prepared";
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
  // F10：这个标记是前端的「暂时无法确认」事实，持久化后刷新仍要如实显示。
  if (payload.unconfirmed === true) ref.unconfirmed = true;
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
  // 暂时无法确认（F10）：给一个「重试」入口 —— 重试的是**重新向后端核对**（见 Composer.retryOne），
  // 不是重新复制内容，所以不依赖后端的 retry 动作。
  if (ref.unconfirmed) return ["retry"];
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
  // 暂时无法确认的附件（F10）状态未知：不能当成就绪发送。
  return ref.state === "ready" && !ref.unconfirmed;
}

/** 一句话状态（界面与错误提示共用，避免各处自己编词）。 */
export function stateText(ref: AttachmentRef): string {
  if (ref.unconfirmed) return "暂时无法确认";
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

/** 非最终状态（还在准备）：响应说这些 = 后端已受理、还在准备，前端必须继续等（F24）。 */
const PREPARING_STATES = new Set([
  "prepared",
  "preparing",
  "processing",
  "pending",
  "queued",
  "running",
  "accepted",
  "copying",
  "uploading",
]);

/** 后端响应给出的准备状态：final（可以收手）/ pending（继续等）/ unknown（没给出状态）。 */
type AttachmentPayloadKind = "final" | "pending" | "unknown";

function attachmentPayloadKind(payload: Record<string, unknown>): AttachmentPayloadKind {
  // 显式最终标记最权威（E 定稿后可能用 final）。
  if (payload.final === true) return "final";
  if (payload.final === false) return "pending";
  if (payload.preparing === true || payload.processing === true) return "pending";
  const raw = payload.state ?? payload.status;
  if (typeof raw !== "string" || !raw) return "unknown";
  return PREPARING_STATES.has(raw.toLowerCase()) ? "pending" : "final";
}

/** 取原始 payload：判定「是否最终态」必须看后端原话，不能只看归一化后的 5 个状态。 */
async function getAttachmentRaw(id: string): Promise<Record<string, unknown>> {
  const res = await request<AttachmentPayload>("/api/attachments/" + encodeURIComponent(id));
  return res.attachment ?? {};
}

/**
 * 等待附件离开「准备中」（F24：只有最终态才收手）。
 *
 * 判据（契约 C4/C5）：响应含 preparing/processing 标识、state 是非最终态别名、或 final === false
 * → **继续等待**；真正的最终态（ready/failed/missing/changed/cancelled）才返回。状态完全缺失时
 * **有界等待**（不永久等待），超时后如实停在「准备中」，不谎报失败。
 *
 * 假设：E 的后端把「已受理且正在准备」与最终 missing/failed 区分开的字段尚未定稿，
 * 这里对多种形态兼容；定稿后可收窄到确切字段。
 */
export async function waitUntilSettled(
  ref: AttachmentRef,
  options: { timeoutMs?: number; intervalMs?: number; onUpdate?: (next: AttachmentRef) => void } = {},
): Promise<AttachmentRef> {
  const timeoutMs = options.timeoutMs ?? 120_000;
  const intervalMs = options.intervalMs ?? 400;
  const deadline = Date.now() + timeoutMs;
  let current = ref;
  // 输入已经是最终态（非 prepared）：直接返回，不额外请求。
  if (current.state !== "prepared") return current;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
    const payload = await getAttachmentRaw(ref.id);
    const kind = attachmentPayloadKind(payload);
    if (kind === "final") {
      current = toAttachmentRef(payload);
      options.onUpdate?.(current);
      return current;
    }
    if (kind === "pending") {
      current = toAttachmentRef(payload);
      options.onUpdate?.(current);
    }
    // unknown：后端没给出状态 —— 不据此报失败、也不当成最终态；留在当前状态继续有界等待。
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

/**
 * blob URL 交给新窗口后的释放延迟。
 *
 * **不能立即 revoke**：新文档可能还在读这个 URL；而且 `noopener` 打开没有可依赖的完成信号，
 * 立刻释放会把「已经打开」变成一次空白页。保留一段时间再释放，正确性与内存都有界。
 */
const BLOB_URL_TTL_MS = 60_000;

/** 浏览器打开一个 Blob 的结果：可验证失败 / 已交出打开请求（无法同步确认新窗口是否真的出现）。 */
export type BlobOpenOutcome =
  | { status: "handed" }
  | { status: "unavailable"; reason: string };

/** 尽力释放一个 blob URL：不因重复释放失败而抛错，也不吞掉调用方的其它异常。 */
function revokeBlobUrlSoon(url: string, delayMs: number): void {
  setTimeout(() => {
    try {
      URL.revokeObjectURL(url);
    } catch {
      /* 已经释放过就算了 */
    }
  }, delayMs);
}

/**
 * 在浏览器新标签页打开一个 Blob。
 *
 * 诚实规则（F03）：`window.open(url, "_blank", "noopener,noreferrer")` 在 noopener 下
 * **成功打开也会返回 null**，所以这个返回值**不是**成败判据 —— 只要调用没有抛错，就当作
 * 「打开请求已交给浏览器」（handed）；只有可验证的失败（没有 window / 不支持 Blob URL /
 * window.open 抛错）才返回 unavailable，由调用方如实说明并回退下载。
 * 安全参数一个都不去掉：不用拿到窗口句柄换取「能判成功」。
 */
export function openBlobInNewTab(blob: Blob): BlobOpenOutcome {
  if (typeof window === "undefined") return { status: "unavailable", reason: "这个环境没有浏览器窗口" };
  const url = blobUrlOf(blob);
  if (!url) return { status: "unavailable", reason: "这个环境不支持 Blob 查看（可以下载副本）" };
  const open = window.open;
  if (typeof open !== "function") {
    revokeBlobUrlSoon(url, 0);
    return { status: "unavailable", reason: "这个环境不允许打开新窗口（可以下载副本）" };
  }
  try {
    // 返回值丢弃：noopener 下 null 既可能是「被拦截」也可能是「已经打开」，无法同步区分。
    open.call(window, url, "_blank", "noopener,noreferrer");
  } catch (err) {
    revokeBlobUrlSoon(url, 0);
    return { status: "unavailable", reason: "浏览器拒绝了打开新窗口（" + (err as Error).message + "）" };
  }
  revokeBlobUrlSoon(url, BLOB_URL_TTL_MS);
  return { status: "handed" };
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
  if (plan.action === "view") {
    const outcome = openBlobInNewTab(blob);
    if (outcome.status === "handed") {
      return {
        action: "view",
        note: "已交给浏览器在新标签页打开；如果没有出现，可能是浏览器拦截了弹窗（允许弹窗后重试，或用「下载」保存副本）",
      };
    }
    // 只有可验证的打开失败才回退下载：不把「已经打开但返回 null」误判成失败、更不自动再下载一次。
    downloadBlob(blob, ref.name);
    return { action: "download", note: outcome.reason + "：已改为下载副本" };
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
  bumpPendingRevision(topicId);
}

export function loadPendingAttachments(topicId: string | null | undefined): AttachmentRef[] {
  return readPendingBox()[pendingKey(topicId)] ?? [];
}

// ---------------------------------------------------------------------------
// 待发列表的修订号 / 移除 tombstone / 已发送失效集（契约 K1.4—K1.6）
// ---------------------------------------------------------------------------

/**
 * 修订号：每次待发列表落盘 +1。恢复（restorePendingAttachments）带上它，
 * Composer 才知道「恢复在途期间有没有人写过这个话题」，从而决定补丁的生效范围。
 */
const REVISION_KEY = "qio.pending-attachments.revision.v1";

/**
 * 已移除附件的 tombstone：键含 topicId + id，值 = **单调移除序号**。
 *
 * 为什么必须持久化：组件卸载/重挂载、切话题、刷新之后，晚到的轮询或恢复结果不能把
 * 用户已经移除的附件 upsert 回来 —— 只靠组件内存里的 Set 挡不住重挂载。
 * 序号的作用：恢复补丁里出现某个候选，说明服务核对该候选时它还没有 tombstone；
 * 只有「移除水位之后」才被移除的候选才需要在合并时挡住（见 attachmentOps.mergeRestorePatch）。
 * 清理时机：发送被受理、用户显式重新选择同一个文件、或话题被清空。
 */
const REMOVED_KEY = "qio.pending-attachments.removed.v1";

/** 移除序号：进程内单调；跨会话用时间戳起算（旧会话的序号不会比新会话大）。 */
let removedSeq = Date.now();
function nextRemovedSeq(): number {
  removedSeq = Math.max(Date.now(), removedSeq + 1);
  return removedSeq;
}

/**
 * 存储不可用时的内存镜像：tombstone 语义仍然成立（不因存储故障而复活已移除项）。
 * 只在存储读写失败（degraded）时参与判定，避免镜像与磁盘各说各话。
 */
const removedFallback = new Map<string, Map<string, number>>();
let removedStorageDegraded = false;

function readJsonObject<T>(key: string): Record<string, T> | null {
  try {
    const raw = globalThis.localStorage?.getItem(key);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const box: Record<string, T> = {};
    for (const [entryKey, value] of Object.entries(parsed)) {
      if (value !== undefined) box[entryKey] = value as T;
    }
    return box;
  } catch {
    return null;
  }
}

function writeJsonObject(key: string, box: Record<string, unknown>): boolean {
  try {
    globalThis.localStorage?.setItem(key, JSON.stringify(box));
    return true;
  } catch {
    return false;
  }
}

function removedRecord(topicId: string | null | undefined): Record<string, number> {
  const key = pendingKey(topicId);
  const box = readJsonObject<Record<string, number>>(REMOVED_KEY);
  if (box === null) removedStorageDegraded = true;
  const stored = box?.[key] ?? {};
  if (!removedStorageDegraded) return { ...stored };
  const memory = removedFallback.get(key);
  return memory ? { ...stored, ...Object.fromEntries(memory) } : { ...stored };
}

/** 该话题当前已移除（tombstone）的附件 id。 */
export function loadRemovedAttachmentIds(topicId: string | null | undefined): string[] {
  return Object.keys(removedRecord(topicId));
}

export function isAttachmentRemoved(topicId: string | null | undefined, id: string): boolean {
  return Object.prototype.hasOwnProperty.call(removedRecord(topicId), id);
}

/** 这条 tombstone 的移除序号（0 = 没有 tombstone）。 */
export function attachmentRemovedSeq(topicId: string | null | undefined, id: string): number {
  return removedRecord(topicId)[id] ?? 0;
}

/** 移除水位：发起异步核对前捕获，用来判断「之后有没有这条被移除」。 */
export function attachmentRemovalBarrier(): number {
  return removedSeq;
}

/** 记录一条移除 tombstone（K1.4：随持久化一起存，跨卸载重挂载有效）。 */
export function markAttachmentRemoved(topicId: string | null | undefined, id: string): number {
  const key = pendingKey(topicId);
  const seq = nextRemovedSeq();
  const box = readJsonObject<Record<string, number>>(REMOVED_KEY);
  const stored = box?.[key] ?? {};
  const written = box !== null && writeJsonObject(REMOVED_KEY, { ...box, [key]: { ...stored, [id]: seq } });
  if (!written) {
    removedStorageDegraded = true;
    const memory = removedFallback.get(key) ?? new Map<string, number>();
    memory.set(id, seq);
    removedFallback.set(key, memory);
  }
  return seq;
}

/** 撤销一条 tombstone：删除失败（这一条其实还在）、或用户显式重新添加同一个 id。 */
export function forgetAttachmentRemoved(topicId: string | null | undefined, id: string): void {
  const key = pendingKey(topicId);
  const memory = removedFallback.get(key);
  if (memory) {
    memory.delete(id);
    if (!memory.size) removedFallback.delete(key);
  }
  const box = readJsonObject<Record<string, number>>(REMOVED_KEY);
  if (box === null || !box[key] || !(id in box[key])) return;
  const next = { ...box[key] };
  delete next[id];
  if (Object.keys(next).length) box[key] = next;
  else delete box[key];
  writeJsonObject(REMOVED_KEY, box);
}

/** 清理 tombstones：发送被受理（这批已经进轮次）、或话题被清空。 */
export function clearAttachmentTombstones(
  topicId: string | null | undefined,
  ids?: readonly string[],
): void {
  const key = pendingKey(topicId);
  if (!ids) {
    removedFallback.delete(key);
    const box = readJsonObject<Record<string, number>>(REMOVED_KEY);
    if (box && box[key]) {
      delete box[key];
      writeJsonObject(REMOVED_KEY, box);
    }
    return;
  }
  for (const id of ids) forgetAttachmentRemoved(topicId, id);
}

/** 仅供测试：清掉 tombstone 的内存镜像（磁盘内容与真实存储路径无关）。 */
export function clearRemovedAttachmentMemory(): void {
  removedFallback.clear();
  removedStorageDegraded = false;
}

/** 该话题待发列表的修订号（0 = 从未写过）。 */
export function pendingRevision(topicId: string | null | undefined): number {
  const value = readJsonObject<number>(REVISION_KEY)?.[pendingKey(topicId)];
  return typeof value === "number" ? value : 0;
}

function bumpPendingRevision(topicId: string | null | undefined): number {
  const key = pendingKey(topicId);
  const box = readJsonObject<number>(REVISION_KEY) ?? {};
  const next = (typeof box[key] === "number" ? box[key] : 0) + 1;
  box[key] = next;
  writeJsonObject(REVISION_KEY, box);
  return next;
}

// --- 已发送失效集（K1.5）：随发送受理生效；会话级（刷新后由服务端绑定事实兜底） --------

const sentAttachments = new Set<string>();

function sentKey(topicId: string | null | undefined, id: string): string {
  return pendingKey(topicId) + "\u0000" + id;
}

/** 这批附件已随发送受理绑到轮次：晚到的结果必须静默丢弃。 */
export function markAttachmentsSent(topicId: string | null | undefined, ids: readonly string[]): void {
  for (const id of ids) sentAttachments.add(sentKey(topicId, id));
}

export function isAttachmentSent(topicId: string | null | undefined, id: string): boolean {
  return sentAttachments.has(sentKey(topicId, id));
}

export function forgetSentAttachments(
  topicId: string | null | undefined,
  ids?: readonly string[],
): void {
  if (ids) {
    for (const id of ids) sentAttachments.delete(sentKey(topicId, id));
    return;
  }
  const prefix = pendingKey(topicId) + "\u0000";
  for (const key of [...sentAttachments]) {
    if (key.startsWith(prefix)) sentAttachments.delete(key);
  }
}

/** 已移除或已发送：晚到的结果一律不得复活它。 */
export function isAttachmentDead(topicId: string | null | undefined, id: string): boolean {
  return isAttachmentRemoved(topicId, id) || isAttachmentSent(topicId, id);
}

// ---------------------------------------------------------------------------
// 待发附件「收件箱」：跨组件把**已确认可用**的新附件送进发起话题的待发列表（F04）
// ---------------------------------------------------------------------------

export interface PendingAttachmentEvent {
  /** 发起操作的话题（Composer 只在自己的当前话题匹配时更新 UI） */
  topicId: string | null;
  attachment: AttachmentRef;
}

const pendingListeners = new Set<(event: PendingAttachmentEvent) => void>();

/**
 * 订阅「有附件加入待发列表」事件。返回退订函数。
 *
 * 与 composerMetrics 的模块级订阅同一模式：Composer 与 MessageItem 都依赖本模块，
 * 组件之间不需要互相挂事件，也不改其它人的 store。
 */
export function subscribePendingAttachment(
  listener: (event: PendingAttachmentEvent) => void,
): () => void {
  pendingListeners.add(listener);
  return () => {
    pendingListeners.delete(listener);
  };
}

/**
 * 把一个**已确认可进入待发列表**的新附件加入某个话题并广播。
 *
 * 历史消息的「重新上传」用它：新附件进入**发起话题**的待发送列表，原历史记录保持不变。
 * 同 id 重复加入按更新处理（不产生重复条目）。
 */
export function addPendingAttachment(
  topicId: string | null | undefined,
  attachment: AttachmentRef,
): boolean {
  // K1.3：已移除（tombstone）或已随发送受理（sent）的 id 绝不复活 —— 迟到的广播也不许。
  // 显式重新添加同一个文件走 registerAttachmentIdentity（它会清理这两处痕迹）。
  if (isAttachmentDead(topicId, attachment.id)) return false;
  const list = loadPendingAttachments(topicId);
  const next = list.some((item) => item.id === attachment.id)
    ? list.map((item) => (item.id === attachment.id ? attachment : item))
    : [...list, attachment];
  savePendingAttachments(topicId, next);
  const event: PendingAttachmentEvent = { topicId: topicId ?? null, attachment };
  for (const listener of pendingListeners) listener(event);
  return true;
}

/** 能从待发列表发送的状态（与后端绑定校验一致：prepared / ready / changed）。 */
export function isBindable(ref: AttachmentRef): boolean {
  return ref.state === "prepared" || ref.state === "ready" || ref.state === "changed";
}

export interface RestorePendingOutcome {
  /** 已确认仍在待发列表、且核对期间没被别的轮绑走的附件（用后端新事实）。 */
  items: AttachmentRef[];
  /** **确认永久无效**（404/410 / 已绑定别的轮 / 不属于本话题）：已从持久化清理，报出名字。 */
  dropped: string[];
  /** 暂时没能确认（超时 / 5xx / 离线 / 鉴权失败）：身份与原数据保留，界面显示可重试。 */
  unconfirmed: AttachmentRef[];
}

/** 404/410 = 后端明确说「这个附件不存在 / 已永久失效」；其余错误一律按**暂时失败**处理（F10）。 */
function isPermanentlyGone(err: unknown): boolean {
  return err instanceof AttachmentRequestError && (err.status === 404 || err.status === 410);
}

/**
 * 恢复待发附件：**用户看到的必须等于将发送的**，所以逐条向后端核对现在的事实：
 * 还在不在、属不属于本话题、有没有被别的轮次绑走（已被绑走的不能再发）。
 *
 * F10 区分两种「对不上」：
 *   * **确认永久无效**（404/410、已绑走、不属于本话题）→ 清理持久化并报出名字；
 *   * **暂时无法确认**（超时 / 5xx / 离线 / 鉴权失败）→ 保留身份与原持久化数据，
 *     返回 unconfirmed，界面展示可重试状态，**绝不清空**。
 * 落盘时合并「恢复期间新增」的条目，旧恢复不覆盖用户编辑（F08）。
 */
/** 恢复补丁请求（K1.6）：候选与发起时的修订号；服务**不再自行写持久化**。 */
export interface RestorePatchOptions {
  /** 要核对的候选 id（缺省 = 该话题持久化里的条目） */
  candidateIds?: readonly string[];
  /** 发起恢复时的列表修订号（原样回显，发起方据此判断恢复期间有没有人写过） */
  revision?: number;
}

/** 恢复补丁结果（K1.6）：由 Composer 合并；只新增、绝不复活 removed/sent。 */
export interface RestorePendingPatch {
  topicId: string | null;
  revision: number;
  /** 确认可用（含暂时无法确认、带 unconfirmed 标记）的条目 */
  restored: AttachmentRef[];
  /** 确认永久无效的名字（给人看） */
  missing: string[];
  /** 确认永久无效的 id（用来从当前列表剔除） */
  missingIds: string[];
}

/** 候选还不够了解时的最小引用：核对成功后由后端事实替换，失败则如实显示「暂时无法确认」。 */
function unverifiedRef(id: string): AttachmentRef {
  return {
    id,
    name: id,
    sizeBytes: 0,
    kind: "copy",
    display: COPY_LABEL,
    state: "prepared",
    error: null,
  };
}

export function restorePendingAttachments(
  topicId: string | null | undefined,
): Promise<RestorePendingOutcome>;
export function restorePendingAttachments(
  topicId: string | null | undefined,
  options: RestorePatchOptions,
): Promise<RestorePendingPatch>;
export async function restorePendingAttachments(
  topicId: string | null | undefined,
  options?: RestorePatchOptions,
): Promise<RestorePendingOutcome | RestorePendingPatch> {
  const stored = loadPendingAttachments(topicId);
  const storedById = new Map(stored.map((item) => [item.id, item]));
  const candidates: AttachmentRef[] = options?.candidateIds
    ? options.candidateIds.map((id) => storedById.get(id) ?? unverifiedRef(id))
    : stored;
  const items: AttachmentRef[] = [];
  const dropped: string[] = [];
  const unconfirmed: AttachmentRef[] = [];
  const droppedIds = new Set<string>();
  for (const item of candidates) {
    // K1.4：已移除（tombstone）的候选绝不复活 —— 连核对请求都不发
    if (isAttachmentRemoved(topicId, item.id)) continue;
    try {
      const fresh = await getAttachment(item.id);
      if (fresh.turnId) {
        dropped.push(item.name);
        droppedIds.add(item.id);
        continue;
      }
      if (topicId && fresh.topicId && fresh.topicId !== String(topicId)) {
        dropped.push(item.name);
        droppedIds.add(item.id);
        continue;
      }
      items.push({ ...fresh, name: fresh.name || item.name, error: fresh.error ?? item.error ?? null });
    } catch (err) {
      if (isPermanentlyGone(err)) {
        dropped.push(item.name);
        droppedIds.add(item.id);
        continue;
      }
      unconfirmed.push({ ...item, unconfirmed: true });
    }
  }

  if (options) {
    /**
     * 补丁模式（K1.6）：**不写任何持久化**。
     * 合并权在发起方（Composer）—— 只有它同时掌握当前列表、tombstone 与 sent 失效集；
     * 服务按旧快照整表回写正是「已移除附件被恢复结果重新加入」的入口。
     *
     * R3：回显值不是判据，**当前本地修订号**才是。
     * `options.revision` 是发起核对时捕获的修订号；核对在途期间本地可能又写过
     * （用户改了同一条 / 重新添加）。这时 `missingIds` 是核对那一刻的旧事实，
     * 应用它会删掉用户刚更新的记录 —— 当前修订号更大（期间有新写入）时**不应用**这次剔除，
     * 保留当前记录，交给下一次恢复清理。`revision` 仍按发起时捕获的值原样回显（调用方据此判断）。
     *
     * 用「当前 > 捕获」而不是「不相等」：修订号按话题单调递增，所以更大 = 期间确实有人写过；
     * 当前为 0（本地没有这个话题的记录）只说明没有可比的记录，不能据此声称有人写过。
     */
    const requestedRevision = typeof options.revision === "number" ? options.revision : null;
    const revisionMoved = requestedRevision !== null && pendingRevision(topicId) > requestedRevision;
    return {
      topicId: topicId ?? null,
      revision: requestedRevision ?? pendingRevision(topicId),
      restored: [...items, ...unconfirmed],
      missing: revisionMoved ? [] : dropped,
      missingIds: revisionMoved ? [] : [...droppedIds],
    } satisfies RestorePendingPatch;
  }

  // 旧调用形状（不带 options）的兼容路径：沿用既有「自行合并落盘」语义（F08/F10 用例仍走这里）。
  // 生产调用方（Composer）一律走上面的补丁模式，不再由服务整表回写旧快照。
  // 落盘：确认可用的用新事实；确认无效的清理；暂时失败的保留；恢复期间新写入的不能被抹掉。
  const storedIds = new Set(stored.map((i) => i.id));
  const confirmedById = new Map(items.map((i) => [i.id, i]));
  const unconfirmedById = new Map(unconfirmed.map((i) => [i.id, i]));
  const merged: AttachmentRef[] = [];
  for (const s of stored) {
    if (droppedIds.has(s.id)) continue;
    const confirmed = confirmedById.get(s.id);
    if (confirmed) {
      merged.push(confirmed);
      continue;
    }
    const kept = unconfirmedById.get(s.id);
    if (kept) merged.push(kept);
  }
  const current = readPendingBox()[pendingKey(topicId)] ?? [];
  for (const c of current) {
    if (storedIds.has(c.id) || droppedIds.has(c.id)) continue;
    if (merged.some((m) => m.id === c.id)) continue;
    merged.push(c);
  }
  savePendingAttachments(topicId, merged);
  return { items, dropped, unconfirmed };
}

