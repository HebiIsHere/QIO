<!-- 互动板面画布（子智能体 A 负责）：卡片、组框、关系连线、拖动、选择、板内搜索。

  契约：docs/interactive-mode-contract.md §1.2 / §1.3 / §4.3 / §4.4。
  三条硬规则在这里落地：
  1. 拖动期间只预演（previewDrop），松手才 dropCard 并提交；Esc / 中断丢弃本地拖动状态即可，
     因为板面状态从头到尾没被改过，卡片自然回到操作前位置。
  2. 组件绝不直接改 store.board：所有变化都算成 next 之后调 store.commit(next, 中文说明)。
  3. 位置只影响显示，不构成意图依据；链接方向只表示用户写明的方向，不推断因果 / 先后。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import {
  addCard,
  addLink,
  cardById,
  CARD_KIND_LABELS,
  createGroup,
  dissolveGroup,
  dropCard,
  duplicateCard,
  groupById,
  groupOfCard,
  joinGroup,
  mergeGroups,
  previewDrop,
  removeCard,
  renameGroup,
  removeFromGroup,
  removeLink,
  selectInRect,
  setBookmark,
  setChecked,
  setFolded,
  setGroupOrdered,
  setHidden,
  setSelection,
  updateCard,
  updateLink,
  type DropPreview,
} from "../../interactive/board";
import type { BoardCard as BoardCardModel, BoardState, CardKind } from "../../interactive/types";
import BoardCard from "./BoardCard.vue";
import BoardGroupFrame from "./BoardGroupFrame.vue";
import BoardLinkLayer from "./BoardLinkLayer.vue";
import BoardSearchPanel from "./BoardSearchPanel.vue";
import BoardToolbar from "./BoardToolbar.vue";

/** 板面坐标系大小：卡片位置是板面状态，滚动只是查看方式。 */
const SURFACE_W = 2400;
const SURFACE_H = 1600;

type BoardMode = "select" | "rect" | "link";
type GroupOp = "form" | "join" | "leave" | "dissolve" | "ordered" | "unordered" | "merge";

interface DragState {
  cardId: string;
  offsetX: number;
  offsetY: number;
  x: number;
  y: number;
  preview: DropPreview;
  fromGroupId: string | null;
}

interface RectDrag {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  additive: boolean;
}

const store = useInteractiveStore();
const boardState = computed<BoardState | null>(() => store.board);

const surface = ref<HTMLElement | null>(null);
const viewport = ref<HTMLElement | null>(null);
const mode = ref<BoardMode>("select");
const dragging = ref<DragState | null>(null);
const rectSelect = ref<RectDrag | null>(null);
const linkSource = ref<string | null>(null);
const editingLinkId = ref<string | null>(null);
const linkMeaning = ref("");
const linkDirection = ref(false);
const notice = ref("");
const highlightId = ref<string | null>(null);
let highlightTimer: ReturnType<typeof setTimeout> | null = null;

const liveCards = computed(() => (boardState.value?.cards ?? []).filter((card) => !card.deleted));
const groups = computed(() => boardState.value?.groups ?? []);
const links = computed(() => boardState.value?.links ?? []);
const selection = computed(() => boardState.value?.selection ?? []);

const selectedGroupIds = computed(() => {
  const current = boardState.value;
  if (!current) return [] as string[];
  const ids = new Set<string>();
  for (const cardId of current.selection) {
    const group = groupOfCard(current, cardId);
    if (group) ids.add(group.id);
  }
  return [...ids];
});

function cardSummary(card: BoardCardModel | null | undefined): string {
  if (!card) return "（卡片）";
  const text = (card.content || String(card.meta?.name ?? card.meta?.title ?? "")).trim();
  if (!text) return CARD_KIND_LABELS[card.kind];
  return text.length > 14 ? text.slice(0, 14) + "…" : text;
}

function groupIdOf(cardId: string): string | null {
  const current = boardState.value;
  if (!current) return null;
  const group = groupOfCard(current, cardId);
  return group ? group.id : null;
}

function groupNameOf(cardId: string): string | null {
  const current = boardState.value;
  if (!current) return null;
  const group = groupOfCard(current, cardId);
  return group ? group.name : null;
}

function displayX(card: BoardCardModel): number {
  const drag = dragging.value;
  return drag && drag.cardId === card.id ? drag.x : card.x;
}

function displayY(card: BoardCardModel): number {
  const drag = dragging.value;
  return drag && drag.cardId === card.id ? drag.y : card.y;
}

