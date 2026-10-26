<script setup lang="ts">
/**
 * 恢复与收尾面板（Phase 3-C 契约 §3 / §6.3 / §6.4）。
 *
 * 覆盖的真实事项（每一类都来自既有 store 或服务端接口，不新建状态源）：
 * 1. 保存失败（持续可见 + 可重试；被影响确认挡住时如实说明）；
 * 2. 提交成功但刷新失败 / 提交结果未知（只重试读取或按身份查证，绝不重复提交）；
 * 3. 卡片草稿保存失败、正文/删除冲突、未决草稿冲突（由用户明确选择）；
 * 4. 服务端意图恢复：paused / failed / partially_completed 且授权仍有效；
 * 5. 部分完成的真实计数、失败收尾说明（operations_summary / summary，原样展示）；
 * 6. 自动重试耗尽（RetryPanel，次数与状态全部来自服务端记录）。
 *
 * 不自动做任何决定：重试/恢复/冲突选择都必须是用户点一次。
 */
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useInteractiveStore } from "../../../stores/interactive";
import {
  fetchIntentViews,
  fetchRecoveryPreview,
  recoverIntent as recoverIntentApi,
} from "../../../services/interactive";
import {
  canRecover,
  operationsSummaryText,
  recoveryOutcomeText,
  statusLabel,
  statusTone,
  type IntentView,
  type RecoveryPreviewResponse,
} from "../../../interactive/intentWorkbench";
import { saveStatusView } from "./saveStatus";
import RetryPanel from "./RetryPanel.vue";
import type { RetryPanelApi, RetrySeriesEnvelope } from "./retryApi";
import {
  recoveryEmptyText,
  recoveryItemCount,
  recoveryStoreCount,
  type RecoveryServerFacts,
  type RecoveryStoreFacts,
} from "./recoverySummary";

export interface RecoveryPanelDeps {
  fetchIntentViews: typeof fetchIntentViews;
  fetchRecoveryPreview: typeof fetchRecoveryPreview;
  recoverIntent: typeof recoverIntentApi;
}

const props = withDefaults(
  defineProps<{
    /** 板面 id；缺省用 store.boardId（真实事实，绝不猜）。 */
    boardId?: string | null;
    /** 面板展开时才拉服务端数据（默认加载）。 */
    active?: boolean;
    /** 测试缝：默认真实服务（生产不传）。 */
    deps?: RecoveryPanelDeps;
    /** 透传给重试面板的测试缝。 */
    retryApi?: RetryPanelApi;
  }>(),
  { boardId: null, active: true, deps: undefined, retryApi: undefined },
);

const emit = defineEmits<{
  /** 事项数量变化（供输入区事项行徽标使用；只数真实事项）。 */
  updated: [count: number];
  /** 恢复动作已提交（ok 原样来自服务端）。 */
  recovered: [result: { ok: boolean; intentId: string; reason: string }];
}>();

const store = useInteractiveStore();
const deps = computed<RecoveryPanelDeps>(
  () =>
    props.deps ?? {
      fetchIntentViews,
      fetchRecoveryPreview,
      recoverIntent: recoverIntentApi,
    },
);

const resolvedBoardId = computed(() => props.boardId || store.boardId || "");

/* ------------------------------------------------------------------ */
/* 1) store 一侧：保存 / 草稿 / 受理不明 / 冲突                          */
/* ------------------------------------------------------------------ */

const saveView = computed(() =>
  saveStatusView({
    status: store.saveStatus as "idle" | "saving" | "saved" | "error",
    dirty: store.dirty,
    error: store.saveError,
  }),
);

const saveBlockedByImpact = computed(() => Boolean(store.pendingImpact));

const contentConflicts = computed(() => {
  const cards = store.board?.cards ?? [];
  return cards
    .map((card) => ({ cardId: card.id, conflict: store.boardContentConflictFor(card.id) }))
    .filter((row): row is { cardId: string; conflict: { local: string; server: string } } => Boolean(row.conflict));
});

