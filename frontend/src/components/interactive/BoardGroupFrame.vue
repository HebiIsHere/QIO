<!-- 组框（子智能体 A 负责视觉，规则见契约 §1.3 / §9.6）：组名、有序 / 普通切换、解除组、
  组内顺序与拖动插入位置提示。

  - 视觉层级：**组框比卡片更轻** —— 一条 1px 细线 + 很淡的底，没有第二层厚边框与阴影；
    正式内容用实线（虚线只留给待审批完成后消失的预览，并配文字说明）；
  - 普通组：自由摆放不表示先后 → 不显示序号，只列出成员；
  - 有序组：显示明确 1..n 序号，顺序调整可作为依据；
  - 组名：初始是系统给的默认名；输入即用；留空或取消保留默认名，组仍然成立；
  - 拖动预演时显示「将加入这一组」与**插入位置**（第 N 位），落地成事时才有；
  - 组框本身不吃指针事件（否则卡片拖不动），只有头部两行可交互；
  - 头部高度控制在 board.ts 的 GROUP_PAD_TOP（58px）以内，序号条不会压住成员卡片；
  - **常态只显示组名、成员数、是否有序**（任务书 §7.6）：改组名输入即用；
    成员管理与顺序操作（设为有序 / 解除组 / 上移下移 / 移出）收进「成员与顺序」面板，
    用户要看的时候才展开，不把天天要读的组框变成一排行操作按钮。
    面板用 v-show（不是 v-if）：顺序 chip 始终在无障碍树与既有测试的 DOM 里，
    收起只是不占版面。展开时面板按浮层读（自带底与边），不会把成员卡片的标题压花。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from "vue";
import type { BoardCard, BoardGroup } from "../../interactive/types";
import {
  GROUP_HEADER_HEIGHT,
  GROUP_PAD_BOTTOM,
  GROUP_PAD_X,
  groupOverviewSlots,
  groupResizeHandles,
  type GroupOverviewSlot,
  type GroupResizeEdge,
} from "../../interactive/linkEndpoints";
// 组内顺序 / 成员条是**横向浏览**区域：滚动能力保留，滚动条外观统一走 QIO 指示条。
import { vQioScrollbar } from "../../composables/useQioScrollbars";

const props = withDefaults(
  defineProps<{
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
    /** 组收起（Phase 2）：只影响显示，由 BoardCanvas 传入；没有这个字段的旧调用方默认展开。 */
    collapsed?: boolean;
    /** 组标题拖整组（只发意图，不改板面状态）。 */
    draggable?: boolean;
    /** 组缩放把手（右 / 下 / 右下；只发意图）。 */
    resizable?: boolean;
  }>(),
  { collapsed: false, draggable: true, resizable: true },
);

const emit = defineEmits<{
  (e: "rename", groupId: string, name: string): void;
  (e: "toggle-ordered", groupId: string, ordered: boolean): void;
  (e: "dissolve", groupId: string): void;
  (e: "move-member", groupId: string, cardId: string, index: number): void;
  (e: "leave", groupId: string, cardId: string): void;
  (e: "select-member", cardId: string): void;
  /** 收起 / 展开组（纯显示状态，不写成员 / 顺序 / 材料范围）。 */
  (e: "toggle-collapse", groupId: string, collapsed: boolean): void;
  /** 标题拖动整组：坐标是屏幕 client 坐标，位移由父组件按画布 scale 换算回板面单位。 */
  (e: "group-drag-start", groupId: string, point: { x: number; y: number }): void;
  (e: "group-drag-move", groupId: string, delta: { dx: number; dy: number }): void;
  (e: "group-drag-end", groupId: string, delta: { dx: number; dy: number }): void;
  (e: "group-drag-cancel", groupId: string): void;
  /** 组缩放把手：edge 为右 / 下 / 右下；同样是屏幕 client 位移，父组件负责提交。 */
  (e: "group-resize-start", groupId: string, edge: GroupResizeEdge, point: { x: number; y: number }): void;
  (e: "group-resize-move", groupId: string, edge: GroupResizeEdge, delta: { dx: number; dy: number }): void;
  (e: "group-resize-end", groupId: string, edge: GroupResizeEdge, delta: { dx: number; dy: number }): void;
  (e: "group-resize-cancel", groupId: string): void;
}>();

