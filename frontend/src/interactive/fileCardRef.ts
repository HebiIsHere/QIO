/**
 * 文件卡的「源引用」视图（R9 契约 §6 / §8）。
 *
 * 为什么单独一个模块：
 * * 卡片 meta 里的 `file` 块是**板面状态**（会随撤销/重做/复制一起走），
 *   而附件 payload 是**服务端事实**（来源 / 可用性 / 用户说明 / 材料开关）。
 *   两者合并的规则必须只有一处，否则卡片、预览、复制、撤销会各说各话；
 * * 卡片 meta 是复制卡片时共享 attachmentId 的唯一载体（撤销/重做是整份 JSON 快照，
 *   不破坏共享的前提是 meta 里只存引用 id，不存文件内容）；
 * * 纯函数，可单测，不 import vue / store / DOM。
 *
 * 诚实口径（不伪造）：
 * * 没关联附件 → 明确说「QIO 读不到正文」，不拿文件名冒充；
 * * 浏览器上传 → 说清「已保存为工作区材料源文件（来源：浏览器上传）」，不称原文件；
 * * 可用性未知 → 显示「可用性待核对」，不默认「可用」；
 * * 「作为互动材料」只控制是否主动纳入互动上下文，不是文件访问权限。
 */
import type { AttachmentRef } from "../services/attachments";
import {
  attachmentAvailabilityText,
  attachmentOriginLabel,
  attachmentReferenceId,
  attachmentSourceLabel,
  attachmentSourceNote,
  isAttachmentSourceMissing,
  MATERIAL_LABEL,
} from "../services/attachments";

/** 卡片 meta 里 file 块的字段（camelCase；payload 是 snake_case，转换只发生在这里）。 */
export interface FileCardRef {
  attachmentId: string;
  referenceId: string;
  name: string;
  /** 媒体类型标签（图片 / PDF / 表格 / 文档 …） */
  typeLabel: string;
  /** 来源入口（桌面选择 / 拖放 / 浏览器上传 / 剪贴板截图 / 历史副本） */
  sourceLabel: string;
  /** 来源说明（引用本地源文件 / 已保存为工作区材料源文件） */
  sourceNote: string;
  /** 材料归属（你的原文件 / QIO 首次落盘的材料 / QIO 生成） */
  originLabel: string;
  /** 可用性文字（可用 / 缺失：原因 / 位置待确认 / 已重新指定位置） */
  availabilityText: string;
  /** 源文件缺失 / 位置待确认：需要「重新指定位置」 */
  missing: boolean;
  /** 用户说明（登记时写下；没有就是空串） */
  userNote: string;
  /** 作为互动材料（默认 true：材料默认纳入互动上下文，可手动关闭） */
  materialEnabled: boolean;
  /** 已知字节数（未知 = null，不拿 0 冒充） */
  sizeBytes: number | null;
}

/** 卡片 meta 里 file 块的完整形状（旧字段 name/attachmentId/size 保留兼容）。 */
export interface FileCardMetaBlock {
  name?: string;
  attachmentId?: string;
  size?: number | null;
  referenceId?: string;
  filename?: string;
  mediaKind?: string;
  sourceKind?: string;
  origin?: string;
  availability?: string;
  userNote?: string;
  materialEnabled?: boolean;
  missingReason?: string | null;
}

/** 从附件 payload（AttachmentRef）生成写进卡片 meta 的 file 块（不写文件内容）。 */
export function fileMetaFromAttachment(ref: AttachmentRef): FileCardMetaBlock {
  const block: FileCardMetaBlock = {
    name: ref.filename || ref.name || "未命名附件",
    attachmentId: ref.attachmentId || ref.id,
    size: Number.isFinite(ref.sizeBytes) ? ref.sizeBytes : null,
    materialEnabled: ref.materialEnabled !== false,
  };
  if (ref.referenceId) block.referenceId = ref.referenceId;
  if (ref.filename) block.filename = ref.filename;
  if (ref.mediaKind) block.mediaKind = ref.mediaKind;
  if (ref.sourceKind) block.sourceKind = ref.sourceKind;
  if (ref.origin) block.origin = ref.origin;
  if (ref.availability) block.availability = ref.availability;
  if (ref.userNote) block.userNote = ref.userNote;
  if (ref.missingReason !== undefined && ref.missingReason !== null) block.missingReason = ref.missingReason;
  return block;
}

