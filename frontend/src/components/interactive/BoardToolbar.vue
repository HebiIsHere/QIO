<!--
  底部横向悬浮工具栏（子智能体 A 负责）。

  契约：docs/interactive-mode-contract.md
  - §8.1：板面为主体，操作浮在板面上，**没有常驻右侧栏**；底部横向悬浮工具栏（添加菜单 / 整理 /
    选择 / 撤销重做 / 板内搜索），**右端是提交区**，与编辑操作留出间距并区分样式；
  - §8.2：切换查看方式（模式）不形成表达、不调用 QIO、不触发保存；
  - §8.4.1：会改板面状态的操作由触发方自己调 board.ts 纯函数 + store.commit；指针模式走 store；
    一次性定位走 window 事件 qio:interactive:locate-card。

  自包含（本轮改版的核心）：不再接收任何 props ——
  选中集合读 store.board.selection，分组读 store.board.groups，指针模式读/写 store.boardMode，
  撤销/重做直接调 store.undo() / store.redo()，添加卡片在 AddMenu 里自己 commit。
  右端渲染 C 的 <SubmitCluster />：提交与保存是两条独立路径，样式上也与编辑操作分开。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
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
import AddMenu from "./AddMenu.vue";
import BoardSearchPanel from "./BoardSearchPanel.vue";
import SubmitCluster from "./SubmitCluster.vue";

type BoardMode = "select" | "rect" | "link";

const store = useInteractiveStore();

/** 板内搜索浮层是否展开（只影响查看，不改板面数据）。 */
const searchOpen = ref(false);
/** 「加入组」下拉当前选中的组；选中即执行，不额外点一次确认。 */
const joinTarget = ref("");
/** 一次操作后的文字说明（状态不能只靠颜色；几秒后自动收起，不长期占位置）。 */
const notice = ref("");
let noticeTimer: ReturnType<typeof setTimeout> | null = null;

function say(text: string): void {
  notice.value = text;
  if (noticeTimer) clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => {
    notice.value = "";
  }, 6000);
}

onBeforeUnmount(() => {
  if (noticeTimer) clearTimeout(noticeTimer);
});

const board = computed<BoardState | null>(() => store.board);

/** 仍然存在的选中卡片（已删除的不算）。 */
const selection = computed<string[]>(() => {
  const current = board.value;
  if (!current) return [];
  return current.selection.filter((cardId) => Boolean(cardById(current, cardId)));
});

/** 选中卡片所在的组（去重）。 */
const selectedGroupIds = computed<string[]>(() => {
  const current = board.value;
  if (!current) return [];
  const ids = new Set<string>();
  for (const cardId of selection.value) {
    const group = groupOfCard(current, cardId);
    if (group) ids.add(group.id);
  }
  return [...ids];
});

const selectedGroups = computed(() =>
  (board.value?.groups ?? []).filter((group) => !group.deleted && selectedGroupIds.value.includes(group.id)),
);

/** 可加入的组（未删除）。 */
const groupOptions = computed(() => (board.value?.groups ?? []).filter((group) => !group.deleted));

const hasSelection = computed(() => selection.value.length > 0);
/** 成组至少要两张卡片：一张卡片的「组」没有意义。 */
const canForm = computed(() => selection.value.length >= 2);
const canLeave = computed(() =>
  selection.value.some((cardId) => Boolean(board.value && groupOfCard(board.value, cardId))),
);
const canDissolve = computed(() => selectedGroups.value.length > 0);
const canOrder = computed(() => selectedGroups.value.some((group) => !group.ordered));
const canUnorder = computed(() => selectedGroups.value.some((group) => group.ordered));
const canMerge = computed(() => selectedGroups.value.length > 1);
const mode = computed<BoardMode>(() => store.boardMode);

// 组没了（解除 / 合并 / 撤销）就把下拉里的失效选项清掉，避免下一次点击指向不存在的组。
watch(groupOptions, (groups) => {
  if (joinTarget.value && !groups.some((group) => group.id === joinTarget.value)) joinTarget.value = "";
});

