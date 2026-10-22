/**
 * 对话页右侧只读预览：纯逻辑（C 拥有）。
 *
 * 这一份只放**可单测的纯函数**，不 import vue、不读写 DOM（剪贴板辅助除外，已做环境守卫）：
 *   1. 预览身份与标签去重（契约 §4.3：身份不是文件名）；
 *   2. 每个标签各自的轻量阅读位置（只存数字与短串）；
 *   3. 后端预览结果 / HTTP 失败 → 诚实的界面状态（缺失、无权限、过大、解析失败、不支持分开说）；
 *   4. 文件内容搜索（行 / 单元格 / 幻灯片 / 段落）；
 *   5. 版本竞争：迟到的旧请求不得覆盖新版本（纯逻辑，组件只负责发号与丢弃）。
 *
 * 标签与位置最终写进 conversationShell 的 previewTabs / activePreviewTabId（服务端持久化），
 * 这里不存正文、不存原始输出。
 */
import type { AttachmentRef } from "../services/attachments";
import { COPY_LABEL, REFERENCE_LABEL } from "../services/attachments";
import type { CodeView } from "../interactive/renderers/code";
import type { DocxView } from "../interactive/renderers/docx";
import type { PptxView } from "../interactive/renderers/pptx";
import type { XlsxView } from "../interactive/renderers/xlsx";
import { kindForName, rendererFor } from "../interactive/previewPlan";
import type { PreviewKind, PreviewPosition, PreviewTabState, PreviewTarget } from "./previewTypes";
import { attachmentPreviewId, replyCodePreviewId, taskOutputPreviewId } from "./previewTypes";

// ---------------------------------------------------------------------------
// 格式与能力
// ---------------------------------------------------------------------------

/** 预览类别的人话标签（标题/标签栏第二行用；不把内部枚举直接给用户看）。 */
export const PREVIEW_KIND_LABEL: Record<PreviewKind, string> = {
  image: "图片",
  pdf: "PDF",
  docx: "Word 文档",
  xlsx: "表格",
  pptx: "演示文稿",
  text: "文本",
  code: "代码",
  markdown: "Markdown",
  audio: "音频",
  video: "视频",
  unsupported: "文件",
};

/** 纯文本类扩展名：界面上是「文本」而不是「代码」（与 interactive/previewPlan 同口径）。 */
const PLAIN_TEXT_EXT = new Set(["txt", "log", "csv", "tsv", "rst", "tex"]);
const AUDIO_EXT = new Set(["mp3", "wav", "m4a", "ogg", "oga", "flac", "aac", "opus", "wma"]);
const VIDEO_EXT = new Set(["mp4", "webm", "mov", "mkv", "avi", "m4v", "wmv", "mpg", "mpeg"]);

export function extensionOf(name: string): string {
  const base = String(name || "").replace(/\\/g, "/").split("/").pop() ?? "";
  const dot = base.lastIndexOf(".");
  return dot <= 0 ? "" : base.slice(dot + 1).toLowerCase();
}

/** 文件名 → 预览类别。分类表复用 interactive/previewPlan（不复制第二套 Office 判断）。 */
export function previewKindForName(name: string, mime?: string | null): PreviewKind {
  const ext = extensionOf(name);
  if (AUDIO_EXT.has(ext)) return "audio";
  if (VIDEO_EXT.has(ext)) return "video";
  const mimeText = String(mime || "").toLowerCase();
  if (mimeText.startsWith("image/")) return "image";
  if (mimeText === "application/pdf") return "pdf";
  if (mimeText.startsWith("audio/")) return "audio";
  if (mimeText.startsWith("video/")) return "video";
  const renderer = rendererFor(kindForName(name), name);
  if (renderer === "markdown") return "markdown";
  if (renderer === "code") return PLAIN_TEXT_EXT.has(ext) ? "text" : "code";
  if (renderer === "none") return "unsupported";
  return renderer as PreviewKind;
}

/** 能在右侧栏内读完的类别（音频/视频不在内嵌阅读里，保留系统打开/下载）。 */
export function isInlinePreviewKind(kind: PreviewKind): boolean {
  return kind !== "audio" && kind !== "video" && kind !== "unsupported";
}

/** 有「文字搜索」意义的类别（PDF 在文字层可提取时支持逐页搜索）。 */
export function isSearchableKind(kind: PreviewKind): boolean {
  return (
    kind === "pdf" ||
    kind === "text" ||
    kind === "code" ||
    kind === "markdown" ||
    kind === "docx" ||
    kind === "xlsx" ||
    kind === "pptx"
  );
}

