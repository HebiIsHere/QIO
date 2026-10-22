<!--
  组条（方案 A 的最终呈现，2026-10-22 用户确认）：

  - 组不再常驻画任何轮廓/线条：常驻只有一条「组条」+ 成员卡片左缘的归属标记；
  - 组条 = 拖动把手 + 组名（可改）+ 成员数 + 有序说明 + 成员入口；按住组条空白处拖动整组；
  - 拖入组时的提示（将插入第 N 位 / 并入）是组条上方的独立浮标，不塞进组条里，
    因此不会把组条文字挤到显示不全，也不会被组条裁切；
  - 组框位置按成员包围盒顶部计算；组条本身只接管自己的指针，不挡成员卡片；
  - 正式内容不画虚线；虚线只属于待审批预览（另一个组件）。
-->
<script setup lang="ts">
import { computed, nextTick, ref } from "vue";
import type { BoardCard, BoardGroup } from "../../interactive/types";
// 组内顺序 / 成员条是**横向浏览**区域：滚动能力保留，滚动条外观统一走 QIO 指示条。
import { vQioScrollbar } from "../../composables/useQioScrollbars";

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
  /** 按住组条拖动整组（由 BoardCanvas 处理：实时预览 + 松手一次提交） */
  (e: "drag-start", groupId: string, event: PointerEvent): void;
}>();

const nameInput = ref<HTMLInputElement | null>(null);

/**
 * 「成员与顺序」面板是否展开：**只有用户点它才展开**（缺省收起）。
 * 这是纯显示状态，不进任何存储，也不改变分组 / 顺序本身。
 */
const membersOpen = ref(false);

const memberCards = computed(() =>
  props.group.members
    .map((id) => props.cards.find((card) => card.id === id))
    .filter((card): card is BoardCard => Boolean(card)),
);

/** 拖动中把被拖动卡片临时藏起来，序号条显示的是「放下后的顺序」。 */
const displayCards = computed(() =>
  props.dropCardId ? memberCards.value.filter((card) => card.id !== props.dropCardId) : memberCards.value,
);

/** 成员包围盒（只用于摆组条；不再画轮廓）。 */
const memberBounds = computed(() => {
  const members = memberCards.value;
  if (!members.length) return null;
  const minX = Math.min(...members.map((card) => card.x));
  const minY = Math.min(...members.map((card) => card.y));
  return { x: minX, y: minY };
});

/** 组条位置：成员最上方偏左一点（高度按 GROUP_PAD_TOP 留白带）。没有成员时不渲染。 */
const railStyle = computed<Record<string, string>>(() => {
  const bounds = memberBounds.value;
  if (!bounds) return { display: "none" } as Record<string, string>;
  return {
    left: Math.max(0, bounds.x) + "px",
    top: Math.max(0, bounds.y - 52) + "px",
  };
});

/** 输入框留空时的提示：留空不生效，组名仍是系统给的默认名，组依然成立。 */
const nameHint = computed(() => props.group.name || "组名");

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

function isInteractiveTarget(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element || typeof element.closest !== "function") return false;
  return Boolean(
    element.closest("button, input, textarea, select, a, label, [data-im='group-tools']"),
  );
}

/** 按住组条（空白处）开始整组拖动；点输入框 / 按钮 / 成员面板不进入拖动。 */
function onHeaderPointerDown(event: PointerEvent): void {
  if (event.button !== undefined && event.button !== 0) return;
  if (isInteractiveTarget(event.target)) return;
  event.preventDefault();
  event.stopPropagation();
  emit("drag-start", props.group.id, event);
}

async function focusName() {
  await nextTick();
  nameInput.value?.focus();
  nameInput.value?.select();
}
defineExpose({ focusName });
</script>

