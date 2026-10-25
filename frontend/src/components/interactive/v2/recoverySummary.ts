/**
 * 互动页「恢复 / 收尾」事项的纯计算（Phase 3-C）。
 *
 * 两个用途：
 * 1. 事项行徽标数量：只数**真实需要用户处理**的事项（保存失败、草稿失败、内容冲突、
 *    受理不明、可恢复 / 部分完成 / 失败的意图、自动重试用尽）；
 * 2. 文案：面板标题与空态说明，避免「有事项但没有一句话」。
 *
 * 这里不读全局、不发请求：调用方把**服务端与 store 的真实事实**传进来。
 */

export interface RecoveryStoreFacts {
  /** 板面保存失败（store.saveStatus === "error"） */
  saveFailed: boolean;
  /** 卡片草稿保存失败（store.draftSaveStatus === "error"） */
  draftSaveFailed: boolean;
  /** 提交成功但随后刷新失败（store.submitRefreshState === "error"） */
  submitRefreshFailed: boolean;
  /** 提交结果未知（store.submitResultUnknown） */
  submitUnknown: boolean;
  /** 有正文/删除冲突的卡片 id（真实冲突，不是「可能冲突」） */
  contentConflictCardIds: readonly string[];
  /** 有未决聊天/卡片草稿冲突的键 */
  draftConflictKeys: readonly string[];
}

export interface RecoveryServerFacts {
  /** 服务端意图视图里**可以恢复**的条数（canRecover：paused/failed/partially_completed 且授权有效） */
  recoverableIntents: number;
  /** 服务端重试记录里 status === "exhausted" 的条数（没有接口时传 0，不代表没有重试） */
  exhaustedRetries: number;
}

/** store 一侧的真实事项数（逐类相加，同一事项不重复计）。 */
export function recoveryStoreCount(facts: RecoveryStoreFacts | null | undefined): number {
  if (!facts) return 0;
  let count = 0;
  if (facts.saveFailed) count += 1;
  if (facts.draftSaveFailed) count += 1;
  if (facts.submitRefreshFailed) count += 1;
  if (facts.submitUnknown) count += 1;
  // 冲突按对象计数：每张卡 / 每个草稿键都是一件要用户决定的事
  count += dedupe(facts.contentConflictCardIds).length;
  count += dedupe(facts.draftConflictKeys).length;
  return count;
}

function dedupe(values: readonly string[] | null | undefined): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const value of values ?? []) {
    const key = String(value ?? "");
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(key);
  }
  return out;
}

/** 事项行徽标总数：store 事实 + 服务端事实（服务端还没读到时不加，绝不虚报）。 */
export function recoveryItemCount(
  store: RecoveryStoreFacts | null | undefined,
  server: RecoveryServerFacts | null | undefined,
): number {
  const storeCount = recoveryStoreCount(store);
  const recoverable = Math.max(0, Math.floor(server?.recoverableIntents ?? 0));
  const exhausted = Math.max(0, Math.floor(server?.exhaustedRetries ?? 0));
  return storeCount + recoverable + exhausted;
}

/** 入口是否该出现：有任何真实事项才出现（没有就不占位置）。 */
export function recoveryEntryVisible(count: number): boolean {
  return Number.isFinite(count) && count > 0;
}

/** 入口标题 / 悬停说明（只描述真实覆盖范围，不承诺服务端没给的能力）。 */
export const RECOVERY_ENTRY_TEXT = {
  title: "恢复与收尾",
  hint: "保存失败、草稿冲突、受理不明、需要恢复的意图与自动重试用尽都在这里处理",
} as const;

/** 面板空态：没有事项时明确说「没有」，而不是留一片空白。 */
export function recoveryEmptyText(): string {
  return "现在没有需要恢复或收尾的事项。";
}
/** 恢复事项的种类（身份前缀就是它，保证同一事项在刷新后仍是同一个 id）。 */
export type RecoveryItemKind =
  | "impact-wait"
  | "save-failed"
  | "draft-save-failed"
  | "submit-refresh-failed"
  | "submit-unknown"
  | "content-conflict"
  | "removal-conflict"
  | "draft-conflict"
  | "recoverable"
  | "retry-exhausted";

export interface RecoveryItem {
  /** 稳定身份：`<kind>:<对象 id>`（同一事项刷新 / 重新进入页面后仍是同一个 id） */
  id: string;
  kind: RecoveryItemKind;
  title: string;
  text: string;
  intentId?: string;
  cardId?: string;
  key?: string;
}

