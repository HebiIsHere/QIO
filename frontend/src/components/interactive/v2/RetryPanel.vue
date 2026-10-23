<script setup lang="ts">
/**
 * 重试系列面板（Phase 3-C 契约 §6.3）。
 *
 * 只呈现**服务端事实**：
 * - 次数、上限、状态、last_error、next_action、updated_at 全部来自 retry 系列接口；
 * - 自动重试由服务端调度（这里没有 setTimeout / 本地计时器）；
 * - 「自动重试已用尽」只说明一次，并明确「手动重试一次只算一次尝试，不重新赠送自动次数」；
 * - 手动重试 = 一次点击一次 POST；成功后**重新读取服务端记录**，本地绝不 ++ 次数、绝不重置；
 * - 接口尚未上线（404/405/501）时如实显示「能力未上线」，不显示 0 次、也不给假按钮。
 *
 * 端点与字段归一化集中在 ./retryApi.ts（D 后端改名只需改那一处）。
 */
import { computed, onBeforeUnmount, ref, watch } from "vue";
import {
  fetchRetrySeries,
  requestManualRetry,
  retryAttemptsText,
  retryInFlight,
  retryStatusLabel,
  type RetryPanelApi,
  type RetrySeriesEnvelope,
  type RetrySeriesView,
} from "./retryApi";

const props = withDefaults(
  defineProps<{
    /** 只读这一条意图的重试系列（与 boardId 二选一）。 */
    intentId?: string;
    /** 读整块板面的重试系列（不传 intentId 时使用）。 */
    boardId?: string | null;
    /** 面板被展开才加载（默认加载）。 */
    active?: boolean;
    /** 测试缝：默认就是真实的 retryApi（生产不传）。 */
    api?: RetryPanelApi;
  }>(),
  { intentId: "", boardId: null, active: true, api: undefined },
);

const emit = defineEmits<{
  /** 重试记录被刷新（含手动重试后的重新读取）。 */
  updated: [envelope: RetrySeriesEnvelope];
  /** 手动重试已提交给服务端（ok 原样来自服务端回执）。 */
  retried: [result: { ok: boolean; intentId: string; reason: string }];
}>();

const api = computed<RetryPanelApi>(
  () =>
    props.api ?? {
      fetchSeries: fetchRetrySeries,
      retry: requestManualRetry,
    },
);

const loading = ref(false);
const error = ref("");
const envelope = ref<RetrySeriesEnvelope>({ available: false, reason: "", series: [] });
const notice = ref("");
const busyId = ref<string | null>(null);
let seq = 0;
let controller: AbortController | null = null;

const hasTarget = computed(() => Boolean(props.intentId || props.boardId));
const title = computed(() => (props.intentId ? "这一项的重试" : "重试与收尾"));
const series = computed(() => envelope.value.series);
const unavailableReason = computed(() => (envelope.value.available ? "" : envelope.value.reason));
const emptyText = computed(() => {
  if (!envelope.value.available) return "";
  if (series.value.length) return "";
  return "服务端记录里没有重试系列（首次失败不计入自动重试，也不会凭空显示次数）";
});
const exhaustedSeries = computed(() => series.value.filter((item) => item.status === "exhausted"));

async function load(): Promise<void> {
  if (!hasTarget.value) {
    envelope.value = { available: false, reason: "还没有确定要读哪一项的重试记录", series: [] };
    return;
  }
  const mine = ++seq;
  controller?.abort();
  controller = new AbortController();
  loading.value = true;
  error.value = "";
  try {
    const next = await api.value.fetchSeries(
      { boardId: props.boardId, intentId: props.intentId },
      { signal: controller.signal },
    );
    if (mine !== seq) return;
    envelope.value = next;
    emit("updated", next);
  } catch (err) {
    if (mine !== seq) return;
    error.value = "读取重试记录失败：" + messageOf(err) + "（可以重试）";
  } finally {
    if (mine === seq) loading.value = false;
  }
}

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function retryTitle(item: RetrySeriesView): string {
  const parts = [retryStatusLabel(item.status), retryAttemptsText(item)];
  if (item.nextAction) parts.push("下一步：" + item.nextAction);
  return parts.join(" · ");
}

function canManualRetry(item: RetrySeriesView): boolean {
  if (busyId.value) return false;
  // 服务端说正在重试就不叠加第二次（同一系列同一时刻只允许一个有效执行）
  return !retryInFlight(item.status);
}

async function manualRetry(item: RetrySeriesView): Promise<void> {
  if (!canManualRetry(item)) return;
  busyId.value = item.seriesId;
  notice.value = "";
  error.value = "";
  try {
    const result = await api.value.retry(item.intentId, {
      boardId: props.boardId,
      seriesId: item.seriesId,
      expectedAuthorizationVersion: item.authorizationVersion,
    });
    if (result.ok) {
      notice.value =
        "已提交一次手动重试：" +
        (result.reason || "服务端已受理") +
        "（只算这一次尝试，不会重置自动重试次数；结果以服务端记录为准）";
    } else {
      error.value =
        "这次手动重试没有被服务端接受：" + (result.reason || "服务端未说明原因") + "（记录未改变，可以再试）";
    }
    emit("retried", { ok: result.ok, intentId: item.intentId, reason: result.reason });
    // 无论回执如何都重新读一次服务端事实：界面上的次数永远不是本地算的
    await load();
  } catch (err) {
    error.value = "手动重试没有成功：" + messageOf(err) + "（记录未改变，可以再试）";
  } finally {
    busyId.value = null;
  }
}

