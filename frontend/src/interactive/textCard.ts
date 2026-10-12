/**
 * 统一文字卡的内容模型（块序列 ↔ 编辑区文本）。
 *
 * 契约：docs/superpowers/plans/2026-10-12-unified-cards-media-preview-contract.md §1。
 * 本文件是**纯函数**：不依赖 vue、不碰 DOM —— 组件与 store 只消费结果，判断只有一份。
 *
 * 三条不可动摇的性质（测试锁死）：
 *   1. 代码块逐字保真：缩进 / 换行 / 行尾空白 / 语言信息都不改写；
 *   2. serialize(parse(t)) === t（规范写法）；
 *   3. parse(serialize(doc)) 的块序与 code / list 文本不变。
 */

export type TextBlockKind = "paragraph" | "heading" | "list" | "quote" | "code" | "link";

export interface TextBlockAttrs {
  language?: string;
  href?: string;
  fence?: string;
}

export interface TextBlock {
  id: string;
  kind: TextBlockKind;
  text: string;
  /** heading 专用：1..3 */
  level?: number;
  /** list 专用：true = 有序列表 */
  ordered?: boolean;
  attrs?: TextBlockAttrs;
}

export interface TextDoc {
  schema: "qio.textcard/1";
  blocks: TextBlock[];
  /** 解析过程中如实记录的问题（内容仍然保留） */
  issues?: string[];
}

export const TEXT_DOC_SCHEMA = "qio.textcard/1" as const;
export const FENCE = "\u0060\u0060\u0060";
export const MAX_HEADING_LEVEL = 3;

let blockSeq = 0;

export function newBlockId(): string {
  blockSeq += 1;
  const rand = Math.random().toString(16).slice(2, 10);
  return "b_" + blockSeq.toString(16).padStart(4, "0") + rand;
}

export function emptyDoc(): TextDoc {
  return { schema: TEXT_DOC_SCHEMA, blocks: [] };
}

export function newBlock(kind: TextBlockKind, text = "", extra: Partial<TextBlock> = {}): TextBlock {
  const block: TextBlock = { id: newBlockId(), kind, text };
  if (kind === "heading") block.level = clampLevel(extra.level ?? 1);
  if (kind === "list") block.ordered = Boolean(extra.ordered);
  if (kind === "code") block.attrs = { language: extra.attrs?.language ?? "", fence: FENCE };
  if (kind === "link") block.attrs = { href: extra.attrs?.href ?? "" };
  return block;
}

function clampLevel(level: number): number {
  const n = Math.round(Number(level));
  if (!Number.isFinite(n) || n < 1) return 1;
  return Math.min(MAX_HEADING_LEVEL, n);
}

// ---------------------------------------------------------------------------
// 编辑区文本 → 块序列
// ---------------------------------------------------------------------------

interface RawLine {
  text: string;
  blank: boolean;
}

function linesOf(text: string): RawLine[] {
  const raw = String(text ?? "").split("\n");
  return raw.map((line) => ({ text: line.replace(/\r$/, ""), blank: line.trim() === "" }));
}

