/**
 * 记忆星球「待处理」真实数量（B → A 的唯一契约，见 `_MP-COMMON.md` §4.2）。
 *
 * A 用它渲染「管理」入口角标与待处理入口旁边的数字。三条底线：
 *
 * 1. **数量来自真实接口**：`GET /api/memory/pending` 的 `total`（知识待审批 +
 *    后台维护提问）加上 `GET /api/entities/candidates` 的 `total`（实体更新候选）。
 *    不从消息、知识列表或实体卡内容里推断，也不把「稍后处理」当成已处理。
 * 2. **一个来源读失败，不能把另一个来源的真实数量也清零**：两类各自记录，
 *    合并数量只用**最近一次真实读到**的那份，读失败的一侧保留上一次已知值并标记
 *    `status = "partial" / "error"`，绝不把「不知道」显示成「没有」。
 * 3. `refresh()` 单飞：并发的调用共享同一个请求（A 在角标、B 在处理完事项后都会刷新）。
 *
 * 已知限制（如实记录，不在这里编造能力）：实体卡 `field_meta.pending_relations`
 * （只有名称、目标身份不确定的关系候选）**没有**读写端点，因此不计入数量 ——
 * 计一个用户处理不了的数字等于制造假待办。
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import { api } from "../services/api";
import { listAllCandidates } from "../services/entityCandidatesApi";

/** 一次刷新的结果状态；A 只需要 count 时可以不看它。 */
export type PlanetPendingStatus = "idle" | "loading" | "ready" | "partial" | "error";

/** 后端 `total` 缺失/非法时的保守回退：用真实取回的条数，不夸大。 */
function realNumber(value: unknown, fallback: number): number {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) && n >= 0 ? Math.floor(n) : fallback;
}

function errorText(error: unknown): string {
  const reason = (error as Error | null)?.message;
  return reason && String(reason).trim() ? String(reason) : "未知原因";
}

export const usePlanetPendingStore = defineStore("planetPending", () => {
  /** 真实未处理数 = 记忆待确认 + 实体更新候选（A 直接消费这个 ref）。 */
  const count = ref(0);
  /** 两类来源各自的真实数量（诊断 / 后续细分入口用）。 */
  const memoryCount = ref(0);
  const candidateCount = ref(0);
  const status = ref<PlanetPendingStatus>("idle");
  /** 读取失败 / 部分失败的可读原因（空串 = 没有已知问题）。 */
  const error = ref("");
  /** 最近一次至少有一侧成功读到的时刻（毫秒）；null = 还没读过。 */
  const loadedAt = ref<number | null>(null);

  let inflight: Promise<void> | null = null;

  async function refresh(): Promise<void> {
    if (inflight) return inflight;
    status.value = "loading";
    inflight = (async () => {
      // 两类真实来源并行读：一份失败不影响另一份的事实落地。
      const [memory, candidates] = await Promise.allSettled([
        api.listMemoryPending({ limit: 500 }),
        listAllCandidates(true, 500),
      ]);
      const problems: string[] = [];
      let memoryOk = false;
      let candidatesOk = false;

      if (memory.status === "fulfilled") {
        memoryOk = true;
        const listing = memory.value;
        memoryCount.value = realNumber(listing?.total, Array.isArray(listing?.items) ? listing.items.length : 0);
        if (listing?.maintenance_available === false) {
          // 服务端明确说维护事项存储不可用：知识待审批仍然真实，但总数不完整。
          problems.push(
            (listing.warnings && listing.warnings[0]) ||
              "维护事项存储不可用：后台维护提问读不到，待处理数量可能少算。",
          );
        } else if (listing?.truncated) {
          problems.push("待处理事项超过服务端一次返回的上限，列表只显示一部分。");
        }
      } else {
        problems.push("读取记忆待确认失败：" + errorText(memory.reason));
      }

      if (candidates.status === "fulfilled") {
        candidatesOk = true;
        const listing = candidates.value;
        candidateCount.value = realNumber(
          listing?.total,
          Array.isArray(listing?.candidates) ? listing.candidates.length : 0,
        );
        if (listing?.truncated) {
          problems.push("实体更新候选超过服务端一次返回的上限，列表只显示一部分。");
        }
      } else {
        problems.push("读取实体更新候选失败：" + errorText(candidates.reason));
      }

      // 合并只用最近一次真实读到的数量：失败的一侧保留旧值，不清零。
      count.value = memoryCount.value + candidateCount.value;
      error.value = problems.join(" ");
      if (memoryOk && candidatesOk) status.value = problems.length ? "partial" : "ready";
      else if (memoryOk || candidatesOk) status.value = "partial";
      else status.value = "error";
      if (memoryOk || candidatesOk) loadedAt.value = Date.now();
    })().finally(() => {
      inflight = null;
    });
    return inflight;
  }

  return { count, memoryCount, candidateCount, status, error, loadedAt, refresh };
});