// --- 整理：全部自己算 next 再 commit（契约 §8.4.1） -------------------------

function formGroup(): void {
  const current = board.value;
  if (!current || !canForm.value) return;
  store.commit(createGroup(current, selection.value), "把所选卡片分成一组");
  say("已把所选卡片分成一组：用默认组名，是普通组（摆放顺序不代表先后）。");
}

function joinSelected(groupId: string): void {
  const current = board.value;
  if (!current || !groupId || !hasSelection.value) return;
  const target = groupOptions.value.find((group) => group.id === groupId);
  let next = current;
  for (const cardId of selection.value) next = joinGroup(next, cardId, groupId);
  store.commit(next, "把所选卡片加入组");
  say(
    target
      ? "已把所选卡片加入「" +
          target.name +
          "」" +
          (target.ordered ? "（有序组：列表顺序就是序号）。" : "（普通组：摆放顺序不代表先后）。")
      : "已把所选卡片加入组。",
  );
}

function onJoinChange(event: Event): void {
  const value = (event.target as HTMLSelectElement).value;
  joinTarget.value = value;
  if (value) joinSelected(value);
}

function leaveGroups(): void {
  const current = board.value;
  if (!current || !canLeave.value) return;
  let next = current;
  for (const cardId of selection.value) next = removeFromGroup(next, cardId);
  store.commit(next, "把所选卡片移出组");
  say("已把所选卡片移出组：卡片位置不变，组空了会自动消失。");
}

function dissolveGroups(): void {
  const current = board.value;
  if (!current || !canDissolve.value) return;
  let next = current;
  for (const groupId of selectedGroupIds.value) next = dissolveGroup(next, groupId);
  store.commit(next, "解除组");
  say("已解除组：成员恢复自由摆放，卡片本身不动。");
}

function setOrdered(ordered: boolean): void {
  const current = board.value;
  if (!current) return;
  if (ordered ? !canOrder.value : !canUnorder.value) return;
  let next = current;
  for (const groupId of selectedGroupIds.value) next = setGroupOrdered(next, groupId, ordered);
  store.commit(next, ordered ? "设为有序组（序号表示顺序）" : "取消有序（摆放不代表先后）");
  say(ordered ? "已设为有序组：列表顺序就是序号（从 1 开始）。" : "已取消有序：摆放顺序不再表示先后。");
}

function mergeSelected(): void {
  const current = board.value;
  if (!current || !canMerge.value) return;
  const targets = selectedGroupIds.value;
  const target = targets[0];
  let next = current;
  for (const groupId of targets.slice(1)) next = mergeGroups(next, groupId, target);
  store.commit(next, "合并组（新组用默认名，成员连续插入）");
  say("已合并组：合并后的组用默认名，成员连续插入；两个有序组合并仍有序，有序与普通组合并变普通组。");
}

function deleteSelected(): void {
  const current = board.value;
  if (!current || !hasSelection.value) return;
  let next = current;
  for (const cardId of selection.value) next = removeCard(next, cardId);
  store.commit(next, "删除所选卡片（撤回材料或注释）");
  say("已删除所选卡片：删除等于撤回这条材料或注释，保存后仍可撤销。");
}

// --- 查看方式 -------------------------------------------------------------

function setMode(next: BoardMode): void {
  if (store.boardMode === next) return;
  store.setBoardMode(next);
  const hints: Record<BoardMode, string> = {
    select: "单选 / 多选：点卡片选择，Shift 加选；拖动空白处是平移。",
    rect: "区域选择：拖动空白处框选卡片（不用按空格）。",
    link: "关系模式：从卡片的连接点拖到目标卡片建链，拖到无效位置不建链。",
  };
  say(hints[next]);
}

function toggleSearch(): void {
  searchOpen.value = !searchOpen.value;
}
</script>