/** 阅读缩放对哪些类别有意义（文档正文按字号缩放；图片/PDF 按实际比例）。 */
export function supportsZoom(kind: PreviewKind): boolean {
  return isInlinePreviewKind(kind);
}

/** 可以整体复制成文字的类别（图片/PDF/音视频没有可直接复制的文字层）。 */
export function supportsTextCopy(kind: PreviewKind): boolean {
  return kind === "text" || kind === "code" || kind === "markdown" || kind === "docx" || kind === "xlsx" || kind === "pptx";
}

/** 结构化转换（不是原版面）的类别：界面上必须说明范围，不能声称与 Office 排版完全一致。 */
export function isStructuredKind(kind: PreviewKind): boolean {
  return kind === "docx" || kind === "xlsx" || kind === "pptx";
}

export const STRUCTURED_SCOPE_NOTE =
  "结构化只读预览：保留正文、列表与表格，不保证与原始 Office 排版完全一致";

// ---------------------------------------------------------------------------
// 打开：身份与目标
// ---------------------------------------------------------------------------

export interface AttachmentPreviewInput {
  attachmentId: string;
  title: string;
  version?: string | null;
  mime?: string | null;
  size?: number | null;
  topicId?: string | null;
}

/** 附件标签身份：attachment:<id>@<version>（版本只用于判断内容是否更新，不拿文件名当身份）。 */
export function attachmentPreviewTarget(input: AttachmentPreviewInput): PreviewTarget {
  const attachmentId = String(input.attachmentId || "").trim();
  const title = String(input.title || "").trim() || "未命名附件";
  const version = input.version ? String(input.version) : undefined;
  return {
    id: attachmentPreviewId(attachmentId, version),
    kind: previewKindForName(title, input.mime),
    title,
    source: "attachment",
    attachmentId,
    version,
    topicId: input.topicId ?? undefined,
    mime: input.mime ?? undefined,
    size: typeof input.size === "number" ? input.size : undefined,
  };
}

export interface ReplyCodePreviewInput {
  messageId: string;
  blockIndex: number;
  /** 可读标题（例如「Python 代码」）；缺省用「回复中的代码」。 */
  title?: string;
  language?: string;
}

/** 回复代码块身份：replycode:<messageId>:<blockIndex>（同一回复的不同代码块各自独立）。 */
export function replyCodePreviewTarget(input: ReplyCodePreviewInput): PreviewTarget {
  const language = String(input.language || "").trim();
  const title = String(input.title || "").trim() || (language ? language + " 代码" : "回复中的代码");
  return {
    id: replyCodePreviewId(String(input.messageId || ""), input.blockIndex),
    kind: "code",
    title,
    source: "reply_code",
    messageId: String(input.messageId || ""),
    blockIndex: input.blockIndex,
  };
}

export interface TaskOutputPreviewInput {
  taskId: string;
  title: string;
  /** 附件 id（优先）或规范路径；两者都没有时用 "output" 表示任务输出正文。 */
  ref?: string | null;
  attachmentId?: string | null;
  filePath?: string | null;
  kind?: PreviewKind;
  mime?: string | null;
  size?: number | null;
  topicId?: string | null;
  version?: string | null;
}

/** 任务成果身份：task:<taskId>:<attachmentId 或规范路径>。 */
export function taskOutputPreviewTarget(input: TaskOutputPreviewInput): PreviewTarget {
  const title = String(input.title || "").trim() || "任务成果";
  const attachmentId = String(input.attachmentId || "").trim() || undefined;
  const filePath = String(input.filePath || "").trim() || undefined;
  const ref = attachmentId || filePath || String(input.ref || "").trim() || "output";
  const kind = input.kind ?? (attachmentId || filePath ? previewKindForName(title, input.mime) : "code");
  return {
    id: taskOutputPreviewId(String(input.taskId || ""), ref),
    kind,
    title,
    source: "task_output",
    attachmentId,
    filePath,
    version: input.version ? String(input.version) : undefined,
    topicId: input.topicId ?? undefined,
    mime: input.mime ?? undefined,
    size: typeof input.size === "number" ? input.size : undefined,
  };
}

/**
 * 同一次「打开」应该落在哪个标签上（纯函数；store 据此写 shell 的 previewTabs）。
 *
 * 去重规则（契约 §4.3 + 本任务要求「同一文件再次打开定位已有标签」）：
 *   * 身份 id 完全相同 → 复用该标签（更新元数据，保留阅读位置）；
 *   * 附件按 **attachmentId** 认同一份文件：版本变了也只是同一份文件的新内容，
 *     复用原标签并更新 id/version（位置尽量保留），**不新增第二个标签**；
 *   * 回复代码 / 任务成果按各自稳定身份去重（不同消息、不同任务不合并）。
 */
