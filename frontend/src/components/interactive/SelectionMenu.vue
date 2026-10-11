<!--
  选中对象的整理菜单（子智能体 A 负责，契约 §9.6）。

  为什么单独一个组件：分组类操作（成组 / 加入组 / 移出组 / 解除组 / 设为有序 / 取消有序 /
  合并组 / 删除所选）不再常驻底部工具栏 —— 那里未选中时会铺一排禁用按钮。
  这些操作跟着**所选对象**出现：本组件由 BoardCard 渲染在卡片局部工具栏里，
  所以它天然靠近被选中的卡片，未选中时整块不存在。

  「多选只显示共同可用操作」的落法：
  - 作用于卡片本身的操作（移出组）要求**每一张**所选卡片都在组里，否则整条不出现；
    成组 / 删除所选本来就作用于全部所选；
  - 作用于组的操作（解除组 / 设为有序 / 取消有序 / 合并组）统一作用于所选卡片涉及的所有组，
    结果对整批一致；
  - 条件不满足的操作**不出现**（不是禁用），所以菜单里没有一排点不动的按钮。

  契约：docs/interactive-mode-contract.md §8.4.1（触发方自己调 board.ts 纯函数 + store.commit）、
  §9.6（工具栏轻、分组操作移到选中对象附近）、§1.3（组名、有序、合并规则）。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import {
  cardById,
  createGroup,
  dissolveGroup,
  groupOfCard,
  joinGroup,
  mergeGroups,
  removeCard,
  removeFromGroup,
  setGroupOrdered,
} from "../../interactive/board";
import type { BoardState } from "../../interactive/types";

const props = defineProps<{
  /** 面板向工具栏上方展开（工具栏在卡片上方时），避免盖住卡片标题、连接点与正文 */
  openUp: boolean;
}>();

const store = useInteractiveStore();
const open = ref(false);
const root = ref<HTMLElement | null>(null);
const trigger = ref<HTMLButtonElement | null>(null);
const panel = ref<HTMLElement | null>(null);
/**
  展开方式（打开时量一次再定）：
  - up / down：朝**卡片外侧**展开（父组件按卡片位置给出偏好），上下量出来的空间够就用它；
  - side-right / side-left：上方空间不够时贴着工具栏左右两侧展开 —— 这两块区域在卡片外侧，
    所以宁可换个方向，也不把面板翻到卡片身上去盖标题、连接点或正在编辑的正文。
*/
type Place = "up" | "down" | "side-left" | "side-right";
const place = ref<Place>(props.openUp ? "up" : "down");
/** 侧向展开时相对触发按钮的偏移（量的是整条工具栏的边，不是按钮的边） */
const sideOffset = ref(0);
/** 上下展开时：面板贴触发按钮的左边还是右边（贴右时向左边长，窄窗口里不出视口） */
const alignRight = ref(false);
/** 面板最高多少（按实际可用区域算，不用窗口高度减固定值） */
const panelMax = ref<number | null>(null);
/** 一次操作后的短回执（状态不能只靠颜色；说清做到了什么） */
const notice = ref("");

const board = computed<BoardState | null>(() => store.board);

/** 仍然存在的所选卡片 */
const selectedIds = computed<string[]>(() => {
  const current = board.value;
  if (!current) return [];
  return current.selection.filter((cardId) => Boolean(cardById(current, cardId)));
});

const count = computed(() => selectedIds.value.length);

/** 所选卡片涉及的组（去重，按板面顺序） */
const selectedGroups = computed(() => {
  const current = board.value;
  if (!current) return [];
  const ids = new Set<string>();
  for (const cardId of selectedIds.value) {
    const group = groupOfCard(current, cardId);
    if (group) ids.add(group.id);
  }
  return current.groups.filter((group) => !group.deleted && ids.has(group.id));
});

/** 每一张所选卡片都在某个组里 —— 「移出组」在多选下的共同可用条件 */
const allInGroups = computed(() => {
  const current = board.value;
  if (!current || !count.value) return false;
  return selectedIds.value.every((cardId) => Boolean(groupOfCard(current, cardId)));
});

/** 可以加入的组：所选卡片目前都不在里面的组（加入后结果对整批一致） */
const joinOptions = computed(() => {
  const current = board.value;
  if (!current) return [];
  const occupied = new Set(selectedGroups.value.map((group) => group.id));
  return current.groups.filter((group) => !group.deleted && !occupied.has(group.id));
});

