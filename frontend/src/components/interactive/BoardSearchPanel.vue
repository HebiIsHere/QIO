<!-- 板内搜索（子智能体 A 负责）：查用户自己的板面，书签优先。

  契约：docs/interactive-mode-contract.md §1.4 / §4.4。
  板内搜索能查到未勾选的注释，这不等于把它交给 QIO —— 界面上必须写明这一点。
-->
<script setup lang="ts">
import { computed, ref } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { CARD_KIND_LABELS, cardById, searchCards } from "../../interactive/board";

const store = useInteractiveStore();
const query = ref("");

const emit = defineEmits<{
  (e: "locate", cardId: string): void;
}>();

interface Hit {
  id: string;
  kindLabel: string;
  summary: string;
  bookmarked: boolean;
  checked: boolean;
  hidden: boolean;
}

const hits = computed<Hit[]>(() => {
  const state = store.board;
  if (!state || !query.value.trim()) return [];
  return searchCards(state, query.value)
    .map((id) => cardById(state, id))
    .filter((card): card is NonNullable<typeof card> => Boolean(card))
    .map((card) => {
      const text = (card.content || String(card.meta?.name ?? card.meta?.title ?? "")).trim();
      return {
        id: card.id,
        kindLabel: CARD_KIND_LABELS[card.kind],
        summary: text ? (text.length > 28 ? text.slice(0, 28) + "…" : text) : "（无内容）",
        bookmarked: card.bookmarked,
        checked: card.checked,
        hidden: card.hidden,
      };
    });
});
</script>

<template>
  <aside class="search" aria-label="板内搜索">
    <label class="field">
      <span class="label">板内搜索</span>
      <input v-model="query" type="search" data-im="search" placeholder="搜注释、材料名称或网址" />
    </label>
    <p class="note">只查你自己的板面：能搜到未勾选的注释，这不等于交给 QIO。</p>
    <ul v-if="hits.length" class="results">
      <li v-for="hit in hits" :key="hit.id">
        <button class="result" type="button" data-im="search-result" :data-card-id="hit.id" @click="emit('locate', hit.id)">
          <span class="kind mono">{{ hit.kindLabel }}</span>
          <span class="summary">{{ hit.summary }}</span>
          <span class="tags">
            <span v-if="hit.bookmarked" class="tag bookmark">书签优先</span>
            <span v-if="hit.hidden" class="tag hidden">已隐藏</span>
            <span v-else-if="hit.checked" class="tag">已勾选</span>
            <span v-else class="tag">未勾选</span>
          </span>
        </button>
      </li>
    </ul>
    <p v-else-if="query.trim()" class="note">没有匹配的卡片。</p>
  </aside>
</template>

<style scoped>
.search {
  width: 240px;
  flex: none;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
  background: var(--bg-surface);
  border-left: 1px solid var(--border-subtle);
  overflow: auto;
}
.field { display: flex; flex-direction: column; gap: var(--sp-1); }
.label { font-size: var(--fs-xs); color: var(--text-muted); }
input {
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--text-primary);
  background: var(--bg-inset);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  padding: var(--sp-1) var(--sp-2);
}
input:focus-visible { outline: 2px solid var(--link); outline-offset: 1px; }
.note { margin: 0; font-size: var(--fs-xs); color: var(--text-faint); }
.results { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-1); }
.result {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 2px;
  text-align: left;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: var(--sp-1) var(--sp-2);
  cursor: pointer;
}
.result:hover { border-color: var(--accent); }
.result:focus-visible { outline: 2px solid var(--link); outline-offset: 1px; }
.kind { color: var(--text-muted); }
.summary { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tags { display: flex; gap: var(--sp-1); color: var(--text-faint); }
.tag { border: 1px solid var(--border-subtle); border-radius: var(--r-pill); padding: 0 var(--sp-1); }
.tag.bookmark { color: var(--warning); border-color: var(--warning); }
.tag.hidden { color: var(--text-muted); border-style: dashed; }
@media (max-width: 900px) {
  .search { width: 180px; }
}
</style>
