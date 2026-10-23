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