/* ── Phase 2-B：收起概览 / 标题拖整组 / 组缩放把手 ─────────────────────────
 * 组件层只负责「发出用户意图」：拖动与缩放都只发 start / move / end / cancel，
 * 由 BoardCanvas 按画布 scale 换算成板面坐标后走 store.commit —— 这里不改任何状态。
 * 收起是纯显示状态（collapsed prop），不改成员 / 顺序 / 材料范围。
 * 收起概览使用**固定槽位**：位置只由槽位序号决定，与卡片 id / 组是否有序无关，
 * 也不显示序号 —— 显示槽位不是执行顺序。
 */
/** 收起后的紧凑外框高度：组框只保留标题 + 一行固定槽位，不保留成员区域的整块空白。 */
const COLLAPSED_GROUP_H = 120;
const frameH = computed(() =>
  props.collapsed ? Math.min(COLLAPSED_GROUP_H, Math.max(56, props.group.h)) : props.group.h,
);
const overview = computed(() =>
  groupOverviewSlots({ group: { ...props.group, h: frameH.value }, cards: props.cards }),
);
const overviewLabel = computed(
  () => "组「" + (props.group.name || "未命名组") + "」成员概览（只显示成员，不代表执行顺序）",
);
const overviewStyle = computed(() => ({
  left: GROUP_PAD_X + "px",
  top: GROUP_HEADER_HEIGHT + "px",
  width: Math.max(0, props.group.w - GROUP_PAD_X * 2) + "px",
  height: Math.max(0, frameH.value - GROUP_HEADER_HEIGHT - GROUP_PAD_BOTTOM) + "px",
}));
const handles = computed(() =>
  props.resizable && !props.collapsed
    ? groupResizeHandles({ x: 0, y: 0, w: props.group.w, h: props.group.h })
    : [],
);

function handleStyle(handle: { x: number; y: number }): Record<string, string> {
  return { left: handle.x + "px", top: handle.y + "px" };
}

function slotStyle(slot: GroupOverviewSlot): Record<string, string> {
  return {
    left: slot.x - overview.value.inner.x + "px",
    top: slot.y - overview.value.inner.y + "px",
    width: slot.w + "px",
    height: slot.h + "px",
  };
}

function slotTitle(slot: GroupOverviewSlot): string | undefined {
  if (!slot.cardId) return undefined;
  const card = props.cards.find((item) => item.id === slot.cardId);
  return card ? titleOf(card) : undefined;
}

function slotText(slot: GroupOverviewSlot): string {
  if (!slot.cardId) return "";
  const card = props.cards.find((item) => item.id === slot.cardId);
  if (!card) return "";
  return titleOf(card).slice(0, 6);
}

/* ---- 手势：pointerdown 起手，move / up / cancel / Esc 都挂 document（与项目既有手势一致） ---- */
interface DragGesture {
  startX: number;
  startY: number;
  moved: boolean;
}

let dragGesture: DragGesture | null = null;
let resizeGesture: { edge: GroupResizeEdge; startX: number; startY: number } | null = null;

function clientPoint(event: MouseEvent): { x: number; y: number } {
  return { x: event.clientX, y: event.clientY };
}

function clientDelta(event: MouseEvent, startX: number, startY: number): { dx: number; dy: number } {
  return { dx: event.clientX - startX, dy: event.clientY - startY };
}

/** 输入框 / 按钮 / 链接上的按下不属于「拖标题」：它们自己的交互优先。 */
function insideControl(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  return Boolean(element?.closest("input, textarea, select, button, a, [data-no-group-drag]"));
}

function isPrimaryButton(event: MouseEvent): boolean {
  return event.button === undefined || event.button === 0;
}

function stopDragListeners(): void {
  document.removeEventListener("pointermove", onDragMove, true);
  document.removeEventListener("pointerup", onDragEnd, true);
  document.removeEventListener("pointercancel", onDragCancel, true);
  document.removeEventListener("keydown", onDragKeydown, true);
  dragGesture = null;
}

function onDragMove(event: Event): void {
  if (!dragGesture) return;
  const pointer = event as MouseEvent;
  const delta = clientDelta(pointer, dragGesture.startX, dragGesture.startY);
  if (!dragGesture.moved && Math.abs(delta.dx) + Math.abs(delta.dy) > 3) {
    dragGesture.moved = true;
  }
  if (dragGesture.moved) emit("group-drag-move", props.group.id, delta);
}

function onDragEnd(event: Event): void {
  if (!dragGesture) return;
  const pointer = event as MouseEvent;
  const delta = clientDelta(pointer, dragGesture.startX, dragGesture.startY);
  const moved = dragGesture.moved;
  stopDragListeners();
  if (moved) emit("group-drag-end", props.group.id, delta);
  else emit("group-drag-cancel", props.group.id);
}