export interface PreviewTabOpenResult {
  tabs: PreviewTabState[];
  activeId: string;
  /** 打开后应被激活的标签。 */
  tab: PreviewTabState;
  /** 是否复用了已有标签（没有新增）。 */
  reused: boolean;
  /** 被复用的旧标签 id（版本变化时与 tab.id 不同，store 要迁移运行时状态）。 */
  previousId: string | null;
  /** 被替换的旧标签在数组里的下标（null = 新增到末尾）。 */
  replacedIndex: number | null;
}

function definedPatch<T extends object>(value: T): Partial<T> {
  const out: Partial<T> = {};
  for (const [key, entry] of Object.entries(value)) {
    if (entry !== undefined) (out as Record<string, unknown>)[key] = entry;
  }
  return out;
}

function mergeTarget(existing: PreviewTarget, incoming: PreviewTarget): PreviewTarget {
  return { ...existing, ...definedPatch(incoming), id: incoming.id, source: incoming.source };
}

export function openPreviewTab(tabs: PreviewTabState[], target: PreviewTarget): PreviewTabOpenResult {
  const sameId = tabs.findIndex((tab) => tab.id === target.id);
  if (sameId >= 0) {
    const existing = tabs[sameId];
    const next: PreviewTabState = {
      id: target.id,
      target: mergeTarget(existing.target, target),
      position: { ...existing.position },
    };
    const nextTabs = tabs.slice();
    nextTabs[sameId] = next;
    return {
      tabs: nextTabs,
      activeId: target.id,
      tab: next,
      reused: true,
      previousId: target.id,
      replacedIndex: sameId,
    };
  }
  if (target.source === "attachment" && target.attachmentId) {
    const sameFile = tabs.findIndex(
      (tab) => tab.target.source === "attachment" && tab.target.attachmentId === target.attachmentId,
    );
    if (sameFile >= 0) {
      const existing = tabs[sameFile];
      const next: PreviewTabState = {
        id: target.id,
        target,
        // 版本变了仍然尽量保留阅读位置；渲染时按新内容夹取（页码越界会回到最后一页）。
        position: { ...existing.position },
      };
      const nextTabs = tabs.slice();
      nextTabs[sameFile] = next;
      return {
        tabs: nextTabs,
        activeId: target.id,
        tab: next,
        reused: true,
        previousId: existing.id,
        replacedIndex: sameFile,
      };
    }
  }
  const tab: PreviewTabState = { id: target.id, target, position: {} };
  return {
    tabs: [...tabs, tab],
    activeId: target.id,
    tab,
    reused: false,
    previousId: null,
    replacedIndex: null,
  };
}

/** 关闭一个标签：只关这一份预览；当前标签被关时优先激活右邻居，其次左邻居。 */
export function closePreviewTab(
  tabs: PreviewTabState[],
  activeId: string | null,
  id: string,
): { tabs: PreviewTabState[]; activeId: string | null; closed: boolean } {
  const index = tabs.findIndex((tab) => tab.id === id);
  if (index < 0) return { tabs, activeId, closed: false };
  const next = tabs.filter((tab) => tab.id !== id);
  let nextActive = activeId && next.some((tab) => tab.id === activeId) ? activeId : null;
  if (activeId === id) nextActive = next[index]?.id ?? next[index - 1]?.id ?? null;
  return { tabs: next, activeId: nextActive, closed: true };
}

/** 当前激活的标签：activeId 失效时回退到最后一个（与 sanitizeLayoutPrefs 同一口径）。 */
export function activePreviewTab(
  tabs: PreviewTabState[],
  activeId: string | null,
): PreviewTabState | null {
  if (!tabs.length) return null;
  return tabs.find((tab) => tab.id === activeId) ?? tabs[tabs.length - 1];
}

// ---------------------------------------------------------------------------
// 阅读位置
// ---------------------------------------------------------------------------

const MIN_ZOOM = 0.25;
const MAX_ZOOM = 4;
const MAX_SHEET_CHARS = 200;
const MAX_SEARCH_CHARS = 120;

function clampNumber(value: unknown, min: number, max: number): number | undefined {
  if (typeof value !== "number" || !Number.isFinite(value)) return undefined;
  return Math.min(max, Math.max(min, Math.round(value * 1000) / 1000));
}

/**
 * 位置夹取：位置是显示偏好，坏值直接丢弃（不让一个 NaN / 负数把阅读器弄坏）。
 * 每标签独立保存；不存正文。
 */
