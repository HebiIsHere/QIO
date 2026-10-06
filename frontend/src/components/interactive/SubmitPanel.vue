<!--
  提交区域（子智能体 B）。

  契约 §4.4：提交按钮始终容易找到；状态同时用文字说明。
  这里说清四件事：
  1. 现在是「未提交 / 已提交」；保存与提交是两件事（保存不调用 QIO）；
  2. 本次有效改动（服务端在每次保存后重算，见 board_pending；不是操作流水）；
  3. 本次允许查看的范围，并明确写出未勾选的不在其中；
  4. 提交失败保留了什么、草稿与自动保存的真实时机（不承诺未保存的输入能恢复）。
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { fetchBoardState } from "../../services/interactive";
import type { BoardStateResponse, Expression } from "../../interactive/types";
import {
  draftHintText,
  localVisibleRange,
  saveStateText,
  submitFailureText,
  submitStateLabel,
  submitStatusText,
  summarizeChanges,
  visibleRangeText,
} from "../../interactive/submission";
import BoardChangeList from "./BoardChangeList.vue";

const store = useInteractiveStore();
const busy = computed(() => store.submitStatus === "submitting");

/**
 * 未提交的有效改动由**服务端**在每次保存后算好（board_pending），这里只读取展示；
 * 服务端仍是唯一权威（本地不做第二套求差逻辑，免得两边语义漂移）。
 */
const pendingExpressions = ref<Expression[]>([]);
const pendingError = ref<string | null>(null);
let fetchSeq = 0;

async function refreshPending() {
  if (!store.board) {
    pendingExpressions.value = [];
    return;
  }
  const seq = ++fetchSeq;
  try {
    const payload = (await fetchBoardState(store.boardId)) as BoardStateResponse & {
      pending?: { expressions?: Expression[]; error?: string };
    };
    if (seq !== fetchSeq) return;
    pendingExpressions.value = payload.pending?.expressions ?? [];
    pendingError.value = payload.pending?.error ?? null;
  } catch (err) {
    if (seq !== fetchSeq) return;
    pendingError.value = (err as Error).message;
  }
}

// 板面加载完成、每次保存完成、提交结束后各刷新一次预览（板面本身由 store / Lead 的视图负责加载）。
onMounted(() => {
  void refreshPending();
});

watch(
  () => [store.loading, store.saveStatus, store.submitStatus] as const,
  ([loading]) => {
    if (!loading) void refreshPending();
  },
);

const summary = computed(() => summarizeChanges(pendingExpressions.value));
const rangeText = computed(() => visibleRangeText(localVisibleRange(store.board)));
const stateLabel = computed(() =>
  submitStateLabel(store.submitStatus, {
    pendingCount: pendingExpressions.value.length,
    hasSubmission: Boolean(store.lastSubmission),
  }),
);
const statusText = computed(() =>
  submitStatusText(store.submitStatus, {
    result: store.lastSubmission,
    pendingCount: pendingExpressions.value.length,
    error: store.submitError,
  }),
);
const saveText = computed(() =>
  saveStateText(store.saveStatus, store.dirty, store.lastSavedAt),
);
const isFailed = computed(
  () => store.submitStatus === "failed" || store.lastSubmission?.status === "failed",
);
const failureText = computed(() => submitFailureText(store.lastSubmission));
const draftText = computed(() => draftHintText());
</script>

<template>
  <footer class="submit-panel" aria-label="提交">
    <BoardChangeList
      :expressions="pendingExpressions"
      title="本次有效改动（尚未提交）"
      empty-text="本次还没有可提交的改动（服务端在每次保存后重算）。"
    />
    <p v-if="pendingError" class="line err" role="status">
      未提交改动预览失败：{{ pendingError }}（提交时服务端仍会重新求差，不影响提交）
    </p>

    <div class="bar">
      <div class="texts">
        <p class="state" data-im="submit-status" role="status">
          <strong class="state-label" :class="{ ok: store.submitStatus === 'succeeded', warn: store.submitStatus === 'failed' }">
            {{ stateLabel }}
          </strong>
          <span class="state-text">{{ statusText }}</span>
        </p>
        <p class="line" data-im="save-status" role="status">{{ saveText }}</p>
        <p class="line" data-im="visible-range">{{ rangeText }}</p>
        <p v-if="isFailed" class="line retain" role="alert">{{ failureText }}</p>
        <p class="line faint">{{ draftText }}</p>
      </div>
      <div class="action">
        <span class="pending">{{ summary.headline }}</span>
        <button
          class="submit"
          type="button"
          data-im="submit"
          :disabled="busy || !store.board"
          @click="store.submit()"
        >
          {{ busy ? "正在提交…" : "提交给 QIO" }}
        </button>
      </div>
    </div>
  </footer>
</template>

<style scoped>
.submit-panel {
  flex: none;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-5);
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-surface);
}
.bar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--sp-4);
  min-width: 0;
}
.texts {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.state {
  margin: 0;
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  min-width: 0;
}
.state-label {
  flex: none;
  font-family: var(--mono);
  font-size: var(--fs-xs);
  color: var(--text-primary);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-1);
}
.state-label.ok { color: var(--success); border-color: var(--success); }
.state-label.warn { color: var(--danger); border-color: var(--danger); }
.state-text { color: var(--text-muted); }
.line {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.line.faint { color: var(--text-faint); }
.line.err { color: var(--danger); }
.line.retain { color: var(--warning); }
.action {
  flex: none;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--sp-1);
}
.pending {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  max-width: 320px;
  text-align: right;
}
.submit {
  font: inherit;
  font-size: var(--fs-base);
  color: var(--on-accent);
  background: var(--accent);
  border: none;
  border-radius: var(--r-sm);
  padding: var(--sp-2) var(--sp-6);
  cursor: pointer;
}
.submit:hover { background: var(--accent-hover); }
.submit:disabled { opacity: 0.55; cursor: default; }
.submit:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
@media (max-width: 900px) {
  .bar { flex-direction: column; align-items: stretch; }
  .action { align-items: stretch; }
  .pending { text-align: left; }
}
</style>
