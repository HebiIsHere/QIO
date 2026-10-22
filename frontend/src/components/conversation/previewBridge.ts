/**
 * 「在右侧查看」→ C 的只读预览接线桥（B 提供，Lead 集成时注册）。
 *
 * 身份规则按契约 §4.3：附件标签身份是 `attachment:<attachmentId>@<version>`，
 * 没有 version 时用 `attachment:<attachmentId>`；**绝不用文件名当身份**。
 * 这里只负责生成 target 与调用可注册的 opener；C 的 store 未就绪时不渲染按钮。
 */
import {
  attachmentPreviewId,
  type PreviewKind,
  type PreviewTarget,
} from "../../conversation/previewTypes";

export type ConversationPreviewOpener = (target: PreviewTarget) => void | Promise<void>;

let opener: ConversationPreviewOpener | null = null;

export function registerConversationPreviewOpener(fn: ConversationPreviewOpener | null): void {
  opener = fn;
}

export function hasConversationPreviewOpener(): boolean {
  return opener !== null;
}

/** 打开右侧预览；没有接线时返回 false（调用方如实说明，不假装已打开）。 */
export function openConversationPreview(target: PreviewTarget): boolean {
  if (!opener) return false;
  try {
    void opener(target);
    return true;
  } catch {
    return false;
  }
}

const EXT_KIND: Record<string, PreviewKind> = {
  png: "image", jpg: "image", jpeg: "image", gif: "image", webp: "image", bmp: "image", svg: "image",
  pdf: "pdf",
  docx: "docx", doc: "docx",
  xlsx: "xlsx", xls: "xlsx", csv: "text",
  pptx: "pptx", ppt: "pptx",
  md: "markdown", markdown: "markdown",
  txt: "text", log: "text", jsonl: "text",
  json: "code", ts: "code", tsx: "code", js: "code", jsx: "code", vue: "code", py: "code",
  html: "code", css: "code", scss: "code", yml: "code", yaml: "code", toml: "code",
  sh: "code", ps1: "code", bat: "code", rs: "code", go: "code", java: "code", kt: "code",
  c: "code", h: "code", cpp: "code", hpp: "code", cs: "code", rb: "code", php: "code",
  sql: "code", xml: "code", ini: "code", conf: "code",
  mp3: "audio", wav: "audio", m4a: "audio", ogg: "audio", flac: "audio", aac: "audio",
  mp4: "video", mov: "video", webm: "video", mkv: "video", avi: "video", m4v: "video",
};

/** 从文件名推断预览类型（只用于选择渲染器；身份仍是 attachment id）。 */
export function previewKindOf(name: string): PreviewKind {
  const ext = String(name ?? "").split(".").pop()?.toLowerCase() ?? "";
  return EXT_KIND[ext] ?? "unsupported";
}

export interface AttachmentPreviewInput {
  id: string;
  name: string;
  /** 内容版本（后端有就给；没有时身份退化为 attachment:<id>） */
  version?: string | null;
  storedPath?: string | null;
  sourcePath?: string | null;
  topicId?: string | null;
  mime?: string;
  size?: number;
}

/** 附件 → 预览 target（同名不同 id 仍是两个标签）。 */
export function previewTargetForAttachment(input: AttachmentPreviewInput): PreviewTarget {
  const kind = previewKindOf(input.name);
  return {
    id: attachmentPreviewId(input.id, input.version ?? null),
    kind,
    title: input.name,
    source: "attachment",
    attachmentId: input.id,
    ...(input.version ? { version: input.version } : {}),
    ...(input.topicId ? { topicId: input.topicId } : {}),
    ...(input.storedPath || input.sourcePath
      ? { filePath: input.storedPath ?? input.sourcePath ?? undefined }
      : {}),
    ...(input.mime ? { mime: input.mime } : {}),
    ...(typeof input.size === "number" ? { size: input.size } : {}),
    updatedAt: new Date().toISOString(),
  };
}

/** 能否在右侧预览：未接线或格式不支持时都不显示按钮。 */
export function canPreviewAttachment(input: { name: string }): boolean {
  return hasConversationPreviewOpener() && previewKindOf(input.name) !== "unsupported";
}
