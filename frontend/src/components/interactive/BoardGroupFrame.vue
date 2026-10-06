<!-- 组框（子智能体 B 负责）：组名、有序 / 普通切换、解除组、组内顺序与拖动插入位置提示。

  契约：docs/interactive-mode-contract.md §1.3 / §8.2。
  - 普通组：自由摆放不表示先后 → 不显示序号，只列出成员；
  - 有序组：显示明确 1..n 序号，顺序调整可作为依据；
  - 组名：初始是系统默认名（「组 N」）；输入即用；留空或取消保留默认名，组仍然成立；
  - 拖动预演时显示「将加入这一组」与**插入位置**（第 N 位）；
  - 组框本身不吃指针事件（否则卡片拖不动），只有头部两行可交互；
  - 头部高度控制在 board.ts 的 GROUP_PAD_TOP（58px）以内，序号条不会压住成员卡片。
-->
<script setup lang="ts">
import { computed, nextTick, ref } from "vue";
import { DEFAULT_GROUP_NAME_PREFIX } from "../../interactive/board";
import type { BoardCard, BoardGroup } from "../../interactive/types";

const props = defineProps<{
  group: BoardGroup;
  cards: BoardCard[];
  selectedIds: string[];
  /** 拖动预演：卡片将落入这个组 */
  dropTarget: boolean;
  /** 拖动预演：这个组将被并入落点所在的组 */
  dropMerge: boolean;
  /** 拖动预演：插入位置（0 起），仅 dropTarget 时有意义 */
  dropIndex: number | null;
  /** 拖动预演：被拖动的那张卡片（有序组里要显示它将插到哪两位之间） */
  dropCardId: string | null;
}>();

const emit = defineEmits<{
  (e: "rename", groupId: string, name: string): void;
  (e: "toggle-ordered", groupId: string, ordered: boolean): void;
  (e: "dissolve", groupId: string): void;
  (e: "move-member", groupId: string, cardId: string, index: number): void;
  (e: "leave", groupId: string, cardId: string): void;
  (e: "select-member", cardId: string): void;
}>();

const nameInput = ref<HTMLInputElement | null>(null);

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

/** 系统默认名的提示：输入框留空时用它说明「留空会保留这个名字」。 */
const defaultHint = computed(() => props.group.name || DEFAULT_GROUP_NAME_PREFIX + " N");

function titleOf(card: BoardCard): string {
  const text = (card.content || String(card.meta?.name ?? card.meta?.title ?? "")).trim();
  if (!text) return "（无内容）";
  return text.length > 10 ? text.slice(0, 10) + "…" : text;
}

/** 插入位置的文字说明：有序组要能读出「插到第 N 位」。 */
const insertText = computed(() => {
  if (!props.dropTarget || props.dropIndex === null) return "";
  return "将插入第 " + (props.dropIndex + 1) + " 位";
});

/** 组名：输入即用（用户敲完即生效）；留空或取消保留原默认名。 */
function onRename(event: Event) {
  const target = event.target as HTMLInputElement;
  emit("rename", props.group.id, target.value);
  // 名字没变或为空时把输入框恢复成真实组名（留空不生效，组仍然成立）
  target.value = props.group.name;
}

function onNameKeydown(event: KeyboardEvent) {
  if (event.key === "Enter") {
    (event.target as HTMLInputElement).blur();
  }
}

/** 拖动中把被拖动卡片临时藏起来，序号条显示的是「放下后的顺序」。 */
const displayCards = computed(() =>
  props.dropCardId ? memberCards.value.filter((card) => card.id !== props.dropCardId) : memberCards.value,
);

async function focusName() {
  await nextTick();
  nameInput.value?.focus();
  nameInput.value?.select();
}
defineExpose({ focusName });
</script>

<template>
  <div
    class="group"
    :class="{ ordered: group.ordered, 'drop-target': dropTarget, 'drop-merge': dropMerge }"
    :style="style"
    data-im="group"
    :data-group-id="group.id"
  >
    <div class="group-head">
      <div class="head-row">
        <input
          ref="nameInput"
          class="group-name"
          type="text"
          :value="group.name"
          :placeholder="defaultHint"
          data-im="group-name"
          :data-group-id="group.id"
          aria-label="组名"
          @change="onRename"
          @keydown.enter="onNameKeydown"
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
        <span v-if="group.defaultName" class="badge default-name">系统默认名，可改</span>
        <span v-if="dropMerge" class="badge merge">拖动中：这一组将被并入落点所在的组</span>
        <span v-else-if="dropTarget" class="badge" data-im="group-drop-hint">
          {{ insertText || "将加入这一组" }}
        </span>
      </div>

      <ol v-if="group.ordered" class="sequence" aria-label="组内顺序">
        <li
          v-for="(card, index) in displayCards"
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
            :disabled="index === displayCards.length - 1"
            @click="emit('move-member', group.id, card.id, index + 1)"
          >
            下移
          </button>
          <button class="btn tiny" type="button" @click="emit('leave', group.id, card.id)">移出</button>
        </li>
        <li v-if="dropTarget && dropCardId" class="chip insert">
          <span class="chip-text">插入位置：第 {{ (dropIndex ?? displayCards.length) + 1 }} 位</span>
        </li>
      </ol>
      <ul v-else class="sequence plain" aria-label="组成员">
        <li
          v-for="card in displayCards"
          :key="card.id"
          class="chip"
          :class="{ selected: selectedIds.includes(card.id) }"
        >
          <button class="chip-title" type="button" @click="emit('select-member', card.id)">
            <span class="chip-text">{{ titleOf(card) }}</span>
          </button>
          <button class="btn tiny" type="button" @click="emit('leave', group.id, card.id)">移出</button>
        </li>
        <li v-if="dropTarget && dropCardId" class="chip insert">
          <span class="chip-text">插入位置：第 {{ (dropIndex ?? displayCards.length) + 1 }} 位</span>
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
.group.drop-target { border-style: solid; border-color: var(--accent); background: var(--accent-soft); }
.group.drop-merge { border-style: solid; border-color: var(--warning); }
.badge {
  flex: none;
  font-size: var(--fs-tech, var(--fs-xs));
  color: var(--on-accent);
  background: var(--accent);
  border-radius: var(--r-pill);
  padding: 0 var(--sp-2);
}
.badge.merge { background: var(--warning); color: var(--bg-base); }
.badge.default-name { color: var(--text-muted); background: none; border: 1px solid var(--border-subtle); }
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
.head-row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); min-width: 0; }
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
.chip.insert { border-style: dashed; border-color: var(--accent); color: var(--text-secondary); }
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