const draftConflicts = computed(() =>
  Object.keys(store.draftConflicts ?? {})
    .map((key) => ({ key, conflict: store.draftConflictFor(key) }))
    .filter((row): row is { key: string; conflict: { local: string; server: string } } => Boolean(row.conflict)),
);

const storeFacts = computed<RecoveryStoreFacts>(() => ({
  saveFailed: store.saveStatus === "error",
  draftSaveFailed: store.draftSaveStatus === "error",
  submitRefreshFailed: store.submitRefreshState === "error",
  submitUnknown: Boolean(store.submitResultUnknown),
  contentConflictCardIds: contentConflicts.value.map((row) => row.cardId),
  draftConflictKeys: draftConflicts.value.map((row) => row.key),
}));

const storeActionNotice = ref("");
const storeActionTone = ref<"ok" | "error">("ok");
const busyAction = ref("");

function say(text: string, tone: "ok" | "error" = "ok"): void {
  storeActionNotice.value = text;
  storeActionTone.value = tone;
}

async function retrySave(): Promise<void> {
  if (busyAction.value) return;
  busyAction.value = "save";
  try {
    await store.saveNow();
    if (store.saveStatus === "error") {
      say("保存仍然失败：" + (store.saveError || "原因未知") + "（可以再试）", "error");
    } else {
      say("已重新保存：" + (store.dirty ? "还有未保存的改动" : "服务端已确认这次保存"));
    }
  } catch (err) {
    say("保存没有完成：" + messageOf(err) + "（可以再试）", "error");
  } finally {
    busyAction.value = "";
  }
}

async function retryDraftSave(): Promise<void> {
  if (busyAction.value) return;
  busyAction.value = "draft";
  try {
    await store.retryDraftSave();
    if (store.draftSaveStatus === "error") {
      say("草稿仍然没有保存：" + (store.draftSaveError || "原因未知") + "（可以再试）", "error");
    } else {
      say("已重试保存草稿（只写本机/服务端草稿，不会发送消息）");
    }
  } finally {
    busyAction.value = "";
  }
}

async function retryRefreshAfterSubmit(): Promise<void> {
  if (busyAction.value) return;
  busyAction.value = "refresh";
  try {
    const result = await store.retrySubmitRefresh();
    if (!result.ok) say("刷新没有完成：" + (result.error || "原因未知") + "（只重试读取，不会重复提交）", "error");
    else say("已重新读取提交后的状态（没有重复提交）");
  } finally {
    busyAction.value = "";
  }
}

async function verifyUnknownSubmit(): Promise<void> {
  if (busyAction.value) return;
  busyAction.value = "lookup";
  try {
    const result = await store.confirmAcceptanceAfterUnclear();
    if (!result) {
      say("查证没有结果：" + (store.acceptanceLookupNote || "仍无法确认这次提交是否受理"), "error");
      return;
    }
    say("查证结果以服务端为准：" + (store.acceptanceLookupNote || "已按请求身份核对"));
  } finally {
    busyAction.value = "";
  }
}

function resolveContentConflict(cardId: string, choice: "local" | "server"): void {
  store.resolveBoardContentConflict(cardId, choice);
  say(choice === "local" ? "已保留本页版本（会随下一次保存提交）" : "已改用服务器版本（本页对应内容被放弃）");
}

function resolveDraftConflict(key: string, choice: "local" | "server"): void {
  store.resolveDraftConflict(key, choice);
  say(choice === "local" ? "已选择本机草稿版本" : "已选择服务器草稿版本");
}

/* ------------------------------------------------------------------ */
/* 2) 服务端一侧：可恢复意图 / 部分完成 / 失败收尾                        */
/* ------------------------------------------------------------------ */

