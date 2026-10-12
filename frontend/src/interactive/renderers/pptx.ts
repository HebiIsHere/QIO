/**
 * PPTX 渲染器：按幻灯片给出**文本（含备注）/ 表格 / 图片清单**。
 *
 * 诚实口径（契约 §3.1）：**不是原版面渲染**。界面必须写明 —— PPTX_NOTICE 是那句话的唯一来源。
 * 备注页文字在后端带「备注：」前缀；这里把它拆出来单独展示（来源仍然写得清清楚楚）。
 */
import { asArray, asBool, asNumber, asRecord, asString, fail, humanBytes, ok } from "./types";
import type { RenderResult } from "./types";

export const RENDERER = "pptx";

export const PPTX_NOTICE =
  "结构化幻灯片预览（不是原版面）：按页给出文本框文字、表格与图片清单；图片只列清单，不在这里显示画面";

export const NOTES_PREFIX = "备注：";

export interface PptxImageView {
  name: string;
  bytes: number;
  size: string;
}

export interface PptxSlideView {
  index: number;
  title: string;
  texts: string[];
  notes: string[];
  images: PptxImageView[];
  /** 每一张表：行 × 单元格文本 */
  tables: string[][][];
}

export interface PptxView {
  slides: PptxSlideView[];
  truncated: boolean;
  notice: string;
  empty: boolean;
}

export function view(data: unknown): RenderResult<PptxView> {
  const record = asRecord(data);
  if (!record || record.kind !== "pptx") return fail("后端没有给出演示文稿的结构化信息");
  const slides: PptxSlideView[] = asArray(record.slides)
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => item !== null)
    .map((slide, position) => {
      const texts: string[] = [];
      const notes: string[] = [];
      for (const raw of asArray(slide.texts)) {
        const text = asString(raw);
        if (!text) continue;
        if (text.startsWith(NOTES_PREFIX)) notes.push(text.slice(NOTES_PREFIX.length));
        else texts.push(text);
      }
      const images: PptxImageView[] = asArray(slide.images)
        .map((item) => asRecord(item))
        .filter((item): item is Record<string, unknown> => item !== null)
        .map((image) => {
          const bytes = asNumber(image.bytes) ?? 0;
          return { name: asString(image.name, "未命名图片"), bytes, size: humanBytes(bytes) };
        });
      const tables: string[][][] = asArray(slide.tables).map((table) =>
        asArray(table).map((row) => asArray(row).map((cell) => asString(cell))),
      );
      return {
        index: asNumber(slide.index) ?? position + 1,
        title: asString(slide.title),
        texts,
        notes,
        images,
        tables,
      };
    });
  return ok({
    slides,
    truncated: asBool(record.truncated),
    notice: PPTX_NOTICE,
    empty: slides.length === 0,
  });
}
