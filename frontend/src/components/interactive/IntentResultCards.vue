<script setup lang="ts">
/**
 * 结果卡（阶段三 C，契约 §13.5）：
 * - 正文完整来自服务端（不静默截断成四千字），支持 Markdown 表格 / 代码块 / 标题 / 列表；
 * - 长卡片默认折叠 + 明确的「展开全文」，展开后在自己的滚动层里读，不撑破板面；
 * - 空正文不显示假卡，也不二次生成正文。
 */
import { computed, ref } from "vue";
import MarkdownContent from "../MarkdownContent.vue";
import {
  hasResultContent,
  isLongResultContent,
  type IntentResultView,
} from "../../interactive/intentWorkbench";
import { vQioScrollbar } from "../../composables/useQioScrollbars";

const props = defineProps<{ results: IntentResultView[] }>();

/** 只渲染真的有正文的结果卡（空正文不是「结果」）。 */
const visibleResults = computed(() => props.results.filter(hasResultContent));
const expanded = ref<Record<string, boolean>>({});

function isOpen(resultId: string): boolean {
  return expanded.value[resultId] === true;
}

function toggle(resultId: string): void {
  expanded.value = { ...expanded.value, [resultId]: !isOpen(resultId) };
}

function isLong(result: IntentResultView): boolean {
  return isLongResultContent(result.content);
}
</script>

<template>
  <section class="result-cards" aria-label="结果卡" data-im="result-cards">
    <header class="result-head">
      <h3 class="result-heading">结果</h3>
      <span v-if="visibleResults.length" class="result-count mono" data-im="result-count">
        {{ visibleResults.length }} 张
      </span>
    </header>
    <p v-if="!visibleResults.length" class="result-empty" data-im="result-empty">
      这次没有带正文的结果卡（空正文不会显示成假卡）。
    </p>
    <ul v-else class="result-list">
      <li
        v-for="result in visibleResults"
        :key="result.result_id"
        class="result-card"
        data-im="result-card"
        :data-result-id="result.result_id"
        :class="{ folded: isLong(result) && !isOpen(result.result_id) }"
      >
        <div class="result-card-head">
          <span class="result-title">{{ result.title || "结果卡" }}</span>
          <span class="result-meta mono">{{ result.card_id || result.result_id }}</span>
          <span v-if="result.intent_id" class="result-meta mono">意图 {{ result.intent_id }}</span>
        </div>
        <!-- 完整正文（不截断）：长卡片折叠时只裁显示高度，并给显式展开入口 -->
        <div
          class="result-body"
          :class="{ clipped: isLong(result) && !isOpen(result.result_id) }"
          :data-im="'result-body:' + result.result_id"
          v-qio-scrollbar="{ policy: 'auto', axis: 'y' }"
        >
          <MarkdownContent :source="result.content || ''" />
        </div>
        <button
          v-if="isLong(result)"
          class="result-toggle"
          type="button"
          data-im="result-toggle"
          @click="toggle(result.result_id)"
        >
          {{ isOpen(result.result_id) ? "收起" : "展开全文（共 " + String((result.content || "").length) + " 字）" }}
        </button>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.result-cards {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-width: 0;
}
.result-head,
.result-card-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  flex-wrap: wrap;
  min-width: 0;
}
.result-heading {
  margin: 0;
  font-family: var(--serif);
  font-size: var(--fs-sm);
  color: var(--text-strong);
}
.result-count,
.result-meta {
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.mono { font-family: var(--mono); }
.result-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.result-card {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
  padding: var(--sp-2) var(--sp-3);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-width: 0;
}
.result-title {
  font-size: var(--fs-sm);
  color: var(--text-strong);
}
.result-body {
  font-size: var(--fs-sm);
  color: var(--text-primary);
  line-height: 1.7;
  min-width: 0;
  overflow-wrap: anywhere;
}
/* 折叠只改显示高度，不改正文；展开后最大 60vh，自己滚，不撑破面板 */
.result-body.clipped {
  max-height: 300px;
  overflow: hidden;
}
.result-card:not(.folded) .result-body {
  max-height: 60vh;
  overflow-y: auto;
}
.result-toggle {
  align-self: flex-start;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
}
.result-toggle:hover { color: var(--accent-hover); }
.result-toggle:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.result-empty {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
</style>
