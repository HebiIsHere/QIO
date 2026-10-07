/**
 * 草稿持久化的纯逻辑（子智能体 C 负责实现，主智能体先给出契约骨架）。
 *
 * 契约：docs/interactive-mode-contract.md §9.4 / §9.5。
 *
 * 设计要点（为什么需要这一层）：
 * - 聊天草稿与卡片草稿要**分别持久保存**，并且都要能处理「保存回执迟到」：
 *   旧请求的返回值不许覆盖更新的内容，所以每次写入带一个单调递增的 seq。
 * - 存储不可用（隐私模式、配额满、被禁用、内容损坏）时必须**明确失败**，
 *   由调用方显示原因并允许重试 —— 不许静默吞掉。
 * - 这里只做纯逻辑与存储读写，不碰网络、不碰 Vue、不发消息。
 */

/** 草稿作用域：聊天按会话/话题，卡片按卡片 id */
export type DraftScope = "chat" | "card";

export interface DraftRecord {
  /** 草稿正文（原样保存，包含换行） */
  text: string;
  /** 最后写入时间（毫秒时间戳），用于清理与调试 */
  updatedAt: number;
  /** 单调递增的写入序号：迟到的回执用它判断自己是否已经过期 */
  seq: number;
}

/** 存储键：不同作用域、不同对象互不干扰 */
export function draftStorageKey(scope: DraftScope, id: string): string {
  return "qio.draft." + scope + "." + (id || "default");
}

/** 读一条草稿；不存在、损坏或存储不可用时返回 null（调用方据此走「没有草稿」路径）。 */
export function readDraft(key: string): DraftRecord | null {
  void key;
  return null;
}

/** 写一条草稿；返回真实结果，失败必须带原因（调用方要显示给用户）。 */
export function writeDraft(key: string, text: string, seq: number): { ok: boolean; error?: string } {
  void key;
  void text;
  void seq;
  return { ok: false, error: "尚未实现" };
}

/** 删除一条草稿（例如发送成功后）。 */
export function removeDraft(key: string): void {
  void key;
}

/** 迟到的保存回执是否应当被忽略。 */
export function isStaleReceipt(receiptSeq: number, currentSeq: number): boolean {
  return receiptSeq < currentSeq;
}
