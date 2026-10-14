/**
 * 卡片内预览的**计划**（契约 §3.5，冻结形状）。
 *
 * 纯函数：把 (卡片, 渲染状态, 环境) 翻成
 *   { mode: "inline"|"external"|"unsupported", renderer: "image"|"pdf"|"docx"|"xlsx"|"pptx"|"code"|"none",
 *     reason: string, actions: PreviewAction[] }
 * 不碰 DOM、不发请求、不 import vue —— 组件只负责按计划渲染与接线。
 *
 * 三条诚实规则（与契约 §0 一致）：
 * 1. **拿到文件名 ≠ 读到正文**：卡片 meta 里没有 attachmentId 时，没有预览、也没有假按钮；
 * 2. **预览 ≠ 模型已读**：展开、翻页、缩放都不触发模型调用、不产生表达与意图；
 * 3. **失败给真实原因 + 打开/下载入口**，绝不显示一个点了没用的按钮。
 *
 * "点一下会怎样"**没有第二套判断**：一律复用 services/attachments 的 openPlanFor
 * （它与 /content 的真实能力、可执行类不自动运行的规则同源）。
 */
import { humanSize, openPlanFor } from "../services/attachments";
import type { AttachmentRef } from "../services/attachments";
import type { BoardCard } from "./types";

export type PreviewMode = "inline" | "external" | "unsupported";
export type PreviewRenderer = "image" | "pdf" | "docx" | "xlsx" | "pptx" | "code" | "none";
export type PreviewActionKind = "open" | "open_in_folder" | "view" | "download";

export interface PreviewAction {
  kind: PreviewActionKind;
  label: string;
  /** 点下去真的会发生什么（可读；不谎报"已打开"） */
  note: string;
}

/** 后端 render_for 的结果里与本计划有关的字段。 */
export interface PreviewRenderState {
  status: "idle" | "loading" | "ready" | "failed" | "unsupported";
  /** 后端 classify_preview 的类别；还没拿到时可以缺省，前端按扩展名先判 */
  kind?: string;
  /** 后端给的真实原因（失败 / 不支持时一定有） */
  reason?: string;
  truncated?: boolean;
  version?: string;
}

export interface PreviewEnvironment {
  desktop: boolean;
}

export interface PreviewPlan {
  mode: PreviewMode;
  renderer: PreviewRenderer;
  reason: string;
  actions: PreviewAction[];
}

/** 后端 kind → 渲染器（渲染器名与契约 §3.5 一致）。 */
export const RENDERER_BY_KIND: Record<string, PreviewRenderer> = {
  image: "image",
  pdf: "pdf",
  docx: "docx",
  xlsx: "xlsx",
  pptx: "pptx",
  code: "code",
};

/** 扩展名 → 后端 kind（与服务端 classify_preview 同一张表；拿不到 kind 时先按它判）。 */
const KIND_BY_EXT: Record<string, string> = {
  png: "image", jpg: "image", jpeg: "image", gif: "image", webp: "image", bmp: "image",
  pdf: "pdf", docx: "docx", xlsx: "xlsx", xlsm: "xlsx", pptx: "pptx",
  doc: "legacy_office", xls: "legacy_office", ppt: "legacy_office",
  txt: "code", md: "code", markdown: "code", log: "code", csv: "code", tsv: "code",
  json: "code", jsonl: "code", ndjson: "code", json5: "code", xml: "code", yaml: "code",
  yml: "code", toml: "code", ini: "code", cfg: "code", conf: "code", env: "code",
  properties: "code", py: "code", pyi: "code", js: "code", mjs: "code", cjs: "code",
  ts: "code", tsx: "code", jsx: "code", vue: "code", svelte: "code", java: "code",
  kt: "code", kts: "code", scala: "code", go: "code", rs: "code", c: "code", h: "code",
  cc: "code", cpp: "code", hpp: "code", cs: "code", rb: "code", php: "code", pl: "code",
  lua: "code", r: "code", m: "code", swift: "code", sh: "code", bash: "code", zsh: "code",
  ps1: "code", psm1: "code", bat: "code", cmd: "code", sql: "code", gradle: "code",
  css: "code", scss: "code", less: "code", rst: "code", tex: "code", diff: "code",
  patch: "code", html: "code", htm: "code", xhtml: "code", svg: "code",
};

export const LEGACY_OFFICE_REASON =
  "旧版 Office（.doc/.xls/.ppt）是二进制格式，不能内嵌预览：请用系统程序打开，或另存为新格式后再预览";

