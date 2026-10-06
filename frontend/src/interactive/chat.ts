/**
 * 聊天与提交状态的纯函数（子智能体 C 负责实现，主智能体先给出契约骨架）。
 *
 * 契约：docs/interactive-mode-contract.md §8.3 / §8.4。
 *
 * 这里只放**可以离线验证**的判断：能不能发送、失败怎么说。
 * 真正的会话能力复用 stores/session.ts，不在这里重建。
 */

/** 空白（含全角空格）不能发送；返回原因由界面原样显示。 */
export function canSend(text: string): { ok: boolean; reason?: string } {
  if (!text || !text.trim()) return { ok: false, reason: "还没有输入内容" };
  return { ok: true };
}

/** 发送失败时的说明：保留输入、给出真实原因，不伪装成功。 */
export function sendFailureText(message: string | null): string {
  const detail = (message ?? "").trim();
  return detail
    ? `发送失败：${detail}（输入已保留，可以重试）`
    : "发送失败：原因未知（输入已保留，可以重试）";
}
