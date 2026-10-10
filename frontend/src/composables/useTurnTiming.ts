/**
 * 一轮耗时的加载状态：只在用户真的要看的时候拉一次，并在会话内缓存。
 *
 * 为什么懒加载 + 缓存：
 * - 一页历史可能有几十条助手消息，每条都自动拉 trace 就是几十个请求；
 * - 同一轮展开/收起多次不该反复请求；
 * - 失败**不重试成灾**：把 null 也缓存住，用户想重来可以再展开一次（force）。
 *
 * 五种状态（契约 §3），互不混淆：
 * * `idle`    未请求（折叠态显示已知总耗时，不显示「读取中」）；
 * * `loading` 正在请求明细（只有这时才显示「读取中」）；
 * * `ready`   成功：可能有分项（rows），也可能是「成功但无分项」/「旧记录」——
 *             由 `timing.legacy` 与 `timing.rows` 区分；
 * * `missing` 后端没有这条记录（缺失 ≠ 0，也不永久转圈）；
 * * `error`   请求失败（**不抹掉**已知总耗时，也不清掉已经加载过的明细）。
 */

import { ref, type Ref } from "vue";
import { fetchTurnTiming, type TurnTiming } from "../services/trace";

export type TurnTimingState = "idle" | "loading" | "ready" | "missing" | "error";

/** 会话内缓存（成功与「没有账本」都缓存；错误不缓存，允许用户再试） */
const CACHE_LIMIT = 80;
const cache = new Map<string, TurnTiming | null>();

function remember(turnId: string, value: TurnTiming | null): void {
  cache.set(turnId, value);
  while (cache.size > CACHE_LIMIT) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
}

export function clearTurnTimingCache(): void {
  cache.clear();
}

export function useTurnTiming(turnId: () => string | null | undefined): {
  state: Ref<TurnTimingState>;
  timing: Ref<TurnTiming | null>;
  load: (force?: boolean) => Promise<void>;
} {
  const state = ref<TurnTimingState>("idle");
  const timing = ref<TurnTiming | null>(null);

  async function load(force = false): Promise<void> {
    const id = turnId();
    if (!id) {
      state.value = "missing";
      timing.value = null;
      return;
    }
    if (!force && cache.has(id)) {
      timing.value = cache.get(id) ?? null;
      state.value = timing.value ? "ready" : "missing";
      return;
    }
    state.value = "loading";
    try {
      const result = await fetchTurnTiming(id);
      remember(id, result);
      // 请求期间用户可能已经翻到别的消息：只认当前 turn
      if (turnId() !== id) return;
      timing.value = result;
      state.value = result ? "ready" : "missing";
    } catch {
      // 拿不到耗时明细不影响对话，也**不影响已知总耗时**：
      // 只标记失败，绝不把已经加载出来的明细清空（旧实现会把它抹成 null）。
      if (turnId() !== id) return;
      state.value = "error";
    }
  }

  return { state, timing, load };
}
