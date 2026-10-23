/**
 * 图片显示比例（Phase 2-C 冻结字段：meta.display = { width, height, aspectRatio }）。
 *
 * 这个模块是**纯函数**：不 import vue、不碰 DOM、不发请求。
 * 卡片、画布、阅读态与测试都从这里取同一套判定，避免「各算一套比例」：
 *
 * * 解码尺寸只认真实像素：宽高必须是正的有限数；0 / NaN / 负值 / 超范围一律当作未知，
 *   **不写、不猜、不用 0 占位**（契约 §5.2）；
 * * meta.display 记录浏览器解码后的显示像素与比例，不参与材料语义版本（写入时不改其它 meta）；
 * * 卡片尺寸按比例约束：所有拖动 / 键盘 / 组缩放 / 工具尺寸 / 恢复路径都可以复用
 *   constrainCardSizeToRatio / suggestedCardSizeForRatio，再由 Lead 在 BoardCanvas 接线；
 * * 长图 / 竖图有可测阈值，用来保证「不会永远打不开」——极端比例也必须有详情入口。
 *
 * 引用：docs/interactive-page-v2-contract.md §5.2；Phase 1 数据审计 §5.2。
 */
import { CARD_MAX_SIZE, CARD_MIN_SIZE } from "../geometry";

export interface ImageDisplayMeta {
  /** 解码后的真实显示宽度（设备无关像素；由浏览器解码结果给出） */
  width: number;
  /** 解码后的真实显示高度（已包含 EXIF 方向的效果） */
  height: number;
  /** width / height，保留 6 位小数；写入时一定是有限正数 */
  aspectRatio: number;
}

export interface ImageDisplayLimits {
  minW: number;
  minH: number;
  maxW: number;
  maxH: number;
}

/** 卡片口径的默认限制（与 geometry 的唯一来源一致；这里只做只读引用）。 */
export const IMAGE_CARD_LIMITS: ImageDisplayLimits = {
  minW: CARD_MIN_SIZE.w,
  minH: CARD_MIN_SIZE.h,
  maxW: CARD_MAX_SIZE.w,
  maxH: CARD_MAX_SIZE.h,
};

/** 比例允许误差：2% 以内视为「已满足比例」（像素取整后必然有一点点偏差）。 */
export const IMAGE_RATIO_TOLERANCE = 0.02;

/** 极端长图阈值：宽高比 ≥ 2.2（横向长条）。 */
export const IMAGE_LONG_RATIO = 2.2;
/** 极端竖图阈值：宽高比 ≤ 0.45（竖向长条）。 */
export const IMAGE_TALL_RATIO = 0.45;

/** 解码像素的合理上界：超过它的值来自脏数据，不当作真实尺寸。 */
export const IMAGE_DISPLAY_MAX_SIDE = 100_000;

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** 正整数像素（自然解码尺寸是整数；这里不接受 0 / 小数脏值）。 */
export function isDecodedPixel(value: unknown): value is number {
  return (
    isFiniteNumber(value) &&
    value > 0 &&
    value <= IMAGE_DISPLAY_MAX_SIDE &&
    Math.round(value) === value
  );
}

function asPositive(value: unknown): number | null {
  return isFiniteNumber(value) && value > 0 && value <= IMAGE_DISPLAY_MAX_SIDE ? value : null;
}

function roundRatio(ratio: number): number {
  return Math.round(ratio * 1_000_000) / 1_000_000;
}

function roundSize(value: number): number {
  return Math.max(1, Math.round(value));
}

/**
 * 宽高 → 比例；任何一项不合法都返回 null（不拿 0 做除法、不返回 NaN/Infinity）。
 */
export function normalizeAspectRatio(width: unknown, height: unknown): number | null {
  const w = asPositive(width);
  const h = asPositive(height);
  if (w === null || h === null) return null;
  const ratio = w / h;
  return Number.isFinite(ratio) && ratio > 0 ? roundRatio(ratio) : null;
}

/**
 * 浏览器解码结果 → meta.display。
 * 只接受正整数的真实像素；0 / NaN / 负值 / 超范围返回 null（调用方不得写盘）。
 */
