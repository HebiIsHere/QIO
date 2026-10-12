/**
 * 多媒体附件的**读取事实**（契约 §4.4、§5.1）。
 *
 * 唯一的职责：把后端给出的诚实事实翻成界面能直接显示的人话。**不判断交付结果** ——
 * 有没有交付、为什么没交付都由后端如实给出，界面只负责照原样呈现，绝不美化。
 *
 * 边界（必须守住）：
 *   * 「已交付模型」是**请求侧的事实**，不等于「模型理解正确」；
 *   * 抽样帧必须写明时间点与「不是逐帧」；转写文字只代表实际得到的文字；
 *   * 没有能力 / 没有转换程序时，把后端的具体原因原样带出来（不缩写成「不支持」）。
 */

export type MediaKind = "image" | "audio" | "video";

/** 五态词（契约 §5.1）：界面与文档统一用这五个词。 */
export const MEDIA_STATES = [
  "附件已保存",
  "预览可用",
  "内容已处理",
  "已交付模型",
  "回答已完成",
] as const;

export type MediaState = (typeof MEDIA_STATES)[number];

export const MEDIA_SOURCE_FACTS = {
  states: [...MEDIA_STATES],
} as const;

const AUDIO_EXTS = new Set([
  ".mp3",
  ".wav",
  ".m4a",
  ".aac",
  ".flac",
  ".ogg",
  ".oga",
  ".opus",
  ".wma",
  ".amr",
  ".aiff",
]);

const VIDEO_EXTS = new Set([
  ".mp4",
  ".mov",
  ".m4v",
  ".avi",
  ".mkv",
  ".webm",
  ".wmv",
  ".flv",
  ".mpeg",
  ".mpg",
  ".ts",
  ".3gp",
]);

/** 按扩展名认音视频；不是就返回 null（**不猜**文件类型）。 */
export function mediaKindOfName(name: string | null | undefined): MediaKind | null {
  const value = String(name ?? "").trim().toLowerCase();
  const dot = value.lastIndexOf(".");
  if (dot < 0) return null;
  const ext = value.slice(dot);
  if (AUDIO_EXTS.has(ext)) return "audio";
  if (VIDEO_EXTS.has(ext)) return "video";
  return null;
}

/**
 * 入口区的能力提示：**说真话**。
 *
 * 交付与否由**本次真实使用的链路**决定（适配器能力声明是唯一事实来源），
 * 所以这里只说明「会怎么处理」与「没有能力时会怎样」，绝不承诺一定看得见 / 听得见。
 */
export const MEDIA_CAPABILITY_HINT: Record<MediaKind, string> = {
  image:
    "图片：本次链路有视觉能力时把真实字节交给模型自己看；没有视觉能力就如实标注未交付，不编造画面描述",
  audio:
    "音频：本次链路声明了音频能力就交付字节、声明了转写通道就给出转写文字；两者都没有就如实说未交付（不编造语音内容）",
  video:
    "视频：按时间点抽样帧交付（抽样，不是逐帧读完）；本机没有可用的转换程序（ffmpeg）时如实说不能抽样",
};

export interface MediaFact {
  kind: MediaKind;
  /** 短事实（例如「视频：抽样帧已交付」） */
  label: string;
  /** 是否真的读到了内容（与后端条目 `read` 同一口径） */
  read: boolean;
  /** 具体说明：时间点 / 时段 / 原因，逐条给人看 */
  detail: string;
  /** 转写文本（只有真的拿到才有；没有就是空串） */
  transcript: string;
  /** 派生处理用了谁（如 ffmpeg / 转写提供方）；没有派生就是空串 */
  engine: string;
  /** 这条材料的来源取值（未知时是空串） */
  source: string;
}

const SOURCE_LABELS: Record<string, { kind: MediaKind; label: string; read: boolean }> = {
  image_delivered: { kind: "image", label: "图片：已交付模型", read: true },
  image_unsupported: { kind: "image", label: "图片：未交付", read: false },
  audio_delivered: { kind: "audio", label: "音频：已交付模型", read: true },
  audio_transcribed: { kind: "audio", label: "音频：已转写为文字并交付", read: true },
  audio_unsupported: { kind: "audio", label: "音频：未交付", read: false },
  video_delivered: { kind: "video", label: "视频：已交付模型", read: true },
  video_sampled: { kind: "video", label: "视频：抽样帧已交付", read: true },
  video_unsupported: { kind: "video", label: "视频：未交付", read: false },
};

