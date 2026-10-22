<!--
  组框（问题 4c 彻底修法，2026-10-22 用户拍板）：

  - 组框不再是矩形包围盒，而是**贴合成员的并集轮廓**：只描成员自己的扩张区边界，
    组外卡片（除非落进成员 16px 扩张带 / 顶行 58px 头部带）不会被围进组里；
  - 轮廓由 groupOutline 纯函数按成员**显示矩形**算出：拖动成员 / 整组时实时跟随；
  - 组名 / 有序 / 成员与顺序等操作收进轮廓上方的浮动头部；按住头部空白处拖动 = 整组拖动
    （拖动由 BoardCanvas 处理并作为一次板面变更提交）；
  - 轮廓与填充不吃指针（pointer-events: none），只有头部子控件接管，成员卡片照常可拖；
  - 正式内容用实线；虚线只属于待审批预览。
-->
<script setup lang="ts">
import { computed, nextTick, ref } from "vue";
import type { BoardCard, BoardGroup } from "../../interactive/types";
import { groupOutline } from "../../interactive/groupOutline";
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
  /**
   * 是否显示贴合轮廓（方案 A）：常驻只有组条 + 成员标记，
   * 悬停组条 / 悬停成员 / 选中成员 / 组拖动或落点预演时才显示轮廓。
   */
  active?: boolean;
}>();