export function clampPreviewPosition(position: Partial<PreviewPosition>): PreviewPosition {
  const out: PreviewPosition = {};
  const scrollTop = clampNumber(position.scrollTop, 0, 10_000_000);
  if (scrollTop !== undefined) out.scrollTop = scrollTop;
  const page = clampNumber(position.page, 1, 1_000_000);
  if (page !== undefined) out.page = Math.round(page);
  const zoom = clampNumber(position.zoom, MIN_ZOOM, MAX_ZOOM);
  if (zoom !== undefined) out.zoom = zoom;
  const line = clampNumber(position.line, 1, 10_000_000);
  if (line !== undefined) out.line = Math.round(line);
  if (typeof position.sheet === "string" && position.sheet.trim()) {
    out.sheet = position.sheet.trim().slice(0, MAX_SHEET_CHARS);
  }
  if (typeof position.search === "string" && position.search.trim()) {
    out.search = position.search.trim().slice(0, MAX_SEARCH_CHARS);
  }
  return out;
}

/** 合并两个位置：patch 里合法的键覆盖旧值，其余保留。 */
export function mergePreviewPosition(
  base: Partial<PreviewPosition> | null | undefined,
  patch: Partial<PreviewPosition> | null | undefined,
): PreviewPosition {
  return clampPreviewPosition({ ...(base ?? {}), ...(patch ?? {}) });
}

/** 只保留与类别相关的位置键（换标签时不会把 PDF 页码带到代码标签上）。 */
export function positionForKind(kind: PreviewKind, position: PreviewPosition): PreviewPosition {
  const next = clampPreviewPosition(position);
  if (kind === "pdf") return { page: next.page, zoom: next.zoom, scrollTop: next.scrollTop };
  if (kind === "xlsx") return { sheet: next.sheet, scrollTop: next.scrollTop, zoom: next.zoom, search: next.search };
  if (kind === "pptx") return { page: next.page, scrollTop: next.scrollTop, zoom: next.zoom, search: next.search };
  if (kind === "image") return { zoom: next.zoom, scrollTop: next.scrollTop };
  return { scrollTop: next.scrollTop, zoom: next.zoom, search: next.search, line: next.line };
}

export function zoomStep(current: number, direction: 1 | -1, min = 0.5, max = 3): number {
  const base = Number.isFinite(current) && current > 0 ? current : 1;
  const next = direction > 0 ? base * 1.25 : base / 1.25;
  return Math.min(max, Math.max(min, Math.round(next * 100) / 100));
}

// ---------------------------------------------------------------------------
// 失败 / 不支持：诚实分类（缺失、无权限、过大、解析失败、不支持分开）
// ---------------------------------------------------------------------------

export type PreviewProblemKind =
  | "missing"
  | "permission"
  | "reference_only"
  | "content_missing"
  | "too_large"
  | "parse_failed"
  | "unsupported"
  | "legacy_office"
  | "archived"
  | "network"
  | "media"
  | "unknown";

export interface PreviewProblem {
  kind: PreviewProblemKind;
  /** 一句话主因（可展示）。 */
  title: string;
  /** 后端/网络给的真实原因（可展示；不能编造）。 */
  detail: string;
  /** 重试有意义（网络抖动 / 权限 / 解析失败可能是暂时状态；缺失/过大/不支持不给假重试）。 */
  retryable: boolean;
  /** 有真实附件 / 本地路径可交给系统打开。 */
  openable: boolean;
  downloadable: boolean;
  copyable: boolean;
}

export interface PreviewProblemInput {
  /** 预览接口的 HTTP 状态（请求失败时）；200 的失败结果传 null。 */
  httpStatus?: number | null;
  /** 失败原因（HTTP detail / envelope.reason / 前端异常消息）。 */
  message?: string | null;
  /** envelope.status（ready / failed / unsupported）。 */
  status?: string | null;
  /** target 里是否真有可打开的附件或路径。 */
  hasOpenTarget?: boolean;
  /** 可复制的正文是否存在（回复代码 / 任务输出文本）。 */
  hasCopyText?: boolean;
  /** 已明确是「正文被保留策略清理 / 内容不在当前页面」的说明。 */
  unavailableReason?: string | null;
}

const TOO_LARGE_RE = /超过[^。；;]*上限|超过预览|过大|超出上限/;
const MISSING_RE = /没有这个附件|附件不存在|已被移除|不存在|已经不在/;
const ENCRYPTED_RE = /加密|需要密码|password/i;
const BROKEN_RE = /损坏|不是有效|打不开|解析失败|读不出|无法解析|二进制|不是文本/;
const REFERENCE_RE = /引用本地文件|引用型/;
const COPY_MISSING_RE = /副本文件已经不在了|没有 QIO 管理的副本/;
const NETWORK_RE = /没有响应|网络|超时|断网|offline|fetch|Failed to fetch|NetworkError/i;

