/**
 * DOCX 渲染器：把后端结构化数据（段落 / 标题 / 列表 / 表格）翻成可读视图。
 *
 * 诚实口径（契约 §3.1）：**逐页版面外观不保真**。界面必须写明
 * 「结构化正文预览，不是原版面」—— DOCX_NOTICE 就是那句话的唯一来源。
 */
import { asArray, asBool, asNumber, asRecord, asString, fail, ok } from "./types";
import type { RenderResult } from "./types";

export const RENDERER = "docx";

export const DOCX_NOTICE = "结构化正文预览，不是原版面：按正文顺序给出段落、标题、列表与表格的文字";

export interface DocxRunView {
  text: string;
  bold: boolean;
  italic: boolean;
}

export interface DocxBlockView {
  type: "heading" | "paragraph" | "list";
  level: number | null;
  ordered: boolean | null;
  runs: DocxRunView[];
  text: string;
  /** 列表标记（"•" / "1." / "?"）；非列表为空串 */
  marker: string;
}

export interface DocxTableView {
  rows: string[][];
  columns: number;
}

export interface DocxView {
  blocks: DocxBlockView[];
  tables: DocxTableView[];
  truncated: boolean;
  notice: string;
  empty: boolean;
}

function runsOf(raw: unknown): DocxRunView[] {
  return asArray(raw)
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => item !== null)
    .map((item) => ({
      text: asString(item.text),
      bold: asBool(item.bold),
      italic: asBool(item.italic),
    }))
    .filter((run) => run.text !== "");
}

export function view(data: unknown): RenderResult<DocxView> {
  const record = asRecord(data);
  if (!record || record.kind !== "docx") return fail("后端没有给出 Word 文档的结构化信息");
  const rawBlocks = asArray(record.blocks);
  const blocks: DocxBlockView[] = [];
  /** 有序列表的序号按**连续**的有序列表段累计（跨类型/跨标题重新从 1 开始） */
  let orderedCounter = 0;
  for (const item of rawBlocks) {
    const block = asRecord(item);
    if (!block) continue;
    const type = asString(block.type, "paragraph");
    const kind: DocxBlockView["type"] =
      type === "heading" || type === "list" ? (type as "heading" | "list") : "paragraph";
    const runs = runsOf(block.runs);
    const text = runs.map((run) => run.text).join("");
    let marker = "";
    if (kind === "heading") {
      orderedCounter = 0;
      const level = asNumber(block.level) ?? 1;
      marker = "H" + Math.max(1, Math.min(3, Math.round(level)));
    } else if (kind === "list") {
      const ordered = block.ordered;
      if (ordered === true) {
        orderedCounter += 1;
        marker = orderedCounter + ".";
      } else if (ordered === false) {
        orderedCounter = 0;
        marker = "•";
      } else {
        // 后端从 numbering.xml 推不出编号格式时给 null：界面如实标"编号未知"
        orderedCounter = 0;
        marker = "?";
      }
    } else {
      orderedCounter = 0;
    }
    blocks.push({
      type: kind,
      level: kind === "heading" ? Math.max(1, Math.min(3, Math.round(asNumber(block.level) ?? 1))) : null,
      ordered: kind === "list" ? (typeof block.ordered === "boolean" ? block.ordered : null) : null,
      runs,
      text,
      marker,
    });
  }
  const tables: DocxTableView[] = asArray(record.tables)
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => item !== null)
    .map((item) => {
      const rows = asArray(item.rows).map((row) => asArray(row).map((cell) => asString(cell)));
      return { rows, columns: rows.reduce((max, row) => Math.max(max, row.length), 0) };
    })
    .filter((table) => table.rows.length > 0);
  return ok({
    blocks,
    tables,
    truncated: asBool(record.truncated),
    notice: DOCX_NOTICE,
    empty: blocks.length === 0 && tables.length === 0,
  });
}
