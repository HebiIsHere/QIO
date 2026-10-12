/**
 * 渲染器共用的最小类型与取值辅助（渲染器模块之间**不互相 import**）。
 *
 * 每个渲染器导出一个纯函数 view(data)：把后端**结构化数据**翻成视图模型，
 * 数据不合契约时返回 { ok: false, reason } —— 界面据此显示真实原因，
 * 绝不"猜着渲染"出一份看起来正确的内容。
 */

export type RendererName = "image" | "pdf" | "docx" | "xlsx" | "pptx" | "code";

export type RenderResult<T> = { ok: true; view: T } | { ok: false; reason: string };

export function ok<T>(view: T): RenderResult<T> {
  return { ok: true, view };
}

export function fail<T = never>(reason: string): RenderResult<T> {
  return { ok: false, reason };
}

export function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function asBool(value: unknown): boolean {
  return value === true;
}

/** 给界面用的字节数（十进制，与后端 human_size 同一口径）。 */
export function humanBytes(bytes: number): string {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return "大小未知";
  if (value < 1000) return Math.round(value) + " B";
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1000;
  let index = 0;
  while (size >= 1000 && index < units.length - 1) {
    size /= 1000;
    index += 1;
  }
  return size.toFixed(1) + " " + units[index];
}