const emit = defineEmits<{
  (e: "rename", groupId: string, name: string): void;
  (e: "toggle-ordered", groupId: string, ordered: boolean): void;
  (e: "dissolve", groupId: string): void;
  (e: "move-member", groupId: string, cardId: string, index: number): void;
  (e: "leave", groupId: string, cardId: string): void;
  (e: "select-member", cardId: string): void;
  /** 按住头部拖动整组（由 BoardCanvas 处理：实时预览 + 松手一次提交） */
  (e: "drag-start", groupId: string, event: PointerEvent): void;
  /** 悬停组条（进入 / 离开）：由 BoardCanvas 决定是否临时显示轮廓 */
  (e: "hover", groupId: string, entering: boolean): void;
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

/** 贴合成员的轮廓（没有成员时返回 null：不画空框）。 */
const outline = computed(() =>
  groupOutline(
    memberCards.value.map((card) => ({
      cardId: card.id,
      rect: { x: card.x, y: card.y, w: card.w, h: card.h },
    })),
  ),
);

const svgStyle = computed<Record<string, string>>(() => {
  const result = outline.value;
  if (!result) return { display: "none" } as Record<string, string>;
  return {
    left: result.bounds.x + "px",
    top: result.bounds.y + "px",
    width: Math.max(1, result.bounds.w) + "px",
    height: Math.max(1, result.bounds.h) + "px",
  };
});

const headerStyle = computed<Record<string, string>>(() => {
  const result = outline.value;
  if (!result) return { display: "none" } as Record<string, string>;
  return {
    left: result.header.x + "px",
    top: result.header.y + "px",
    width: Math.max(120, result.header.w) + "px",
  };
});

/** 轮廓只做“交互态”提示（方案 A）：常驻不画，避免把组表达成一个几何边界。 */
const showBoundary = computed(() => Boolean(props.active || props.dropTarget || props.dropMerge));

const segments = computed(() => {
  const result = outline.value;
  if (!result) return [];
  const dx = result.bounds.x;
  const dy = result.bounds.y;
  return result.segments.map((segment) => ({
    x1: segment.x1 - dx,
    y1: segment.y1 - dy,
    x2: segment.x2 - dx,
    y2: segment.y2 - dy,
  }));
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

/** 按住头部（空白处）开始整组拖动；点输入框 / 按钮 / 成员面板不进入拖动。 */
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
  <template v-if="outline">
    <svg
      v-if="showBoundary"
      class="group-outline"
      :class="{ ordered: group.ordered, 'drop-target': dropTarget, 'drop-merge': dropMerge }"
      :style="svgStyle"
      aria-hidden="true"
      data-im="group-outline"
      :data-group-id="group.id"
    >
      <line
        v-for="(segment, index) in segments"
        :key="'seg-' + index"
        class="group-segment"
        :x1="segment.x1"
        :y1="segment.y1"
        :x2="segment.x2"
        :y2="segment.y2"
      />
    </svg>

    <div
      class="group"
      :class="{ ordered: group.ordered, 'drop-target': dropTarget, 'drop-merge': dropMerge }"
      :style="headerStyle"
      data-im="group"
      :data-group-id="group.id"
      :data-ordered="group.ordered ? 'true' : 'false'"
      @pointerenter="emit('hover', group.id, true)"
      @pointerleave="emit('hover', group.id, false)"
    >
      <div
        class="group-head"
        :class="{ 'tools-open': membersOpen }"
        data-im="group-head"
        title="按住组条拖动整组"
        @pointerdown="onHeaderPointerDown"
      >
        <!-- 常态一行：拖动把手、组名（输入即用）、成员数、是否有序、以及按需展开的成员管理入口。 -->
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
          <span v-if="dropMerge" class="badge merge">拖动中：这一组将被并入落点所在的组</span>
          <span v-else-if="dropTarget" class="badge" data-im="group-drop-hint">
            {{ insertText || "将加入这一组" }}
          </span>
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
    </div>
  </template>
</template>

<style scoped>
/*
  组框比卡片更轻：一条 1px 细线 + 一层很淡的底，没有阴影、没有第二层厚边框。
  正式内容用实线；虚线只属于「待审批完成后才会出现的预览」，并且一定配文字说明。
*/
.group-outline {
  position: absolute;
  overflow: visible;
  pointer-events: none;
  z-index: 2;
}
.group-segment {
  stroke: var(--border-subtle);
  stroke-width: 1.25;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.group-outline.ordered .group-segment { stroke: var(--border-strong); }
.group-outline.drop-target .group-segment { stroke: var(--accent); }
.group-outline.drop-merge .group-segment { stroke: var(--warning); }
/*
 * 头部是浮动小条，放在轮廓顶部的留白带里：只改显示、不影响成员卡片。
 * 按住头部空白处 = 拖动整组；输入框 / 按钮 / 成员面板照常各自操作。
 */
.group {
  position: absolute;
  z-index: 8;
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: var(--fs-xs);
  pointer-events: none;
}
/*
 * 组条（方案 A）：常驻的“组身份 + 拖动把手”。
 * 比卡片轻：薄边、无阴影、小字号；整条都是拖动热区（输入框 / 按钮除外）。
 */
.group-head {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 2px var(--sp-2);
  pointer-events: auto;
  cursor: grab;
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  font-size: var(--fs-xs);
  overflow: hidden;
}
.group-head:active { cursor: grabbing; }
.group.ordered .group-head { border-color: var(--border-strong); }
.group.drop-target .group-head { border-color: var(--accent); background: var(--accent-soft); }
.group.drop-merge .group-head { border-color: var(--warning); }
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
/* 单行不折行：成员数 / 系统默认名 / 拖动提示再多也不能把头部挤成多行 */
.head-row { display: flex; flex-wrap: nowrap; align-items: center; gap: var(--sp-2); min-width: 0; }
/*
 * 展开「成员与顺序」时面板按浮层读（自带底 / 边 / 阴影），不改变头部高度、
 * 也不把成员卡片挤走；收起即恢复。
 */
.group-head.tools-open { overflow: visible; border-radius: var(--r-md); }
.group-tools {
  position: absolute;
  left: 0;
  top: calc(100% + 4px);
  width: max(340px, 100%);
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 2px;
  padding: var(--sp-1) var(--sp-2) var(--sp-2);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  box-shadow: var(--shadow-2);
}
.tools-row { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); }
.tools-hint { font-size: var(--fs-xs); color: var(--text-muted); }
/* 头部里所有可操作子元素重新接管指针（父级 none，保证空白处不挡卡片） */
.group-head input,
.group-head button,
.group-head .chip,
.group-head .btn { pointer-events: auto; cursor: default; }
/* 抓握图标只是拖动提示：不单独接管指针，整条组条都能拖 */
.grip {
  flex: none;
  color: var(--text-muted);
  letter-spacing: -1px;
  user-select: none;
  pointer-events: none;
}
.group-head:hover .grip { color: var(--text-secondary); }
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
  max-width: 140px;
  min-height: 22px;
  box-sizing: border-box;
}
.group-name:hover { border-color: var(--border-strong); }
.group-name:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
/* 说明行自己一行：可压缩、可省略，不跟按钮抢同一行的宽度 */
.mode { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); }
/* 成员数量是次级信息：小、灰，但不至于看不清 */
.count { flex: none; color: var(--text-muted); }
/*
  成员 / 顺序条：短的横向浏览区。
  横向滚动能力保留，滚动条外观由统一 QIO 指示条负责（不占布局列、默认隐藏）。
*/
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
  /* 操作按钮要能稳定点中：给足高度，不被同行的说明文字挤扁 */
  min-height: 22px;
  cursor: pointer;
}
.btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
.btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.btn:disabled { opacity: 0.45; cursor: default; }
.btn.tiny { font-size: var(--fs-tech, var(--fs-xs)); }
</style>