export function decodeDisplayMeta(width: unknown, height: unknown): ImageDisplayMeta | null {
  if (!isDecodedPixel(width) || !isDecodedPixel(height)) return null;
  const ratio = normalizeAspectRatio(width, height);
  if (ratio === null) return null;
  return { width, height, aspectRatio: ratio };
}

/**
 * 从卡片 meta 读取 meta.display。
 *
 * 规则：
 * * 宽高必须都是正的有限数；否则未知（null），**不猜比例**；
 * * aspectRatio 缺失 / 非法 / 与宽高明显不符时，按宽高算（这是计算，不是猜测）；
 * * 返回对象按契约固定为 width / height / aspectRatio 三个字段。
 */
export function readImageDisplay(meta: unknown): ImageDisplayMeta | null {
  if (!meta || typeof meta !== "object" || Array.isArray(meta)) return null;
  const block = (meta as Record<string, unknown>).display;
  if (!block || typeof block !== "object" || Array.isArray(block)) return null;
  const raw = block as Record<string, unknown>;
  const width = asPositive(raw.width);
  const height = asPositive(raw.height);
  if (width === null || height === null) return null;
  const derived = normalizeAspectRatio(width, height);
  if (derived === null) return null;
  const declared = asPositive(raw.aspectRatio);
  const aspectRatio =
    declared !== null && Math.abs(declared - derived) / derived <= 0.01 ? roundRatio(declared) : derived;
  return {
    width: roundSize(width),
    height: roundSize(height),
    aspectRatio,
  };
}

/** 写 meta.display：只替换 display 块，其它 meta 字段原样保留（不产生第二份比例来源）。 */
export function writeImageDisplay(
  meta: Record<string, unknown> | null | undefined,
  display: ImageDisplayMeta | null,
): Record<string, unknown> {
  const base = meta && typeof meta === "object" && !Array.isArray(meta) ? { ...meta } : {};
  if (!display) return base;
  const clean = decodeDisplayMeta(display.width, display.height);
  if (!clean) return base;
  base.display = { width: clean.width, height: clean.height, aspectRatio: clean.aspectRatio };
  return base;
}

/** 两份比例是否相同（默认 2% 误差）。 */
export function sameAspectRatio(a: number | null | undefined, b: number | null | undefined, tolerance = IMAGE_RATIO_TOLERANCE): boolean {
  if (!isFiniteNumber(a) || !isFiniteNumber(b) || a <= 0 || b <= 0) return false;
  return Math.abs(a - b) / Math.max(a, b) <= tolerance;
}

/** 卡片尺寸是否已满足图片比例（默认 2% 误差）。 */
export function isAspectSatisfied(
  size: { w: number; h: number } | null | undefined,
  ratio: number | null | undefined,
  tolerance = IMAGE_RATIO_TOLERANCE,
): boolean {
  if (!size || !isFiniteNumber(size.w) || !isFiniteNumber(size.h) || size.w <= 0 || size.h <= 0) return false;
  return sameAspectRatio(size.w / size.h, ratio, tolerance);
}

/** 图片是否是横向长条（≥ 2.2:1）。 */
export function isLongImage(display: Pick<ImageDisplayMeta, "aspectRatio"> | null | undefined): boolean {
  return Boolean(display && isFiniteNumber(display.aspectRatio) && display.aspectRatio >= IMAGE_LONG_RATIO);
}

/** 图片是否是竖向长条（≤ 0.45:1）。 */
export function isTallImage(display: Pick<ImageDisplayMeta, "aspectRatio"> | null | undefined): boolean {
  return Boolean(display && isFiniteNumber(display.aspectRatio) && display.aspectRatio > 0 && display.aspectRatio <= IMAGE_TALL_RATIO);
}

/**
 * 是否需要「详情」入口 / 提示：
 * * 极端长图、极端竖图：永远返回 true（保证可展开，不会被简洁态永远锁住）；
 * * 比例未知：返回 true（详情态会显示加载 / 失败状态，也不会假成功）；
 * * 已知比例但与卡片比例明显不符：true（简洁态会留白，详情态可以完整看）。
 */