<template>
  <div
    v-if="memberCards.length"
    class="group"
    :class="{ ordered: group.ordered, 'drop-target': dropTarget, 'drop-merge': dropMerge }"
    :style="railStyle"
    data-im="group"
    :data-group-id="group.id"
    :data-ordered="group.ordered ? 'true' : 'false'"
  >
    <div class="group-body">
      <!--
        组条：常驻的“组身份 + 拖动把手”。整条空白处都能拖；输入框 / 按钮除外。
      -->
      <div
        class="group-head"
        :class="{ 'tools-open': membersOpen }"
        data-im="group-head"
        title="按住组条拖动整组"
        @pointerdown="onHeaderPointerDown"
      >
        <div class="head-row">
          <span class="grip" aria-hidden="true">⠿</span>
          <input
            ref="nameInput"
            class="group-name"
            type="text"
            :value="group.name"
            :placeholder="nameHint"
            data-im="group-name"
            :data-group-id="group.id"
            aria-label="组名"
            @change="onRename"
            @keydown.enter="onNameKeydown"
          />
          <span class="count mono">成员 {{ group.members.length }}</span>
          <span class="mode" :title="group.ordered ? '有序组：序号 1..n 表示顺序' : '普通组：自由摆放不表示先后'">
            {{ group.ordered ? "有序：序号 1..n 表示顺序" : "普通组：摆放顺序不代表先后" }}
          </span>
          <span v-if="group.defaultName" class="badge default-name">系统默认名，可改</span>
          <button
            class="btn"
            type="button"
            data-im="group-members-toggle"
            :data-group-id="group.id"
            :aria-expanded="membersOpen"
            :aria-controls="'group-tools-' + group.id"
            @click="membersOpen = !membersOpen"
          >
            {{ membersOpen ? "收起成员与顺序" : "成员与顺序" }}
          </button>
        </div>

        <div v-show="membersOpen" :id="'group-tools-' + group.id" class="group-tools" data-im="group-tools">
          <div class="tools-row">
            <button
              class="btn"
              type="button"
              data-im="toggle-ordered"
              :data-group-id="group.id"
              @click="emit('toggle-ordered', group.id, !group.ordered)"
            >
              {{ group.ordered ? "取消有序" : "设为有序" }}
            </button>
            <button
              class="btn"
              type="button"
              data-im="dissolve-group"
              :data-group-id="group.id"
              @click="emit('dissolve', group.id)"
            >
              解除组
            </button>
            <span class="tools-hint">这里只改顺序与归属，不改变成员的材料范围</span>
          </div>

          <ol v-if="group.ordered" class="sequence" aria-label="组内顺序" v-qio-scrollbar="{ policy: 'auto', axis: 'x' }">
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
              <button
                class="btn tiny"
                type="button"
                data-im="move-member-up"
                :data-group-id="group.id"
                :data-card-id="card.id"
                :disabled="index === 0"
                @click="emit('move-member', group.id, card.id, index - 1)"
              >
                上移
              </button>
              <button
                class="btn tiny"
                type="button"
                data-im="move-member-down"
                :data-group-id="group.id"
                :data-card-id="card.id"
                :disabled="index === displayCards.length - 1"
                @click="emit('move-member', group.id, card.id, index + 1)"
              >
                下移
              </button>
              <button
                class="btn tiny"
                type="button"
                data-im="leave-member"
                :data-group-id="group.id"
                :data-card-id="card.id"
                @click="emit('leave', group.id, card.id)"
              >
                移出
              </button>
            </li>
            <li v-if="dropTarget && dropCardId" class="chip insert">
              <span class="chip-text">插入位置：第 {{ (dropIndex ?? displayCards.length) + 1 }} 位</span>
            </li>
          </ol>
          <ul v-else class="sequence plain" aria-label="组成员" v-qio-scrollbar="{ policy: 'auto', axis: 'x' }">
            <li
              v-for="card in displayCards"
              :key="card.id"
              class="chip"
              :class="{ selected: selectedIds.includes(card.id) }"
            >
              <button class="chip-title" type="button" @click="emit('select-member', card.id)">
                <span class="chip-text">{{ titleOf(card) }}</span>
              </button>
              <button
                class="btn tiny"
                type="button"
                data-im="leave-member"
                :data-group-id="group.id"
                :data-card-id="card.id"
                @click="emit('leave', group.id, card.id)"
              >
                移出
              </button>
            </li>
            <li v-if="dropTarget && dropCardId" class="chip insert">
              <span class="chip-text">插入位置：第 {{ (dropIndex ?? displayCards.length) + 1 }} 位</span>
            </li>
          </ul>
        </div>
      </div>

      <!--
        拖入提示：独立浮标放在组条**上方**（不塞进组条里），
        所以组条文字不会被挤到显示不全，浮标也不会被组条裁切。
      -->
      <span
        v-if="dropMerge"
        class="drop-badge merge"
        data-im="group-drop-hint"
        role="status"
      >拖动中：这一组将被并入落点所在的组</span>
      <span
        v-else-if="dropTarget"
        class="drop-badge"
        data-im="group-drop-hint"
        role="status"
      >{{ insertText || "将加入这一组" }}</span>
    </div>
  </div>
