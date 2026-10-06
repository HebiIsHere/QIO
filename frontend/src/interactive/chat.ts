/**
 * 聊天与提交状态的纯函数（子智能体 C 负责实现）。
 *
 * 契约：docs/interactive-mode-contract.md §8.3 / §8.4。
 *
 * 这里只放**可以离线验证**的判断：能不能发送、按键要不要发送、失败怎么说。
 * 真正的会话能力复用 stores/session.ts，不在这里重建订阅、不重复写消息、不重复建轮次。
 */

/**
 * 判定「空白」时忽略的零宽字符。
 *
 * 它们看不见，但会让一个「看起来是空的」输入框通过 trim 检查：
 * 用户按下 Enter 只会得到一条空消息。它们**只用于空白判定**，
 * 不会从真正发出的文字里删掉（例如零宽连接符 U+200D 是 emoji 组合序列的一部分，
 * 删掉会毁掉内容）。
 */
const INVISIBLE_CHARS = /[\u200B-\u200D\u2060\uFEFF]/g;

/** 是否是空白输入：空串、纯空格、全角空格、换行、制表、零宽字符都算空白。 */
export function isBlankText(text: string | null | undefined): boolean {
  if (text == null) return true;
  return text.replace(INVISIBLE_CHARS, "").trim() === "";
}

/** 空白（含全角空格）不能发送；返回原因由界面原样显示。 */
export function canSend(text: string): { ok: boolean; reason?: string } {
  if (isBlankText(text)) return { ok: false, reason: "还没有输入内容" };
  return { ok: true };
}

/**
 * 真正发出去的文字：只去掉首尾空白，正文一个字都不改（不替用户改写、不追加任何板面信息）。
 * 中间的空行与缩进原样保留。
 */
export function normalizeSendText(text: string): string {
  return (text ?? "").trim();
}

/**
 * 按键事件的最小形状：浏览器 KeyboardEvent 满足它，测试可以离线构造。
 * 这里不 import DOM 类型，保证纯函数在 node 环境也能验证。
 */
export interface SendKeyLike {
  key?: string;
  shiftKey?: boolean;
  /** 输入法正在组字 / 选字（中文选字时为 true） */
  isComposing?: boolean;
  /** 部分输入法只给 229 这个键码；它同样是「正在选字」的信号 */
  keyCode?: number;
}

/**
 * 这个按键要不要发送。
 *
 * 三条规则（契约 §8.3）：
 * 1. Enter 发送、Shift+Enter 换行；
 * 2. 输入法正在选字时不发送（`e.isComposing || keyCode === 229`）—— 选字时的 Enter 属于输入法；
 * 3. 其它按键都不发送，由输入框自己做默认行为。
 */
export function shouldSendOnKeydown(event: SendKeyLike | null | undefined): boolean {
  if (!event) return false;
  // 中文输入法正在选字：Enter 是「确认候选词」，不能当成发送
  if (event.isComposing || event.keyCode === 229) return false;
  // Shift+Enter 换行
  if (event.shiftKey) return false;
  return event.key === "Enter";
}

/** 发送失败时的说明：保留输入、给出真实原因，不伪装成功。 */
export function sendFailureText(message: string | null): string {
  const detail = (message ?? "").trim();
  return detail
    ? `发送失败：${detail}（输入已保留，可以重试）`
    : "发送失败：原因未知（输入已保留，可以重试）";
}

/**
 * 悬浮聊天的能力边界，如实说明：哪些已经接入、哪些还没有。
 *
 * 第一阶段**没有接入** QIO 对板面的理解与工具执行；文字发送只走当前会话，
 * 所以这里不能写成「QIO 已经看到板面」这类不成立的承诺。
 */
export function chatScopeText(): string {
  // 顺序不是随意的：「尚未接入」放最前，窄窗口里被裁掉时也不能裁掉这句
  return [
    "QIO 对板面的理解与工具执行尚未接入",
    "这里和对话页共用同一个会话，只发送输入的文字",
    "不带板面、未提交改动、注释或选择范围，也不会调用板面提交接口",
  ].join("；");
}

/**
 * 面板顶部状态：说清「正在跑」还是「空闲」，不猜测后台在做什么。
 * 排队中的消息单独计数，避免把「已受理」说成「正在回答」。
 */
export function chatStatusText(options: {
  turnRunning: boolean;
  queuedCount?: number;
}): string {
  const queued = options.queuedCount ?? 0;
  if (options.turnRunning) {
    return queued > 0 ? `QIO 正在回答…（另有 ${queued} 条排队中）` : "QIO 正在回答…";
  }
  if (queued > 0) return `有 ${queued} 条消息排队中`;
  return "空闲：可以发送下一条";
}

/** 面板里消息被截断时的说明：更早的内容仍在对话页，不是丢了。 */
export function olderMessagesText(hiddenCount: number): string {
  if (hiddenCount <= 0) return "";
  return `面板只显示最近的消息，更早的 ${hiddenCount} 条在对话页里（收起面板不会丢）。`;
}

/** 草稿说明：收起面板不清草稿，草稿也不等于发送。 */
export function chatDraftHintText(): string {
  return "收起面板不会清掉草稿；草稿只在点「发送」或按 Enter 时才发出去。";
}
