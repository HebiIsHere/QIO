<!--
  互动页书签栏（Phase 2/3 Lead 集成）：只列真实 bookmarked 卡片，点击定位并高亮。
  纯查看：不改位置、尺寸、参与状态或正文；空时明确说没有。
-->
<script setup lang="ts">
import { computed } from "vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { CARD_KIND_LABELS } from "../../../interactive/board";
import { useEscapeLayer } from "../../../composables/useEscapeLayer";

const emit = defineEmits<{ (e: "close"): void }>();

/* #73：工具栏入口可收起；书签栏打开时也接受 Esc（只收面板，不改板面与选择）。 */
useEscapeLayer({
  id: "interactive-bookmarks",
  active: () => true,
  onEscape: () => {
    emit("close");
    return true;
  },
});
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
    <!-- #73：单条书签用紧凑单行；关闭入口在列表正上方（头部「收起」），长列表自己滚动。 -->
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
.bookmarks { display: flex; flex-direction: column; height: 100%; min-height: 0; gap: var(--sp-1); padding: var(--sp-1) var(--sp-2); }
.bk-head { display: flex; align-items: center; gap: var(--sp-2); flex: none; min-height: 26px; padding: 0 var(--sp-1); }
.bk-title { font-size: var(--fs-sm); color: var(--text-strong); }
.bk-count { font-size: var(--fs-xs); color: var(--text-muted); }
.bk-close { margin-left: auto; font: inherit; font-size: var(--fs-xs); line-height: 1.5; color: var(--text-secondary); background: none; border: 1px solid transparent; border-radius: var(--r-pill); padding: 0 var(--sp-2); cursor: pointer; }
.bk-close:hover { color: var(--text-strong); border-color: var(--border-subtle); }
.bk-close:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.bk-empty { margin: var(--sp-1); padding: var(--sp-2); border: 1px dashed var(--border-subtle); border-radius: var(--r-lg); color: var(--text-muted); font-size: var(--fs-xs); line-height: 1.6; }
/* 长列表单独滚动：头部与关闭入口固定，不跟着列表滚出可视区。 */
.bk-list { flex: 1; list-style: none; margin: 0; padding: 0; overflow-y: auto; min-height: 0; }
.bk-row { display: flex; align-items: baseline; gap: var(--sp-2); width: 100%; padding: 2px var(--sp-2); border: 0; border-radius: var(--r-lg); background: none; color: var(--text-primary); text-align: left; cursor: pointer; }
.bk-row:hover { background: var(--layer-hover); }
.bk-row:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: -2px; }
.bk-name { flex: 1; min-width: 0; font-size: var(--fs-xs); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bk-kind { flex: none; color: var(--text-muted); font-size: 10px; }
</style>