const canForm = computed(() => count.value >= 2);
const canJoin = computed(() => joinOptions.value.length > 0);
const canLeave = computed(() => allInGroups.value);
const canDissolve = computed(() => selectedGroups.value.length > 0);
const canOrder = computed(() => selectedGroups.value.some((group) => !group.ordered));
const canUnorder = computed(() => selectedGroups.value.some((group) => group.ordered));
const canMerge = computed(() => selectedGroups.value.length > 1);
/** 没有一项可做时（例如只选了一张普通卡片）就不显示入口，避免一个点不动的按钮 */
const hasAction = computed(
  () => canForm.value || canJoin.value || canLeave.value || canDissolve.value || canOrder.value || canUnorder.value || canMerge.value || count.value > 0,
);

/** 面板的行内位置：上下展开时贴左/贴右，侧向展开时按量到的偏移贴边 */
const panelStyle = computed<Record<string, string>>(() => {
  const style: Record<string, string> = {};
  if (panelMax.value) style.maxHeight = panelMax.value + "px";
  if (place.value === "side-right") style.left = sideOffset.value + "px";
  else if (place.value === "side-left") style.right = sideOffset.value + "px";
  else if (alignRight.value) style.right = "0px";
  else style.left = "0px";
  return style;
});

const selectionText = computed(() => {
  if (!count.value) return "未选中对象";
  const names = selectedGroups.value.map((group) => group.name);
  const head = count.value === 1 ? "已选 1 张" : "已选 " + count.value + " 张";
  return names.length ? head + " · 组：" + names.join("、") : head;
});

function toggle(): void {
  open.value = !open.value;
}

function close(returnFocus = false): void {
  if (!open.value) return;
  open.value = false;
  if (returnFocus) void nextTick(() => trigger.value?.focus());
}

function onDocumentPointerDown(event: PointerEvent): void {
  const target = event.target as Node | null;
  if (target && root.value && !root.value.contains(target)) close(false);
}

/** Esc 先关这个菜单：不让板面把它当成取消拖动 */
function onDocumentKeydown(event: KeyboardEvent): void {
  if (event.key !== "Escape") return;
  event.stopPropagation();
  if (!open.value) return;
  close(true);
}

const GAP = 8;

/**
  可用的**实际**区域：窗口边界 + 所有会裁剪的祖先容器（板面滚动区是 overflow:auto）。
  实测教训：只按窗口算空间时，上方「看起来」有 116px，但菜单一展开就被板面滚动区裁到只剩 12px，
  看起来像「点了整理什么也没出现」。所以这里逐级向上取交集，不用「窗口高度减固定值」。
*/
function clipBox(element: HTMLElement): { top: number; bottom: number; left: number; right: number } {
  const box = { top: 0, bottom: window.innerHeight, left: 0, right: window.innerWidth };
  let node: HTMLElement | null = element.parentElement;
  while (node) {
    const style = getComputedStyle(node);
    const clipsY = /(auto|scroll|hidden)/.test(style.overflowY) || /(auto|scroll|hidden)/.test(style.overflowX);
    if (clipsY) {
      const rect = node.getBoundingClientRect();
      box.top = Math.max(box.top, rect.top);
      box.bottom = Math.min(box.bottom, rect.bottom);
      box.left = Math.max(box.left, rect.left);
      box.right = Math.min(box.right, rect.right);
    }
    node = node.parentElement;
  }
  return box;
}

/** 打开时量一次：面板既不出可用区域，也不盖住卡片标题、连接点与正在编辑的正文 */
async function placePanel(): Promise<void> {
  await nextTick();
  const element = panel.value;
  const button = trigger.value;
  if (!element || !button) return;
  const rect = button.getBoundingClientRect();
  const box = element.getBoundingClientRect();
  const clip = clipBox(element);
  const toolbar = typeof button.closest === "function" ? (button.closest('[data-im="card-toolbar"]') as HTMLElement | null) : null;
  const bar = toolbar ? toolbar.getBoundingClientRect() : rect;
  const want: Place = props.openUp ? "up" : "down";
  const roomUp = rect.top - clip.top - GAP;
  const roomDown = clip.bottom - rect.bottom - GAP;
  const need = Math.min(box.height, 260);
  const room = want === "up" ? roomUp : roomDown;
  if (room >= need) {
    place.value = want;
    // 实测：卡片贴近右边界时工具栏被画布往左夹，菜单仍贴按钮左边就会伸到视口外 169px
    alignRight.value = rect.left + box.width > clip.right - 12;
    panelMax.value = Math.min(320, Math.max(160, Math.floor(room)));
    return;
  }
  // 上方 / 下方装不下整份菜单：换到工具栏左右两侧（同样在卡片外侧），整体可滚动
  const rightRoom = clip.right - bar.right - GAP;
  if (rightRoom >= Math.min(box.width, 240)) {
    place.value = "side-right";
    sideOffset.value = Math.round(bar.right - rect.left + GAP);
  } else {
    place.value = "side-left";
    sideOffset.value = Math.round(rect.right - bar.left + GAP);
  }
  panelMax.value = Math.max(160, Math.floor(clip.bottom - bar.top - GAP));
}