function toBoardPoint(event: PointerEvent | MouseEvent): { x: number; y: number } {
  const element = surface.value;
  if (!element) return { x: 0, y: 0 };
  const rect = element.getBoundingClientRect();
  return { x: event.clientX - rect.left, y: event.clientY - rect.top };
}

function commit(next: BoardState, label: string) {
  store.commit(next, label);
}

// --- 拖动：预演 → 放下 / 中断 ---------------------------------------------

function onDragStart(cardId: string, event: PointerEvent) {
  const current = boardState.value;
  if (!current) return;
  if (mode.value === "link") {
    pickLinkCard(cardId);
    return;
  }
  const card = cardById(current, cardId);
  if (!card) return;
  const point = toBoardPoint(event);
  const from = groupOfCard(current, cardId);
  dragging.value = {
    cardId,
    offsetX: point.x - card.x,
    offsetY: point.y - card.y,
    x: card.x,
    y: card.y,
    preview: previewDrop(current, cardId, card.x, card.y),
    fromGroupId: from ? from.id : null,
  };
  notice.value = "";
  attachPointerListeners();
}

function onPointerMove(event: PointerEvent) {
  const current = boardState.value;
  const drag = dragging.value;
  if (drag && current) {
    const point = toBoardPoint(event);
    const x = Math.max(0, point.x - drag.offsetX);
    const y = Math.max(0, point.y - drag.offsetY);
    drag.x = x;
    drag.y = y;
    // 只预演：板面状态在松手前不变
    drag.preview = previewDrop(current, drag.cardId, x, y);
    return;
  }
  const rect = rectSelect.value;
  if (rect) {
    const point = toBoardPoint(event);
    rect.x1 = point.x;
    rect.y1 = point.y;
  }
}

function onPointerUp() {
  const current = boardState.value;
  const drag = dragging.value;
  if (drag && current) {
    const result = dropCard(current, drag.cardId, drag.x, drag.y);
    const label = dragLabel(drag, result.groupId, result.merged);
    detachPointerListeners();
    dragging.value = null;
    commit(result.state, label);
    notice.value = label + "：" + (result.groupId ? "已放入组并保存。" : "只改变位置（位置不构成意图依据）。");
    return;
  }
  const rect = rectSelect.value;
  if (rect && current) {
    rectSelect.value = null;
    detachPointerListeners();
    const area = {
      x: Math.min(rect.x0, rect.x1),
      y: Math.min(rect.y0, rect.y1),
      w: Math.abs(rect.x1 - rect.x0),
      h: Math.abs(rect.y1 - rect.y0),
    };
    if (area.w > 4 || area.h > 4) commit(selectInRect(current, area, rect.additive), "区域选择");
  }
}

/** Esc / 指针中断：不提交，卡片回到操作前位置。 */
function onDragCancel() {
  detachPointerListeners();
  if (dragging.value) {
    dragging.value = null;
    notice.value = "已取消拖动：卡片回到操作前的位置，板面没有改动。";
  }
  rectSelect.value = null;
}

function dragLabel(drag: DragState, groupId: string | null, merged: boolean): string {
  if (merged) return "合并组";
  if (groupId && drag.fromGroupId === groupId) return "调整组内顺序";
  if (groupId && drag.fromGroupId) return "换组";
  if (groupId) return "加入组";
  if (drag.fromGroupId) return "移出组";
  return "移动卡片";
}

function dragHintText(): string {
  const drag = dragging.value;
  const current = boardState.value;
  if (!drag || !current) return "";
  const preview = drag.preview;
  if (preview.groupId) {
    const target = groupById(current, preview.groupId);
    const name = target ? target.name : "组";
    if (preview.mergesWith) {
      const source = groupById(current, preview.mergesWith);
      return (
        "将把「" +
        (source ? source.name : "原组") +
        "」并入「" +
        name +
        "」，整组连续插入第 " +
        ((preview.index ?? 0) + 1) +
        " 位；合并后的新组用默认名。"
      );
    }
    if (target && target.ordered) {
      return "将加入「" + name + "」，插入第 " + ((preview.index ?? 0) + 1) + " 位（后续序号自动更新）。";
    }
    return "将加入「" + name + "」（普通组：摆放顺序不代表先后）。";
  }
  if (preview.mergesWith) {
    const other = cardById(current, preview.mergesWith);
    return "将与「" + cardSummary(other) + "」自动成组（默认组名「组 N」）。";
  }
  if (drag.fromGroupId) return "松手后移出组（自由摆放，组空了会自动消失）。";
  return "松手后只移动位置：位置不构成意图依据。";
}