function formatMoment(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

watch(
  () => [props.intentId, props.boardId, props.active] as const,
  () => {
    if (props.active) void load();
  },
  { immediate: true },
);

onBeforeUnmount(() => {
  seq += 1;
  controller?.abort();
  controller = null;
});

defineExpose({ load, retry: manualRetry });
</script>

<template>
  <section
    class="retry-panel"
    data-im="p3c-retry-panel"
    :data-state="loading ? 'loading' : error ? 'error' : envelope.available ? 'ready' : 'unavailable'"
    :data-intent-id="props.intentId || undefined"
  >
    <header class="retry-head">
      <h4 class="retry-title">{{ title }}</h4>
      <button
        type="button"
        class="retry-refresh"
        data-im="p3c-retry-refresh"
        :disabled="loading"
        @click="load"
      >{{ loading ? "读取中…" : "刷新" }}</button>
    </header>

    <p v-if="loading" class="retry-note" role="status" data-im="p3c-retry-loading">正在读取服务端的重试记录…</p>
    <p v-else-if="error" class="retry-error" role="alert" data-im="p3c-retry-error">
      {{ error }}
      <button type="button" class="retry-link" @click="load">重试读取</button>
    </p>
    <p
      v-else-if="unavailableReason"
      class="retry-note"
      role="status"
      data-im="p3c-retry-unavailable"
    >{{ unavailableReason }}</p>
    <p v-else-if="emptyText" class="retry-note" role="status" data-im="p3c-retry-empty">{{ emptyText }}</p>

    <ul v-if="!loading && series.length" class="retry-list">
      <li
        v-for="item in series"
        :key="item.seriesId"
        class="retry-item"
        :data-series-id="item.seriesId"
        :data-status="item.status"
        data-im="p3c-retry-item"
      >
        <p class="retry-status" :data-tone="item.status === 'failed' || item.status === 'exhausted' ? 'problem' : 'plain'">
          <span class="retry-label" data-im="p3c-retry-status">{{ retryStatusLabel(item.status) }}</span>
          <span class="retry-attempts mono" data-im="p3c-retry-attempts">{{ retryAttemptsText(item) }}</span>
        </p>
        <p v-if="item.lastError" class="retry-last-error" data-im="p3c-retry-last-error">
          最近一次失败：{{ item.lastError }}
        </p>
        <p v-if="item.nextAction" class="retry-next" data-im="p3c-retry-next-action">
          下一步：{{ item.nextAction }}
        </p>
        <p v-if="item.updatedAt" class="retry-time mono" data-im="p3c-retry-updated">
          服务端更新于 {{ formatMoment(item.updatedAt) }}
        </p>
        <!-- 自动重试耗尽：只说明一次；手动重试一次算一次，不重新赠送自动次数 -->
        <p
          v-if="item.status === 'exhausted'"
          class="retry-exhausted"
          data-im="p3c-retry-exhausted"
          role="status"
        >
          自动重试已经用尽：不会再自动尝试。需要时点「手动重试一次」——它只算一次尝试，不会重新赠送自动重试次数。
        </p>
        <div class="retry-actions">
          <button
            type="button"
            class="retry-btn"
            data-im="p3c-retry-manual"
            :disabled="!canManualRetry(item)"
            :title="retryTitle(item)"
            @click="manualRetry(item)"
          >{{ busyId === item.seriesId ? "提交中…" : "手动重试一次" }}</button>
        </div>
      </li>
    </ul>

    <p v-if="notice" class="retry-notice" role="status" data-im="p3c-retry-notice">{{ notice }}</p>
    <!-- 耗尽提示只出现一次（按系列去重后的第一句），不重复堆叠 -->
    <p
      v-if="exhaustedSeries.length > 1"
      class="retry-note"
      data-im="p3c-retry-exhausted-summary"
    >有 {{ exhaustedSeries.length }} 项自动重试用尽：它们都不会再自动尝试。</p>
  </section>
</template>

<style scoped>
.retry-panel {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-width: 0;
}
.retry-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
}
.retry-title {
  margin: 0;
  flex: 1;
  min-width: 0;
  font-family: var(--serif);
  font-size: var(--fs-sm);
  color: var(--text-strong);
}
.retry-refresh,
.retry-btn,
.retry-link {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 2px var(--sp-2);
  cursor: pointer;
}
.retry-refresh:disabled,
.retry-btn:disabled { color: var(--text-muted); cursor: default; }
.retry-link { border: none; background: none; color: var(--link); padding: 0 2px; }
.retry-note,
.retry-empty { margin: 0; color: var(--text-muted); font-size: var(--fs-xs); line-height: 1.6; overflow-wrap: anywhere; }
.retry-error { margin: 0; color: var(--danger); font-size: var(--fs-xs); line-height: 1.6; overflow-wrap: anywhere; }
.retry-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
.retry-item {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--bg-inset);
  min-width: 0;
}
.retry-status { margin: 0; display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--sp-2); font-size: var(--fs-sm); color: var(--text-primary); }
.retry-status[data-tone="problem"] { color: var(--danger); }
.retry-attempts { color: var(--text-muted); font-size: var(--fs-tech); }
.retry-last-error { margin: 0; color: var(--danger); font-size: var(--fs-xs); line-height: 1.6; overflow-wrap: anywhere; }
.retry-next,
.retry-time { margin: 0; color: var(--text-muted); font-size: var(--fs-xs); line-height: 1.6; overflow-wrap: anywhere; }
.retry-exhausted { margin: 0; color: var(--text-secondary); font-size: var(--fs-xs); line-height: 1.6; overflow-wrap: anywhere; }
.retry-actions { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
.retry-notice { margin: 0; color: var(--text-secondary); font-size: var(--fs-xs); line-height: 1.6; overflow-wrap: anywhere; }
</style>