/**
 * 把附件事实合并进卡片 meta：只改 `file` 块与显示名，保留其它 meta（doc / title / …）。
 * **不复制内容**：板面 meta 只存引用 id 与展示事实。
 */
export function mergeAttachmentIntoMeta(
  meta: Record<string, unknown> | null | undefined,
  ref: AttachmentRef,
  kind: "file" = "file",
): Record<string, unknown> {
  const source = meta && typeof meta === "object" ? { ...meta } : {};
  const previous = readFileBlock(source, kind);
  const next = fileMetaFromAttachment(ref);
  // 用户在卡片上手动关掉的「作为互动材料」不能被一次重新关联悄悄打开：
  // payload 明确给出 material_enabled 时以服务端为准，否则沿用卡片当前选择。
  if (typeof ref.materialEnabled !== "boolean" && previous && previous.materialEnabled === false) {
    next.materialEnabled = false;
  }
  return { ...source, name: next.name, [kind]: next };
}

/** 只读 meta 的 file 块（老卡片没有新字段时返回 null 字段，不猜）。 */
export function readFileBlock(
  meta: Record<string, unknown> | null | undefined,
  kind: "file" = "file",
): FileCardMetaBlock | null {
  const raw = meta?.[kind];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const block = raw as Record<string, unknown>;
  const text = (value: unknown): string | undefined => (typeof value === "string" ? value : undefined);
  const out: FileCardMetaBlock = {};
  const name = text(block.name) ?? text(block.filename);
  if (name !== undefined) out.name = name;
  const attachmentId = text(block.attachmentId);
  if (attachmentId !== undefined) out.attachmentId = attachmentId;
  if (typeof block.size === "number" && Number.isFinite(block.size)) out.size = block.size;
  const referenceId = text(block.referenceId);
  if (referenceId !== undefined) out.referenceId = referenceId;
  const filename = text(block.filename);
  if (filename !== undefined) out.filename = filename;
  const mediaKind = text(block.mediaKind);
  if (mediaKind !== undefined) out.mediaKind = mediaKind;
  const sourceKind = text(block.sourceKind);
  if (sourceKind !== undefined) out.sourceKind = sourceKind;
  const origin = text(block.origin);
  if (origin !== undefined) out.origin = origin;
  const availability = text(block.availability);
  if (availability !== undefined) out.availability = availability;
  const userNote = text(block.userNote);
  if (userNote !== undefined) out.userNote = userNote;
  if (typeof block.materialEnabled === "boolean") out.materialEnabled = block.materialEnabled;
  if (typeof block.missingReason === "string") out.missingReason = block.missingReason;
  else if (block.missingReason === null) out.missingReason = null;
  return out;
}

/** 「作为互动材料」开关：默认 **开启**（材料默认在互动上下文里），用户可关。 */
export function materialEnabledOf(
  meta: Record<string, unknown> | null | undefined,
  kind: "file" = "file",
): boolean {
  return readFileBlock(meta, kind)?.materialEnabled !== false;
}

/** 写回开关（局部 patch 用；不动附件 id / 名称）。 */
export function withMaterialEnabled(
  meta: Record<string, unknown> | null | undefined,
  enabled: boolean,
  kind: "file" = "file",
): Record<string, unknown> {
  const source = meta && typeof meta === "object" ? { ...meta } : {};
  const block = readFileBlock(source, kind) ?? {};
  return { ...source, [kind]: { ...block, materialEnabled: Boolean(enabled) } };
}

/** 媒体类型标签：media_kind 优先，其次按扩展名；都拿不到就「文件」。 */
export function mediaTypeLabel(name: string, mediaKind?: string): string {
  const raw = String(mediaKind ?? "").trim().toLowerCase();
  const known = MEDIA_KIND_LABELS[raw];
  if (known) return known;
  const ext = extensionOf(name);
  return EXTENSION_LABELS[ext] ?? "文件";
}

const MEDIA_KIND_LABELS: Record<string, string> = {
  image: "图片",
  pdf: "PDF",
  document: "文档",
  doc: "文档",
  word: "文档",
  spreadsheet: "表格",
  sheet: "表格",
  excel: "表格",
  presentation: "幻灯片",
  slides: "幻灯片",
  ppt: "幻灯片",
  text: "文本",
  code: "代码",
  archive: "压缩包",
  audio: "音频",
  video: "视频",
  other: "文件",
  unknown: "文件",
};