const dragHint = computed(dragHintText);

function attachPointerListeners() {
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp);
  window.addEventListener("pointercancel", onDragCancel);
  // 指针移出窗口 / 窗口失焦时不会再有 pointerup：按中断处理，回到操作前位置
  window.addEventListener("blur", onDragCancel);
}

function detachPointerListeners() {
  window.removeEventListener("pointermove", onPointerMove);
  window.removeEventListener("pointerup", onPointerUp);
  window.removeEventListener("pointercancel", onDragCancel);
  window.removeEventListener("blur", onDragCancel);
}

// --- 选择 -----------------------------------------------------------------

function onSelect(cardId: string, additive: boolean) {
  if (mode.value === "link") return;
  const current = boardState.value;
  if (!current) return;
  const currentSelection = current.selection;
  let ids: string[];
  if (additive) {
    ids = currentSelection.includes(cardId)
      ? currentSelection.filter((id) => id !== cardId)
      : [...currentSelection, cardId];
  } else {
    ids = [cardId];
  }
  commit(setSelection(current, ids), additive ? "加选 / 减选卡片" : "选择卡片");
}

function onSurfacePointerDown(event: PointerEvent) {
  const target = event.target as HTMLElement | null;
  if (target && target.closest("article, button, input, textarea, select, a, .group-head, .sequence")) return;
  const point = toBoardPoint(event);
  if (mode.value === "rect" || event.shiftKey) {
    rectSelect.value = { x0: point.x, y0: point.y, x1: point.x, y1: point.y, additive: event.shiftKey };
    attachPointerListeners();
    return;
  }
  const current = boardState.value;
  if (current && current.selection.length) commit(setSelection(current, []), "清空选择");
}

const rectStyle = computed(() => {
  const rect = rectSelect.value;
  if (!rect) return {};
  return {
    left: Math.min(rect.x0, rect.x1) + "px",
    top: Math.min(rect.y0, rect.y1) + "px",
    width: Math.abs(rect.x1 - rect.x0) + "px",
    height: Math.abs(rect.y1 - rect.y0) + "px",
  };
});

// --- 卡片操作 -------------------------------------------------------------

function onAdd(kind: CardKind) {
  const current = boardState.value;
  if (!current) return;
  const defaults: Record<CardKind, { content: string; meta: Record<string, unknown> }> = {
    text: { content: "", meta: {} },
    file: { content: "待补充文件说明", meta: { name: "未命名文件" } },
    image: { content: "", meta: { name: "未命名图片" } },
    code: { content: "// 待补充代码", meta: { language: "text" } },
    url: { content: "", meta: { href: "https://", title: "待补充标题" } },
    reply: { content: "", meta: {} },
  };
  const preset = defaults[kind];
  commit(addCard(current, { kind, content: preset.content, meta: preset.meta }), "添加" + CARD_KIND_LABELS[kind]);
  notice.value =
    kind === "text"
      ? "已添加文字注释：默认未勾选，QIO 看不到它的文字；勾选后才允许查看，而且仍要提交。"
      : "已添加" + CARD_KIND_LABELS[kind] + "：材料默认在本次允许查看范围内，不需要勾选。";
}

function onCardPatch(cardId: string, patch: Partial<BoardCardModel>, label: string) {
  const current = boardState.value;
  if (!current) return;
  commit(updateCard(current, cardId, patch), label);
}

function onCardToggle(cardId: string, flag: "checked" | "hidden" | "folded" | "bookmarked") {
  const current = boardState.value;
  if (!current) return;
  const card = cardById(current, cardId);
  if (!card) return;
  if (flag === "checked") {
    commit(setChecked(current, cardId, !card.checked), card.checked ? "取消勾选" : "勾选注释（本次允许 QIO 查看）");
    return;
  }
  if (flag === "hidden") {
    commit(setHidden(current, cardId, !card.hidden), card.hidden ? "取消隐藏" : "隐藏卡片（退出讨论范围）");
    return;
  }
  if (flag === "folded") {
    commit(setFolded(current, cardId, !card.folded), card.folded ? "展开卡片" : "折叠卡片");
    return;
  }
  commit(setBookmark(current, cardId, !card.bookmarked), card.bookmarked ? "取消书签" : "加书签");
}