function onDragCancel(): void {
  if (!dragGesture) return;
  stopDragListeners();
  emit("group-drag-cancel", props.group.id);
}

function onDragKeydown(event: Event): void {
  if ((event as KeyboardEvent).key !== "Escape") return;
  (event as KeyboardEvent).preventDefault();
  onDragCancel();
}

function onHeadPointerDown(event: MouseEvent): void {
  if (!props.draggable || dragGesture) return;
  if (!isPrimaryButton(event) || insideControl(event.target)) return;
  dragGesture = { startX: event.clientX, startY: event.clientY, moved: false };
  emit("group-drag-start", props.group.id, clientPoint(event));
  document.addEventListener("pointermove", onDragMove, true);
  document.addEventListener("pointerup", onDragEnd, true);
  document.addEventListener("pointercancel", onDragCancel, true);
  document.addEventListener("keydown", onDragKeydown, true);
  event.preventDefault();
}

function stopResizeListeners(): void {
  document.removeEventListener("pointermove", onResizeMove, true);
  document.removeEventListener("pointerup", onResizeEnd, true);
  document.removeEventListener("pointercancel", onResizeCancel, true);
  document.removeEventListener("keydown", onResizeKeydown, true);
  resizeGesture = null;
}

function onResizeMove(event: Event): void {
  if (!resizeGesture) return;
  emit("group-resize-move", props.group.id, resizeGesture.edge, clientDelta(event as MouseEvent, resizeGesture.startX, resizeGesture.startY));
}

function onResizeEnd(event: Event): void {
  if (!resizeGesture) return;
  const edge = resizeGesture.edge;
  const delta = clientDelta(event as MouseEvent, resizeGesture.startX, resizeGesture.startY);
  stopResizeListeners();
  emit("group-resize-end", props.group.id, edge, delta);
}

function onResizeCancel(): void {
  if (!resizeGesture) return;
  stopResizeListeners();
  emit("group-resize-cancel", props.group.id);
}

function onResizeKeydown(event: Event): void {
  if ((event as KeyboardEvent).key !== "Escape") return;
  (event as KeyboardEvent).preventDefault();
  onResizeCancel();
}

function onResizePointerDown(edge: GroupResizeEdge, event: MouseEvent): void {
  if (!props.resizable || props.collapsed || resizeGesture) return;
  if (!isPrimaryButton(event)) return;
  resizeGesture = { edge, startX: event.clientX, startY: event.clientY };
  emit("group-resize-start", props.group.id, edge, clientPoint(event));
  document.addEventListener("pointermove", onResizeMove, true);
  document.addEventListener("pointerup", onResizeEnd, true);
  document.addEventListener("pointercancel", onResizeCancel, true);
  document.addEventListener("keydown", onResizeKeydown, true);
  event.preventDefault();
  event.stopPropagation();
}

onBeforeUnmount(() => {
  stopDragListeners();
  stopResizeListeners();
});

const nameInput = ref<HTMLInputElement | null>(null);
/**
 * 「成员与顺序」面板是否展开：**只有用户点它才展开**（缺省收起）。
 * 这是纯显示状态，不进任何存储，也不改变分组 / 顺序本身。
 */
const membersOpen = ref(false);

const style = computed(() => ({
  left: props.group.x + "px",
  top: props.group.y + "px",
  width: props.group.w + "px",
  height: frameH.value + "px",
}));