const LINK_LINE = /^\[([^\]]*)\]\(([^)]*)\)$/;
const UL_LINE = /^[-*+] (.*)$/;
const OL_LINE = /^\d{1,9}\. (.*)$/;
const QUOTE_LINE = /^> ?(.*)$/;
const HEADING_LINE = /^(#{1,3}) (.*)$/;

/** 编辑区文本 → 块序列。围栏内逐字；未闭合围栏记 issue 并读到末尾。 */
export function parseDocText(text: string): TextDoc {
  const lines = linesOf(text);
  const blocks: TextBlock[] = [];
  const issues: string[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.blank) {
      i += 1;
      continue;
    }
    // 1) 代码围栏
    if (line.text.startsWith(FENCE)) {
      const language = line.text.slice(FENCE.length).trim();
      const body: string[] = [];
      i += 1;
      let closed = false;
      while (i < lines.length) {
        if (lines[i].text.startsWith(FENCE)) {
          closed = true;
          i += 1;
          break;
        }
        body.push(lines[i].text);
        i += 1;
      }
      if (!closed) issues.push("代码围栏未闭合，已按代码块读到末尾");
      blocks.push(newBlock("code", body.length ? body.join("\n") + "\n" : "", { attrs: { language } }));
      continue;
    }
    // 2) 链接（单独成行）
    const link = LINK_LINE.exec(line.text);
    if (link) {
      blocks.push(newBlock("link", link[1], { attrs: { href: link[2] } }));
      i += 1;
      continue;
    }
    // 3) 连续同类行归为一个块
    const heading = HEADING_LINE.exec(line.text);
    if (heading) {
      blocks.push(newBlock("heading", heading[2], { level: heading[1].length }));
      i += 1;
      continue;
    }
    if (QUOTE_LINE.test(line.text)) {
      const parts: string[] = [];
      while (i < lines.length && !lines[i].blank && QUOTE_LINE.test(lines[i].text)) {
        parts.push(QUOTE_LINE.exec(lines[i].text)![1]);
        i += 1;
      }
      blocks.push(newBlock("quote", parts.join("\n")));
      continue;
    }
    const ordered = OL_LINE.test(line.text);
    if (ordered || UL_LINE.test(line.text)) {
      const re = ordered ? OL_LINE : UL_LINE;
      const parts: string[] = [];
      while (i < lines.length && !lines[i].blank && re.test(lines[i].text)) {
        parts.push(re.exec(lines[i].text)![1]);
        i += 1;
      }
      blocks.push(newBlock("list", parts.join("\n"), { ordered }));
      continue;
    }
    // 4) 普通段落：吃到空行；行内以 # / > / - 开头的仍然是正文（不会被拆出来）
    const para: string[] = [];
    while (i < lines.length && !lines[i].blank) {
      para.push(lines[i].text);
      i += 1;
    }
    blocks.push(newBlock("paragraph", para.join("\n")));
  }
  const doc: TextDoc = { schema: TEXT_DOC_SCHEMA, blocks };
  if (issues.length) doc.issues = issues;
  return doc;
}

// ---------------------------------------------------------------------------
// 块序列 → 编辑区文本
// ---------------------------------------------------------------------------

function linesOfText(text: string): string[] {
  const raw = String(text ?? "").split("\n");
  if (raw.length && raw[raw.length - 1] === "") raw.pop();
  return raw;
}

function serializeBlock(block: TextBlock): string[] {
  switch (block.kind) {
    case "heading":
      return ["#".repeat(clampLevel(block.level ?? 1)) + " " + block.text];
    case "list": {
      const prefix = block.ordered ? "1. " : "- ";
      return linesOfText(block.text).map((line) => prefix + line);
    }
    case "quote":
      return linesOfText(block.text).map((line) => "> " + line);
    case "code": {
      const lang = block.attrs?.language ?? "";
      const body = linesOfText(block.text);
      if (linesOfText(block.text).length === 0 && block.text.endsWith("\n")) body.push("");
      return [FENCE + lang, ...body, FENCE];
    }
    case "link": {
      const href = block.attrs?.href ?? "";
      return ["[" + (block.text ?? "") + "](" + href + ")"];
    }
    default:
      return linesOfText(block.text);
  }
}

/** 块序列 → 编辑区文本（块之间恰好一个空行）。 */
export function serializeDocText(doc: TextDoc | null | undefined): string {
  const blocks = doc?.blocks ?? [];
  const chunks: string[] = [];
  for (const block of blocks) {
    const lines = serializeBlock(block);
    chunks.push(lines.join("\n"));
  }
  if (!chunks.length) return "";
  return chunks.join("\n\n") + "\n";
}

// ---------------------------------------------------------------------------
// 派生
// ---------------------------------------------------------------------------

