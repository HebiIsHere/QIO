<!--
  卡片草稿的保存提示（子智能体 C 提供，渲染位置由 A 的 BoardCard.vue 决定）。

  契约：docs/interactive-mode-contract.md §9.5。

  为什么单独做成组件：草稿保存失败必须**贴近正在编辑的那张卡**说清楚，
  而不是在板面顶部挂一条全局提示（那会与「已保存 / 已提交」两个主状态打架，
  也离用户正在编辑的对象太远）。组件只读 stores/interactive.ts 的状态，
  重试只重写草稿，不建卡、不提交板面、不调用 QIO。

  用法（A 在 BoardCard.vue 的编辑区里渲染任意一处即可）：
    <CardDraftHint :card-id="card.id" />        // 键按 card:<id> 推导
    <CardDraftHint draft-key="card:xxx" />      // 已经有键时直接传
-->
<script setup lang="ts">
import { computed, ref } from "vue";
import { useInteractiveStore } from "../../stores/interactive";

const props = defineProps<{
  /** 卡片 id：内部按契约的 card:<id> 组成草稿键 */
  cardId?: string;
  /** 已算好的草稿键（与 cardId 二选一，优先用它） */
  draftKey?: string;
}>();

const store = useInteractiveStore();
const retrying = ref(false);

const key = computed(() => {
  if (props.draftKey) return props.draftKey;
  if (props.cardId) return "card:" + props.cardId;
  // 没给具体对象时退回「最近编辑的那一条」：至少提示不会指向别的卡片
  return store.lastDraftKey;
});

const state = computed(() => store.draftStateFor(key.value));
/** idle 什么都不显示（不该在板面上长期占位） */
const visible = computed(() => Boolean(key.value) && state.value.status !== "idle");
const isError = computed(() => state.value.status === "error");

async function retry() {
  if (retrying.value) return;
  retrying.value = true;
  try {
    // 只重写草稿：不建卡、不提交、不调 QIO
    await store.retryDraftSave(key.value);
  } finally {
    retrying.value = false;
  }
}
</script>

<template>
  <p
    v-if="visible"
    class="card-draft-hint"
    :class="{ err: isError }"
    :role="isError ? 'alert' : 'status'"
    :aria-live="isError ? 'assertive' : 'polite'"
    :title="state.error ?? ''"
    data-im="card-draft-hint"
  >
    <template v-if="state.status === 'error'">
      <span class="msg" data-im="card-draft-error">
        草稿未保存：{{ state.error || "原因未知" }}（输入内容已保留）
      </span>
      <button
        class="retry"
        type="button"
        data-im="card-draft-retry"
        :disabled="retrying"
        @click="retry"
      >
        {{ retrying ? "重试中…" : "重试保存" }}
      </button>
    </template>
    <span v-else-if="state.status === 'saving'" class="msg mono">草稿保存中…</span>
    <span v-else class="msg mono">草稿已保存</span>
  </p>
</template>

<style scoped>
/* 一行轻量提示：贴着编辑对象，不铺底、不加阴影（比卡片本身更轻） */
.card-draft-hint {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--sp-2);
  margin: var(--sp-1) 0 0;
  font-family: var(--sans);
  font-size: var(--fs-sm);
  line-height: var(--lh-tight);
  color: var(--text-muted);
}
.card-draft-hint.err {
  color: var(--danger);
}
.msg {
  min-width: 0;
  /* 原因可能很长（后端原话）：换行显示，不截断成看不懂的省略号 */
  overflow-wrap: anywhere;
}
.retry {
  flex: none;
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--link);
  background: none;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  padding: 1px var(--sp-2);
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.retry:hover:not(:disabled) {
  border-color: var(--link);
}
.retry:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.retry:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