const loading = ref(false);
const loadError = ref("");
const views = ref<IntentView[]>([]);
const previews = ref<Record<string, RecoveryPreviewResponse | null>>({});
const previewError = ref<Record<string, string>>({});
const previewBusy = ref("");
const recoverBusy = ref("");
const recoverNotice = ref("");
let seq = 0;

const recoverableIntents = computed(() => views.value.filter((view) => canRecover(view)));
const exhaustedRetries = ref(0);

const serverFacts = computed<RecoveryServerFacts>(() => ({
  recoverableIntents: recoverableIntents.value.length,
  exhaustedRetries: exhaustedRetries.value,
}));

const totalCount = computed(() =>
  recoveryItemCount(storeFacts.value, serverFacts.value),
);
const storeOnlyCount = computed(() => recoveryStoreCount(storeFacts.value));
const empty = computed(
  () =>
    !loading.value &&
    !loadError.value &&
    totalCount.value === 0 &&
    storeActionNotice.value === "",
);

async function load(): Promise<void> {
  if (!props.active || !resolvedBoardId.value) return;
  const mine = ++seq;
  loading.value = true;
  loadError.value = "";
  try {
    const response = await deps.value.fetchIntentViews(resolvedBoardId.value);
    if (mine !== seq) return;
    if (!response.available) {
      views.value = [];
      const reason = (response as { reason?: string }).reason ?? "";
      loadError.value =
        "服务端的意图恢复能力当前不可用：" + (reason || "没有给出原因") + "（不代表没有需要恢复的事项）";
      return;
    }
    views.value = Array.isArray(response.intents) ? response.intents : [];
  } catch (err) {
    if (mine !== seq) return;
    loadError.value = "读取需要恢复的事项失败：" + messageOf(err) + "（可以重试）";
  } finally {
    if (mine === seq) loading.value = false;
  }
}

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

async function loadPreview(intentId: string): Promise<void> {
  if (previewBusy.value) return;
  previewBusy.value = intentId;
  previewError.value = { ...previewError.value, [intentId]: "" };
  try {
    const response = await deps.value.fetchRecoveryPreview(intentId);
    previews.value = { ...previews.value, [intentId]: response };
  } catch (err) {
    previewError.value = {
      ...previewError.value,
      [intentId]: "读取恢复预览失败：" + messageOf(err) + "（可以重试）",
    };
  } finally {
    previewBusy.value = "";
  }
}

async function runRecover(view: IntentView): Promise<void> {
  if (recoverBusy.value) return;
  recoverBusy.value = view.intent_id;
  recoverNotice.value = "";
  try {
    const result = await deps.value.recoverIntent(view.intent_id, resolvedBoardId.value, view.version);
    recoverNotice.value = recoveryOutcomeText(result);
    emit("recovered", { ok: result?.ok === true, intentId: view.intent_id, reason: result?.reason ?? "" });
    if (result?.ok) await load();
  } catch (err) {
    recoverNotice.value = "恢复没有完成：" + messageOf(err) + "（记录未改变，可以再试）";
  } finally {
    recoverBusy.value = "";
  }
}

function onRetryUpdated(envelope: RetrySeriesEnvelope): void {
  exhaustedRetries.value = envelope.available
    ? envelope.series.filter((item) => item.status === "exhausted").length
    : 0;
}

watch(
  () => [props.active, resolvedBoardId.value] as const,
  () => {
    if (props.active) void load();
  },
  { immediate: true },
);

watch(
  totalCount,
  (count) => emit("updated", count),
  { immediate: true },
);

onBeforeUnmount(() => {
  seq += 1;
});

defineExpose({ load, count: totalCount, storeCount: storeOnlyCount });
</script>

