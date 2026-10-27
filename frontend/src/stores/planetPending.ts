// PLACEHOLDER(mp-a)：真实实现在 B 分支，接口以 _MP-COMMON §4.2 为准；集成时由 Lead 以 B 版本覆盖本文件。
/**
 * 「待处理」真实未处理数量（薄占位）。
 *
 * 契约（§4.2）：B 提供 `usePlanetPendingStore()`，含 `count: Ref<number>` 与 `refresh()`。
 * A 只用它渲染「管理」按钮角标与「待处理」入口的数量 —— 数量必须是真实未处理数，
 * 拿不到就保持上一次的值（不猜、不归零冒充「没有待办」）。
 *
 * 占位实现只读 `api.listMemoryPending().total`；实体更新候选的合并由 B 在真实实现里做。
 */
import { ref, type Ref } from "vue";
import { api } from "../services/api";

const count = ref(0);
let inflight: Promise<void> | null = null;

export interface PlanetPendingStore {
  /** 真实未处理数量（内存待确认 + 实体更新候选的合并结果由 B 负责） */
  count: Ref<number>;
  refresh(): Promise<void>;
}

async function refresh(): Promise<void> {
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      const listing = await api.listMemoryPending();
      const total = typeof listing.total === "number" ? listing.total : listing.items.length;
      count.value = Math.max(0, total);
    } catch {
      // 读取失败保持上一次数量：角标不能凭猜测归零（下一次刷新会再试）
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

export function usePlanetPendingStore(): PlanetPendingStore {
  return { count, refresh };
}
