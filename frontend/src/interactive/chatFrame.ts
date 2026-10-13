/**
 * chatFrame —— 互动对话窗口的位置 / 尺寸查看偏好（互动模式契约 2026-10-13 · IRL §2）
 *
 * 为什么单独落一份本机偏好，而不进板面状态：
 * - 对话窗口不属于板面材料，进板面状态会改板面版本、进提交载荷（任务书 §3.9 / §4.1）；
 * - 但它仍然要「收起再打开、刷新、重启后恢复」，所以按**板面身份**绑定一条本机记录。
 *
 * 持久化范式与既有 `floatingState.ts` 一致：localStorage + 防御式读写 +
 * 存储不可用（隐私模式 / 受限 WebView）时静默降级为「仅本次会话生效」，绝不抛错。
 */
import { clampBox, convergeBox, limitsForArea, type SizeLimits } from "./resize";

export interface ChatFrame {
  x: number;
  y: number;
  w: number;
  h: number;
}

/**
 * 对话窗口最小尺寸：保证**消息阅读区**不被附件条 / 帮助 / 错误 / 审批压成一行。
 *
 * 拆解（设计令牌下的近似值，只用于定最小值，不做像素级还原）：
 * - 标题栏 ≈ 44px
 * - 底部输入区（附件条 + 输入 + 发送）≈ 92px
 * - 消息阅读区至少 3 行正文 ≈ 72px
 * 合计 ≈ 208 → 取 220 留一点余量。宽度 300 以下正文换行会碎成单字。
 */
export const CHAT_FRAME_MIN: { w: number; h: number } = { w: 300, h: 220 };

/** 首次打开时的默认框（未保存过偏好时使用）。 */
export const CHAT_FRAME_DEFAULT: ChatFrame = { x: 0, y: 0, w: 420, h: 520 };

/** 一条本机记录一个键；板面身份写在键里，换板面互不覆盖。 */
export const CHAT_FRAME_KEY_PREFIX = "qio-chat-frame";

const CHAT_FRAME_VERSION = 1;

interface StoredFrame extends ChatFrame {
  version: number;
}

export function chatFrameKey(boardId: string | null | undefined): string {
  const id = String(boardId ?? "").trim();
  return id ? CHAT_FRAME_KEY_PREFIX + ":" + id : CHAT_FRAME_KEY_PREFIX + ":default";
}

function backend(): Storage | null {
  try {
    return (globalThis as { localStorage?: Storage | null }).localStorage ?? null;
  } catch {
    // 隐私模式 / 企业策略下连访问属性都会抛。
    return null;
  }
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function normalize(raw: unknown): ChatFrame | null {
  if (!raw || typeof raw !== "object") return null;
  const item = raw as Partial<StoredFrame>;
  if (!isFiniteNumber(item.x) || !isFiniteNumber(item.y) || !isFiniteNumber(item.w) || !isFiniteNumber(item.h)) {
    return null;
  }
  if (item.w <= 0 || item.h <= 0) return null;
  return { x: item.x, y: item.y, w: item.w, h: item.h };
}

export function loadChatFrame(boardId: string | null | undefined): ChatFrame | null {
  const store = backend();
  if (!store) return null;
  try {
    const raw = store.getItem(chatFrameKey(boardId));
    if (!raw) return null;
    return normalize(JSON.parse(raw));
  } catch {
    return null;
  }
}

/** 写失败只是「本次会话生效」：不抛错、不拦用户操作。 */
export function saveChatFrame(boardId: string | null | undefined, frame: ChatFrame): boolean {
  const store = backend();
  if (!store) return false;
  const next = normalize(frame);
  if (!next) return false;
  try {
    store.setItem(chatFrameKey(boardId), JSON.stringify({ ...next, version: CHAT_FRAME_VERSION }));
    return true;
  } catch {
    return false;
  }
}

export function clearChatFrame(boardId: string | null | undefined): void {
  const store = backend();
  if (!store) return;
  try {
    store.removeItem(chatFrameKey(boardId));
  } catch {
    /* 存储不可用：没有可清的东西 */
  }
}

/**
 * 可用区域 → 对话窗口的尺寸限制。
 * 窗口比最小值还小时最小值向内收敛（不强行套用超过屏幕宽度的最小值），
 * 这一条由 `limitsForArea` 统一保证。
 */
export function chatFrameLimits(area: { w: number; h: number }): SizeLimits {
  return limitsForArea(area, CHAT_FRAME_MIN);
}

/**
 * 恢复已保存的框：收敛进当前可用区域。
 * `area` 用视口（或可视工作区）尺寸，`origin` 是它的左上角 —— 标题栏避让由调用方
 * 把 origin.y 抬到标题栏之下（0.2.3 自绘标题条）。
 */
export function convergeChatFrame(
  frame: ChatFrame,
  area: { w: number; h: number },
  origin: { x: number; y: number } = { x: 0, y: 0 },
): ChatFrame {
  const limits = chatFrameLimits({ w: area.w, h: area.h });
  // 顺序很重要：**先把宽高抬进 [min, max]，再收敛位置**。
  // 反过来的话，一个存档里 10px 宽的窗口会先被定位在右边缘、再被抬到最小值，
  // 结果右边界直接越过可见区域（E 复核 F1 的最小复现）。
  return convergeBox(clampBox(frame, limits), area, origin);
}

/** 首次打开：贴右下角，并留出 origin（标题栏 / 工具栏）的空间。 */
export function defaultChatFrame(
  area: { w: number; h: number },
  origin: { x: number; y: number } = { x: 0, y: 0 },
  margin = 16,
): ChatFrame {
  const limits = chatFrameLimits({ w: area.w, h: area.h });
  const w = Math.min(CHAT_FRAME_DEFAULT.w, limits.maxW);
  const h = Math.min(CHAT_FRAME_DEFAULT.h, limits.maxH);
  return convergeChatFrame(
    {
      x: origin.x + Math.max(0, area.w - w - margin),
      y: origin.y + Math.max(0, area.h - h - margin),
      w,
      h,
    },
    area,
    origin,
  );
}