<template>
  <section class="recovery-panel" data-im="p3c-recovery-panel">
    <!-- 保存 / 提交：持续可见，不自动消失 -->
    <section v-if="storeFacts.saveFailed" class="rp-block" data-im="p3c-recovery-save" role="alert">
      <h4 class="rp-title">板面没有保存</h4>
      <p class="rp-text">{{ saveView.text }}</p>
      <p v-if="saveView.detail" class="rp-detail">{{ saveView.detail }}</p>
      <p v-if="saveBlockedByImpact" class="rp-warn" data-im="p3c-recovery-save-impact">
        保存被影响确认挡住了：先在提交区处理这次影响确认，再重试保存（这里不会替你决定）。
      </p>
      <button type="button" class="rp-btn primary" data-im="p3c-recovery-save-retry" :disabled="Boolean(busyAction)" @click="retrySave">
        {{ busyAction === "save" ? "重试中…" : "重试保存" }}
      </button>
    </section>

    <section v-if="storeFacts.submitRefreshFailed" class="rp-block" data-im="p3c-recovery-submit-refresh">
      <h4 class="rp-title">提交成功，但页面没刷新过来</h4>
      <p class="rp-text">{{ store.submitRefreshError || "刷新失败（原因未说明）" }}</p>
      <button type="button" class="rp-btn" data-im="p3c-recovery-submit-refresh-retry" :disabled="Boolean(busyAction)" @click="retryRefreshAfterSubmit">
        {{ busyAction === "refresh" ? "读取中…" : "重试读取（不会重复提交）" }}
      </button>
    </section>

    <section v-if="storeFacts.submitUnknown" class="rp-block" data-im="p3c-recovery-submit-unknown">
      <h4 class="rp-title">这次提交的结果还没有确认</h4>
      <p class="rp-text">
        没有拿到服务端回执：可能已经受理，也可能没有。按请求身份查证一次，不会重复提交。
        <template v-if="store.acceptanceLookupNote">{{ store.acceptanceLookupNote }}</template>
      </p>
      <button type="button" class="rp-btn" data-im="p3c-recovery-submit-verify" :disabled="Boolean(busyAction)" @click="verifyUnknownSubmit">
        {{ busyAction === "lookup" ? "查证中…" : "按身份查证" }}
      </button>
    </section>

    <section v-if="storeFacts.draftSaveFailed" class="rp-block" data-im="p3c-recovery-draft-save">
      <h4 class="rp-title">草稿没有保存</h4>
      <p class="rp-text">{{ store.draftSaveError || "原因未知" }}（文字还在编辑器里）</p>
      <button type="button" class="rp-btn" data-im="p3c-recovery-draft-retry" :disabled="Boolean(busyAction)" @click="retryDraftSave">
        {{ busyAction === "draft" ? "重试中…" : "重试保存草稿" }}
      </button>
    </section>

    <!-- 内容冲突：必须由用户明确选择，不静默覆盖 -->
    <section v-if="contentConflicts.length" class="rp-block" data-im="p3c-recovery-content-conflict">
      <h4 class="rp-title">内容冲突（{{ contentConflicts.length }}）</h4>
      <p class="rp-text">这些卡片在本页和服务器上都被改过：保留哪一份由你决定，不会自动覆盖。</p>
      <ul class="rp-list">
        <li v-for="row in contentConflicts" :key="row.cardId" class="rp-item" :data-card-id="row.cardId">
          <p class="rp-line mono">{{ row.cardId }}</p>
          <p class="rp-preview">本页：{{ row.conflict.local || "（空）" }}</p>
          <p class="rp-preview">服务器：{{ row.conflict.server || "（空）" }}</p>
          <div class="rp-actions">
            <button type="button" class="rp-btn" data-im="p3c-recovery-conflict-local" @click="resolveContentConflict(row.cardId, 'local')">保留本页版本</button>
            <button type="button" class="rp-btn" data-im="p3c-recovery-conflict-server" @click="resolveContentConflict(row.cardId, 'server')">改用服务器版本</button>
          </div>
        </li>
      </ul>
    </section>

    <section v-if="draftConflicts.length" class="rp-block" data-im="p3c-recovery-draft-conflict">
      <h4 class="rp-title">草稿冲突（{{ draftConflicts.length }}）</h4>
      <p class="rp-text">同一份草稿在本页与服务器上各有一份：两份都保留，直到你明确选择。</p>
      <ul class="rp-list">
        <li v-for="row in draftConflicts" :key="row.key" class="rp-item" :data-draft-key="row.key">
          <p class="rp-line mono">{{ row.key }}</p>
          <p class="rp-preview">本页：{{ row.conflict.local || "（空）" }}</p>
          <p class="rp-preview">服务器：{{ row.conflict.server || "（空）" }}</p>
          <div class="rp-actions">
            <button type="button" class="rp-btn" data-im="p3c-recovery-draft-conflict-local" @click="resolveDraftConflict(row.key, 'local')">保留本页草稿</button>
            <button type="button" class="rp-btn" data-im="p3c-recovery-draft-conflict-server" @click="resolveDraftConflict(row.key, 'server')">改用服务器草稿</button>
          </div>
        </li>
      </ul>
    </section>

    <p v-if="storeActionNotice" class="rp-notice" :class="{ err: storeActionTone === 'error' }" role="status" data-im="p3c-recovery-store-notice">
      {{ storeActionNotice }}
    </p>

    <!-- 服务端：可恢复意图 / 部分完成 / 失败收尾 -->
    <section class="rp-block" data-im="p3c-recovery-intents">
      <div class="rp-head">
        <h4 class="rp-title">需要恢复的意图</h4>
        <button type="button" class="rp-btn" data-im="p3c-recovery-refresh" :disabled="loading" @click="load">
          {{ loading ? "读取中…" : "刷新" }}
        </button>
      </div>
      <p v-if="loading" class="rp-text" role="status" data-im="p3c-recovery-loading">正在读取服务端事项…</p>
      <p v-else-if="loadError" class="rp-error" role="alert" data-im="p3c-recovery-load-error">
        {{ loadError }}
        <button type="button" class="rp-link" @click="load">重试读取</button>
      </p>
      <p v-else-if="!recoverableIntents.length" class="rp-text" data-im="p3c-recovery-intents-empty">
        服务端没有可恢复的意图（暂停 / 失败 / 部分完成且授权仍有效的才会出现在这里）。
      </p>
      <ul v-else class="rp-list">
        <li
          v-for="view in recoverableIntents"
          :key="view.intent_id"
          class="rp-item"
          :data-intent-id="view.intent_id"
          :data-status="view.status"
          data-im="p3c-recovery-intent"
        >
          <p class="rp-line">
            <span class="rp-name">{{ view.title || view.intent_id }}</span>
            <span class="rp-tag mono" :data-tone="statusTone(view.status)">{{ statusLabel(view.status) }}</span>
          </p>
          <p v-if="operationsSummaryText(view)" class="rp-text" data-im="p3c-recovery-intent-ops">
            服务端操作：{{ operationsSummaryText(view) }}
          </p>
          <p v-if="view.status === 'partially_completed'" class="rp-warn" data-im="p3c-recovery-partial">
            部分完成：已应用的操作与失败项以服务端账本为准（不会把部分完成说成完成）。
          </p>
          <p v-if="view.summary" class="rp-text" data-im="p3c-recovery-intent-summary">{{ view.summary }}</p>
          <p v-if="view.authorization_valid === false" class="rp-warn" data-im="p3c-recovery-intent-unauthorized">
            授权已失效：这次不能恢复，需要重新确认权限。
          </p>
          <p v-if="previews[view.intent_id]" class="rp-preview" data-im="p3c-recovery-preview">
            <template v-if="previews[view.intent_id]?.available">
              可恢复：{{ previews[view.intent_id]?.preview?.can_resume ? "是" : "否" }}
              <template v-if="previews[view.intent_id]?.preview?.reasons?.length">
                · 原因：{{ previews[view.intent_id]?.preview?.reasons?.join("；") }}
              </template>
              <template v-if="previews[view.intent_id]?.preview?.unfinished_operations?.length">
                · 未完成操作 {{ previews[view.intent_id]?.preview?.unfinished_operations?.length }} 项
              </template>
            </template>
            <template v-else>恢复预览不可用：{{ previews[view.intent_id]?.preview ? "服务端未给出原因" : "没有返回预览" }}</template>
          </p>
          <p v-if="previewError[view.intent_id]" class="rp-error" role="alert">{{ previewError[view.intent_id] }}</p>
          <div class="rp-actions">
            <button type="button" class="rp-btn" data-im="p3c-recovery-preview-btn" :disabled="previewBusy === view.intent_id" @click="loadPreview(view.intent_id)">
              {{ previewBusy === view.intent_id ? "读取中…" : "恢复预览" }}
            </button>
            <button
              type="button"
              class="rp-btn primary"
              data-im="p3c-recovery-recover"
              :disabled="recoverBusy === view.intent_id || view.authorization_valid === false"
              @click="runRecover(view)"
            >{{ recoverBusy === view.intent_id ? "提交中…" : "恢复" }}</button>
          </div>
        </li>
      </ul>
      <p v-if="recoverNotice" class="rp-notice" role="status" data-im="p3c-recovery-recover-notice">{{ recoverNotice }}</p>
    </section>

    <!-- 自动重试：次数 / 上限 / 耗尽全部来自服务端记录 -->
    <section class="rp-block" data-im="p3c-recovery-retries">
      <RetryPanel
        :board-id="resolvedBoardId"
        :active="props.active"
        :api="props.retryApi"
        @updated="onRetryUpdated"
      />
    </section>

    <p v-if="empty" class="rp-text" data-im="p3c-recovery-empty">{{ recoveryEmptyText() }}</p>
  </section>