export function classifyPreviewProblem(input: PreviewProblemInput): PreviewProblem {
  const message = String(input.message || "").trim();
  const status = String(input.status || "").toLowerCase();
  const httpStatus = typeof input.httpStatus === "number" ? input.httpStatus : null;
  const openable = input.hasOpenTarget === true;
  const copyable = input.hasCopyText === true;
  const base = {
    detail: message,
    openable,
    downloadable: openable,
    copyable,
  };
  if (input.unavailableReason) {
    const reason = String(input.unavailableReason).trim();
    return {
      ...base,
      kind: "archived",
      title: "这份内容已不在当前会话里",
      detail: reason || message,
      retryable: false,
      openable,
      downloadable: openable,
    };
  }
  if (httpStatus === 401 || httpStatus === 403) {
    return {
      ...base,
      kind: "permission",
      title: "没有权限读取这个文件",
      detail: message || "本机 API 拒绝了这次请求",
      retryable: true,
      openable,
      downloadable: openable,
    };
  }
  if (httpStatus === 404 || (!httpStatus && MISSING_RE.test(message))) {
    return {
      ...base,
      kind: "missing",
      title: "这个附件已不存在（可能已被移除）",
      detail: message,
      retryable: false,
      openable: false,
      downloadable: false,
    };
  }
  if (httpStatus === 410) {
    return {
      ...base,
      kind: "archived",
      title: "这个文件的内容已被清理",
      detail: message,
      retryable: false,
      openable: false,
      downloadable: false,
    };
  }
  if (httpStatus === 409 || (!httpStatus && REFERENCE_RE.test(message))) {
    if (COPY_MISSING_RE.test(message)) {
      return {
        ...base,
        kind: "content_missing",
        title: "QIO 保存的副本文件已经不在了",
        detail: message,
        retryable: true,
        openable,
        downloadable: openable,
      };
    }
    return {
      ...base,
      kind: "reference_only",
      title: "这是引用本地文件的附件，没有可读取的副本",
      detail: message,
      retryable: false,
      openable,
      downloadable: false,
    };
  }
  if (NETWORK_RE.test(message)) {
    return {
      ...base,
      kind: "network",
      title: "读取失败：与 QIO 的连接没有响应",
      detail: message,
      retryable: true,
      openable,
      downloadable: openable,
    };
  }
  if (status === "unsupported") {
    if (/旧版 Office|legacy/i.test(message)) {
      return {
        ...base,
        kind: "legacy_office",
        title: "旧版 Office（.doc/.xls/.ppt）不能内嵌预览",
        detail: message,
        retryable: false,
        openable,
        downloadable: openable,
      };
    }
    return {
      ...base,
      kind: "unsupported",
      title: "这种格式暂不支持内嵌预览",
      detail: message,
      retryable: false,
      openable,
      downloadable: openable,
    };
  }
  if (TOO_LARGE_RE.test(message)) {
    return {
      ...base,
      kind: "too_large",
      title: "文件太大，超过内嵌预览上限",
      detail: message,
      retryable: false,
      openable,
      downloadable: openable,
    };
  }
  if (ENCRYPTED_RE.test(message)) {
    return {
      ...base,
      kind: "parse_failed",
      title: "这个文件有加密（需要密码），不能内嵌预览",
      detail: message,
      retryable: false,
      openable,
      downloadable: openable,
    };
  }
  if (status === "failed" || BROKEN_RE.test(message) || message) {
    return {
      ...base,
      kind: "parse_failed",
      title: "这个文件解析失败",
      detail: message || "后端没有给出失败原因",
      retryable: true,
      openable,
      downloadable: openable,
    };
  }
  return {
    ...base,
    kind: "unknown",
    title: "暂时无法预览这个文件",
    detail: message,
    retryable: true,
    openable,
    downloadable: openable,
  };
}

/** 音频 / 视频：不做内嵌阅读，但保留真实的系统打开与下载（不是「不支持」的失败态）。 */
export function mediaPreviewProblem(kind: "audio" | "video"): PreviewProblem {
  const word = kind === "audio" ? "音频" : "视频";
  return {
    kind: "media",
    title: kind === "audio" ? "音频不做内嵌播放" : "视频不做内嵌播放",
    detail:
      "用「打开」交给系统播放器；浏览器里可以下载副本。" + word + "内容不参与内嵌阅读（只读预览不改动原文件）。",
    retryable: false,
    openable: true,
    downloadable: true,
    copyable: false,
  };
}

