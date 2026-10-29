/**
 * 聊天输入的空白 / 规范化纯函数（仍被生产代码使用）。
 *
 * 契约：docs/interactive-mode-contract.md §8.3。
 *
 * 2026-10-26（合并后清理 · C2 收尾）：旧 ChatDock 删除后，只服务它的面板文案与按键判定
 * （chatScopeText / chatStatusText / olderMessagesText / chatDraftHintText / canSend /
 * sendFailureText / pendingSendText / shouldSendOnKeydown / SendKeyLike）已无消费者，一并删除；
 * 对应能力由 v2/InteractiveConversation 与 components/Composer.vue 承担（Enter / Shift+Enter /
 * 输入法选字语义在 Composer 内实现并有组件级用例）。这里只保留 session.ts 仍在用的两个判定。
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

/**
 * 真正发出去的文字：只去掉首尾空白，正文一个字都不改（不替用户改写、不追加任何板面信息）。
 * 中间的空行与缩进原样保留。
 */
export function normalizeSendText(text: string): string {
  return (text ?? "").trim();
}
