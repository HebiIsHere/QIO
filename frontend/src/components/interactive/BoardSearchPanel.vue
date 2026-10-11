<!--
  板内搜索浮层（子智能体 A 负责）。

  契约：docs/interactive-mode-contract.md
  - §8.1：板内搜索是底部工具栏的一个开关，**不再常驻右列**；浮层不能压住底部工具栏与右下聊天；
  - §8.4.1：一次性动作「定位到某张卡片」走 window 事件 qio:interactive:locate-card（detail { cardId }）；
  - §1.4：板内搜索能查到用户自己**未勾选**的注释，这不等于把它交给 QIO —— 界面上必须写明这一点。

  本组件由 BoardToolbar 渲染：绝对定位在工具栏正上方、左对齐，所以不会盖住底部工具栏，
  也不会碰到右下角的聊天入口（聊天在右下，搜索在左下向上展开）。
-->
<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { CARD_KIND_LABELS, cardById, searchCards } from "../../interactive/board";

const emit = defineEmits<{
  (e: "close"): void;
}>();

const store = useInteractiveStore();
const query = ref("");
const input = ref<HTMLInputElement | null>(null);
/** 刚刚定位过的结果：给一个「已在板面上定位」的文字回执，不只靠高亮颜色。 */
const locatedId = ref<string | null>(null);

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

/** 未勾选、也没有明确隐藏的注释数量：搜索能看到它们，但它们不会因此进入提交。 */
const uncheckedNotes = computed(
  () => (store.board?.cards ?? []).filter((card) => !card.deleted && card.kind === "text" && !card.checked && !card.hidden).length,
);

function locate(hit: Hit): void {
  locatedId.value = hit.id;
  // 一次性命令用事件（画布在监听）；不把「定位」做成持久状态
  window.dispatchEvent(new CustomEvent("qio:interactive:locate-card", { detail: { cardId: hit.id } }));
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== "Escape") return;
  // Esc 只收起搜索浮层：不让板面把它当成「取消拖动 / 清空选择」
  event.stopPropagation();
  emit("close");
}

onMounted(async () => {
  await nextTick();
  input.value?.focus();
});
</script>

<template>
  <section class="search-panel" data-im="search-panel" aria-label="板内搜索" @keydown="onKeydown">
    <header class="panel-head">
      <h2 class="panel-title">板内搜索</h2>
      <button class="close" type="button" data-im="search-close" title="收起搜索（Esc）" @click="emit('close')">收起</button>
    </header>

    <label class="field">
      <span class="label">搜你自己的板面</span>
      <input
        ref="input"
        v-model="query"
        type="search"
        data-im="search"
        placeholder="搜注释、材料名称或网址"
        aria-label="板内搜索"
      />
    </label>

    <p class="note">
      只查你自己的板面：能搜到未勾选的注释，<strong>这不等于交给 QIO</strong>。
      现在有 {{ uncheckedNotes }} 条未勾选注释，它们不会进入提交。
    </p>

    <ul v-if="hits.length" class="results">
      <li v-for="hit in hits" :key="hit.id">
        <button
          class="result"
          type="button"
          data-im="search-result"
          :data-card-id="hit.id"
          :class="{ located: locatedId === hit.id }"
          @click="locate(hit)"
        >
          <span class="row">
            <span class="kind mono">{{ hit.kindLabel }}</span>
            <span class="summary">{{ hit.summary }}</span>
          </span>
          <span class="tags">
            <span v-if="hit.bookmarked" class="tag bookmark">书签优先</span>
            <span v-if="hit.hidden" class="tag hidden">已隐藏（退出讨论范围）</span>
            <span v-else-if="hit.checked" class="tag checked">已勾选</span>
            <span v-else class="tag">未勾选</span>
            <span v-if="locatedId === hit.id" class="tag located-tag">已在板面上定位</span>
          </span>
        </button>
      </li>
    </ul>
    <p v-else-if="query.trim()" class="note">没有匹配的卡片。</p>
    <p v-else class="note">输入关键词后，点结果会在板面上定位（不会修改板面）。</p>
  </section>
</template>

<style scoped>
/*
  浮在工具栏正上方、左对齐：工具栏是 position:absolute，所以这里的 100% 就是工具栏的上边。
  这样既不遮住底部工具栏，也不碰右下角的聊天入口。背景用令牌，不做重度模糊。
*/
.search-panel {
  position: absolute;
  left: 0;
  bottom: calc(100% + var(--sp-2));
  z-index: 2;
  width: min(340px, calc(100vw - 48px));
  max-height: min(420px, calc(100vh - 220px));
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  box-shadow: var(--elev-floating, var(--shadow-2));
}

.panel-head { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2); }
.panel-title { margin: 0; font-family: var(--serif); font-size: var(--fs-md); font-weight: 600; color: var(--text-strong); }
.close {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  background: none;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 1px var(--sp-2);
  cursor: pointer;
}
.close:hover { color: var(--text-strong); border-color: var(--border-strong); }
.close:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }

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
input:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }

.note { margin: 0; font-size: var(--fs-xs); color: var(--text-faint); line-height: var(--lh-tight); }
.note strong { color: var(--text-muted); }

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
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: var(--sp-1) var(--sp-2);
  cursor: pointer;
}
.result:hover { border-color: var(--border-strong); background: var(--bg-elevated); }
.result:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.result.located { border-color: var(--warning); }
.row { display: flex; align-items: baseline; gap: var(--sp-1); min-width: 0; }
.kind { color: var(--text-muted); flex: none; }
.summary { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tags { display: flex; flex-wrap: wrap; gap: var(--sp-1); color: var(--text-faint); }
.tag { border: 1px solid var(--border-subtle); border-radius: var(--r-pill); padding: 0 var(--sp-1); }
.tag.bookmark { color: var(--warning); border-color: var(--warning); }
.tag.hidden { color: var(--text-muted); border-style: dashed; }
.tag.checked { color: var(--success); border-color: var(--success); }
.tag.located-tag { color: var(--warning); border-color: var(--warning); }
</style>