</template>

<style scoped>
.recovery-panel {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  min-width: 0;
  font-size: var(--fs-sm);
}
.rp-block {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
  padding: var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-inset);
}
.rp-head { display: flex; align-items: baseline; gap: var(--sp-2); }
.rp-title { margin: 0; flex: 1; min-width: 0; font-family: var(--serif); font-size: var(--fs-sm); color: var(--text-strong); }
.rp-text, .rp-detail, .rp-warn, .rp-notice, .rp-error, .rp-preview, .rp-line { margin: 0; font-size: var(--fs-xs); line-height: 1.6; overflow-wrap: anywhere; }
.rp-text { color: var(--text-secondary); }
.rp-detail { color: var(--text-muted); }
.rp-warn { color: var(--warning); }
.rp-error { color: var(--danger); }
.rp-notice { color: var(--text-secondary); }
.rp-notice.err { color: var(--danger); }
.rp-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
.rp-item { display: flex; flex-direction: column; gap: var(--sp-1); min-width: 0; padding-top: var(--sp-1); border-top: 1px solid var(--border-subtle); }
.rp-item:first-child { border-top: 0; padding-top: 0; }
.rp-line { display: flex; align-items: baseline; flex-wrap: wrap; gap: var(--sp-2); color: var(--text-primary); }
.rp-name { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.rp-tag { flex: none; font-size: 10.5px; color: var(--text-muted); border: 1px solid var(--border-subtle); border-radius: var(--qio-ui-radius); padding: 0 var(--sp-1); }
.rp-tag[data-tone="problem"] { color: var(--danger); border-color: var(--danger); }
.rp-tag[data-tone="waiting"] { color: var(--warning); border-color: var(--warning); }
.rp-tag[data-tone="active"] { color: var(--accent); border-color: var(--accent); }
.rp-preview { color: var(--text-muted); white-space: pre-wrap; }
.rp-actions { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
.rp-btn, .rp-link {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  padding: 2px var(--sp-2);
  cursor: pointer;
}
.rp-btn.primary { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }
.rp-btn:disabled { color: var(--text-muted); cursor: default; opacity: 0.7; }
.rp-link { border: none; background: none; color: var(--link); padding: 0 2px; }
</style>