/** 去掉行内格式标记（\*\* / * / __ / ~~ / 单个反引号）。返回空串时回退原文，绝不丢内容。 */
export function inlinePlain(text: string): string {
  const source = String(text ?? "");
  if (!source) return "";
  const stripped = source
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/__(.+?)__/g, "$1")
    .replace(/~~(.+?)~~/g, "$1")
    .replace(/(?<![\w\u4e00-\u9fff])`([^`]+)`/g, "$1")
    .replace(/\*([^*\n]+)\*/g, "$1");
  return stripped;
}

/** 块序列 → 纯文本（界面摘要、搜索、card.content）。 */
export function blocksToText(doc: TextDoc | null | undefined): string {
  const blocks = doc?.blocks ?? [];
  const out: string[] = [];
  for (const block of blocks) {
    switch (block.kind) {
      case "heading":
        out.push("#".repeat(clampLevel(block.level ?? 1)) + " " + block.text);
        break;
      case "list":
        for (const line of linesOfText(block.text)) out.push((block.ordered ? "1. " : "- ") + line);
        break;
      case "quote":
        out.push(block.text);
        break;
      case "code":
        out.push(block.text);
        break;
      case "link": {
        const href = block.attrs?.href ?? "";
        const title = block.text ?? "";
        out.push(title ? title + " <" + href + ">" : href);
        break;
      }
      default:
        out.push(inlinePlain(block.text));
    }
  }
  return out.join("\n");
}

// ---------------------------------------------------------------------------
// 卡片字段
// ---------------------------------------------------------------------------

export interface CardDocFields {
  content: string;
  meta: { doc: TextDoc; docText: string };
}

export function cardDocFields(doc: TextDoc): CardDocFields {
  const safe = doc ?? emptyDoc();
  return {
    content: blocksToText(safe),
    meta: { doc: safe, docText: serializeDocText(safe) },
  };
}

function isDoc(value: unknown): value is TextDoc {
  return Boolean(value) && typeof value === "object" && Array.isArray((value as TextDoc).blocks);
}

/**
 * 卡片 → 内容模型。
 * meta.doc 是权威；meta.docText 次之；最后用 content 兜底（旧数据整体成为一个段落）。
 */
export function docFromCard(card: { content?: string; meta?: Record<string, unknown> } | null | undefined): TextDoc {
  const meta = card?.meta ?? {};
  if (isDoc(meta.doc)) return normalizeDoc(meta.doc);
  if (typeof meta.docText === "string" && meta.docText !== "") return parseDocText(meta.docText);
  const content = String(card?.content ?? "");
  if (!content) return emptyDoc();
  return parseDocText(content.endsWith("\n") ? content : content + "\n");
}

/** 任何输入都返回合法 doc：损坏内容保留为段落并记 issues，绝不静默丢内容。 */
export function normalizeDoc(raw: unknown): TextDoc {
  const issues: string[] = [];
  const source = raw as { blocks?: unknown; issues?: unknown };
  const incoming = Array.isArray(source?.blocks) ? (source!.blocks as unknown[]) : [];
  if (raw && !Array.isArray(source?.blocks)) issues.push("内容结构无法识别，已按原文保留");
  const blocks: TextBlock[] = [];
  for (const item of incoming) {
    const block = item as Partial<TextBlock> | null;
    if (!block || typeof block !== "object" || typeof block.kind !== "string") {
      issues.push("有一个内容块结构损坏，已按原文保留");
      blocks.push(newBlock("paragraph", typeof item === "string" ? item : ""));
      continue;
    }
    const text = typeof block.text === "string" ? block.text : "";
    switch (block.kind) {
      case "heading":
        blocks.push({ id: block.id || newBlockId(), kind: "heading", text, level: clampLevel(block.level ?? 1) });
        break;
      case "list":
        blocks.push({ id: block.id || newBlockId(), kind: "list", text, ordered: Boolean(block.ordered) });
        break;
      case "quote":
        blocks.push({ id: block.id || newBlockId(), kind: "quote", text });
        break;
      case "code":
        blocks.push({
          id: block.id || newBlockId(),
          kind: "code",
          text,
          attrs: { language: block.attrs?.language ?? "", fence: FENCE },
        });
        break;
      case "link":
        blocks.push({ id: block.id || newBlockId(), kind: "link", text, attrs: { href: block.attrs?.href ?? "" } });
        break;
      case "paragraph":
        blocks.push({ id: block.id || newBlockId(), kind: "paragraph", text });
        break;
      default:
        issues.push("有一个内容块的类型无法识别，已按原文保留为普通文字");
        blocks.push({ id: block.id || newBlockId(), kind: "paragraph", text });
    }
  }
  const merged = [...(Array.isArray(source?.issues) ? (source!.issues as string[]) : []), ...issues];
  const doc: TextDoc = { schema: TEXT_DOC_SCHEMA, blocks };
  if (merged.length) doc.issues = merged;
  return doc;
}

// ---------------------------------------------------------------------------
// 块级操作（TextCardEditor 与测试共用的最小集合）
// ---------------------------------------------------------------------------

export function blockAt(doc: TextDoc, index: number): TextBlock | undefined {
  return doc?.blocks?.[index];
}

export function insertBlock(doc: TextDoc, index: number, block: TextBlock): TextDoc {
  const blocks = [...(doc?.blocks ?? [])];
  const at = Math.max(0, Math.min(blocks.length, index));
  blocks.splice(at, 0, block);
  return { ...doc, blocks };
}

export function removeBlock(doc: TextDoc, index: number): TextDoc {
  const blocks = [...(doc?.blocks ?? [])];
  if (index >= 0 && index < blocks.length) blocks.splice(index, 1);
  return { ...doc, blocks };
}

export function replaceBlock(doc: TextDoc, index: number, block: TextBlock): TextDoc {
  const blocks = [...(doc?.blocks ?? [])];
  if (index >= 0 && index < blocks.length) blocks[index] = block;
  return { ...doc, blocks };
}

export function moveBlock(doc: TextDoc, from: number, to: number): TextDoc {
  const blocks = [...(doc?.blocks ?? [])];
  if (from < 0 || from >= blocks.length) return { ...doc, blocks };
  const [item] = blocks.splice(from, 1);
  const at = Math.max(0, Math.min(blocks.length, to));
  blocks.splice(at, 0, item);
  return { ...doc, blocks };
}

// ---------------------------------------------------------------------------
// 选中文字 / 段落的格式设置（工具栏）：只改编辑区文本，不改模型
// ---------------------------------------------------------------------------

export type InlineFormat = "bold" | "italic" | "code" | "strike" | "heading2" | "quote" | "list" | "link";

export interface WrapResult {
  text: string;
  selection: { start: number; end: number };
}

function toggleWrap(text: string, start: number, end: number, mark: string): WrapResult {
  const before = text.slice(0, start);
  const selected = text.slice(start, end);
  const after = text.slice(end);
  if (selected.startsWith(mark) && selected.endsWith(mark) && selected.length >= mark.length * 2) {
    const inner = selected.slice(mark.length, selected.length - mark.length);
    return { text: before + inner + after, selection: { start, end: start + inner.length } };
  }
  if (before.endsWith(mark) && after.startsWith(mark)) {
    return {
      text: before.slice(0, before.length - mark.length) + selected + after.slice(mark.length),
      selection: { start: start - mark.length, end: end - mark.length },
    };
  }
  return {
    text: before + mark + selected + mark + after,
    selection: { start: start + mark.length, end: end + mark.length },
  };
}

function lineRangeOf(text: string, start: number, end: number): { from: number; to: number } {
  const from = text.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
  const nextBreak = text.indexOf("\n", end);
  const to = nextBreak === -1 ? text.length : nextBreak;
  return { from, to };
}

function mapLines(text: string, start: number, end: number, fn: (line: string) => string): WrapResult {
  const { from, to } = lineRangeOf(text, start, end);
  const block = text.slice(from, to);
  const mapped = block.split("\n").map(fn).join("\n");
  const delta = mapped.length - block.length;
  return { text: text.slice(0, from) + mapped + text.slice(to), selection: { start, end: end + delta } };
}

/**
 * 工具栏的一次格式动作。返回新的编辑区文本与新的选区 —— 调用方负责写回并把选区还给用户，
 * 所以「在文字、链接和代码之间继续输入 / 移动光标 / 选择 / 撤销重做」不被格式动作打断。
 */
export function wrapSelection(
  text: string,
  start: number,
  end: number,
  kind: InlineFormat,
  href = "",
): WrapResult {
  const source = String(text ?? "");
  const safeStart = Math.max(0, Math.min(source.length, start));
  const safeEnd = Math.max(safeStart, Math.min(source.length, end));
  switch (kind) {
    case "bold":
      return toggleWrap(source, safeStart, safeEnd, "**");
    case "italic":
      return toggleWrap(source, safeStart, safeEnd, "*");
    case "strike":
      return toggleWrap(source, safeStart, safeEnd, "~~");
    case "code":
      return toggleWrap(source, safeStart, safeEnd, "`");
    case "link": {
      const label = source.slice(safeStart, safeEnd) || "链接";
      const inserted = "[" + label + "](" + href + ")";
      return {
        text: source.slice(0, safeStart) + inserted + source.slice(safeEnd),
        selection: { start: safeStart + 1, end: safeStart + 1 + label.length },
      };
    }
    case "heading2":
      return mapLines(source, safeStart, safeEnd, (line) => (line.startsWith("## ") ? line.slice(3) : "## " + line));
    case "quote":
      return mapLines(source, safeStart, safeEnd, (line) => (line.startsWith("> ") ? line.slice(2) : "> " + line));
    case "list":
      return mapLines(source, safeStart, safeEnd, (line) => (line.startsWith("- ") ? line.slice(2) : "- " + line));
    default:
      return { text: source, selection: { start: safeStart, end: safeEnd } };
  }
}