export const UNLINKED_REASON_SUFFIX =
  "QIO 读不到它的正文，也打不开它（卡片上只记着文件名）";

/** 后端 kind（拿不到就按扩展名）→ 渲染器。 */
export function rendererFor(kind: string | undefined, name: string): PreviewRenderer {
  const resolved = kind || KIND_BY_EXT[extensionOf(name)] || "unsupported";
  return RENDERER_BY_KIND[resolved] ?? "none";
}

export function kindForName(name: string): string {
  return KIND_BY_EXT[extensionOf(name)] ?? "unsupported";
}

function extensionOf(name: string): string {
  const dot = String(name || "").lastIndexOf(".");
  return dot <= 0 ? "" : String(name).slice(dot + 1).toLowerCase();
}

/** 表格类扩展名（分类器把它们都算成 code，但界面上它们是「表格」）。 */
const TABULAR_EXT = new Set(["csv", "tsv", "xls", "xlsx", "xlsm"]);
/** 纯文本类扩展名（与源码区分：界面上一个是「文本」、一个是「代码」）。 */
const PLAIN_TEXT_EXT = new Set(["txt", "md", "markdown", "log", "rst", "tex"]);

/**
 * 卡片 meta 里已保存附件的显示名（meta.file 优先，其次 meta.image —— 与后端同一口径）。
 * 迁移把 meta.image 搬到 meta.file 之后，两条路都还能读到老数据。
 */
export function attachmentDisplayName(meta: Record<string, unknown> | null | undefined): string {
  const raw = (meta ?? {}) as Record<string, unknown>;
  for (const key of ["file", "image"]) {
    const block = raw[key];
    if (block && typeof block === "object") {
      const name = (block as { name?: unknown }).name;
      if (typeof name === "string" && name.trim()) return name.trim();
    }
  }
  const top = raw.name;
  return typeof top === "string" ? top.trim() : "";
}

/**
 * 材料卡的类型标签（2026-10-14 第九个问题）：**按附件类型显示**，不再区分「文件 / 图片」两种卡片。
 * 图片 / PDF / 表格 / 演示文稿 / 文档 / 文本 / 代码 / 其它；没有附件时显示「文件」。
 * 非材料卡返回空串，调用方用自己的标签（文字注释 / QIO 结果）。
 */
export function materialTypeLabel(
  kind: string | undefined,
  meta: Record<string, unknown> | null | undefined,
): string {
  if (kind !== "file" && kind !== "image") return "";
  const name = attachmentDisplayName(meta);
  if (!name) return "文件";
  const ext = extensionOf(name);
  if (TABULAR_EXT.has(ext)) return "表格";
  switch (kindForName(name)) {
    case "image":
      return "图片";
    case "pdf":
      return "PDF";
    case "xlsx":
      return "表格";
    case "pptx":
      return "演示文稿";
    case "docx":
      return "文档";
    case "code":
      return PLAIN_TEXT_EXT.has(ext) ? "文本" : "代码";
    default:
      return "文件";
  }
}

/** 卡片 meta 里的已保存附件 id（meta.file / meta.image，与后端同一口径）。 */
export function cardAttachmentId(card: Pick<BoardCard, "meta"> | null | undefined): string {
  const meta = (card?.meta ?? {}) as Record<string, unknown>;
  for (const key of ["image", "file"]) {
    const block = meta[key];
    if (block && typeof block === "object") {
      const value = (block as Record<string, unknown>).attachmentId ?? (block as Record<string, unknown>).attachment_id;
      if (value) return String(value).trim();
    }
  }
  const top = (meta.attachmentId as unknown) ?? (meta.attachment_id as unknown);
  return top ? String(top).trim() : "";
}

export function cardFileName(card: Pick<BoardCard, "meta"> | null | undefined): string {
  const meta = (card?.meta ?? {}) as Record<string, unknown>;
  for (const key of ["image", "file"]) {
    const block = meta[key];
    if (block && typeof block === "object") {
      const name = (block as Record<string, unknown>).name;
      if (name) return String(name);
    }
  }
  return meta.name ? String(meta.name) : "";
}

export function cardSizeBytes(card: Pick<BoardCard, "meta"> | null | undefined): number | null {
  const meta = (card?.meta ?? {}) as Record<string, unknown>;
  for (const key of ["image", "file"]) {
    const block = meta[key];
    if (block && typeof block === "object") {
      const size = (block as Record<string, unknown>).size;
      if (typeof size === "number") return size;
    }
  }
  return typeof meta.size === "number" ? (meta.size as number) : null;
}