function onCardRemove(cardId: string) {
  const current = boardState.value;
  if (!current) return;
  commit(removeCard(current, cardId), "删除卡片（撤回这条材料或注释）");
}

function onCardDuplicate(cardId: string) {
  const current = boardState.value;
  if (!current) return;
  commit(duplicateCard(current, cardId), "复制卡片");
}

function onCardLeaveGroup(cardId: string) {
  const current = boardState.value;
  if (!current) return;
  commit(removeFromGroup(current, cardId), "移出组");
}

function onCardJoinGroup(cardId: string, groupId: string) {
  const current = boardState.value;
  if (!current) return;
  commit(joinGroup(current, cardId, groupId), "把卡片加入组");
}

function deleteSelected() {
  const current = boardState.value;
  if (!current || !current.selection.length) return;
  let next = current;
  for (const cardId of current.selection) next = removeCard(next, cardId);
  commit(next, "删除所选卡片（撤回材料或注释）");
}

// --- 分组与顺序 -----------------------------------------------------------

function onGroupOp(op: GroupOp, groupId?: string) {
  const current = boardState.value;
  if (!current) return;
  const selected = current.selection.filter((cardId) => Boolean(cardById(current, cardId)));
  if (op === "form") {
    commit(createGroup(current, selected), "把所选卡片分成一组");
    return;
  }
  if (op === "join" && groupId) {
    let next = current;
    for (const cardId of selected) next = joinGroup(next, cardId, groupId);
    commit(next, "把所选卡片加入组");
    return;
  }
  if (op === "leave") {
    let next = current;
    for (const cardId of selected) next = removeFromGroup(next, cardId);
    commit(next, "把所选卡片移出组");
    return;
  }
  const targets = selectedGroupIds.value;
  if (!targets.length) {
    notice.value = "所选卡片不在任何组里：先用「所选成组」或把卡片拖到一起。";
    return;
  }
  if (op === "dissolve") {
    let next = current;
    for (const id of targets) next = dissolveGroup(next, id);
    commit(next, "解除组");
    return;
  }
  if (op === "ordered" || op === "unordered") {
    let next = current;
    for (const id of targets) next = setGroupOrdered(next, id, op === "ordered");
    commit(next, op === "ordered" ? "设为有序组（序号表示顺序）" : "取消有序（摆放不代表先后）");
    return;
  }
  if (op === "merge") {
    if (targets.length < 2) {
      notice.value = "合并组需要所选卡片分布在两个以上的组里。";
      return;
    }
    let next = current;
    const target = targets[0];
    for (const id of targets.slice(1)) next = mergeGroups(next, id, target);
    commit(next, "合并组（新组用默认名，成员连续插入）");
  }
}

function onMoveMember(groupId: string, cardId: string, index: number) {
  const current = boardState.value;
  if (!current) return;
  const group = groupById(current, groupId);
  if (!group || !group.members.includes(cardId)) return;
  commit(joinGroup(current, cardId, groupId, index), "调整组内顺序");
}

function onGroupRename(groupId: string, name: string) {
  const current = boardState.value;
  if (!current) return;
  commit(renameGroup(current, groupId, name), "改组名");
}

function onToggleOrdered(groupId: string, ordered: boolean) {
  const current = boardState.value;
  if (!current) return;
  commit(setGroupOrdered(current, groupId, ordered), ordered ? "设为有序组（序号表示顺序）" : "取消有序（摆放不代表先后）");
}

function onDissolveGroup(groupId: string) {
  const current = boardState.value;
  if (!current) return;
  commit(dissolveGroup(current, groupId), "解除组");
}

function onLeaveMember(groupId: string, cardId: string) {
  const current = boardState.value;
  if (!current) return;
  commit(removeFromGroup(current, cardId), "把卡片移出组");
}

// --- 关系链接 -------------------------------------------------------------

function pickLinkCard(cardId: string) {
  const current = boardState.value;
  if (!current) return;
  const source = linkSource.value;
  if (!source) {
    linkSource.value = cardId;
    notice.value = "关系模式：已选起点「" + cardSummary(cardById(current, cardId)) + "」，再点一张卡片建立关系。";
    return;
  }
  if (source === cardId) {
    linkSource.value = null;
    notice.value = "已取消建立关系。";
    return;
  }
  const next = addLink(current, source, cardId, false, "");
  commit(next, "新建关系（方向与含义由你写明）");
  const created = next.links.find(
    (link) => (link.src === source && link.dst === cardId) || (link.src === cardId && link.dst === source),
  );
  linkSource.value = null;
  if (created) openLinkEditor(created.id);
}