export interface RecoveryItemInput {
  /** 正在等用户确认的保存前影响说明（也是「尚未保存」的真实事项） */
  impactPending?: boolean;
  saveFailed: boolean;
  saveError?: string | null;
  draftSaveFailed: boolean;
  draftError?: string | null;
  submitRefreshFailed: boolean;
  submitRefreshError?: string | null;
  submitUnknown: boolean;
  submitNote?: string | null;
  contentConflictCardIds: readonly string[];
  removalConflictIds: readonly string[];
  draftConflictKeys: readonly string[];
  /** 服务端意图视图里可以恢复的意图（id + 标题） */
  recoverableIntents: readonly { intentId: string; title: string }[];
  /** 服务端重试记录里已经耗尽（status === "exhausted"）的意图 */
  exhaustedRetryIntentIds: readonly string[];
}

/**
 * 把真实事实摊成**逐条事项**（F05）：同一身份只出现一次，
 * 数量 = 条目数（处理后立刻减少，绝不用「历史最大数量」）。
 *
 * 这里不判断「要不要显示入口」：读取失败 / 尚未读取由调用方单独表达，
 * 绝不能用空数组冒充「没有事项」。
 */
export function buildRecoveryItems(input: RecoveryItemInput | null | undefined): RecoveryItem[] {
  if (!input) return [];
  const items: RecoveryItem[] = [];
  const seen = new Set<string>();
  const push = (item: RecoveryItem): void => {
    if (!item.id || seen.has(item.id)) return;
    seen.add(item.id);
    items.push(item);
  };

  if (input.impactPending) {
    push({
      id: "impact-wait:board",
      kind: "impact-wait",
      title: "保存前的影响确认还没有决定",
      text: "这次改动会影响正在执行的任务：确认或取消之后才会保存（现在尚未保存）。",
    });
  }
  if (input.saveFailed) {
    push({
      id: "save-failed:board",
      kind: "save-failed",
      title: "板面没有保存",
      text: input.saveError || "保存失败（原因未说明）",
    });
  }
  if (input.draftSaveFailed) {
    push({
      id: "draft-save-failed:board",
      kind: "draft-save-failed",
      title: "卡片草稿没有保存",
      text: input.draftError || "草稿保存失败（原因未说明）",
    });
  }
  if (input.submitRefreshFailed) {
    push({
      id: "submit-refresh-failed:board",
      kind: "submit-refresh-failed",
      title: "提交成功但页面没刷新过来",
      text: input.submitRefreshError || "刷新失败（原因未说明）",
    });
  }
  if (input.submitUnknown) {
    push({
      id: "submit-unknown:board",
      kind: "submit-unknown",
      title: "这次提交的结果还没有确认",
      text: input.submitNote || "没有拿到回执：可以按请求身份查证，不会重复提交。",
    });
  }
  for (const cardId of dedupe(input.contentConflictCardIds)) {
    push({
      id: "content-conflict:" + cardId,
      kind: "content-conflict",
      title: "卡片正文冲突",
      text: "这张卡在本页与服务器上都被改过：保留哪一份由你决定。",
      cardId,
    });
  }
  for (const id of dedupe(input.removalConflictIds)) {
    push({
      id: "removal-conflict:" + id,
      kind: "removal-conflict",
      title: "删除与服务器修改相撞",
      text: "本地删除了这个对象，但服务器上它被改过：请选择保持删除还是恢复服务器版。",
      key: id,
    });
  }
  for (const key of dedupe(input.draftConflictKeys)) {
    push({
      id: "draft-conflict:" + key,
      kind: "draft-conflict",
      title: "草稿冲突",
      text: "同一份草稿在本页与服务器上各有一份：两份都保留，等你明确选择。",
      key,
    });
  }
  for (const intent of input.recoverableIntents ?? []) {
    const intentId = String(intent?.intentId ?? "");
    if (!intentId) continue;
    push({
      id: "recoverable:" + intentId,
      kind: "recoverable",
      title: "可以恢复的意图",
      text: "「" + (String(intent.title ?? "") || intentId) + "」已暂停 / 失败 / 部分完成：可以恢复（不自动执行操作）。",
      intentId,
    });
  }
  for (const intentId of dedupe(input.exhaustedRetryIntentIds)) {
    push({
      id: "retry-exhausted:" + intentId,
      kind: "retry-exhausted",
      title: "自动重试用尽",
      text: "这条意图的自动重试已经用尽，需要你决定下一步（重试记录以服务端为准）。",
      intentId,
    });
  }
  return items;
}