// ---------------------------------------------------------------------------
// 打开 / 下载用的附件引用
// ---------------------------------------------------------------------------

/** 从预览目标构造既有附件打开链路要的 AttachmentRef（不新建第二套文件身份）。 */
export function attachmentRefForTarget(target: PreviewTarget, sizeBytes = 0): AttachmentRef | null {
  const name = String(target.title || "").trim() || "未命名附件";
  if (target.attachmentId) {
    return {
      id: target.attachmentId,
      name,
      sizeBytes: typeof target.size === "number" ? target.size : sizeBytes,
      kind: "copy",
      display: COPY_LABEL,
      state: "ready",
      error: null,
    };
  }
  if (target.source === "task_output" && target.filePath) {
    return {
      id: target.id,
      name,
      sizeBytes: typeof target.size === "number" ? target.size : sizeBytes,
      kind: "reference",
      display: REFERENCE_LABEL,
      state: "ready",
      error: null,
      storedPath: target.filePath,
    };
  }
  return null;
}

// ---------------------------------------------------------------------------
// 文件内容搜索（行 / 段落 / 单元格 / 幻灯片）
// ---------------------------------------------------------------------------

export interface PreviewSearchHit {
  /** 稳定 key（跳转用）。 */
  key: string;
  /** 可读定位（「第 12 行」「Sheet1!B3」「第 2 张幻灯片」）。 */
  label: string;
  line?: number;
  page?: number;
  slide?: number;
  sheet?: string;
  sheetIndex?: number;
  block?: number;
  table?: number;
  row?: number;
  cell?: string;
  /** 命中的那一小段原文（截断展示，不伪造）。 */
  snippet: string;
}

export interface PreviewSearchResult {
  hits: PreviewSearchHit[];
  /** 命中太多时只返回前 limit 条（界面如实说「仅显示前 N 处」）。 */
  truncated: boolean;
  query: string;
}

export const SEARCH_HIT_LIMIT = 200;

function normalizeQuery(query: string): string {
  return String(query || "").trim();
}

function snippetOf(text: string, query: string, max = 60): string {
  const index = text.toLowerCase().indexOf(query.toLowerCase());
  const start = Math.max(0, index - 16);
  const piece = text.slice(start, start + max);
  return (start > 0 ? "…" : "") + piece + (start + max < text.length ? "…" : "");
}

function emptySearch(query: string): PreviewSearchResult {
  return { hits: [], truncated: false, query: normalizeQuery(query) };
}

function pushHits(
  hits: PreviewSearchHit[],
  limit: number,
  query: string,
): PreviewSearchResult {
  return { hits: hits.slice(0, limit), truncated: hits.length > limit, query: normalizeQuery(query) };
}

/** 把一行文本按查询词切成高亮片段（大小写不敏感；没有命中就返回整段）。 */
export function highlightSegments(text: string, query: string): { text: string; hit: boolean }[] {
  const needle = normalizeQuery(query);
  if (!needle) return [{ text, hit: false }];
  const haystack = text.toLowerCase();
  const target = needle.toLowerCase();
  const parts: { text: string; hit: boolean }[] = [];
  let from = 0;
  let index = haystack.indexOf(target, from);
  if (index < 0) return [{ text, hit: false }];
  while (index >= 0) {
    if (index > from) parts.push({ text: text.slice(from, index), hit: false });
    parts.push({ text: text.slice(index, index + needle.length), hit: true });
    from = index + needle.length;
    index = haystack.indexOf(target, from);
  }
  if (from < text.length) parts.push({ text: text.slice(from), hit: false });
  return parts;
}

function occurrencesInLine(text: string, query: string): number {
  const haystack = text.toLowerCase();
  const target = query.toLowerCase();
  if (!target) return 0;
  let count = 0;
  let index = haystack.indexOf(target);
  while (index >= 0) {
    count += 1;
    index = haystack.indexOf(target, index + target.length);
  }
  return count;
}

/** 代码 / 文本按行搜索（每行几处都算；跳转只到行）。 */
export function searchCodeView(view: CodeView | null, query: string): PreviewSearchResult {
  const needle = normalizeQuery(query);
  if (!view || !needle) return emptySearch(query);
  const hits: PreviewSearchHit[] = [];
  for (const line of view.lines) {
    const count = occurrencesInLine(line.text, needle);
    for (let index = 0; index < count; index += 1) {
      hits.push({
        key: "line:" + line.number + ":" + index,
        label: "第 " + line.number + " 行",
        line: line.number,
        snippet: snippetOf(line.text, needle),
      });
    }
  }
  return pushHits(hits, SEARCH_HIT_LIMIT, query);
}

