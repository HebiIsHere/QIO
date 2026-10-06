<!-- 组框（子智能体 A 负责）：组名、有序 / 普通切换、解除组、组内顺序。

  契约：docs/interactive-mode-contract.md §1.3 / §4.4。
  - 普通组：自由摆放不表示先后 → 不显示序号，只列出成员；
  - 有序组：显示 1..n 序号，顺序调整可作为依据；
  - 组框本身不吃指针事件（否则卡片拖不动），只有头部两行可交互；
  - 头部高度控制在 board.ts 的 GROUP_PAD_TOP（58px）以内，序号条不会压住成员卡片。
-->
<script setup lang="ts">
import { computed } from "vue";
import type { BoardCard, BoardGroup } from "../../interactive/types";

const props = defineProps<{
  group: BoardGroup;
  cards: BoardCard[];
  selectedIds: string[];
}>();

const emit = defineEmits<{
  (e: "rename", groupId: string, name: string): void;
  (e: "toggle-ordered", groupId: string, ordered: boolean): void;
  (e: "dissolve", groupId: string): void;
  (e: "move-member", groupId: string, cardId: string, index: number): void;
  (e: "leave", groupId: string, cardId: string): void;
  (e: "select-member", cardId: string): void;
}>();

const style = computed(() => ({
  left: props.group.x + "px",
  top: props.group.y + "px",
  width: props.group.w + "px",
  height: props.group.h + "px",
}));

const memberCards = computed(() =>
  props.group.members
    .map((id) => props.cards.find((card) => card.id === id))
    .filter((card): card is BoardCard => Boolean(card)),
);

function titleOf(card: BoardCard): string {
  const text = (card.content || String(card.meta?.name ?? card.meta?.title ?? "")).trim();
  if (!text) return "（无内容）";
  return text.length > 10 ? text.slice(0, 10) + "…" : text;
}

function onRename(event: Event) {
  const target = event.target as HTMLInputElement;
  emit("rename", props.group.id, target.value);
  // 名字没变或为空时把输入框恢复成真实组名（留空不生效）
  target.value = props.group.name;
}
</script>

<template>
  <div class="group" :class="{ ordered: group.ordered }" :style="style" data-im="group" :data-group-id="group.id">
    <div class="group-head">
      <div class="head-row">
        <input
          class="group-name"
          type="text"
          :value="group.name"
          data-im="group-name"
          :data-group-id="group.id"
          aria-label="组名"
          @change="onRename"
          @keydown.enter="onRename"
        />
        <button
          class="btn"
          type="button"
          data-im="toggle-ordered"
          :data-group-id="group.id"
          @click="emit('toggle-ordered', group.id, !group.ordered)"
        >
          {{ group.ordered ? "取消有序" : "设为有序" }}
        </button>
        <button class="btn" type="button" @click="emit('dissolve', group.id)">解除组</button>
        <span class="mode" :title="group.ordered ? '有序组：序号 1..n 表示顺序' : '普通组：自由摆放不表示先后'">
          {{ group.ordered ? "有序：序号 1..n 表示顺序" : "普通组：摆放顺序不代表先后" }}
        </span>
        <span class="count mono">成员 {{ group.members.length }}</span>
      </div>

      <ol v-if="group.ordered" class="sequence" aria-label="组内顺序">
        <li
          v-for="(card, index) in memberCards"
          :key="card.id"
          class="chip"
          :class="{ selected: selectedIds.includes(card.id) }"
        >
          <button class="chip-title" type="button" @click="emit('select-member', card.id)">
            <span class="index mono">{{ index + 1 }}</span>
            <span class="chip-text">{{ titleOf(card) }}</span>
          </button>
          <button class="btn tiny" type="button" :disabled="index === 0" @click="emit('move-member', group.id, card.id, index - 1)">
            上移
          </button>
          <button
            class="btn tiny"
            type="button"
            :disabled="index === memberCards.length - 1"
            @click="emit('move-member', group.id, card.id, index + 1)"
          >
            下移
          </button>
          <button class="btn tiny" type="button" @click="emit('leave', group.id, card.id)">移出</button>
        </li>
      </ol>
      <ul v-else class="sequence plain" aria-label="组成员">
        <li
          v-for="card in memberCards"
          :key="card.id"
          class="chip"
          :class="{ selected: selectedIds.includes(card.id) }"
        >
          <button class="chip-title" type="button" @click="emit('select-member', card.id)">
            <span class="chip-text">{{ titleOf(card) }}</span>
          </button>
          <button class="btn tiny" type="button" @click="emit('leave', group.id, card.id)">移出</button>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.group {
  position: absolute;
  border: 1px dashed var(--border-strong);
  border-radius: var(--r-lg);
  background: var(--bg-inset);
  pointer-events: none; /* 卡片要能拖动：只有头部两行接管指针 */
  z-index: 2;
}
.group.ordered { border-color: var(--accent); }
.group-head {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 56px; /* 与 board.ts 的 GROUP_PAD_TOP 对齐，不压住成员卡片 */
  overflow: hidden;
  padding: 2px var(--sp-2);
  pointer-events: auto;
  background: var(--bg-elevated);
  border-bottom: 1px solid var(--border-subtle);
  border-radius: var(--r-lg) var(--r-lg) 0 0;
  font-size: var(--fs-xs);
}
.head-row { display: flex; align-items: center; gap: var(--sp-2); min-width: 0; }
.group-name {
  flex: none;
  font: inherit;
  font-family: var(--serif);
  font-size: var(--fs-sm);
  color: var(--text-strong);
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-1);
  min-width: 80px;
  max-width: 160px;
}
.mode { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); }
.count { flex: none; color: var(--text-faint); }
.sequence {
  display: flex;
  flex-wrap: nowrap;
  gap: var(--sp-1);
  margin: 0;
  padding: 0;
  list-style: none;
  overflow-x: auto;
  overflow-y: hidden;
  max-height: 24px;
}
.chip {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 0 var(--sp-1);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  font-size: var(--fs-xs);
}
.chip.selected { border-color: var(--accent); background: var(--bg-accent-subtle); }
.chip-title {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  font: inherit;
  color: var(--text-primary);
  background: none;
  border: none;
  cursor: pointer;
  padding: 0;
}
.index { color: var(--accent); }
.chip-text { max-width: 8em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-1);
  cursor: pointer;
}
.btn:hover { color: var(--text-strong); }
.btn:focus-visible { outline: 2px solid var(--link); outline-offset: 1px; }
.btn:disabled { opacity: 0.45; cursor: default; }
.btn.tiny { font-size: var(--fs-tech, var(--fs-xs)); }
</style>
