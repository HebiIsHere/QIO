/**
 * 代码 / 文本渲染器：**保真**（缩进、行号、语言信息）+ **只读**。
 *
 * 契约 §3.1：保留缩进、行号、语言信息；只读、不执行。
 * html/xml/svg 也走这里（后端已把它们转成纯文本行）—— 界面**绝不**按 HTML 解析。
 */
import { asArray, asBool, asNumber, asRecord, asString, fail, ok } from "./types";
import type { RenderResult } from "./types";

export const RENDERER = "code";

export interface CodeLine {
  /** 行号从 1 开始（与编辑器一致） */
  number: number;
  text: string;
}

export interface CodeView {
  language: string;
  languageLabel: string;
  lines: CodeLine[];
  totalLines: number;
  encoding: string;
  truncated: boolean;
}

const LANGUAGE_LABELS: Record<string, string> = {
  python: "Python", typescript: "TypeScript", tsx: "TSX", javascript: "JavaScript",
  jsx: "JSX", vue: "Vue", svelte: "Svelte", java: "Java", kotlin: "Kotlin",
  scala: "Scala", go: "Go", rust: "Rust", c: "C", cpp: "C++", csharp: "C#",
  ruby: "Ruby", php: "PHP", perl: "Perl", lua: "Lua", r: "R", objectivec: "Objective-C",
  swift: "Swift", bash: "Shell", powershell: "PowerShell", dos: "批处理", sql: "SQL",
  json: "JSON", yaml: "YAML", ini: "配置", markdown: "Markdown", text: "纯文本",
  css: "CSS", scss: "SCSS", less: "Less", xml: "XML", rst: "reStructuredText",
  latex: "LaTeX", diff: "Diff", groovy: "Groovy", dockerfile: "Dockerfile",
  makefile: "Makefile",
};

/** 语言 id → 界面标签（未知 id 原样显示，不假装认识）。 */
export function languageLabel(id: string): string {
  if (!id) return "纯文本";
  return LANGUAGE_LABELS[id] ?? id;
}

export function view(data: unknown): RenderResult<CodeView> {
  const record = asRecord(data);
  if (!record || record.kind !== "code") return fail("后端没有给出文本内容");
  const rawLines = asArray(record.lines);
  if (rawLines.some((line) => typeof line !== "string")) {
    return fail("文本内容形状不对（lines 里出现了非字符串）");
  }
  const lines: CodeLine[] = rawLines.map((text, index) => ({
    number: index + 1,
    text: text as string,
  }));
  const language = asString(record.language);
  return ok({
    language,
    languageLabel: languageLabel(language),
    lines,
    totalLines: asNumber(record.totalLines) ?? lines.length,
    encoding: asString(record.encoding) || "未说明",
    truncated: asBool(record.truncated),
  });
}