function openLinkEditor(linkId: string) {
  const current = boardState.value;
  if (!current) return;
  const link = current.links.find((item) => item.id === linkId);
  if (!link) return;
  editingLinkId.value = linkId;
  linkMeaning.value = link.meaning;
  linkDirection.value = link.direction;
}

function saveLinkEditor() {
  const current = boardState.value;
  const linkId = editingLinkId.value;
  if (!current || !linkId) return;
  commit(updateLink(current, linkId, { meaning: linkMeaning.value, direction: linkDirection.value }), "修改关系含义 / 方向");
  notice.value = "已保存关系的方向与含义（含义是你写的原话，系统不补充解释）。";
  editingLinkId.value = null;
}

function deleteEditingLink() {
  const current = boardState.value;
  const linkId = editingLinkId.value;
  if (!current || !linkId) return;
  commit(removeLink(current, linkId), "删除关系（撤回这条关联）");
  editingLinkId.value = null;
}

const editingLink = computed(() => {
  const current = boardState.value;
  if (!current || !editingLinkId.value) return null;
  return current.links.find((link) => link.id === editingLinkId.value) ?? null;
});

const editingLinkSummary = computed(() => {
  const link = editingLink.value;
  const current = boardState.value;
  if (!link || !current) return "";
  const src = cardSummary(cardById(current, link.src));
  const dst = cardSummary(cardById(current, link.dst));
  return linkDirection.value ? src + " → " + dst + "（方向由你标注）" : src + " ↔ " + dst + "（无方向）";
});

// --- 板内搜索定位 ---------------------------------------------------------

function locate(cardId: string) {
  const current = boardState.value;
  if (!current) return;
  const card = cardById(current, cardId);
  if (!card) return;
  const element = viewport.value;
  const left = Math.max(0, card.x - 80);
  const top = Math.max(0, card.y - 80);
  // jsdom / 老 webview 可能没有 scrollTo：退回直接设置滚动位置
  if (element && typeof element.scrollTo === "function") {
    element.scrollTo({ left, top, behavior: "smooth" });
  } else if (element) {
    element.scrollLeft = left;
    element.scrollTop = top;
  }
  highlightId.value = cardId;
  if (highlightTimer) clearTimeout(highlightTimer);
  highlightTimer = setTimeout(() => {
    highlightId.value = null;
  }, 1600);
}

// --- 键盘 -----------------------------------------------------------------

function onKeyDown(event: KeyboardEvent) {
  const target = event.target as HTMLElement | null;
  const typing = Boolean(
    target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable),
  );
  if (event.key === "Escape") {
    if (dragging.value || rectSelect.value) {
      onDragCancel();
      return;
    }
    if (linkSource.value) {
      linkSource.value = null;
      notice.value = "已退出建立关系。";
      return;
    }
    if (editingLinkId.value) editingLinkId.value = null;
    return;
  }
  if (typing) return;
  if ((event.key === "Delete" || event.key === "Backspace") && selection.value.length) {
    event.preventDefault();
    deleteSelected();
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
    event.preventDefault();
    store.undo();
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "y") {
    event.preventDefault();
    store.redo();
  }
}

onMounted(() => {
  window.addEventListener("keydown", onKeyDown);
});

onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKeyDown);
  detachPointerListeners();
  if (highlightTimer) clearTimeout(highlightTimer);
});
</script>