export function shouldOfferImageDetail(
  display: ImageDisplayMeta | null | undefined,
  size?: { w: number; h: number } | null,
): boolean {
  if (!display) return true;
  if (isLongImage(display) || isTallImage(display)) return true;
  if (!size || !isFiniteNumber(size.w) || !isFiniteNumber(size.h) || size.w <= 0 || size.h <= 0) return true;
  return !isAspectSatisfied(size, display.aspectRatio);
}

/** 详情入口的 aria / title 文案；极端比例说得更具体。 */
export function imageDetailHint(display: ImageDisplayMeta | null | undefined): string {
  if (isLongImage(display)) return "这是一张很长的横向图片：可以查看详情完整阅读";
  if (isTallImage(display)) return "这是一张很长的竖向图片：可以查看详情完整阅读";
  return "查看图片详情（只改查看方式，不改内容与卡片尺寸）";
}

/** 详情态的可见标签（避免和文字卡「展开预览」入口混用同一句话）。 */
export function imageDetailLabel(open: boolean): string {
  return open ? "收起详情" : "查看详情";
}

function clampLimits(limits: Partial<ImageDisplayLimits> | undefined): ImageDisplayLimits {
  const merged = { ...IMAGE_CARD_LIMITS, ...(limits ?? {}) };
  return {
    minW: Math.max(1, merged.minW),
    minH: Math.max(1, merged.minH),
    maxW: Math.max(Math.max(1, merged.minW), merged.maxW),
    maxH: Math.max(Math.max(1, merged.minH), merged.maxH),
  };
}

/** 在限制里按比例求一个尺寸：优先保留面积，再保证宽高都在口径内。 */
export function suggestedCardSizeForRatio(
  size: { w: number; h: number } | null | undefined,
  ratio: number | null | undefined,
  limits?: Partial<ImageDisplayLimits>,
): { w: number; h: number } {
  const box = clampLimits(limits);
  if (!isFiniteNumber(ratio) || ratio <= 0) {
    return {
      w: Math.min(box.maxW, Math.max(box.minW, roundSize(size?.w ?? box.minW))),
      h: Math.min(box.maxH, Math.max(box.minH, roundSize(size?.h ?? box.minH))),
    };
  }
  const currentW = isFiniteNumber(size?.w) && size!.w > 0 ? roundSize(size!.w) : box.minW;
  const currentH = isFiniteNumber(size?.h) && size!.h > 0 ? roundSize(size!.h) : box.minH;
  const area = Math.max(1, currentW * currentH);

  // 以面积为锚点：先按面积求宽高，再按限制夹取；夹取后仍要回到比例上。
  let w = Math.sqrt(area * ratio);
  let h = w / ratio;
  if (h < box.minH) {
    h = box.minH;
    w = h * ratio;
  } else if (h > box.maxH) {
    h = box.maxH;
    w = h * ratio;
  }
  if (w < box.minW) {
    w = box.minW;
    h = w / ratio;
  } else if (w > box.maxW) {
    w = box.maxW;
    h = w / ratio;
  }
  // 浮点取整后再夹一次：极窄/极高比例下不得出现 0 或 NaN。
  w = Math.min(box.maxW, Math.max(box.minW, roundSize(w)));
  h = Math.min(box.maxH, Math.max(box.minH, roundSize(h)));
  return { w, h };
}

/**
 * 把一次尺寸变化约束到图片比例上（供 BoardCanvas 的拖动 / 键盘 / 工具尺寸 / 恢复接线）。
 *
 * * driver = "width"：以 next.w 为准算 h；
 * * driver = "height"：以 next.h 为准算 w；
 * * driver = "auto"：看哪一维相对 current 变化更大，以变化大的那维为准（拖动更跟手）。
 *
 * 返回的 w/h 一定在限制内且为 ≥1 的整数；调用方仍应把结果交给既有尺寸提交路径。
 */
