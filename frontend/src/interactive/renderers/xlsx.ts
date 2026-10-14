/**
 * XLSX 渲染器：工作表切换 + 单元格地址 + **值 / 公式 / 缓存值分开**。
 *
 * 契约 §3.1：保留工作表名、单元格地址、公式与缓存值；**不计算公式**，明确标出。
 * 后端给的是 {ref, value, formula, cached}：
 *   * value   —— 普通单元格的值（共享字符串已解析成文字）
 *   * formula —— 这个单元格里的公式原文（界面显示时补上 "="）
 *   * cached  —— 公式**上一次保存时的缓存结果**（不是我们算的）
 */
import { asArray, asBool, asNumber, asRecord, asString, fail, ok } from "./types";
import type { RenderResult } from "./types";

export const RENDERER = "xlsx";

export type XlsxCellKind = "value" | "formula" | "empty";

export interface XlsxCellView {
  ref: string;
  column: string;
  row: number;
  value: string | null;
  formula: string | null;
  cached: string | null;
  kind: XlsxCellKind;
  /** 界面直接显示的主文本（公式会补上 "="） */
  display: string;
  /** 公式单元格的第二行说明（缓存值 / 没有缓存值） */
  formulaNote: string;
}

export interface XlsxRowView {
  index: number;
  cells: (XlsxCellView | null)[];
}

export interface XlsxSheetView {
  name: string;
  rows: XlsxRowView[];
  columns: string[];
  rowCount: number;
  columnCount: number;
}

export interface XlsxView {
  sheets: XlsxSheetView[];
  truncated: boolean;
  empty: boolean;
}

/** 0 → A、25 → Z、26 → AA（列标是稀疏表格的定位依据）。 */
export function columnLabel(index: number): string {
  let value = Math.max(0, Math.floor(index));
  let label = "";
  do {
    label = String.fromCharCode(65 + (value % 26)) + label;
    value = Math.floor(value / 26) - 1;
  } while (value >= 0);
  return label;
}

/** "B12" → { column: "B", columnIndex: 1, row: 12 }；解不出返回 null（不猜）。 */
export function parseRef(ref: string): { column: string; columnIndex: number; row: number } | null {
  const match = /^([A-Za-z]{1,3})(\d{1,7})$/.exec(String(ref || "").trim());
  if (!match) return null;
  const letters = match[1].toUpperCase();
  let columnIndex = 0;
  for (const char of letters) columnIndex = columnIndex * 26 + (char.charCodeAt(0) - 64);
  return { column: letters, columnIndex: columnIndex - 1, row: Number(match[2]) };
}

function cellView(raw: unknown): XlsxCellView | null {
  const cell = asRecord(raw);
  if (!cell) return null;
  const ref = asString(cell.ref);
  const value = typeof cell.value === "string" ? cell.value : null;
  const formula = typeof cell.formula === "string" && cell.formula !== "" ? cell.formula : null;
  const cached = typeof cell.cached === "string" ? cell.cached : null;
  const parsed = parseRef(ref);
  const kind: XlsxCellKind = formula ? "formula" : value !== null ? "value" : "empty";
  return {
    ref,
    column: parsed?.column ?? "",
    row: parsed?.row ?? 0,
    value,
    formula,
    cached,
    kind,
    display: formula ? "=" + formula : value ?? "",
    formulaNote: formula
      ? cached !== null && cached !== ""
        ? "缓存值：" + cached
        : "没有缓存值（打开文件的程序还没算过它）"
      : "",
  };
}

export function view(data: unknown): RenderResult<XlsxView> {
  const record = asRecord(data);
  if (!record || record.kind !== "xlsx") return fail("后端没有给出表格的结构化信息");
  const sheets: XlsxSheetView[] = asArray(record.sheets)
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => item !== null)
    .map((sheet) => {
      const rows: XlsxRowView[] = [];
      let maxColumn = -1;
      for (const rawRow of asArray(sheet.rows)) {
        const row = asRecord(rawRow);
        if (!row) continue;
        const index = asNumber(row.index) ?? rows.length + 1;
        const cells: (XlsxCellView | null)[] = [];
        for (const rawCell of asArray(row.cells)) {
          const parsedCell = cellView(rawCell);
          if (!parsedCell) continue;
          if (parsedCell.column) {
            const position = parseRef(parsedCell.ref)?.columnIndex ?? cells.length;
            if (position > maxColumn) maxColumn = position;
            while (cells.length < position) cells.push(null);
            cells[position] = parsedCell;
          } else {
            cells.push(parsedCell);
            if (cells.length - 1 > maxColumn) maxColumn = cells.length - 1;
          }
        }
        rows.push({ index, cells });
      }
      const columnCount = maxColumn + 1;
      return {
        name: asString(sheet.name, "未命名工作表"),
        rows,
        columns: Array.from({ length: columnCount }, (_unused, index) => columnLabel(index)),
        rowCount: rows.length,
        columnCount,
      };
    });
  return ok({
    sheets,
    truncated: asBool(record.truncated),

    empty: sheets.length === 0 || sheets.every((sheet) => sheet.rows.length === 0),
  });
}
