/**
 * 图片渲染器：**只**做等比显示与缩放/适应（契约 §3.1：完整比例，可放大/缩小/适应）。
 *
 * 位图字节不在这里取：组件用带认证的 fetch 拿 Blob，再交给 <img> 的 blob URL。
 * 这里只有纯几何 —— 缩放的边界与"适应"的计算，可单测。
 */
import { asNumber, asRecord, fail, ok, humanBytes } from "./types";
import type { RenderResult } from "./types";

export const RENDERER = "image";

export const IMAGE_ZOOM_MIN = 0.25;
export const IMAGE_ZOOM_MAX = 8;
export const IMAGE_ZOOM_STEP = 1.25;

export interface ImageView {
  width: number | null;
  height: number | null;
  bytes: number;
  /** 给界面用的一行事实（宽高或"后端也没给出像素尺寸"） */
  shape: string;
}

export function view(data: unknown): RenderResult<ImageView> {
  const record = asRecord(data);
  if (!record || record.kind !== "image") return fail("后端没有给出图片的结构化信息");
  const width = asNumber(record.width);
  const height = asNumber(record.height);
  const bytes = asNumber(record.bytes) ?? 0;
  return ok({
    width,
    height,
    bytes,
    shape:
      width && height
        ? width + "×" + height + " 像素 · " + humanBytes(bytes)
        : "像素尺寸未能识别（格式变体或文件损坏）· " + humanBytes(bytes),
  });
}

export function clampZoom(value: number): number {
  if (!Number.isFinite(value)) return 1;
  return Math.min(IMAGE_ZOOM_MAX, Math.max(IMAGE_ZOOM_MIN, value));
}

/** 放大 / 缩小一档（等比：宽高同一个缩放因子，"完整比例"由此保证）。 */
export function stepZoom(current: number, direction: 1 | -1): number {
  const base = clampZoom(Number.isFinite(current) && current > 0 ? current : 1);
  return clampZoom(direction > 0 ? base * IMAGE_ZOOM_STEP : base / IMAGE_ZOOM_STEP);
}

/**
 * "适应"：把整张图放进给定盒子（contain），**不放大超过 100%** ——
 * 小图放大只会变糊，不是"看清"。盒子没有效尺寸时返回 1。
 */
export function fitScale(
  natural: { width: number; height: number },
  box: { width: number; height: number },
): number {
  const { width, height } = natural;
  if (!(width > 0) || !(height > 0)) return 1;
  if (!(box.width > 0) || !(box.height > 0)) return 1;
  return clampZoom(Math.min(box.width / width, box.height / height, 1));
}