watch(open, async (value) => {
  if (value) {
    notice.value = "";
    document.addEventListener("pointerdown", onDocumentPointerDown, true);
    document.addEventListener("keydown", onDocumentKeydown);
    await placePanel();
    panel.value?.querySelector("button")?.focus();
  } else {
    document.removeEventListener("pointerdown", onDocumentPointerDown, true);
    document.removeEventListener("keydown", onDocumentKeydown);
  }
});

onBeforeUnmount(() => {
  document.removeEventListener("pointerdown", onDocumentPointerDown, true);
  document.removeEventListener("keydown", onDocumentKeydown);
});

// --- 整理：全部自己算 next 再 commit（契约 §8.4.1） -------------------------

function formGroup(): void {
  const current = board.value;
  if (!current || !canForm.value) return;
  store.commit(createGroup(current, selectedIds.value), "把所选卡片分成一组");
  notice.value = "已把所选卡片分成一组：现在还是普通组，可以再设为有序。";
}

function joinSelected(event: Event): void {
  const select = event.target as HTMLSelectElement;
  const groupId = select.value;
  select.value = "";
  const current = board.value;
  if (!current || !groupId) return;
  const target = joinOptions.value.find((group) => group.id === groupId) ?? board.value?.groups.find((group) => group.id === groupId);
  let next = current;
  for (const cardId of selectedIds.value) next = joinGroup(next, cardId, groupId);
  store.commit(next, "把所选卡片加入组");
  notice.value = target
    ? "已加入「" + target.name + "」" + (target.ordered ? "：里面的顺序就是序号。" : "：普通组的摆放顺序不代表先后。")
    : "已把所选卡片加入组。";
}

function leaveGroups(): void {
  const current = board.value;
  if (!current || !canLeave.value) return;
  let next = current;
  for (const cardId of selectedIds.value) next = removeFromGroup(next, cardId);
  store.commit(next, "把所选卡片移出组");
  notice.value = "已把所选卡片移出组：位置不变，空掉的组会自动消失。";
}

function dissolveGroups(): void {
  const current = board.value;
  if (!current || !canDissolve.value) return;
  let next = current;
  for (const group of selectedGroups.value) next = dissolveGroup(next, group.id);
  store.commit(next, "解除组");
  notice.value = "已解除所选卡片所在的组：成员恢复自由摆放，卡片本身不动。";
}

function setOrdered(ordered: boolean): void {
  const current = board.value;
  if (!current) return;
  if (ordered ? !canOrder.value : !canUnorder.value) return;
  let next = current;
  for (const group of selectedGroups.value) next = setGroupOrdered(next, group.id, ordered);
  store.commit(next, ordered ? "设为有序组（序号表示顺序）" : "取消有序（摆放不代表先后）");
  notice.value = ordered ? "已设为有序组：组内顺序就是序号，从 1 开始。" : "已取消有序：摆放顺序不再表示先后。";
}

function mergeSelected(): void {
  const current = board.value;
  if (!current || !canMerge.value) return;
  const first = selectedGroups.value[0];
  let next = current;
  for (const group of selectedGroups.value.slice(1)) next = mergeGroups(next, group.id, first.id);
  store.commit(next, "合并组（成员连续插入）");
  notice.value = "已合并：两个有序组合并仍然有序，有序与普通组合并后是普通组。";
}

function deleteSelected(): void {
  const current = board.value;
  if (!current || !count.value) return;
  let next = current;
  for (const cardId of selectedIds.value) next = removeCard(next, cardId);
  store.commit(next, "删除所选卡片（撤回材料或注释）");
  notice.value = "已删除所选卡片：这等于撤回所选的材料或注释，保存后仍可撤销。";
}
</script>