<template>
  <section class="board-shell" data-im="board">
    <BoardToolbar
      :mode="mode"
      :selected-ids="selection"
      :groups="groups"
      :selected-group-ids="selectedGroupIds"
      @add="onAdd"
      @mode="mode = $event"
      @undo="store.undo()"
      @redo="store.redo()"
      @group-op="onGroupOp"
      @delete-selected="deleteSelected"
    />

    <p v-if="mode === 'link'" class="banner" role="status">
      关系模式：依次点两张卡片建立关系。方向只表示你写明的方向，含义由你填写；系统不会把它解释成因果、支持或执行顺序。
    </p>
    <p v-if="dragging" class="banner drag" role="status">{{ dragHint }}</p>
    <p v-if="notice" class="banner" role="status">{{ notice }}</p>

    <div class="board-main">
      <div ref="viewport" class="board-viewport" :class="{ dragging: Boolean(dragging) }" @pointerdown="onSurfacePointerDown">
        <div ref="surface" class="board-surface" :style="{ width: SURFACE_W + 'px', height: SURFACE_H + 'px' }">
          <BoardGroupFrame
            v-for="group in groups"
            :key="group.id"
            :group="group"
            :cards="liveCards"
            :selected-ids="selection"
            @rename="onGroupRename"
            @toggle-ordered="onToggleOrdered"
            @dissolve="onDissolveGroup"
            @move-member="onMoveMember"
            @leave="onLeaveMember"
            @select-member="(cardId) => onSelect(cardId, false)"
          />

          <BoardCard
            v-for="card in liveCards"
            :key="card.id"
            :card="card"
            :selected="selection.includes(card.id)"
            :highlight="highlightId === card.id"
            :dragging="dragging !== null && dragging.cardId === card.id"
            :x="displayX(card)"
            :y="displayY(card)"
            :group-name="groupNameOf(card.id)"
            :groups="groups.filter((item) => item.id !== groupIdOf(card.id))"
            @select="onSelect"
            @drag-start="onDragStart"
            @patch="onCardPatch"
            @toggle="onCardToggle"
            @remove="onCardRemove"
            @duplicate="onCardDuplicate"
            @leave-group="onCardLeaveGroup"
            @join-group="onCardJoinGroup"
          />

          <BoardLinkLayer
            :links="links"
            :cards="liveCards"
            :active-link-id="editingLinkId"
            :width="SURFACE_W"
            :height="SURFACE_H"
            @select-link="openLinkEditor"
          />

          <div v-if="rectSelect" class="select-rect" :style="rectStyle"></div>
        </div>
      </div>

      <BoardSearchPanel @locate="locate" />
    </div>

    <div v-if="editingLink" class="link-editor" role="dialog" aria-label="关系编辑">
      <p class="link-title">{{ editingLinkSummary }}</p>
      <label class="link-row">
        <input v-model="linkDirection" type="checkbox" data-im="link-direction" />
        <span>有方向（方向由你写明；系统不解释成因果 / 支持 / 先后）</span>
      </label>
      <label class="link-row">
        <span>含义</span>
        <input v-model="linkMeaning" type="text" data-im="link-meaning" placeholder="写下这条关系的含义（你的原话）" />
      </label>
      <div class="link-actions">
        <button class="btn primary" type="button" @click="saveLinkEditor">保存方向与含义</button>
        <button class="btn danger" type="button" @click="deleteEditingLink">删除关系</button>
        <button class="btn" type="button" @click="editingLinkId = null">关闭</button>
      </div>
    </div>
  </section>
</template>


<style scoped>
.board-shell {
  position: relative;
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  background: var(--bg-base);
}
.banner {
  margin: 0;
  padding: var(--sp-1) var(--sp-4);
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-subtle);
}
.banner.drag { color: var(--text-strong); background: var(--bg-accent-subtle); }
.board-main { flex: 1; display: flex; min-height: 0; min-width: 0; }
.board-viewport {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: auto;
  background: var(--bg-inset);
}
.board-viewport.dragging { user-select: none; }
.board-surface { position: relative; }
.select-rect {
  position: absolute;
  border: 1px dashed var(--accent);
  background: var(--accent-soft);
  pointer-events: none;
  z-index: 40;
}
.link-editor {
  position: absolute;
  left: var(--sp-4);
  bottom: var(--sp-4);
  z-index: 50;
  width: min(420px, 92%);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  box-shadow: var(--shadow-2);
}
.link-title { margin: 0; font-size: var(--fs-sm); color: var(--text-strong); }
.link-row { display: flex; align-items: center; gap: var(--sp-2); font-size: var(--fs-xs); color: var(--text-secondary); }
.link-row input[type="text"] {
  flex: 1;
  min-width: 0;
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--text-primary);
  background: var(--bg-inset);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  padding: var(--sp-1);
}
.link-actions { display: flex; gap: var(--sp-1); flex-wrap: wrap; }
.btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 2px var(--sp-2);
  cursor: pointer;
}
.btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
.btn:focus-visible { outline: 2px solid var(--link); outline-offset: 1px; }
.btn.primary { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }
.btn.danger { color: var(--danger); }
@media (max-width: 900px) {
  .link-editor { left: var(--sp-2); bottom: var(--sp-2); }
}
</style>
