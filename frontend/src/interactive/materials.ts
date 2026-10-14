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
  /*
    2026-10-14：文件与图片合并成同一种材料后，meta 的键可能还是老的那个
    （kind 已经迁移成 file、meta 仍是 meta.image，或反过来）——与后端同一口径：
    `_attachment_id(meta, "file") or _attachment_id(meta, "image")`。
  */
  const own = meta?.[kind];
  const fallback = meta?.[kind === "file" ? "image" : "file"];
  const block = own && typeof own === "object" ? own : fallback;
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

// ---------------------------------------------------------------------------
// 卡片附件「看得见 + 能预览」的判定纯函数（P2 板面接线）
//
// 与上面同一套诚实口径，只多回答一个问题：
// **这张卡片上的附件，现在能看见什么、点一下会发生什么**，理由是什么。
//   有/无 attachmentId  -> 有就是有关联；没有就必须如实说「没有关联」，不给假预览；
//   可执行 / 脚本类      -> 不自动运行（桌面端只「在文件夹中显示」，理由与 Rust 侧一致）；
//   viewable            -> 浏览器里能不能安全内联查看（html / svg / xml 带脚本，绝不内联）；
//   previewable         -> 能不能安全渲染成缩略图（只有位图，且不执行脚本）。
// 这里不 import 附件服务：可执行名单与「能不能内联查看」都从 services/attachments 传入，
// 保证与打开计划（openPlanFor）用的是**同一份**名单，不会各写一套。
// ---------------------------------------------------------------------------

export type CardAttachmentState = "linked" | "unlinked";

export interface CardAttachmentAvailability {
  state: CardAttachmentState;
  /** 卡片 meta 里登记的真实附件 id；没有关联时是空串 */
  attachmentId: string;
  /** 显示名（meta 里的名字，可能为空） */
  name: string;
  /** 已知字节数（meta.size）；未知就是 null —— 不拿 0 冒充未知 */
  size: number | null;
  /** 是否可执行 / 脚本类（由调用方传入附件服务的同一份名单） */
  executable: boolean;
  /** 浏览器里能不能安全内联查看 */
  viewable: boolean;
  /** 能不能安全渲染成缩略图 */
  imagePreviewable: boolean;
}

/** 是否位图扩展名：只有这些能当缩略图（svg / html / xml 会带脚本或外链，绝不当图片渲染）。 */
export function isPreviewableImageName(name: string): boolean {
  return /.(png|jpe?g|gif|webp|bmp|avif|ico)$/i.test(String(name ?? "").trim());
}

export function attachmentCardState(
  meta: Record<string, unknown> | null | undefined,
  kind: CardKind | string,
  lookups: { executable?(name: string): boolean; viewable?(name: string): boolean } = {},
): CardAttachmentAvailability {
  const block = isAttachableKind(kind) ? cardAttachmentBlock(meta, kind) : null;
  const attachmentId = block?.attachmentId ?? "";
  const name = block?.name ?? "";
  const linked = Boolean(attachmentId);
  return {
    state: linked ? "linked" : "unlinked",
    attachmentId,
    name,
    size: block?.size ?? null,
    executable: linked ? Boolean(lookups.executable?.(name)) : false,
    viewable: linked ? Boolean(lookups.viewable?.(name)) : false,
    // 2026-10-14 回归修复：能不能当图片预览只看**附件扩展名**，与 kind 无关 ——
    // 原来写的是 `kind === "image"`，P48 把图片卡迁移成 file 之后预览就整块消失了。
    imagePreviewable: linked && isPreviewableImageName(name),
  };
}

/**
 * 「打开 / 预览」入口的可见标签（可读、带对象名）：
 * 可执行类只给「在文件夹中显示」，其余可执行类在浏览器里会走下载。
 */
export function attachmentPreviewActionLabel(
  availability: Pick<CardAttachmentAvailability, "state" | "name" | "executable" | "attachmentId">,
  desktop: boolean,
): string {
  if (availability.state !== "linked" || !availability.attachmentId) {
    return "没有关联已保存的附件，打不开";
  }
  const name = availability.name || "未命名附件";
  if (availability.executable) return desktop ? "在文件夹中显示「" + name + "」" : "下载「" + name + "」（不自动运行）";
  return desktop ? "查看「" + name + "」（QIO 保存的副本）" : "查看「" + name + "」";
}

/** 缩略图不可用的原因（给用户看的一句话）；可预览时是空串。 */
export function attachmentPreviewUnavailableReason(
  availability: Pick<CardAttachmentAvailability, "state" | "executable" | "imagePreviewable" | "viewable" | "name">,
): string {
  if (availability.state !== "linked") {
    return "没有关联已保存的附件：看不到它的内容，也没有可预览的字节";
  }
  if (availability.executable) {
    return "可执行 / 脚本类附件不渲染预览（渲染前要先执行，风险不可控）；确认来源后再自行打开";
  }
  if (availability.imagePreviewable) return "";
  if (availability.viewable) {
    return "缩略图只用于图片；这个文件没有可安全渲染的图片内容，请看「" + (availability.name || "未命名附件") + "」的详情或直接查看";
  }
  return "这种类型不能安全渲染缩略图（可能带脚本）：不预览，改为查看 / 下载";
}

/** 没有关联时的完整诚实说明：文件名不是正文，也不假装能打开。 */
export function attachmentUnlinkedText(availability: Pick<CardAttachmentAvailability, "name">): string {
  const name = availability.name;
  return name
    ? "卡片上记着文件名「" + name + "」，但没有关联已保存的附件：QIO 读不到它的正文，也打不开它"
    : "还没有关联已保存的附件：QIO 读不到它的正文，也打不开它";
}