interface RawFact {
  source?: unknown;
  reason?: unknown;
  transcript?: unknown;
  range?: { start?: unknown; end?: unknown } | null;
  frames?: { at?: unknown }[] | null;
  engine?: { provider?: unknown; model?: unknown } | null;
  bytes?: unknown;
}

function seconds(value: unknown): string | null {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return null;
  return `${Number.isInteger(parsed) ? parsed : Number(parsed.toFixed(2))}s`;
}

function describeRange(range: RawFact["range"]): string {
  if (!range || typeof range !== "object") return "";
  const start = seconds(range.start);
  const end = seconds(range.end);
  if (!start && !end) return "";
  return `时段 ${start ?? "开头"}–${end ?? "结尾"}`;
}

function describeFrames(frames: RawFact["frames"]): string {
  const list = Array.isArray(frames) ? frames : [];
  const times = list
    .map((frame) => seconds(frame?.at))
    .filter((value): value is string => Boolean(value));
  if (!times.length) return "";
  return `抽样时间点：${times.join("、")}`;
}

function describeEngine(engine: RawFact["engine"]): string {
  if (!engine || typeof engine !== "object") return "";
  const provider = String(engine.provider ?? "").trim();
  const model = String(engine.model ?? "").trim();
  if (!provider && !model) return "";
  return model ? `${provider || "派生处理"}/${model}` : provider;
}

function describeBytes(bytes: unknown): string {
  const parsed = Number(bytes);
  if (!Number.isFinite(parsed) || parsed <= 0) return "";
  if (parsed < 1000) return `${Math.round(parsed)} 字节`;
  const units = ["KB", "MB", "GB"];
  let value = parsed / 1000;
  let index = 0;
  while (value >= 1000 && index < units.length - 1) {
    value /= 1000;
    index += 1;
  }
  return `${value.toFixed(1)} ${units[index]}`;
}

/**
 * 把一条材料事实（或附件条目）翻成界面事实。
 *
 * 传 `undefined` / 缺 `source` 时按「本次没有交付事实」如实呈现 —— 不假装交付过。
 */
export function mediaFactsOf(raw: RawFact | undefined | null): MediaFact {
  const data: RawFact = raw && typeof raw === "object" ? raw : {};
  const source = String(data.source ?? "").trim();
  const known = SOURCE_LABELS[source];
  const reason = String(data.reason ?? "").trim();
  const engine = describeEngine(data.engine);
  const transcript = String(data.transcript ?? "");

  if (!known) {
    return {
      kind: "image",
      // 没有交付事实 = 没有交付给模型（未交付），而不是「不支持这种媒体」
      label: "未交付模型：本次没有交付事实",
      read: false,
      detail: source ? `尚未识别的来源取值：${source}` : "这条材料没有来源事实（可能只加载了元数据）",
      transcript: "",
      engine,
      source,
    };
  }

  const parts: string[] = [];
  if (known.read) {
    if (source === "video_sampled") {
      parts.push("按时间点抽样交付（**不是逐帧读完整段视频**）");
      const frames = describeFrames(data.frames);
      if (frames) parts.push(frames);
      const range = describeRange(data.range);
      if (range) parts.push(range);
    } else if (source === "audio_transcribed") {
      // 诚实边界：转写文字**只**说明拿到了哪些字；对声音本身（怎么说、有什么别的声响）
      // 一个字都不许推断。这里刻意不列举「语气 / 音乐」这类词——列举本身就成了暗示。
      parts.push("经派生转写得到文字（只代表实际得到的文字，与声音本身无关）");
      const range = describeRange(data.range);
      if (range) parts.push(range);
    } else {
      parts.push("真实字节已交给模型，由模型自己看 / 听（这是请求侧的事实）");
    }
    const size = describeBytes(data.bytes);
    if (size) parts.push(size);
    if (reason) parts.push(reason);
  } else {
    parts.push(reason || "本次没有读到内容，也没有交付给模型");
  }
  if (engine) parts.push(`派生处理：${engine}`);

  return {
    kind: known.kind,
    label: known.label,
    read: known.read,
    detail: parts.join("；"),
    transcript,
    engine,
    source,
  };
}