<template>
  <div class="board-toolbar" data-im="board-toolbar" role="toolbar" aria-label="板面操作">
    <p v-if="notice" class="tb-notice" role="status">{{ notice }}</p>

    <div class="tb-main">
      <AddMenu />

      <span class="tb-sep" aria-hidden="true"></span>

      <div class="tb-cluster">
        <span class="tb-label">整理</span>
        <button
          class="tb-btn"
          type="button"
          data-im="group-form"
          :disabled="!canForm"
          :title="canForm ? '把所选卡片分成一组（默认组名，普通组）' : '先选两张以上卡片才能成组'"
          @click="formGroup"
        >
          所选成组
        </button>
        <select
          class="tb-select"
          data-im="group-join"
          :value="joinTarget"
          :disabled="!hasSelection || !groupOptions.length"
          :title="groupOptions.length ? '把所选卡片加入某个组' : '板面上还没有组'"
          aria-label="加入组"
          @change="onJoinChange"
        >
          <option value="">加入组…</option>
          <option v-for="group in groupOptions" :key="group.id" :value="group.id">
            {{ group.name }}{{ group.ordered ? "（有序）" : "" }}
          </option>
        </select>
        <button
          class="tb-btn"
          type="button"
          data-im="group-leave"
          :disabled="!canLeave"
          :title="canLeave ? '把所选卡片移出所在的组' : '所选卡片都不在任何组里'"
          @click="leaveGroups"
        >
          移出组
        </button>
        <button
          class="tb-btn"
          type="button"
          data-im="group-dissolve"
          :disabled="!canDissolve"
          :title="canDissolve ? '解除所选卡片所在的组（成员恢复自由摆放）' : '所选卡片不在任何组里'"
          @click="dissolveGroups"
        >
          解除组
        </button>
        <button
          class="tb-btn"
          type="button"
          data-im="group-ordered"
          :disabled="!canOrder"
          :title="canOrder ? '设为有序组：列表顺序就是序号（从 1 开始）' : '所选组已经是有序组'"
          @click="setOrdered(true)"
        >
          设为有序
        </button>
        <button
          class="tb-btn"
          type="button"
          data-im="group-unordered"
          :disabled="!canUnorder"
          :title="canUnorder ? '取消有序：摆放顺序不再表示先后' : '所选组都是普通组'"
          @click="setOrdered(false)"
        >
          取消有序
        </button>
        <button
          class="tb-btn"
          type="button"
          data-im="group-merge"
          :disabled="!canMerge"
          :title="canMerge ? '把所选卡片所在的多个组合并成一个' : '合并组需要所选卡片分布在两个以上的组里'"
          @click="mergeSelected"
        >
          合并组
        </button>
        <button
          class="tb-btn danger"
          type="button"
          data-im="delete-selected"
          :disabled="!hasSelection"
          :title="hasSelection ? '删除所选卡片（撤回材料或注释）' : '先选择卡片'"
          @click="deleteSelected"
        >
          删除所选
        </button>
      </div>

      <span class="tb-sep" aria-hidden="true"></span>

      <div class="tb-cluster">
        <span class="tb-label">选择</span>
        <button
          class="tb-btn"
          type="button"
          data-im="mode-select"
          :class="{ on: mode === 'select' }"
          :aria-pressed="mode === 'select'"
          title="单选 / 多选：点卡片选择，Shift 加选"
          @click="setMode('select')"
        >
          单选/多选
        </button>
        <button
          class="tb-btn"
          type="button"
          data-im="mode-rect"
          :class="{ on: mode === 'rect' }"
          :aria-pressed="mode === 'rect'"
          title="区域选择：拖动空白处框选卡片"
          @click="setMode('rect')"
        >
          区域选择
        </button>
        <button
          class="tb-btn"
          type="button"
          data-im="mode-link"
          :class="{ on: mode === 'link' }"
          :aria-pressed="mode === 'link'"
          title="关系模式：从连接点拖到另一张卡片建链"
          @click="setMode('link')"
        >
          关系模式
        </button>
      </div>

      <span class="tb-sep" aria-hidden="true"></span>

      <div class="tb-cluster">
        <button
          class="tb-btn"
          type="button"
          data-im="undo"
          :disabled="!store.canUndo"
          title="撤销上一步板面操作"
          @click="store.undo()"
        >
          撤销
        </button>
        <button
          class="tb-btn"
          type="button"
          data-im="redo"
          :disabled="!store.canRedo"
          title="重做被撤销的操作"
          @click="store.redo()"
        >
          重做
        </button>
        <button
          class="tb-btn"
          type="button"
          data-im="search-toggle"
          :class="{ on: searchOpen }"
          :aria-pressed="searchOpen"
          title="板内搜索：只查你自己的板面"
          @click="toggleSearch"
        >
          板内搜索
        </button>
      </div>

      <span class="tb-count mono" aria-live="polite">已选 {{ selection.length }}</span>

      <!-- 提交区（C 负责）：与编辑操作留出间距并区分样式，避免误点 -->
      <div class="tb-submit">
        <SubmitCluster />
      </div>
    </div>

    <BoardSearchPanel v-if="searchOpen" @close="searchOpen = false" />
  </div>