/** 纯文本按行搜索（回复里的代码 / 任务输出的文本）。 */
export function searchTextLines(lines: string[], query: string): PreviewSearchResult {
  const needle = normalizeQuery(query);
  if (!needle || !lines.length) return emptySearch(query);
  const hits: PreviewSearchHit[] = [];
  lines.forEach((text, index) => {
    const count = occurrencesInLine(text, needle);
    for (let at = 0; at < count; at += 1) {
      hits.push({
        key: "text:" + index + ":" + at,
        label: "第 " + (index + 1) + " 行",
        line: index + 1,
        snippet: snippetOf(text, needle),
      });
    }
  });
  return pushHits(hits, SEARCH_HIT_LIMIT, query);
}

/** DOCX：段落 / 列表 / 标题 / 表格单元格。 */
export function searchDocxView(view: DocxView | null, query: string): PreviewSearchResult {
  const needle = normalizeQuery(query);
  if (!view || !needle) return emptySearch(query);
  const hits: PreviewSearchHit[] = [];
  const lower = needle.toLowerCase();
  view.blocks.forEach((block, index) => {
    if (!block.text.toLowerCase().includes(lower)) return;
    hits.push({
      key: "block:" + index,
      label: "第 " + (index + 1) + " 段",
      block: index,
      snippet: snippetOf(block.text, needle),
    });
  });
  view.tables.forEach((table, tableIndex) => {
    table.rows.forEach((row, rowIndex) => {
      const cellIndex = row.findIndex((cell) => cell.toLowerCase().includes(lower));
      if (cellIndex < 0) return;
      hits.push({
        key: "table:" + tableIndex + ":" + rowIndex + ":" + cellIndex,
        label: "表格 " + (tableIndex + 1) + " · 第 " + (rowIndex + 1) + " 行",
        table: tableIndex,
        row: rowIndex,
        snippet: snippetOf(row[cellIndex], needle),
      });
    });
  });
  return pushHits(hits, SEARCH_HIT_LIMIT, query);
}

/** XLSX：工作表单元格（公式原文与显示值都参与搜索）。 */
export function searchXlsxView(view: XlsxView | null, query: string): PreviewSearchResult {
  const needle = normalizeQuery(query);
  if (!view || !needle) return emptySearch(query);
  const hits: PreviewSearchHit[] = [];
  const lower = needle.toLowerCase();
  view.sheets.forEach((sheet, sheetIndex) => {
    sheet.rows.forEach((row) => {
      row.cells.forEach((cell) => {
        if (!cell) return;
        const haystack = (cell.display + " " + (cell.formula ?? "")).toLowerCase();
        if (!haystack.includes(lower)) return;
        hits.push({
          key: "cell:" + sheetIndex + ":" + cell.ref,
          label: sheet.name + "!" + cell.ref,
          sheet: sheet.name,
          sheetIndex,
          row: row.index,
          cell: cell.ref,
          snippet: snippetOf(cell.display || cell.formula || "", needle),
        });
      });
    });
  });
  return pushHits(hits, SEARCH_HIT_LIMIT, query);
}

/** PDF 逐页文字搜索：页面文字由前端 pdf.js 提取（扫描件没有文字层时会如实显示「未找到」）。 */
export function searchPdfTextPages(
  pageTexts: string[],
  query: string,
  options: { limit?: number; maxPages?: number } = {},
): { hits: PreviewSearchHit[]; truncated: boolean; scannedPages: number; totalPages: number } {
  const needle = normalizeQuery(query);
  const limit = options.limit ?? SEARCH_HIT_LIMIT;
  const maxPages = options.maxPages ?? 200;
  const totalPages = pageTexts.length;
  const scannedPages = Math.min(totalPages, maxPages);
  if (!needle) {
    return { hits: [], truncated: false, scannedPages, totalPages };
  }
  const lower = needle.toLowerCase();
  const hits: PreviewSearchHit[] = [];
  let matchedPages = 0;
  for (let index = 0; index < scannedPages; index += 1) {
    const text = pageTexts[index] ?? "";
    const haystack = text.toLowerCase();
    let at = haystack.indexOf(lower);
    let occurrences = 0;
    while (at >= 0) {
      occurrences += 1;
      at = haystack.indexOf(lower, at + Math.max(1, lower.length));
    }
    if (!occurrences) continue;
    matchedPages += 1;
    for (let occurrence = 0; occurrence < occurrences; occurrence += 1) {
      hits.push({
        key: "pdf:" + (index + 1) + ":" + occurrence,
        label: "第 " + (index + 1) + " 页",
        page: index + 1,
        snippet: snippetOf(text, needle),
      });
    }
  }
  return {
    hits: hits.slice(0, limit),
    truncated: hits.length > limit || totalPages > scannedPages,
    scannedPages,
    totalPages,
  };
}