</template>

<style scoped>
/*
 * 组条：比卡片轻的一条悬浮标识，不再画组框轮廓。
 * 位置由成员包围盒给出；宽度按内容自适应（有上限），不会把文字挤成省略。
 */
.group {
  position: absolute;
  z-index: 8;
  font-size: var(--fs-xs);
  pointer-events: none;
}
/* 拖入提示浮标在组条**上方**：column-reverse 让 DOM 里后出现的提示排在上面。 */
.group-body {
  display: flex;
  flex-direction: column-reverse;
  gap: var(--sp-1);
  align-items: flex-start;
}
.group-head {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 160px;
  max-width: 520px;
  padding: 2px var(--sp-2);
  pointer-events: auto;
  cursor: grab;
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  font-size: var(--fs-xs);
}
.group-head:active { cursor: grabbing; }
.group.ordered .group-head { border-color: var(--border-strong); }
.group.drop-target .group-head { border-color: var(--accent); background: var(--accent-soft); }
.group.drop-merge .group-head { border-color: var(--warning); }
.head-row { display: flex; flex-wrap: nowrap; align-items: center; gap: var(--sp-2); min-width: 0; }
.grip { flex: none; color: var(--text-muted); letter-spacing: -1px; user-select: none; pointer-events: none; }
.group-head:hover .grip { color: var(--text-secondary); }
.group-head input,
.group-head button,
.group-head .chip,
.group-head .btn { pointer-events: auto; cursor: default; }
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
  min-width: 72px;
  max-width: 160px;
  min-height: 22px;
  box-sizing: border-box;
}
.group-name:hover { border-color: var(--border-strong); }
.group-name:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.mode { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); }
.count { flex: none; color: var(--text-muted); }
.badge {
  flex: none;
  font-size: var(--fs-tech, var(--fs-xs));
  color: var(--on-accent);
  background: var(--accent);
  border-radius: var(--r-pill);
  padding: 0 var(--sp-2);
}
.badge.default-name { color: var(--text-muted); background: none; border: 1px solid var(--border-subtle); }
/*
 * 拖入提示浮标：组条上方一条独立小签；不参与组条布局，也不会被裁切。
 */
.drop-badge {
  pointer-events: none;
  margin: 0;
  padding: 0 var(--sp-2);
  font-size: var(--fs-tech, var(--fs-xs));
  color: var(--on-accent);
  background: var(--accent);
  border-radius: var(--r-pill);
  white-space: nowrap;
}
.drop-badge.merge { background: var(--warning); color: var(--bg-base); }
.drop-target .drop-badge { background: var(--accent); }
/* 成员面板按浮层读：展开时只改面板自身，不改变组条高度，也不把成员卡片挤走。 */
.group-head.tools-open { border-radius: var(--r-md); }
.group-tools {
  position: absolute;
  left: 0;
  top: calc(100% + 4px);
  width: max(360px, 100%);
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--sp-1) var(--sp-2) var(--sp-2);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  box-shadow: var(--shadow-2);
}
.tools-row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); }
.tools-hint { font-size: var(--fs-xs); color: var(--text-muted); }
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
  color: var(--text-muted);
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
  flex: none;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: none;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-1);
  min-height: 22px;
  cursor: pointer;
}
.btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
.btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.btn:disabled { opacity: 0.45; cursor: default; }
.btn.tiny { font-size: var(--fs-tech, var(--fs-xs)); }
</style>
