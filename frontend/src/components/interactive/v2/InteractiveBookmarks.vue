<!--
  互动页书签栏（Phase 2/3 Lead 集成）：只列真实 bookmarked 卡片，点击定位并高亮。
  纯查看：不改位置、尺寸、参与状态或正文；空时明确说没有。
-->
<script setup lang="ts">
import { computed } from "vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { CARD_KIND_LABELS } from "../../../interactive/board";

const emit = defineEmits<{ (e: "close"): void }>();
const store = useInteractiveStore();

const rows = computed(() =>
  (store.board?.cards ?? [])
    .filter((card) => !card.deleted && card.bookmarked)
    .map((card) => ({
      id: card.id,
      title: (card.content || String(card.meta?.name ?? card.meta?.title ?? "")).trim() || "（无内容）",
      kind: CARD_KIND_LABELS[card.kind],
    })),
);

function locate(cardId: string): void {
  window.dispatchEvent(new CustomEvent("qio:interactive:locate-card", { detail: { cardId } }));
}
</script>

<template>
  <section class="bookmarks" data-im="bookmarks-panel" aria-label="书签栏">
    <header class="bk-head">
      <span class="bk-title serif">书签</span>
      <span class="bk-count mono">{{ rows.length }}</span>
      <button class="bk-close" type="button" aria-label="收起书签栏" title="收起书签栏" @click="emit('close')">收起</button>
    </header>
    <p v-if="!rows.length" class="bk-empty">还没有书签。选中卡片后可在卡片工具里加书签。</p>
    <ul v-else class="bk-list">
      <li v-for="row in rows" :key="row.id">
        <button class="bk-row" type="button" :data-bookmark-id="row.id" @click="locate(row.id)">
          <span class="bk-name">{{ row.title }}</span>
          <span class="bk-kind mono">{{ row.kind }}</span>
        </button>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.bookmarks { display: flex; flex-direction: column; height: 100%; min-height: 0; padding: var(--sp-2); }
.bk-head { display: flex; align-items: center; gap: var(--sp-2); flex: none; padding: var(--sp-1) var(--sp-2); }
.bk-title { font-size: var(--fs-sm); color: var(--text-strong); }
.bk-count { font-size: var(--fs-xs); color: var(--text-muted); }
.bk-close { margin-left: auto; font: inherit; font-size: var(--fs-xs); color: var(--text-secondary); background: none; border: 0; padding: 0; cursor: pointer; }
.bk-empty { margin: var(--sp-2); padding: var(--sp-3); border: 1px dashed var(--border-subtle); border-radius: var(--r-sm); color: var(--text-muted); font-size: var(--fs-xs); line-height: 1.7; }
.bk-list { list-style: none; margin: 0; padding: 0; overflow-y: auto; min-height: 0; }
.bk-row { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; width: 100%; padding: var(--sp-1) var(--sp-2); border: 0; border-radius: var(--r-xs); background: none; color: var(--text-primary); text-align: left; cursor: pointer; }
.bk-row:hover { background: var(--layer-hover); }
.bk-name { font-size: var(--fs-xs); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
.bk-kind { color: var(--text-muted); font-size: 10px; }
</style>