const EXTENSION_LABELS: Record<string, string> = {
  png: "图片", jpg: "图片", jpeg: "图片", gif: "图片", webp: "图片", bmp: "图片", svg: "图片",
  pdf: "PDF",
  doc: "文档", docx: "文档", pages: "文档", rtf: "文档",
  xls: "表格", xlsx: "表格", xlsm: "表格", csv: "表格",
  ppt: "幻灯片", pptx: "幻灯片",
  txt: "文本", md: "文本", log: "文本", json: "文本", yaml: "文本", yml: "文本", xml: "文本",
  js: "代码", ts: "代码", py: "代码", java: "代码", go: "代码", rs: "代码", c: "代码", cpp: "代码",
  zip: "压缩包", rar: "压缩包", "7z": "压缩包", tar: "压缩包", gz: "压缩包",
  mp3: "音频", wav: "音频", flac: "音频", m4a: "音频", ogg: "音频",
  mp4: "视频", mov: "视频", webm: "视频", mkv: "视频",
};

function extensionOf(name: string): string {
  const text = String(name ?? "");
  const dot = text.lastIndexOf(".");
  return dot < 0 ? "" : text.slice(dot + 1).toLowerCase();
}

/**
 * 从**卡片 meta** 直接生成展示视图（不发请求；用于首屏与离线/服务端不可用时）。
 * 服务端事实（getAttachment）到了之后用 fileCardRefFromAttachment 覆盖。
 */
export function fileCardRefFromMeta(
  meta: Record<string, unknown> | null | undefined,
  kind: "file" = "file",
): FileCardRef | null {
  const block = readFileBlock(meta, kind);
  if (!block) return null;
  const attachmentId = block.attachmentId ?? "";
  const name = block.filename || block.name || "未命名附件";
  const sourceKind = block.sourceKind as AttachmentRef["sourceKind"];
  const availability = block.availability as AttachmentRef["availability"];
  const ref: AttachmentRef = {
    id: attachmentId,
    name,
    sizeBytes: block.size ?? 0,
    kind: "reference",
    display: MATERIAL_LABEL,
    state: "ready",
    error: null,
    ...(attachmentId ? { attachmentId } : {}),
    ...(block.referenceId ? { referenceId: block.referenceId } : {}),
    ...(block.filename ? { filename: block.filename } : {}),
    ...(block.mediaKind ? { mediaKind: block.mediaKind } : {}),
    ...(sourceKind ? { sourceKind } : {}),
    ...(block.origin ? { origin: block.origin as AttachmentRef["origin"] } : {}),
    ...(availability ? { availability } : {}),
    ...(block.userNote ? { userNote: block.userNote } : {}),
    ...(block.missingReason ? { missingReason: block.missingReason } : {}),
  };
  return fileCardRefFromAttachment(ref);
}

/** 从附件 payload 事实生成展示视图（唯一合并口径）。 */
export function fileCardRefFromAttachment(ref: AttachmentRef): FileCardRef {
  const name = ref.filename || ref.name || "未命名附件";
  const missing = isAttachmentSourceMissing(ref);
  return {
    attachmentId: ref.attachmentId || ref.id,
    referenceId: attachmentReferenceId(ref),
    name,
    typeLabel: mediaTypeLabel(name, ref.mediaKind),
    sourceLabel: attachmentSourceLabel(ref),
    sourceNote: attachmentSourceNote(ref),
    originLabel: attachmentOriginLabel(ref),
    availabilityText: attachmentAvailabilityText(ref),
    missing,
    userNote: ref.userNote ?? "",
    materialEnabled: ref.materialEnabled !== false,
    sizeBytes: Number.isFinite(ref.sizeBytes) ? ref.sizeBytes : null,
  };
}

/** 「作为互动材料」开关的说明（唯一一句，卡片与设置共用）。 */
export const MATERIAL_SWITCH_HINT =
  "「作为互动材料」只决定是否主动纳入 QIO 的互动上下文，不是文件访问权限；关闭后源文件仍然保留，你仍可主动预览或打开它。";

/** 重新指定位置前的确认标题（契约 §7：只有用户确认「同一文件」才采纳候选）。 */
export function relocateConfirmTitle(): string {
  return "这是同一个文件吗？";
}

/** 确认说明：新路径 + 为什么需要确认 + 不确认时的后果。 */
export function relocateConfirmDetail(path: string): string {
  return (
    "QIO 要在新位置「" +
    path +
    "」继续引用这份材料。只有你确认这是同一个文件，才会把引用改到新位置；" +
    "文件身份不一致时不会自动采纳，取消后原引用保持不变。"
  );
}