/** PPTX：幻灯片正文 / 备注 / 表格。 */
export function searchPptxView(view: PptxView | null, query: string): PreviewSearchResult {
  const needle = normalizeQuery(query);
  if (!view || !needle) return emptySearch(query);
  const lower = needle.toLowerCase();
  const hits: PreviewSearchHit[] = [];
  view.slides.forEach((slide, index) => {
    const texts = [...slide.texts, ...slide.notes];
    let matched: string | undefined = texts.find((text) => text.toLowerCase().includes(lower));
    if (matched === undefined) {
      matched = slide.tables.flat(2).find((cell) => String(cell).toLowerCase().includes(lower));
    }
    if (matched === undefined) return;
    const page = slide.index || index + 1;
    hits.push({
      key: "slide:" + page,
      label: "第 " + page + " 张幻灯片",
      slide: page,
      page,
      snippet: snippetOf(String(matched), needle),
    });
  });
  return pushHits(hits, SEARCH_HIT_LIMIT, query);
}

// ---------------------------------------------------------------------------
// 版本竞争：迟到的旧请求不得覆盖新内容
// ---------------------------------------------------------------------------

export interface PreviewVersionGuard {
  /** 开始一次新请求（返回序号）。后发起的序号更大。 */
  begin(): number;
  /** 这次响应是不是当前最新一代请求的？旧一代一律丢弃。 */
  isCurrent(sequence: number): boolean;
  /** 当前最新一代序号。 */
  latest(): number;
  reset(): void;
}

/**
 * 每个标签一个守卫：请求先 begin() 拿序号，回来时 isCurrent() 判断。
 * 另外任何一次「更新的版本」被接受后，更旧的版本结果（即使序号碰巧更大，例如
 * 用户先点了重试、又触发了更新检查）也不能覆盖 —— 由调用方用
 * shouldApplyFileVersion 再判一次。
 */
export function createPreviewVersionGuard(): PreviewVersionGuard {
  let current = 0;
  return {
    begin(): number {
      current += 1;
      return current;
    },
    isCurrent(sequence: number): boolean {
      return sequence === current;
    },
    latest(): number {
      return current;
    },
    reset(): void {
      current = 0;
    },
  };
}

/**
 * 文件版本比较：**只有拿到明确不同的版本号才认为内容变了**。
 * 空版本（后端没给 / 还没读到）不能当成「更新」，避免每次检查都误报「文件已更新」。
 */
export function isFileVersionChanged(
  knownVersion: string | null | undefined,
  incomingVersion: string | null | undefined,
): boolean {
  const known = String(knownVersion || "").trim();
  const incoming = String(incomingVersion || "").trim();
  return Boolean(incoming) && Boolean(known) && incoming !== known;
}

/** 更新检查的 debounce：QIO 一轮结束后稍等再查，避免读到写了一半的文件。 */
export const FILE_UPDATE_CHECK_DELAY_MS = 900;
/** 打开期间的轻量轮询间隔（没有可靠附件事件时用；离开页面立即清理）。 */
export const FILE_UPDATE_POLL_MS = 15_000;
/** 连续失败多少次后停止自动检查（用户手动重试可恢复）。 */
export const FILE_UPDATE_MAX_FAILURES = 3;

// ---------------------------------------------------------------------------
// 剪贴板（诚实：失败抛出可读原因，不假装复制成功）
// ---------------------------------------------------------------------------

export async function copyTextToClipboard(text: string): Promise<void> {
  const value = String(text ?? "");
  if (!value) throw new Error("没有可复制的内容");
  const nav = typeof navigator !== "undefined" ? navigator : null;
  const clipboard = nav?.clipboard;
  if (clipboard?.writeText) {
    await clipboard.writeText(value);
    return;
  }
  if (typeof document === "undefined") throw new Error("这个环境不支持复制");
  const area = document.createElement("textarea");
  area.value = value;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  try {
    area.select();
    const ok = typeof document.execCommand === "function" ? document.execCommand("copy") : false;
    if (!ok) throw new Error("浏览器拒绝了复制请求");
  } finally {
    area.remove();
  }
}