export function constrainCardSizeToRatio(
  current: { w: number; h: number } | null | undefined,
  next: { w: number; h: number } | null | undefined,
  ratio: number | null | undefined,
  options: { driver?: "width" | "height" | "auto"; limits?: Partial<ImageDisplayLimits> } = {},
): { w: number; h: number } {
  const box = clampLimits(options.limits);
  const safeCurrent = {
    w: isFiniteNumber(current?.w) && current!.w > 0 ? current!.w : box.minW,
    h: isFiniteNumber(current?.h) && current!.h > 0 ? current!.h : box.minH,
  };
  const rawW = isFiniteNumber(next?.w) && next!.w > 0 ? next!.w : safeCurrent.w;
  const rawH = isFiniteNumber(next?.h) && next!.h > 0 ? next!.h : safeCurrent.h;
  if (!isFiniteNumber(ratio) || ratio <= 0) {
    return {
      w: Math.min(box.maxW, Math.max(box.minW, roundSize(rawW))),
      h: Math.min(box.maxH, Math.max(box.minH, roundSize(rawH))),
    };
  }
  let driver = options.driver ?? "auto";
  if (driver === "auto") {
    const dw = Math.abs(rawW - safeCurrent.w) / Math.max(1, safeCurrent.w);
    const dh = Math.abs(rawH - safeCurrent.h) / Math.max(1, safeCurrent.h);
    driver = dh > dw ? "height" : "width";
  }
  let w = driver === "height" ? rawH * ratio : rawW;
  let h = driver === "height" ? rawH : rawW / ratio;
  if (w < box.minW) {
    w = box.minW;
    h = w / ratio;
  } else if (w > box.maxW) {
    w = box.maxW;
    h = w / ratio;
  }
  if (h < box.minH) {
    h = box.minH;
    w = h * ratio;
  } else if (h > box.maxH) {
    h = box.maxH;
    w = h * ratio;
  }
  w = Math.min(box.maxW, Math.max(box.minW, roundSize(w)));
  h = Math.min(box.maxH, Math.max(box.minH, roundSize(h)));
  return { w, h };
}

/**
 * 这个比例能否在卡片口径内被严格满足（宽 ∈ [minW, maxW] 且 高 ∈ [minH, maxH]）。
 *
 * 极端比例（例如 30:1 的长图）在「最小高度 96 / 最大宽度 1600」下无解：
 * 这时卡片的尺寸只能落在口径边界，比例由**详情态**负责完整呈现 ——
 * 界面据此显示可展开的详情入口，而不是假装卡片已经按比例。
 */
export function isRatioFeasible(
  ratio: number | null | undefined,
  limits?: Partial<ImageDisplayLimits>,
): boolean {
  if (!isFiniteNumber(ratio) || ratio <= 0) return false;
  const box = clampLimits(limits);
  const minRatio = box.minW / box.maxH;
  const maxRatio = box.maxW / box.minH;
  return ratio >= minRatio && ratio <= maxRatio;
}

/**
 * 供接线使用的一行入口：卡片 meta → 当前图片比例（没有真实比例时为 null）。
 *
 * BoardCanvas 的**所有**尺寸路径（拖动 / 键盘 / 组缩放 / 工具尺寸 / 恢复）
 * 只要先取这个比例，再把候选尺寸交给 constrainCardSizeForCard，就都遵守同一口径。
 */
export function imageRatioForCard(
  card: { meta?: Record<string, unknown> | null } | null | undefined,
): number | null {
  return readImageDisplay(card?.meta ?? null)?.aspectRatio ?? null;
}

/** 同上，直接约束一次候选尺寸（接线用；driver 语义与 constrainCardSizeToRatio 一致）。 */
export function constrainCardSizeForCard(
  card: { meta?: Record<string, unknown> | null } | null | undefined,
  current: { w: number; h: number } | null | undefined,
  next: { w: number; h: number } | null | undefined,
  options: { driver?: "width" | "height" | "auto"; limits?: Partial<ImageDisplayLimits> } = {},
): { w: number; h: number } {
  return constrainCardSizeToRatio(current, next, imageRatioForCard(card), options);
}

/** 详情态图片在卡片里的定位方式：等比完整显示，绝不 cover 裁主体、也不拉伸。 */
export const IMAGE_DETAIL_OBJECT_FIT = "contain" as const;
