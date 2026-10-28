/**
 * 「当前生效参数」的事实来源（只读展示用）。
 *
 * 为什么需要一个跨分区的集中事实表：
 * - 「当前生效参数」在「工具与权限」，但它要展示记忆分段、对话深度、搜索参数与后台整理；
 * - 这些值由各自分区的保存流程写入，草稿**不能**进这里 ——
 *   只读区只允许展示「已保存且真实生效」的值，否则用户会拿草稿去排查线上行为。
 *
 * 因此每个分区在「加载完成」与「保存成功」两个时点调用 setter 写入真值；
 * 草稿变化不写。值为 null 表示尚未读取到，界面上如实显示「读取中…」。
 */
import { reactive } from "vue";

export interface EffectiveFacts {
  memoryTurns: number | null;
  memoryTokens: number | null;
  maxIterations: number | null;
  outputTokenBudget: number | null;
  searchTopK: number | null;
  searchMaxFetchChars: number | null;
  searchKeyless: boolean | null;
  searxngUrl: string | null;
  maintenanceEnabled: boolean | null;
  maintenanceInterval: number | null;
}

export const effectiveFacts = reactive<EffectiveFacts>({
  memoryTurns: null,
  memoryTokens: null,
  maxIterations: null,
  outputTokenBudget: null,
  searchTopK: null,
  searchMaxFetchChars: null,
  searchKeyless: null,
  searxngUrl: null,
  maintenanceEnabled: null,
  maintenanceInterval: null,
});

export function patchEffectiveFacts(patch: Partial<EffectiveFacts>): void {
  Object.assign(effectiveFacts, patch);
}

/** 测试与重新进入设置页时清空：避免把上一次会话的真值当成当前值。 */
export function resetEffectiveFacts(): void {
  for (const key of Object.keys(effectiveFacts) as (keyof EffectiveFacts)[]) {
    (effectiveFacts as Record<string, unknown>)[key] = null;
  }
}