</template>

<style scoped>
/*
  悬浮在板面底部居中：position:absolute + left:50% + translateX(-50%) + bottom:16px。
  --tb-chat-reserve 是给右下角聊天入口留的横向空间：工具栏在**剩余空间**里居中，
  右端不会压住聊天按钮。数值与 styles/interactive-shell.css 里的 --im-chat-reserve 一致
  （shell.css 由主智能体在集成时导入，两处必须同值；这里是未导入时的兜底）。
  背景只用令牌，不做重度模糊。
*/
.board-toolbar {
  --tb-chat-reserve: 132px;
  --tb-max-width: 1180px;
  position: absolute;
  left: calc(50% - var(--tb-chat-reserve) / 2);
  transform: translateX(-50%);
  bottom: var(--sp-4);
  max-width: min(var(--tb-max-width), calc(100% - var(--tb-chat-reserve) - var(--sp-8)));
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  box-shadow: var(--shadow-2);
  z-index: 30;
  font-family: var(--sans);
}

.tb-notice {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  max-width: 78ch;
}

.tb-main {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-1) var(--sp-2);
}

.tb-cluster { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-1); }
.tb-label { font-size: var(--fs-xs); color: var(--text-faint); margin-right: 2px; }
.tb-sep { width: 1px; height: 18px; background: var(--border-subtle); flex: none; }
.tb-count { font-size: var(--fs-xs); color: var(--text-muted); }

.tb-btn {
  font: inherit;
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 2px var(--sp-2);
  cursor: pointer;
  white-space: nowrap;
}
.tb-btn:hover:not(:disabled) { color: var(--text-strong); border-color: var(--border-strong); }
.tb-btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.tb-btn:disabled { opacity: 0.45; cursor: default; }
.tb-btn.on { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }
.tb-btn.danger:not(:disabled) { color: var(--danger); }

.tb-select {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-inset);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  padding: 2px var(--sp-1);
  max-width: 128px;
}
.tb-select:disabled { opacity: 0.45; }

/* 右端提交区：和编辑操作之间有一条分隔线，样式也交给 C 的 SubmitCluster（主色按钮） */
.tb-submit {
  margin-left: auto;
  display: flex;
  align-items: center;
  padding-left: var(--sp-3);
  border-left: 1px solid var(--border-strong);
}

/* 1024×768：标签与间距收一档，按钮全部保留（主要入口必须可见可点） */
@media (max-width: 1024px) {
  .board-toolbar {
    --tb-chat-reserve: 104px;
    --tb-max-width: 100%;
    bottom: var(--sp-3);
    padding: var(--sp-2);
  }
  .tb-label { display: none; }
}
/* 800×600：隐藏纯装饰的「已选 N」计数与分隔线，按钮继续换行显示 */
@media (max-width: 800px) {
  .board-toolbar {
    --tb-chat-reserve: 88px;
    bottom: var(--sp-2);
  }
  .tb-count { display: none; }
  .tb-sep { display: none; }
  .tb-btn { padding: 2px 5px; }
  .tb-submit { padding-left: var(--sp-2); }
}
</style>
