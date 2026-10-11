/**
 * 材料展示的纯函数（子智能体 A）。
 *
 * 契约：docs/interactive-mode-contract.md §14.1 / §14.6。
 * 服务端给的 source / read / truncated / characters 是**事实**，这里只负责把它翻成
 * 界面能直接显示的文字 —— 不在这里再推断「大概读到了」：
 *
 * - 拿到文件名 / 网址 / 说明文字 **都不等于** 读到正文；
 * - 图片在任何情况下都是 image_unsupported（当前模型链路没有视觉能力）；
 * - read=false 时必须有可读原因（reason），界面直接显示它，不自己编更乐观的说法。
 *
 * 本文件不依赖 store / 组件，只有纯函数，便于单测与复用。
 */
import type { CardKind, MaterialSource, UnderstandingMaterial } from "./types";

/** 材料来源的中文标签（与后端 models.MATERIAL_SOURCES 一一对应）。 */
export const MATERIAL_SOURCE_LABELS: Record<MaterialSource, string> = {
  inline: "板面正文",
  attachment_text: "已保存附件正文",
  url_fetched: "网页正文",
  url_address_only: "只有网址",
  image_unsupported: "图片（无视觉能力）",
  unreadable: "未读到正文",
  unsupported: "不支持的材料",
};

const KNOWN_SOURCES = new Set<string>(Object.keys(MATERIAL_SOURCE_LABELS));

export function isMaterialSource(value: unknown): value is MaterialSource {
  return typeof value === "string" && KNOWN_SOURCES.has(value);
}

/** 来源标签：未知取值如实显示原值，不假装认识它。 */
export function materialSourceLabel(source: MaterialSource | string | null | undefined): string {
  if (isMaterialSource(source)) return MATERIAL_SOURCE_LABELS[source];
  const raw = typeof source === "string" ? source.trim() : "";
  return raw ? "未知来源（" + raw + "）" : "来源未知";
}

/** 是否真的读到了正文（唯一的依据是服务端的 read 事实）。 */
export function isMaterialRead(material: Pick<UnderstandingMaterial, "read">): boolean {
  return material.read === true;
}

export function materialReadText(material: Pick<UnderstandingMaterial, "read">): string {
  return isMaterialRead(material) ? "已读到正文" : "未读到正文";
}

/**
 * 字数说明：characters 是**截断前**已解析到的原始字符数（后端口径），
 * 截断时如实说「未读全」，不把交付量当成全文。
 */
export function materialCharactersText(
  material: Pick<UnderstandingMaterial, "read" | "truncated" | "characters">,
): string {
  const total = Number(material.characters) || 0;
  if (!isMaterialRead(material)) return "0 字（没有读到正文）";
  return material.truncated ? total + " 字（未读全，只看了一部分）" : total + " 字";
}

/** read=false 时的可读原因；读到了正文就返回空串（不制造多余文案）。 */
export function materialReasonText(
  material: Pick<UnderstandingMaterial, "read" | "reason">,
): string {
  if (isMaterialRead(material)) return "";
  const reason = String(material.reason ?? "").trim();
  return reason || "服务端没有说明原因：这次材料没有被读取";
}

/** 一行摘要：类型 + 来源 +「有没有读到」+ 字数（列表与过程说明共用）。 */
export function materialOneLine(material: UnderstandingMaterial): string {
  const parts = [
    materialSourceLabel(material.source),
    materialReadText(material),
  ];
  if (isMaterialRead(material)) parts.push(materialCharactersText(material));
  const reason = materialReasonText(material);
  if (reason) parts.push(reason);
  return parts.join(" · ");
}

// ---------------------------------------------------------------------------
// 卡片 meta 里的附件关联（A 的新形状：meta.file.attachmentId / meta.image.attachmentId）
// ---------------------------------------------------------------------------

export type AttachableKind = "file" | "image";

export function isAttachableKind(kind: CardKind | string): kind is AttachableKind {
  return kind === "file" || kind === "image";
}

/** 卡片 meta 里登记的附件块（{name, attachmentId, size?}），没有就是 null。 */
export function cardAttachmentBlock(
  meta: Record<string, unknown> | null | undefined,
  kind: CardKind | string,
): { name: string; attachmentId: string; size: number | null } | null {
  if (!isAttachableKind(kind)) return null;
  const block = meta?.[kind];
  if (!block || typeof block !== "object") return null;
  const record = block as Record<string, unknown>;
  const attachmentId = typeof record.attachmentId === "string" ? record.attachmentId.trim() : "";
  const name = typeof record.name === "string" ? record.name : "";
  const rawSize = record.size;
  const size = typeof rawSize === "number" && Number.isFinite(rawSize) ? rawSize : null;
  if (!attachmentId && !name) return null;
  return { name, attachmentId, size };
}

/** 卡片关联的真实附件 id（没有就是空串 —— 空串必须如实显示成「没有关联」）。 */
export function cardAttachmentId(
  meta: Record<string, unknown> | null | undefined,
  kind: CardKind | string,
): string {
  return cardAttachmentBlock(meta, kind)?.attachmentId ?? "";
}

export function cardAttachmentName(
  meta: Record<string, unknown> | null | undefined,
  kind: CardKind | string,
): string {
  return cardAttachmentBlock(meta, kind)?.name ?? "";
}

/**
 * 卡片上的附件状态文字（关闭态也要看得见）：
 * 没有关联附件时必须说清「QIO 读不到正文」，不许用文件名冒充读到了内容。
 */
export function cardAttachmentText(
  meta: Record<string, unknown> | null | undefined,
  kind: CardKind | string,
): string {
  if (!isAttachableKind(kind)) return "";
  const block = cardAttachmentBlock(meta, kind);
  if (!block || !block.attachmentId) {
    return "还没有关联已保存的附件：QIO 读不到它的正文";
  }
  const name = block.name || "未命名附件";
  return "已关联已保存的附件：" + name;
}

export function cardAttachmentLinked(
  meta: Record<string, unknown> | null | undefined,
  kind: CardKind | string,
): boolean {
  return Boolean(cardAttachmentId(meta, kind));
}
