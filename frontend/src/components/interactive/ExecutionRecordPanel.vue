<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { fetchIntentResults, fetchLedger } from "../../services/interactive";
import { statusLabel } from "../../interactive/intentWorkbench";
import type {
  IntentResultView,
  IntentView,
  LedgerOperationView,
} from "../../interactive/intentWorkbench";

const props = withDefaults(
  defineProps<{
    intent: IntentView | null;
    boardId: string;
    open?: boolean;
  }>(),
  { open: false },
);
const emit = defineEmits<{ (e: "close"): void }>();

const operations = ref<LedgerOperationView[]>([]);
const results = ref<IntentResultView[]>([]);
const loading = ref(false);
const error = ref("");

const intentId = computed(() => props.intent?.intent_id ?? "");
const scope = computed(() => props.intent?.scope ?? {});
const authorization = computed(() => {
  const raw = props.intent?.authorization;
  return raw && typeof raw === "object" ? Object.entries(raw) : [];
});
const retry = computed(() => props.intent?.retry ?? null);
const retryText = computed(() => {
  if (!retry.value) return "没有重试记录";
  const attempts = retry.value.attempts ?? 0;
  const limit = retry.value.limit ?? 3;
  return `${statusLabel(retry.value.status)} · 已自动重试 ${attempts} / ${limit} 次`;
});

async function load(): Promise<void> {
  if (!props.open || !intentId.value || !props.boardId) return;
  loading.value = true;
  error.value = "";
  try {
    const [ledger, result] = await Promise.all([
      fetchLedger(props.boardId),
      fetchIntentResults(props.boardId),
    ]);
    operations.value = (ledger.operations ?? []).filter(
      (item) => !item.intent_id || item.intent_id === intentId.value,
    );
    results.value = (result.results ?? []).filter(
      (item) => !item.intent_id || item.intent_id === intentId.value,
    );
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    loading.value = false;
  }
}

watch(
  () => [props.open, intentId.value, props.boardId] as const,
  () => void load(),
  { immediate: true },
);

function ids(values: string[] | undefined): string {
  return values?.length ? values.join("、") : "未记录";
}
</script>

<template>
  <section
    v-if="open && intent"
    class="erp"
    data-im="execution-record"
    role="dialog"
    aria-modal="true"
    aria-label="执行记录"
  >
    <header class="erp-head">
      <h2>执行记录</h2>
      <button class="erp-close" type="button" data-im="execution-record-close" aria-label="关闭执行记录" @click="emit('close')">×</button>
    </header>
    <p class="erp-title">{{ intent.title }}</p>
    <p class="erp-status" :data-status="intent.status">状态：{{ statusLabel(intent.status) }}</p>
    <p v-if="intent.goal" class="erp-line"><strong>目标：</strong>{{ intent.goal }}</p>
    <p v-if="intent.expected" class="erp-line"><strong>预期：</strong>{{ intent.expected }}</p>
    <p v-if="intent.summary || intent.outcome" class="erp-line" data-im="execution-record-outcome">
      <strong>真实结果：</strong>{{ intent.summary || intent.outcome }}
    </p>

    <section class="erp-block">
      <h3>确认材料与版本</h3>
      <ul v-if="intent.basis?.length">
        <li v-for="item in intent.basis" :key="item.card_id">
          {{ item.card_id }} · 版本 {{ item.version }}
        </li>
      </ul>
      <p v-else class="erp-muted">没有记录确认材料。</p>
    </section>

    <section class="erp-block">
      <h3>授权范围</h3>
      <p>可读：{{ ids(scope.read_card_ids) }}</p>
      <p>可写：{{ ids(scope.write_card_ids) }}</p>
      <p>组：{{ ids(scope.group_ids) }}</p>
      <p>允许操作：{{ ids(scope.allowed_actions) }}</p>
      <p v-if="authorization.length" class="erp-muted">
        授权快照：{{ authorization.map(([key, value]) => key + "=" + String(value)).join(" · ") }}
      </p>
    </section>

    <section class="erp-block">
      <h3>实际操作</h3>
      <p v-if="loading" class="erp-muted">正在读取操作记录…</p>
      <p v-else-if="error" class="erp-error" role="alert">读取失败：{{ error }}</p>
      <ul v-else-if="operations.length">
        <li v-for="item in operations" :key="item.operation_id || item.operation_key || item.action">
          {{ item.action || "未命名操作" }} · {{ item.status || "状态未记录" }}
          <span v-if="item.operation_key" class="erp-muted">（{{ item.operation_key }}）</span>
        </li>
      </ul>
      <p v-else class="erp-muted">没有已记录的操作。</p>
    </section>

    <section class="erp-block">
      <h3>结果</h3>
      <ul v-if="results.length">
        <li v-for="item in results" :key="item.result_id">
          {{ item.title || item.kind || "结果" }}<span v-if="item.content">：{{ item.content }}</span>
        </li>
      </ul>
      <p v-else class="erp-muted">没有已保存的结果卡。</p>
    </section>

    <section class="erp-block">
      <h3>失败、重试与恢复</h3>
      <p>{{ retryText }}</p>
      <p v-if="retry?.last_error" class="erp-error">最近失败原因：{{ retry.last_error }}</p>
      <p v-if="retry?.next_action" class="erp-muted">下一步：{{ retry.next_action }}</p>
    </section>
  </section>
</template>

<style scoped>
.erp {
  position: absolute;
  inset: 5% 10%;
  z-index: var(--im-z-dialog);
  overflow: auto;
  padding: var(--sp-5);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-3);
  color: var(--text-primary);
  font-family: var(--sans);
}
.erp-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
}
.erp-head h2 { margin: 0; font-family: var(--serif); font-size: var(--fs-lg); }
.erp-close {
  width: 28px;
  height: 28px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  background: var(--bg-base);
  color: var(--text-secondary);
  cursor: pointer;
}
.erp-title { margin: var(--sp-3) 0 var(--sp-1); font-size: var(--fs-base); font-weight: 600; }
.erp-status { margin: 0 0 var(--sp-3); color: var(--text-secondary); font-size: var(--fs-sm); }
.erp-line { margin: var(--sp-1) 0; font-size: var(--fs-sm); line-height: 1.6; }
.erp-block { margin-top: var(--sp-4); padding-top: var(--sp-3); border-top: 1px solid var(--border-subtle); }
.erp-block h3 { margin: 0 0 var(--sp-2); font-size: var(--fs-sm); color: var(--text-strong); }
.erp-block p { margin: var(--sp-1) 0; font-size: var(--fs-sm); line-height: 1.6; overflow-wrap: anywhere; }
.erp-block ul { margin: 0; padding-left: 1.2em; font-size: var(--fs-sm); line-height: 1.7; overflow-wrap: anywhere; }
.erp-muted { color: var(--text-muted); }
.erp-error { color: var(--danger); }
@media (max-width: 720px) {
  .erp { inset: 3% 3%; padding: var(--sp-3); }
}
</style>