<template>
  <span ref="root" class="sel">
    <button
      ref="trigger"
      class="trigger"
      type="button"
      data-im="selection-menu"
      aria-haspopup="menu"
      :aria-expanded="open"
      :title="hasAction ? '整理所选对象：成组、加入组、移出组、解除组、序号、合并、删除' : '所选对象没有可整理的操作'"
      @click="toggle"
    >
      整理
    </button>

    <div
      v-if="open"
      ref="panel"
      class="panel"
      :class="place"
      :style="panelStyle"
      data-im="selection-menu-list"
      role="menu"
      aria-label="整理所选对象"
      @wheel.stop
    >
      <p class="head" data-im="selection-count">{{ selectionText }}</p>

      <button v-if="canForm" class="item" type="button" role="menuitem" data-im="group-form" @click="formGroup">
        所选成组
        <span class="hint">分成一个普通组，之后可以设为有序</span>
      </button>

      <label v-if="canJoin" class="item select-item">
        <span>加入组…</span>
        <select data-im="group-join" aria-label="加入组" @change="joinSelected">
          <option value="">选择一个组</option>
          <option v-for="group in joinOptions" :key="group.id" :value="group.id">
            {{ group.name }}{{ group.ordered ? "（有序）" : "" }}
          </option>
        </select>
      </label>

      <button v-if="canLeave" class="item" type="button" role="menuitem" data-im="group-leave" @click="leaveGroups">
        移出组
        <span class="hint">卡片留在原处，空组自动消失</span>
      </button>
      <button v-if="canDissolve" class="item" type="button" role="menuitem" data-im="group-dissolve" @click="dissolveGroups">
        解除组
        <span class="hint">成员恢复自由摆放</span>
      </button>
      <button v-if="canOrder" class="item" type="button" role="menuitem" data-im="group-ordered" @click="setOrdered(true)">
        设为有序
        <span class="hint">组内顺序从此表示先后（从 1 开始）</span>
      </button>
      <button v-if="canUnorder" class="item" type="button" role="menuitem" data-im="group-unordered" @click="setOrdered(false)">
        取消有序
        <span class="hint">摆放顺序不再表示先后</span>
      </button>
      <button v-if="canMerge" class="item" type="button" role="menuitem" data-im="group-merge" @click="mergeSelected">
        合并组
        <span class="hint">所选卡片所在的几个组合成一个</span>
      </button>
      <button v-if="count" class="item danger" type="button" role="menuitem" data-im="delete-selected" @click="deleteSelected">
        删除所选
        <span class="hint">等于撤回所选的材料或注释，仍可撤销</span>
      </button>

      <p v-if="notice" class="notice" role="status">{{ notice }}</p>
    </div>
  </span>
</template>

<style scoped>
.sel { position: relative; display: inline-flex; }
.trigger {
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 1px var(--sp-2);
  cursor: pointer;
  white-space: nowrap;
}
.trigger:hover { color: var(--text-strong); border-color: var(--border-strong); }
.trigger:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.trigger[aria-expanded="true"] { color: var(--text-strong); border-color: var(--accent); background: var(--bg-inset); }

/* 面板向卡片外侧展开：工具栏在卡片上方时向上，在卡片下方时向下，不盖标题 / 连接点 / 正文 */
.panel {
  position: absolute;
  z-index: 3;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  width: min(272px, calc(100vw - var(--sp-6)));
  max-height: min(320px, 52vh);
  overflow-y: auto;
  padding: var(--sp-2);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  box-shadow: var(--elev-floating, var(--shadow-2));
  text-align: left;
}
.panel.up { bottom: calc(100% + var(--sp-2)); }
.panel.down { top: calc(100% + var(--sp-2)); }
/* 侧向展开：和工具栏同一横带，落在卡片外侧那一块空地上（左右偏移由 placePanel 量出来） */
.panel.side-right,
.panel.side-left { top: 0; }

.head {
  margin: 0 0 var(--sp-1);
  padding-bottom: var(--sp-1);
  border-bottom: 1px solid var(--border-subtle);
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.item {
  display: flex;
  flex-direction: column;
  gap: 1px;
  text-align: left;
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--text-primary);
  background: none;
  border: 1px solid transparent;
  border-radius: var(--r-xs);
  padding: var(--sp-1) var(--sp-2);
  cursor: pointer;
}
.item:hover { background: var(--bg-surface); border-color: var(--border-subtle); }
.item:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.item.danger { color: var(--danger); }
.hint { font-size: var(--fs-xs); color: var(--text-muted); }
.select-item { cursor: default; }
.select-item select {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-inset);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  padding: 1px var(--sp-1);
  max-width: 100%;
}
.notice { margin: var(--sp-1) 0 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: var(--lh-tight); }
</style>