// ---------------------------------------------------------------------------
// 扩展名 → 语言
// ---------------------------------------------------------------------------

const LANGUAGE_BY_EXT: Record<string, string> = {
  py: "python",
  pyi: "python",
  ts: "typescript",
  tsx: "typescript",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  vue: "xml",
  java: "java",
  kt: "kotlin",
  c: "c",
  h: "c",
  cc: "cpp",
  cpp: "cpp",
  cxx: "cpp",
  hpp: "cpp",
  cs: "csharp",
  go: "go",
  rs: "rust",
  rb: "ruby",
  php: "php",
  swift: "swift",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  ps1: "powershell",
  sql: "sql",
  json: "json",
  json5: "json",
  yaml: "yaml",
  yml: "yaml",
  toml: "ini",
  ini: "ini",
  cfg: "ini",
  md: "markdown",
  markdown: "markdown",
  txt: "text",
  log: "text",
  csv: "text",
  tsv: "text",
  css: "css",
  scss: "scss",
  less: "less",
  html: "xml",
  htm: "xml",
  xml: "xml",
  svg: "xml",
  tex: "latex",
  dockerfile: "dockerfile",
  makefile: "makefile",
  bat: "dos",
  cmd: "dos",
};

/** 文件名 / 路径 → 语言 id（未知返回空串）。 */
export function detectLanguage(name: string): string {
  const raw = String(name ?? "").trim();
  if (!raw) return "";
  const base = raw.replace(/\\/g, "/").split("/").pop() ?? "";
  const lower = base.toLowerCase();
  if (lower === "dockerfile") return "dockerfile";
  if (lower === "makefile") return "makefile";
  const dot = lower.lastIndexOf(".");
  if (dot <= 0 || dot === lower.length - 1) return "";
  return LANGUAGE_BY_EXT[lower.slice(dot + 1)] ?? "";
}