/** 卡片 meta → 最小 AttachmentRef（只带真实有的事实，不伪造路径/状态）。 */
export function previewRefFor(card: Pick<BoardCard, "meta">): AttachmentRef {
  const name = cardFileName(card) || "未命名附件";
  return {
    id: cardAttachmentId(card),
    name,
    sizeBytes: cardSizeBytes(card) ?? 0,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
  };
}

/**
 * "点一下"的可执行动作（**复用 openPlanFor**，不另写一套判断）。
 * blocked（浏览器里的引用型）→ 空数组：**不给假按钮**，只把原因说清楚。
 */
export function previewPlanActions(ref: AttachmentRef, env: PreviewEnvironment): PreviewAction[] {
  const plan = openPlanFor(ref, { desktop: env.desktop });
  if (plan.action === "blocked") return [];
  const actions: PreviewAction[] = [];
  if (plan.action === "open") {
    actions.push({ kind: "open", label: "用系统程序打开", note: plan.reason || "交给系统默认程序打开原文件" });
  } else if (plan.action === "reveal") {
    actions.push({ kind: "open_in_folder", label: "在文件夹中显示", note: plan.reason || "可执行/脚本类不自动运行" });
  } else if (plan.action === "view") {
    actions.push({ kind: "view", label: "新标签页查看", note: plan.reason || "查看 QIO 保存的副本字节（带认证 fetch）" });
  } else {
    actions.push({ kind: "download", label: "下载副本", note: plan.reason || "保存到本地再用系统程序打开" });
  }
  if (plan.action !== "download" && ref.kind === "copy") {
    actions.push({ kind: "download", label: "下载副本", note: "保存到本地再用系统程序打开" });
  }
  return actions;
}

/**
 * 预览计划（纯函数）。
 *
 * * 没有关联附件 → unsupported + 无按钮（不给假入口）；
 * * 后端说 failed/unsupported → external（不能内嵌）+ 真实原因 + 打开/下载入口；
 * * legacy_office → external + 旧版 Office 的真实原因；
 * * 其余 → inline + 对应渲染器（loading/idle 阶段也给渲染器，界面先显示骨架）。
 */
export function previewPlan(
  card: Pick<BoardCard, "meta"> | null | undefined,
  state: PreviewRenderState,
  env: PreviewEnvironment,
): PreviewPlan {
  const name = cardFileName(card);
  const attachmentId = cardAttachmentId(card);
  if (!attachmentId) {
    const shown = name ? "「" + name + "」" : "这张卡片";
    return {
      mode: "unsupported",
      renderer: "none",
      reason: "还没有关联已保存的附件：" + shown + "，" + UNLINKED_REASON_SUFFIX,
      actions: [],
    };
  }
  const ref = previewRefFor(card as Pick<BoardCard, "meta">);
  const actions = previewPlanActions(ref, env);
  const declared = state.kind || kindForName(name);
  const renderer = rendererFor(declared, name);

  if (state.status === "failed") {
    return {
      mode: "external",
      renderer: "none",
      reason: state.reason || "这个文件这次没能转换出预览（原因未说明）：可以打开或下载后用本地程序查看",
      actions,
    };
  }
  if (state.status === "unsupported" || declared === "legacy_office" || declared === "unsupported") {
    return {
      mode: "external",
      renderer: "none",
      reason:
        state.reason ||
        (declared === "legacy_office" ? LEGACY_OFFICE_REASON : "这个类型不做内嵌预览：可以打开或下载"),
      actions,
    };
  }
  if (renderer === "none") {
    return {
      mode: "external",
      renderer: "none",
      reason: state.reason || "这个类型目前没有内嵌预览：可以打开或下载",
      actions,
    };
  }
  return { mode: "inline", renderer, reason: state.reason ?? "", actions };
}

/** 状态行文案（界面顶部那行"现在是什么状态"，五件事分开说）。 */
export function previewStatusText(state: PreviewRenderState, sizeBytes: number | null): string {
  const size = sizeBytes && sizeBytes > 0 ? "（" + humanSize(sizeBytes) + "）" : "";
  switch (state.status) {
    case "loading":
      return "正在生成预览…" + size;
    case "ready":
      return "预览可用" + size;
    case "failed":
      return "预览不可用" + size;
    case "unsupported":
      return "不做内嵌预览" + size;
    default:
      return "附件已保存" + size;
  }
}