const memberCards = computed(() =>
  props.group.members
    .map((id) => props.cards.find((card) => card.id === id))
    .filter((card): card is BoardCard => Boolean(card)),
);

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
    :class="{ ordered: group.ordered, 'drop-target': dropTarget, 'drop-merge': dropMerge, collapsed }"
    :style="style"
    data-im="group"
    :data-group-id="group.id"
    :data-ordered="group.ordered ? 'true' : 'false'"
    :data-collapsed="collapsed ? 'true' : 'false'"
  >
    <div
      class="group-head"
      :class="{ 'tools-open': membersOpen, grabbable: draggable }"
      data-im="group-head"
      @pointerdown="onHeadPointerDown"
    >
      <!-- 常态一行：组名（输入即用）、成员数、是否有序、以及按需展开的成员管理入口。
           组语义说明跟操作按钮挤同一行会把整行挤到三行（实测），所以说明文字留在这一行里
           但可压缩省略；真正的操作收进下面的面板。 -->
      <div class="head-row">
        <!-- 拖整组的把手：只有它是明确的「拖动标题」入口；输入框 / 按钮上的按下仍归它们自己 -->
        <span
          v-if="draggable"
          class="drag-grip"
          data-im="group-drag-handle"
          title="拖动组标题移动整组（只改位置，不改变分组关系）"
          aria-hidden="true"
        >⠿</span>
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
          v-if="!collapsed"
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
        <button
          class="btn"
          type="button"
          data-im="group-collapse-toggle"
          :data-group-id="group.id"
          :aria-expanded="collapsed ? 'false' : 'true'"
          :aria-controls="'group-body-' + group.id"
          :title="collapsed ? '展开组成员（只影响显示）' : '收起组成员（只影响显示，不改成员与顺序）'"
          @click="emit('toggle-collapse', group.id, !collapsed)"
        >
          {{ collapsed ? "展开组" : "收起组" }}
        </button>
      </div>

      <div
        v-if="!collapsed"
        v-show="membersOpen"
        :id="'group-tools-' + group.id"
        class="group-tools"
        data-im="group-tools"
      >
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

      <ol
        v-if="!collapsed && group.ordered"
        class="sequence"
        :id="'group-body-' + group.id"
        aria-label="组内顺序"
        v-qio-scrollbar="{ policy: 'auto', axis: 'x' }"
      >
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
      <ul
        v-else-if="!collapsed"
        class="sequence plain"
        :id="'group-body-' + group.id"
        aria-label="组成员"
        v-qio-scrollbar="{ policy: 'auto', axis: 'x' }"
      >
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
      收起概览：固定槽位（位置只由槽位序号决定，与卡片 id / 是否有序无关），
      不显示序号 —— 显示槽位不是执行顺序。只显示成员标题，不渲染成员卡片。
    -->
    <div
      v-if="collapsed"
      class="overview"
      data-im="group-overview"
      :aria-label="overviewLabel"
      :style="overviewStyle"
    >
      <span
        v-for="(slot, index) in overview.slots"
        :key="index"
        class="slot"
        :class="{ empty: !slot.cardId }"
        :data-card-id="slot.cardId ?? undefined"
        :data-empty="slot.cardId ? undefined : 'true'"
        :style="slotStyle(slot)"
        :title="slotTitle(slot)"
      >{{ slotText(slot) }}</span>
      <span
        v-if="overview.overflow"
        class="overview-more mono"
        data-im="group-overview-more"
      >还有 {{ overview.overflow }} 个</span>
    </div>

    <!--
      组缩放把手（右 / 下 / 右下）：只发意图，真正改成员几何由 BoardCanvas 负责；
      收起时不出现（收起态不直接量成员边界）。
    -->
    <span
      v-for="handle in handles"
      :key="handle.edge"
      class="resize-handle"
      data-im="group-resize-handle"
      :data-edge="handle.edge"
      :style="handleStyle(handle)"
      :title="handle.label"
      aria-hidden="true"
      @pointerdown="onResizePointerDown(handle.edge, $event)"
    ></span>
  </div>
</template>

<style scoped>
/*
  组框比卡片更轻：一条 1px 细线 + 一层很淡的底，没有阴影、没有第二层厚边框。
  正式内容用实线；虚线只属于「待审批完成后才会出现的预览」，并且一定配文字说明。
*/
.group {
  container-type: inline-size;
  position: absolute;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  background: var(--bg-inset);
  pointer-events: none; /* 卡片要能拖动：只有头部两行接管指针 */
  z-index: 2;
}
.group.ordered { border-color: var(--border-strong); }
.group.drop-target { border-color: var(--accent); background: var(--accent-soft); }
.group.drop-merge { border-color: var(--warning); }
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
  gap: 1px;
  max-height: 56px; /* 与 board.ts 的 GROUP_PAD_TOP 对齐，不压住成员卡片 */
  overflow: hidden;
  padding: 2px var(--sp-2);
  /**
   * 组操作的层级（V1）：组框本身必须比卡片轻（z-index: 2，卡片 4），
   * 否则会反过来盖住卡片标题与拖动区；但**头部**是用户要点的操作，
   * 必须画在卡片与关系线（3）之上，否则卡片贴到组框顶边时操作就被压住、点不到。
   *
   * z-index 只加在头部、不改 .group：整组抬高会遮挡卡片，头部抬高只影响这条 58px 预留带
   * （board.ts 的 GROUP_PAD_TOP：卡片的最小 y 就在这条带下面）。
   *
   * 头部本身 pointer-events: none：**头部空白处不接管指针**，卡片压上来时仍可点卡片、可拖动；
   * 只有按钮 / 输入框 / 序号 chip 这些子元素接管（它们是用户明确要用的操作）。
   */
  position: relative;
  z-index: 8;
  pointer-events: none;
  /**
   * 头部带一层很淡的底：卡片万一压到同一条带上也不会两层文字叠着读不请。
   * 只到组框自己的底色一档，不加边界、不加阴影，保持「组框比卡片轻」。
   */
  background: var(--bg-inset);
  border-bottom: 1px solid var(--border-subtle);
  border-radius: var(--r-lg) var(--r-lg) 0 0;
  font-size: var(--fs-xs);
}
/*
 * 单行不折行：成员数 / 系统默认名 / 拖动提示再多也不能把这一行挤成三行
 * （头部高度是固定的 56px，被挤满就没有成员那一行了）。宽度不够时先压缩说明文字。
 */
