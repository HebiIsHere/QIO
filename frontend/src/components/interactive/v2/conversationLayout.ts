/**
 * 底部互动对话区的布局 / 状态 / 绑定纯函数（Phase 3-B）。
 *
 * 契约：docs/interactive-page-v2-contract.md §6.4。
 * 边界：只做算术与字符串推导；不读 DOM、不读 store、不发请求、不改任何会话状态。
 * 这样「展开高度、收起状态文案、可信绑定投影」都能离线单测，组件只负责接线。
 */

/** 展开面板的最小 / 默认 / 最大高度（屏幕像素）。 */
export const CONVERSATION_MIN_HEIGHT = 220;
export const CONVERSATION_DEFAULT_HEIGHT = 420;
export const CONVERSATION_MAX_HEIGHT = 720;
/** 展开时面板最多占视口高度的比例（矮窗口里不把板面挤没）。 */
export const CONVERSATION_VIEWPORT_RATIO = 0.8;
/** 收起时输入区上方的那一条（只放状态与展开入口，不占阅读空间）。 */
export const CONVERSATION_COLLAPSED_BAR_HEIGHT = 40;
/** 键盘调整高度的步长（与指针拖动共用同一套夹取口径）。 */
export const CONVERSATION_KEYBOARD_STEP = 24;

/** 拖动/键盘调整后的高度口径：先夹进 [min, min(max, 视口 × 比例)]，非法值退回默认值。 */
export function clampConversationHeight(
  value: number,
  options: { min?: number; max?: number; viewportHeight?: number } = {},
): number {
  const min = positive(options.min, CONVERSATION_MIN_HEIGHT);
  const maxProp = positive(options.max, CONVERSATION_MAX_HEIGHT);
  const viewport = positive(options.viewportHeight, 0);
  const viewportLimit = viewport > 0 ? Math.max(min, Math.floor(viewport * CONVERSATION_VIEWPORT_RATIO)) : maxProp;
  const max = Math.max(min, Math.min(maxProp, viewportLimit));
  const raw = Number(value);
  if (!Number.isFinite(raw)) return Math.min(max, Math.max(min, CONVERSATION_DEFAULT_HEIGHT));
  return Math.round(Math.min(max, Math.max(min, raw)));
}

function positive(value: unknown, fallback: number): number {
  const num = typeof value === "number" ? value : Number(value);
  return Number.isFinite(num) && num > 0 ? num : fallback;
}

/**
 * 收起态的状态文案（只用真实会话状态；没有内容时也不编造「有新消息」）。
 * - 回复进行中：说明回复没有被收起打断；
 * - 有排队：给出真实条数；
 * - 有草稿：草稿仍保留（收起不等于清空）；
 * - 都没有：不写任何提示句（展开入口本身只写「展开对话」，不再加「点开继续对话」
 *   和展开方向说明；没有真实状态时保持安静，不编造未读）。
 */
export function conversationStatusText(input: {
  turnRunning?: boolean;
  queuedCount?: number;
  hasDraft?: boolean;
}): string {
  const queued = Math.max(0, Math.trunc(Number(input.queuedCount) || 0));
  if (input.turnRunning) return queued > 0 ? `正在回复 · 已排队 ${queued} 条` : "正在回复…";
  if (queued > 0) return `已排队 ${queued} 条`;
  if (input.hasDraft) return "草稿已保留";
  return "";
}

/** 发送用的互动绑定（可信 props 投影）：只保留会话层认识的字段，绝不拼进用户原文。 */
export interface ConversationBinding {
  mode?: "interactive" | "normal";
  boardId?: string;
  submissionId?: string;
  /** 意图身份（供未来会话层扩展；当前 session.send 只认 mode/boardId/submissionId）。 */
  intentId?: string;
}

export interface SendBinding {
  mode: "interactive" | "normal";
  boardId?: string;
  submissionId?: string;
}

/**
 * 把外部传入的绑定投影成 session.send 的 interactive 选项：
 * - 没有绑定 → undefined（普通聊天，请求体不带板面身份，契约 §6.4 / §8.3）；
 * - 有绑定 → mode 缺省 interactive，board/submission 原样透传（空串/空白视为没有）。
 */
export function conversationBindingForSend(
  binding: ConversationBinding | null | undefined,
): SendBinding | undefined {
  if (!binding) return undefined;
  const boardId = cleanId(binding.boardId);
  const submissionId = cleanId(binding.submissionId);
  return {
    mode: binding.mode === "normal" ? "normal" : "interactive",
    ...(boardId ? { boardId } : {}),
    ...(submissionId ? { submissionId } : {}),
  };
}

function cleanId(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/**
 * 输入区模式入口的布局（#88）：按对话区**实际可用宽度**提前切换，而不是看视口宽度。
 *
 * inline  = 模式入口在输入框左侧的第一列：输入列（≤860）+ 两侧模式入口（约 92px）
 *           + 列间距都放得下时才用；stacked = 模式入口移到输入框上方、与输入框同一条
 *           860 列，圆点永远不会被侧列裁掉。
 * jsdom / 首帧量不到宽度（0）时返回 stacked：先保证入口完整可见，量到真实宽度后重算。
 */
export const CONVERSATION_INLINE_MIN_WIDTH = 1108;

export function conversationComposerLayout(availableWidth: number): "inline" | "stacked" {
  const width = Number(availableWidth);
  if (!Number.isFinite(width) || width <= 0) return "stacked";
  return width >= CONVERSATION_INLINE_MIN_WIDTH ? "inline" : "stacked";
}

/** 读取本机保存的面板高度（损坏 / 越界一律忽略，返回 null 让调用方用默认值）。 */
export function parseStoredConversationHeight(raw: string | null | undefined): number | null {
  if (typeof raw !== "string" || !raw.trim()) return null;
  const num = Number(raw);
  if (!Number.isFinite(num) || num <= 0) return null;
  return Math.round(num);
}