.head-row { display: flex; flex-wrap: nowrap; align-items: center; gap: var(--sp-2); min-width: 0; }
/*
 * 展开「成员与顺序」时头部才允许长高：收起时仍然是被 GROUP_PAD_TOP（58px）约束的一行，
 * 成员卡片的标题不会被挤掉。展开的面板按浮层读（自带底 / 边 / 阴影），
 * 即使盖在成员卡片上方也能读清，收起即恢复。
 */
.group-head.tools-open { max-height: none; overflow: visible; }
.group-tools {
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
/* 头部里所有可操作子元素重新接管指针（父级是 none，保证空白处不挡卡片） */
.group-head > *,
.group-head button,
.group-head input,
.group-head .chip,
.group-head .btn { pointer-events: auto; }
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
  成员 / 顺序条：短的横向浏览区（头部高度固定 58px，不能靠换行长高）。
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

/* ---- Phase 2-B：拖整组 / 收起概览 / 组缩放把手 ------------------------- */
.group-head.grabbable { cursor: grab; }
.group-head.grabbable:active { cursor: grabbing; }
.drag-grip {
  flex: none;
  pointer-events: auto;
  cursor: grab;
  user-select: none;
  color: var(--text-faint);
  font-size: var(--fs-xs);
  line-height: 1;
  padding: 0 2px;
}
.drag-grip:hover { color: var(--text-secondary); }

/* 收起态：头部保持一行；成员概览按固定槽位绝对定位，不把成员卡片画进组框。 */
.group.collapsed .group-head { max-height: 56px; }
.overview {
  position: absolute;
  pointer-events: none;
  z-index: 1;
}
.overview .slot {
  position: absolute;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  background: var(--bg-surface);
  color: var(--text-muted);
  font-size: var(--fs-tech, var(--fs-xs));
  padding: 0 2px;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.overview .slot.empty { border-style: dashed; background: none; }
.overview-more {
  position: absolute;
  right: var(--sp-1);
  bottom: var(--sp-1);
  color: var(--text-muted);
  font-size: var(--fs-tech, var(--fs-xs));
}

/* 缩放把手：12px 圆点，视觉上明确可拖；只接管自己那一小块指针事件。 */
.resize-handle {
  position: absolute;
  width: 12px;
  height: 12px;
  margin-left: -6px;
  margin-top: -6px;
  border-radius: 50%;
  border: 1px solid var(--border-strong);
  background: var(--bg-elevated);
  pointer-events: auto;
  z-index: 9;
}
.resize-handle[data-edge="e"] { cursor: ew-resize; }
.resize-handle[data-edge="s"] { cursor: ns-resize; }
.resize-handle[data-edge="se"] { cursor: nwse-resize; }
.resize-handle:hover { border-color: var(--accent); }

@media (max-width: 900px) {
  /* 窄窗：说明性文字让位给组名、成员管理与收起组；有序状态仍由序号表达。 */
  .mode,
  .badge.default-name { display: none; }
}

/* 组框本身很窄时同样让位：组名、成员数与两个必要动作优先，不让按钮被裁掉。 */
@container (max-width: 560px) {
  .head-row { gap: 4px; }
  .head-row .mode,
  .head-row .badge.default-name { display: none; }
  .group-name { max-width: 96px; min-width: 56px; }
  .btn { padding: 0 4px; }
}
</style>
